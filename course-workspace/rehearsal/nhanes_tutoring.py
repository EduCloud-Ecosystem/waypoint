"""Optional finite NHANES tutoring on the existing roster, jobs and provider path."""
import json
from pathlib import Path
import secrets
import sqlite3
import time

import jwt
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from playwright.sync_api import expect

from cairn_journey import port, wait_http
from live_notebook import LiveNotebook, notebook_inputs
from roster_presenter import private, command

PORTAGE_PIN = 'b72881f009e61e1311b6e46b92b42841991fd163'


def validate_tutoring(mode, portage, env_file, approved, review_seconds):
    if mode not in ('none', 'stub', 'live'):
        raise ValueError('unknown tutoring mode')
    if type(review_seconds) is not int or not 0 <= review_seconds <= 240:
        raise ValueError('live review must be between 0 and 240 seconds')
    if mode == 'live':
        if not (portage and env_file and approved):
            raise ValueError('live tutoring requires explicit approval, pinned Portage and private key file')
    elif portage or env_file or approved or review_seconds:
        raise ValueError('provider options require explicit live tutoring mode')


class NhanesTutoring:
    def __init__(self, args, root, course, cid, aid, clean, launch, notebook_url, profile):
        self.root = root / 'tutoring'; self.root.mkdir(mode=0o700)
        self.live = None
        self.inputs = notebook_inputs('nhanes-v1')
        self.mode = args.tutor_mode
        self.course_profile = profile
        self.api = 'http://127.0.0.1:' + str(port())
        exporter = self.root / 'enrollment-export'
        private(self.root / 'exporter-build.log', command(['go', 'build', '-o', exporter, './cmd/enrollment-export'], cwd=args.cairn))
        database = root / 'course/cairn.db'
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
            roster_id, host_id = db.execute('SELECT id,host_user_id FROM roster_entries WHERE classroom_id=? AND host_username=?', (cid, 'alice')).fetchone()
        authority = secrets.token_hex(16)
        journal = self.root / 'issuance.db'; snapshot = self.root / 'authorization.json'
        command([exporter, '-journal', journal, '-init-authority', authority])
        digest = command([exporter, '-database', database, '-inspect-assignment', aid])
        binding = {'authority_id': authority, 'issuer': 'https://synthetic-identity.invalid',
            'audience': 'belay-presenter', 'institution_id': 'synthetic-inst', 'lifetime_seconds': 300,
            'bindings': [{'subject': 'synthetic-alice', 'class_id': cid, 'roster_id': roster_id,
                'host': 'forgejo', 'host_user_id': host_id, 'assignments': [{'assignment_id': aid,
                'assignment_sha256': digest, 'exercise_id': profile.exercise, 'exercise_version': profile.task}]}]}
        private(self.root / 'binding.json', binding)
        command([exporter, '-database', database, '-journal', journal, '-config', self.root / 'binding.json', '-output', snapshot])
        document = json.loads(snapshot.read_text()); member = document['subjects']['synthetic-alice'][0]
        # Keep Cairn's actual roster export immutable; only the operator supplies
        # the separate teaching policy for this registered synthetic course.
        private(self.root / 'cairn-authorization-original.json', document)
        if (profile.exercise, profile.task) != ('info511-nhanes-wrangling', 'info511-nhanes-wrangling-v1'):
            raise ValueError('NHANES teaching policy version mismatch')
        overlay = {profile.exercise: {'exercise_version': profile.task,
            'policy_id': 'info511-nhanes-hints-v1', 'mode': 'hints_only', 'retention_seconds': 3600}}
        private(self.root / 'operator-teaching-policy.json', overlay)
        document = dict(document, teaching_policies=overlay)
        private(snapshot, document)
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private(self.root / 'public.pem', self.key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        self.binding = binding
        self.expires = min(int(time.time()) + 240, document['authority']['expires_at'])
        self.issue()
        self.profile = {'version': 1, 'issuer': binding['issuer'], 'audience': binding['audience'],
            'subject': 'synthetic-alice', 'institution_id': binding['institution_id'], 'class_id': cid,
            'learner_id': member['learner_id'], 'grant_generation': member['grant_generation'],
            'authority_id': authority, 'authorization_file': str(snapshot),
            'credential_file': str(self.root / 'credential.jwt'), 'expires_at': self.expires,
            'assistance': 'hints', 'exercise_id': profile.exercise, 'exercise_version': profile.task,
            'assignment_title': 'INFO 511 NHANES — synthetic instructor test'}
        private(self.root / 'profile.json', self.profile)
        private(self.root / 'execution.key', Fernet.generate_key())
        env = dict(clean, PYTHONPATH=str(args.belay / 'backend'), BELAY_ENV='test', TUTOR_PACK='datascience',
            STORE_BACKEND='memory', PROVIDER='local_stub', LLM_BILLING_MODE='unmetered',
            DATABASE_URL='sqlite:///' + str(self.root / 'private.db'), CLASSROOM_CLEANUP_ENABLED='1',
            CONTROLS_MODE='enforced', CONTROLS_JOBS_ENABLED='1', CONTROLS_DEPLOYMENT_ID='presenter',
            CONTROLS_DATABASE_URL='sqlite:///' + str(self.root / 'controls.db'),
            CONTROLS_DATA_KEY_FILE=str(self.root / 'execution.key'), AUTH_ISSUER=binding['issuer'],
            AUTH_AUDIENCE=binding['audience'], AUTH_PUBLIC_KEY_FILE=str(self.root / 'public.pem'),
            AUTH_AUTHORIZATION_FILE=str(snapshot), AUTH_AUTHORITY_STATE_FILE=str(self.root / 'watermark.db'))
        try:
            if args.tutor_mode == 'live':
                if command(['git', 'rev-parse', 'HEAD'], cwd=args.live_portage) != PORTAGE_PIN:
                    raise ValueError('NHANES Portage source pin mismatch')
                if command(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=args.live_portage):
                    raise ValueError('NHANES Portage tracked source must be committed')
                self.live = LiveNotebook(args.live_portage, args.belay, args.env_file, self.root,
                    snapshot, document, member, env, profile='nhanes-v1')
                env.update(self.live.environment)
            limits = dict(tokens=172032, microunits=175000, runs=10, wall_ms=180000)
            private(self.root / 'policy.json', dict(version='nhanes-v1', price_version='synthetic-v1', currency='USD',
                input_price=1 if self.live else 0, output_price=2 if self.live else 0,
                deployment=limits, institution=limits, classroom=limits, learner=limits))
            python = args.belay / '.venv/bin/python'
            command([python, '-c', 'from app.classroom.store import configured_store, metadata; metadata.create_all(configured_store().engine)'], env=env, cwd=self.root)
            private(self.root / 'initialize.log', command([python, '-m', 'app.controls.cli', 'init', '--policy', self.root / 'policy.json'], env=env, cwd=self.root))
            command([python, '-m', 'app.authority_state', self.root / 'watermark.db', authority], env=env, cwd=self.root)
            launch('belay-api', [python, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', self.api.rsplit(':', 1)[1]], env)
            launch('belay-worker', [python, '-m', 'app.controls.jobs_worker'], env)
            wait_http(self.api + '/healthz')
        except BaseException:
            self.close()
            raise
        self.environment = dict(WORKSPACE_BELAY_LOCAL_TEST='1',
            WORKSPACE_BELAY_HOST_ORIGIN='http://127.0.0.1:' + str(port()),
            WORKSPACE_BELAY_API_ORIGIN=self.api, WORKSPACE_BELAY_WORKSPACE_ORIGIN=notebook_url,
            WORKSPACE_BELAY_ASSET_ROOT=str(args.belay / 'frontend'), WORKSPACE_BELAY_LEARNER_ID=member['learner_id'],
            WORKSPACE_BELAY_ROSTER_PROFILE=str(self.root / 'profile.json'))

    def issue(self, expired=False):
        now = int(time.time())
        private(self.root / 'credential.jwt', jwt.encode({'sub': 'synthetic-alice',
            'iss': self.binding['issuer'], 'aud': self.binding['audience'],
            'iat': now - 60 if expired else now, 'exp': now - 1 if expired else self.expires},
            self.key, algorithm='RS256'))

    def counts(self):
        with sqlite3.connect(self.root / 'controls.db') as db:
            return {name: db.execute('SELECT count(*) FROM ' + name).fetchone()[0]
                    for name in ('control_jobs', 'control_attempts')}

    def open_help(self, page):
        cell = page.locator('.jp-Notebook .jp-CodeCell').first.locator('.cm-content')
        cell.fill(self.inputs['source']); cell.click()
        with page.context.expect_page() as opened:
            page.get_by_role('button', name='Ask Belay about this code', exact=True).click()
        help_page = opened.value
        expect(help_page.locator('#source')).to_have_value(self.inputs['source'])
        expect(help_page.locator('#assignment')).to_contain_text('NHANES')
        help_page.locator('#question').fill(self.inputs['question'])
        return help_page

    def verify(self, page, report):
        help_page = self.open_help(page)
        before_checkpoint = self.counts()
        help_page.get_by_role('button', name='Ask for a hint', exact=True).click()
        expect(help_page.locator('#status')).to_contain_text('Your saved work is unchanged', timeout=15000)
        assert self.counts() == before_checkpoint
        if self.live: assert self.live.evidence()['accounting']['dispatch_claims'] == 0
        # This is an operator-authored test prediction, explicitly recorded via UI.
        help_page.locator('#schema-present').check(); help_page.locator('#unit-present').check()
        help_page.get_by_role('button', name='Record prediction checkpoint', exact=True).click()
        expect(help_page.locator('#checkpoint-status')).to_contain_text('Prediction checkpoint recorded', timeout=15000)
        help_page.get_by_role('button', name='Ask for a hint', exact=True).click()
        expect(help_page.locator('#status')).to_contain_text('It is not a grade', timeout=120000)
        expect(help_page.locator('#reply')).not_to_be_empty()
        if self.live:
            expect(help_page.locator('#reply')).not_to_contain_text('Local model stub')
            accounting = self.live.evidence()['accounting']
            assert 3 <= accounting['dispatch_claims'] <= 7
            assert all(a['state'] == 'complete' for a in accounting['attempts'])
        private(self.root / 'reply.txt', help_page.locator('#reply').inner_text())
        help_page.screenshot(path=str(self.root / 'hint.png'), full_page=True)
        before = self.counts()
        transports = self.live.evidence()['accounting']['dispatch_claims'] if self.live else 0
        self.issue(expired=True)
        help_page.get_by_role('button', name='Ask for a hint', exact=True).click()
        expect(help_page.locator('#status')).to_contain_text('Your saved work is unchanged', timeout=15000)
        assert self.counts() == before
        self.issue()  # Restore the original expiration, never renew the authority window.
        private(self.root / 'profile.json', dict(self.profile, assistance='disabled'))
        help_page.reload()
        expect(help_page.locator('#ask')).to_be_disabled()
        expect(help_page.locator('#policy')).to_contain_text('disabled')
        assert self.counts() == before
        if self.live:
            assert self.live.evidence()['accounting']['dispatch_claims'] == transports
        help_page.get_by_role('button', name='Continue without AI', exact=True).click()
        help_page.close()
        private(self.root / 'profile.json', self.profile)
        text = ('Task version: info511-nhanes-wrangling-v1\nDeclaration: ai_assisted\nSummary: Automated synthetic instructor test: requested one ' + ('live' if self.live else 'local-stub') +
                ' Belay hint about missing readings versus missing BPXO records. '
                'The supplied operator-authored solution remains a test fixture, not learner authorship. '
                'Saving, submission and grading used separate explicit controls. Human review of the hint remains pending.\n')
        status = page.evaluate("""async text => {
            const x=document.cookie.split(';').map(s=>s.trim()).find(s=>s.startsWith('_xsrf='));
            const r=await fetch('/api/contents/assistance.md',{method:'PUT',headers:{'Content-Type':'application/json','X-XSRFToken':decodeURIComponent(x.slice(6))},body:JSON.stringify({type:'file',format:'text',content:text})}); return r.status;
        }""", text)
        assert status in (200, 201)
        report['passed'] += ['missing-checkpoint-refused-before-admission', 'NHANES-signed-job-' + self.mode + '-hint', 'expired-and-disabled-help-no-extra-attempts']
        report['tutoring_tested'] = True

    def review(self, browser, page, seconds):
        help_page = self.open_help(page)
        help_page.locator('#question').evaluate('(el)=>el.readOnly=true')
        help_page.locator('#provider-notice').evaluate("el=>el.textContent='Instructor test: one prepared NHANES question is available through OpenRouter/OpenAI. Use only this supplied example. Saving, submission and grading remain separate.'")
        help_page.bring_to_front()
        started = time.monotonic()
        duration = max(0, min(seconds, self.expires - time.time()))
        end = started + duration
        print(json.dumps({'instructor_review': 'open', 'seconds_remaining': round(duration),
            'max_requests': 7, 'max_reserved_usd': 0.175, 'note': 'Use the opened browser; closing it or the deadline stops this session.'}), flush=True)
        while time.monotonic() < end and browser.is_connected() and not page.is_closed():
            page.wait_for_timeout(min(250, max(0, end - time.monotonic()) * 1000))
        return {'elapsed_seconds': round(time.monotonic() - started, 2), 'human_acceptance_recorded': False}

    def evidence(self):
        return self.live.evidence() if self.live else {'mode': 'stub', 'actual_provider_requests': 0}

    def close(self):
        if self.live:
            self.live.close()
