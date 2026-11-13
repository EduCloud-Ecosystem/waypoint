"""Finite local synthetic presenter/receipt check. No models or learner records.

Reuses the ordinary Cairn/Forgejo, Jupyter, enrollment and Belay implementations.
The sole identity issuer is explicitly synthetic fixture code, not campus SSO.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import traceback

import jwt
import requests
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from playwright.sync_api import sync_playwright, expect, TimeoutError as BrowserTimeout

from accessibility import audit, tab_to
from cairn_journey import Course, port, wait_http
from submission_handoff import first_submission, verify_receipt_after_restart
from live_notebook import LiveNotebook

ROOT = Path(__file__).resolve().parents[1]
PINS = {'cairn': '0cf3158fdf5bbbecc52d3cc9cd88ef33bdfb63fc',
        'belay': '656d6ec1e4be5f9ef7267f5a2bb5145a90238dfe'}
SOURCE = "print('ok')\n"


def private(path, value):
    raw = json.dumps(value).encode() if isinstance(value, (dict, list)) else value
    if isinstance(raw, str):
        raw = raw.encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)


def command(args, *, env=None, cwd=None):
    return subprocess.check_output([str(x) for x in args], env=env, cwd=cwd,
                                   stderr=subprocess.STDOUT, text=True, timeout=90).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cairn', type=Path, required=True)
    parser.add_argument('--belay', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--live-portage', type=Path, help='Pinned synthetic live adapter checkout')
    parser.add_argument('--env-file', type=Path, help='Private OpenRouter credential file')
    parser.add_argument('--execute-approved', action='store_true')
    parser.add_argument('--checkout-image', required=True)
    parser.add_argument('--grader-image', required=True)
    parser.add_argument('--timeout-seconds', type=int, choices=range(60, 901), default=900)
    args = parser.parse_args()
    if args.live_portage and not (args.env_file and args.execute_approved):
        parser.error("live mode requires --env-file and --execute-approved: at most 7 calls/USD0.175")
    os.umask(0o077)
    root = args.output.resolve()
    root.mkdir(mode=0o700)  # Existing runs cannot be overwritten or resumed.
    report = {'scope': 'synthetic-loopback-presenter', 'passed': [], 'failed': [],
              'model_calls': 0, 'transport_requests_to_model_provider': 0,
              'institutional_sso': False, 'human_review': False, 'cleanup': False}
    processes, logs = [], []
    course = None
    live = None
    started = time.monotonic()
    def timed_out(*_):
        raise TimeoutError('finite rehearsal window ended')
    signal.signal(signal.SIGALRM, timed_out)
    signal.alarm(args.timeout_seconds)
    def launch(name, args, env, cwd):
        log = (root / (name + '.log')).open('a')
        logs.append(log)
        proc = subprocess.Popen([str(x) for x in args], env=env, cwd=cwd,
                                stdout=log, stderr=subprocess.STDOUT)
        processes.append(proc)
        return proc
    try:
        pins = dict(PINS)
        if args.live_portage:
            pins['belay'] = 'c5a27073e74b9eeb7ab697190915378b50132883'
        for name in pins:
            repo = getattr(args, name).resolve()
            if command(['git', 'rev-parse', 'HEAD'], cwd=repo) != pins[name]:
                raise ValueError('dependency source revision mismatch: ' + name)
            if command(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=repo):
                raise ValueError('dependency tracked source dirty: ' + name)
        report['source_pins'] = pins
        report['waypoint_revision'] = command(['git', 'rev-parse', 'HEAD'], cwd=ROOT)
        report['waypoint_dirty'] = bool(command(['git', 'status', '--porcelain'], cwd=ROOT))
        for image in (args.checkout_image, args.grader_image):
            if not image.startswith('sha256:') or len(image) != 71:
                raise ValueError('preloaded immutable image ID required')
            command(['docker', 'image', 'inspect', image, '--format', '{{.Id}}'])
        report['images'] = {'checkout': args.checkout_image, 'grader': args.grader_image}
        clean = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG', 'DOCKER_HOST', 'DOCKER_CONTEXT') if k in os.environ}
        workspace = root / 'saved-work'; workspace.mkdir()
        notebook = {'nbformat': 4, 'nbformat_minor': 5, 'metadata': {}, 'cells': [
            {'cell_type': 'code', 'id': 'synthetic-cell', 'metadata': {}, 'source': SOURCE,
             'execution_count': None, 'outputs': []}]}
        (workspace / 'assignment.ipynb').write_text(json.dumps(notebook))
        # The learner later saves solution.py via the real authenticated Contents API.
        notebook_url = 'http://127.0.0.1:' + str(port())
        api_url = 'http://127.0.0.1:' + str(port())
        host_url = 'http://127.0.0.1:' + str(port())  # Legacy host unused in roster mode.
        spec = {'version': '1', 'image': args.grader_image, 'tests': [
            {'name': 'synthetic-output', 'run': 'python -c "print(1)"', 'points': 1}],
            'limits': {'memory_mb': 256, 'cpus': 1, 'network': 'none'}}
        course_dir = root / 'course'; course_dir.mkdir()
        course = Course(course_dir, root / 'cairn', notebook_url, args.grader_image, args.cairn,
                        template_files={'grading.json': json.dumps(spec)}, checkout_image=args.checkout_image)
        # Build only pinned local source; logs are retained per fresh invocation.
        for target, package in [('cairn', './cmd/cairn'), ('enrollment-export', './cmd/enrollment-export')]:
            private(root / (target + '-build.log'), command(['go', 'build', '-o', root / target, package], cwd=args.cairn))
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            report['browser'] = browser.version
            revision = course.start()
            teacher, _ = course.login(browser, 'teacher', '/auth/login')
            cid = course.request(teacher, 'POST', '/classrooms', {'name': 'Synthetic presenter course',
                'host': 'forgejo', 'host_namespace': 'course-test', 'join_policy': 'roster'}, 201)['id']
            for user in ('alice', 'bob'):
                course.request(teacher, 'POST', f'/classrooms/{cid}/roster', {'username': user}, 201)
            aid = course.request(teacher, 'POST', f'/classrooms/{cid}/assignments', {
                'title': 'Synthetic saved-file assignment', 'slug': 'presenter-echo', 'type': 'individual',
                'grading_spec': 'grading.json', 'template': {'namespace': 'course-test', 'name': 'template', 'ref': revision}}, 201)['id']
            course.env.update(CAIRN_SUBMISSIONS='true', CAIRN_SUBMISSION_CLASSROOMS=cid,
                              CAIRN_WORKSPACE_URLS=json.dumps({cid: notebook_url}))
            course.restart()
            alice, notebook_page = course.login(browser, 'alice', f'/assignments/{aid}/accept')
            bob, _ = course.login(browser, 'bob', f'/assignments/{aid}/accept')
            end = time.monotonic() + 60
            while time.monotonic() < end:
                rows = course.request(alice, 'GET', '/me/work')['work']
                if rows and rows[0]['status'] == 'active':
                    item = rows[0]; break
                time.sleep(.25)
            else:
                raise AssertionError('synthetic learner repository not ready')
            report['passed'].append('actual-forgejo-oauth-cairn-roster-assignment')
            database = course_dir / 'cairn.db'
            with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
                roster_id, host_id = db.execute('SELECT id,host_user_id FROM roster_entries WHERE classroom_id=? AND host_username=?', (cid, 'alice')).fetchone()
            authority = secrets.token_hex(16)
            exporter = root / 'enrollment-export'
            journal = root / 'issuance.db'; snapshot = root / 'authorization.json'
            command([exporter, '-journal', journal, '-init-authority', authority])
            digest = command([exporter, '-database', database, '-inspect-assignment', aid])
            binding = {'authority_id': authority, 'issuer': 'https://synthetic-identity.invalid',
                'audience': 'belay-presenter', 'institution_id': 'synthetic-inst', 'lifetime_seconds': 300,
                'bindings': [{'subject': 'synthetic-alice', 'class_id': cid, 'roster_id': roster_id,
                    'host': 'forgejo', 'host_user_id': host_id, 'assignments': [{'assignment_id': aid,
                    'assignment_sha256': digest, 'exercise_id': 'echo-1', 'exercise_version': 'v1'}]}]}
            private(root / 'binding.json', binding)
            command([exporter, '-database', database, '-journal', journal, '-config', root / 'binding.json', '-output', snapshot])
            document = json.loads(snapshot.read_text()); member = document['subjects']['synthetic-alice'][0]
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            private(root / 'public.pem', key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
            def issue(expired=False):
                now = int(time.time())
                private(root / 'credential.jwt', jwt.encode({'sub':'synthetic-alice', 'iss':binding['issuer'],
                    'aud':binding['audience'], 'iat':now - (60 if expired else 0), 'exp':now - 1 if expired else now + 240}, key, algorithm='RS256'))
            issue()
            profile = {'version': 1, 'issuer': binding['issuer'], 'audience': binding['audience'],
                'subject': 'synthetic-alice', 'institution_id': binding['institution_id'], 'class_id': cid,
                'learner_id': member['learner_id'], 'grant_generation': member['grant_generation'],
                'authority_id': authority, 'authorization_file': str(snapshot),
                'credential_file': str(root / 'credential.jwt'), 'expires_at': int(time.time()) + 600,
                'assistance': 'hints', 'exercise_id': 'echo-1', 'exercise_version': 'v1',
                'assignment_title': 'Synthetic saved-file assignment'}
            private(root / 'profile.json', profile)
            private(root / 'execution.key', Fernet.generate_key())
            backend = args.belay / 'backend'; python = args.belay / '.venv/bin/python'
            env = dict(clean, PYTHONPATH=str(backend), BELAY_ENV='test', TUTOR_PACK='_skeleton',
                STORE_BACKEND='memory', PROVIDER='local_stub', LLM_BILLING_MODE='unmetered',
                CONTROLS_MODE='enforced', CONTROLS_JOBS_ENABLED='1', CONTROLS_DEPLOYMENT_ID='presenter',
                CONTROLS_DATABASE_URL='sqlite:///' + str(root / 'controls.db'),
                CONTROLS_DATA_KEY_FILE=str(root / 'execution.key'), AUTH_ISSUER=binding['issuer'],
                AUTH_AUDIENCE=binding['audience'], AUTH_PUBLIC_KEY_FILE=str(root / 'public.pem'),
                AUTH_AUTHORIZATION_FILE=str(snapshot), AUTH_AUTHORITY_STATE_FILE=str(root / 'watermark.db'))
            if args.live_portage:
                if command(['git', 'rev-parse', 'HEAD'], cwd=args.live_portage) != '0653fd94fe86acf235a74f72a1ff4af6aa2fa1b9':
                    raise ValueError('Portage integration revision mismatch')
                if command(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=args.live_portage):
                    raise ValueError('commit Portage source before live use')
                report['portage_revision'] = command(['git', 'rev-parse', 'HEAD'], cwd=args.live_portage)
                live = LiveNotebook(args.live_portage.resolve(), args.belay.resolve(), args.env_file, root, snapshot, document, member, env)
                env.update(live.environment)
            limits = dict(tokens=114688, microunits=175000, runs=10, wall_ms=180000)
            private(root / 'policy.json', dict(version='presenter-v1', price_version='synthetic-v1', currency='USD',
                input_price=1 if live else 0, output_price=2 if live else 0, deployment=limits, institution=limits, classroom=limits, learner=limits))
            private(root / 'initialize.log', command([python, '-m', 'app.controls.cli', 'init', '--policy', root / 'policy.json'], env=env, cwd=root))
            command([python, '-m', 'app.authority_state', root / 'watermark.db', authority], env=env, cwd=root)
            launch('belay-api', [python, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', api_url.rsplit(':', 1)[1]], env, root)
            launch('belay-worker', [python, '-m', 'app.controls.jobs_worker'], env, root)
            wait_http(api_url + '/healthz')
            jupyter_data = root / 'jupyter-data'
            extensions = jupyter_data / 'labextensions/@educloud'; extensions.mkdir(parents=True)
            shutil.copytree(ROOT / 'course/labextension-belay', extensions / 'waypoint-belay')
            jupyter_token = secrets.token_urlsafe(32)
            config_dir = root / 'jupyter-config'; config_dir.mkdir()
            private(config_dir / 'jupyter_server_config.json', {'ServerApp': {
                'jpserver_extensions': {'waypoint_belay': True, 'waypoint_cairn': True}}})
            jenv = dict(clean, PYTHONPATH=str(ROOT / 'course'), JUPYTER_PATH=str(jupyter_data), JUPYTER_DATA_DIR=str(jupyter_data),
                JUPYTER_CONFIG_DIR=str(root / 'jupyter-config'), JUPYTER_RUNTIME_DIR=str(root / 'jupyter-runtime'),
                JUPYTER_TOKEN=jupyter_token, WORKSPACE_CAIRN_ORIGIN=course.url, WORKSPACE_CAIRN_LOCAL_TEST='1',
                WORKSPACE_BELAY_LOCAL_TEST='1', WORKSPACE_BELAY_HOST_ORIGIN=host_url,
                WORKSPACE_BELAY_API_ORIGIN=api_url, WORKSPACE_BELAY_WORKSPACE_ORIGIN=notebook_url,
                WORKSPACE_BELAY_ASSET_ROOT=str(args.belay / 'frontend'), WORKSPACE_BELAY_LEARNER_ID=member['learner_id'],
                WORKSPACE_BELAY_ROSTER_PROFILE=str(root / 'profile.json'))
            launch('notebook', [sys.executable, '-m', 'jupyterlab', '--no-browser', '--ip=127.0.0.1',
                '--port=' + notebook_url.rsplit(':', 1)[1], '--ServerApp.port_retries=0', '--ServerApp.root_dir=' + str(workspace)], jenv, root)
            wait_http(notebook_url + '/login')
            notebook_page.goto(notebook_url + '/lab/tree/assignment.ipynb?token=' + jupyter_token)
            expect(notebook_page.get_by_role('button', name='Ask Belay about this code', exact=True)).to_be_visible(timeout=60000)
            # This slice edits/captures saved code; it does not execute a kernel.
            # A fresh isolated Jupyter data directory may offer a kernel chooser.
            try:
                notebook_page.get_by_role('dialog').wait_for(state='visible', timeout=5000)
                private(root / 'notebook-dialog.txt', notebook_page.get_by_role('dialog').inner_text())
                notebook_page.keyboard.press('Escape')
            except BrowserTimeout:
                pass
            notebook_page.locator('.jp-Notebook .jp-CodeCell').first.click()
            with notebook_page.expect_popup() as popup:
                notebook_page.get_by_role('button', name='Ask Belay about this code', exact=True).click()
            help_page = popup.value
            expect(help_page.locator('#source')).to_have_value(SOURCE)
            expect(help_page.locator('#assignment')).to_have_text('Assignment: Synthetic saved-file assignment')
            help_page.locator('#question').fill('I predict ok will print. What should I check?')
            help_page.screenshot(path=str(root / 'help-before-request.png'), full_page=True)
            tab_to(help_page, help_page.get_by_role('button', name='Ask for a hint', exact=True))
            help_page.keyboard.press('Enter')
            expect(help_page.locator('#status')).to_contain_text('It is not a grade', timeout=180000)
            if live:
                expect(help_page.locator('#reply')).not_to_be_empty()
                expect(help_page.locator('#reply')).not_to_contain_text('Local model stub')
                private(root / 'synthetic-tutor-reply.txt', help_page.locator('#reply').inner_text())
                accounting = live.evidence()['accounting']
                assert 3 <= accounting['dispatch_claims'] <= 7
                assert all(a['state'] == 'complete' for a in accounting['attempts'])
                report['passed'].append('native-notebook-signed-job-live-portage-tutoring')
            else:
                expect(help_page.locator('#reply')).to_contain_text('Local model stub')
            expect(help_page.locator('#capture')).not_to_contain_text('Nothing has been sent')
            if not live:
                report['passed'].append('native-jupyter-code-selection-signed-belay-job-stub-response')
            report['axe_sha256'] = audit(help_page, args.cairn)
            help_page.screenshot(path=str(root / 'help-desktop.png'), full_page=True)
            help_page.set_viewport_size({'width': 390, 'height': 844})
            audit(help_page, args.cairn)
            assert help_page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            help_page.screenshot(path=str(root / 'help-mobile.png'), full_page=True)
            def counts():
                with sqlite3.connect(root / 'controls.db') as db:
                    return {t: db.execute('SELECT count(*) FROM ' + t).fetchone()[0]
                            for t in ('control_jobs', 'control_attempts')}
            before = counts()
            issue(expired=True)
            help_page.get_by_role('button', name='Ask for a hint', exact=True).click()
            expect(help_page.locator('#status')).to_contain_text('Your saved work is unchanged', timeout=15000)
            expect(help_page.locator('#policy')).to_contain_text('expired or unavailable')
            expect(help_page.locator('#ask')).to_be_disabled()
            assert counts() == before
            expect(help_page.locator('#source')).to_have_value(SOURCE)
            help_page.screenshot(path=str(root / 'help-expired.png'), full_page=True)
            report['passed'].append('expired-credential-no-job-or-attempt-preserves-code')
            issue()
            private(root / 'profile.json', dict(profile, assistance='disabled'))
            help_page.reload()
            expect(help_page.locator('#policy')).to_contain_text('instructor has disabled')
            expect(help_page.locator('#ask')).to_be_disabled()
            refusal = help_page.evaluate("""async () => {
              const r = await fetch('/belay/api/api/sol/jobs/config', {headers:await BelayAuth.headers(location.origin + '/belay/api')});
              return r.status;
            }""")
            assert refusal == 403 and counts() == before
            help_page.get_by_role('button', name='Continue without AI').click()
            help_page.get_by_role('link', name='Ask a person', exact=True).click()
            expect(help_page.get_by_role('heading', name='Human help', exact=True)).to_be_visible()
            report['passed'].append('instructor-disabled-refusal-no-ai-human-help')
            # Seed an empty synthetic file, then edit and Save through the native
            # editor. Submission reads saved bytes, not this fixture input.
            saved = notebook_page.evaluate("""async source => {
              const xsrf = document.cookie.split(';').map(x=>x.trim()).find(x=>x.startsWith('_xsrf=')).slice(6);
              const r = await fetch('/api/contents/solution.py', {method:'PUT',headers:{'Content-Type':'application/json','X-XSRFToken':decodeURIComponent(xsrf)},
                body:JSON.stringify({type:'file',format:'text',content:source})}); return r.status;
            }""", '')
            assert saved in (200, 201) and (workspace / 'solution.py').read_text() == ''
            notebook_page.goto(notebook_url + '/lab/tree/solution.py')
            try:
                dialog = notebook_page.get_by_role('dialog')
                dialog.wait_for(state='visible', timeout=5000)
                private(root / 'restored-notebook-dialog.txt', dialog.inner_text())
                expect(dialog).to_contain_text('Select Kernel')
                dialog.get_by_role('button', name='No Kernel', exact=True).click()
            except BrowserTimeout:
                pass
            editor = notebook_page.locator('.jp-FileEditor .cm-content')
            expect(editor).to_be_visible()
            editor.click()  # Activate the file widget before the Save command.
            editor.fill(SOURCE)
            notebook_page.keyboard.press('ControlOrMeta+s')
            deadline = time.monotonic() + 10
            while (workspace / 'solution.py').read_text() != SOURCE and time.monotonic() < deadline:
                notebook_page.wait_for_timeout(100)
            assert (workspace / 'solution.py').read_text() == SOURCE
            notebook_page.screenshot(path=str(root / 'native-editor-saved.png'), full_page=True)
            report['passed'].append('native-editor-keyboard-save')
            def submission_page(context, selected):
                help_page.get_by_role('link', name='Submit selected saved files', exact=True).click()
                help_page.get_by_role('checkbox', name='solution.py', exact=False).check()
                help_page.get_by_role('button', name='Prepare saved files', exact=True).click()
                with context.expect_page() as opened:
                    help_page.get_by_role('button', name='Continue in Cairn', exact=True).click()
                receiver = opened.value
                receiver.get_by_label('Assignment', exact=True).select_option(selected['submission_id'])
                receiver.get_by_role('button', name='Receive saved files from Waypoint', exact=True).click()
                expect(receiver.locator('#bridge-status')).to_contain_text('Nothing has been submitted yet.')
                return receiver
            receipt = first_submission(course, alice, bob, item, SOURCE, submission_page)
            report['receipt'] = receipt
            teacher.close()
            teacher, instructor_page = course.login(browser, 'teacher', '/auth/login')
            instructor_rows = course.request(teacher, 'GET', f'/assignments/{aid}/submissions')
            instructor_item = next(row for row in instructor_rows if row['id'] == item['submission_id'])
            assert instructor_item['repo']['namespace'] + '/' + instructor_item['repo']['name'] == item['repo_web_url'].removeprefix(course.forge + '/')
            instructor_page.screenshot(path=str(root / 'instructor-signed-in.png'), full_page=True)
            report['passed'].append('instructor-authorized-submission-repository-visible')
            assert counts() == before
            repository = item['repo_web_url'].removeprefix(course.forge + '/')
            if '/' not in repository or repository.startswith(('http:', 'https:')):
                raise ValueError('unexpected synthetic repository URL')
            content = course.api('GET', '/repos/' + repository + '/contents/solution.py?ref=' + receipt['commit'])
            assert base64.b64decode(content['content']) == SOURCE.encode()
            course.restart()
            alice.close()
            alice, _ = course.login(browser, 'alice', '/student/login?host=forgejo')
            verify_receipt_after_restart(course, alice, item['submission_id'], receipt)
            report['passed'] += ['no-ai-native-saved-file-submission', 'committed-file-reopened-byte-identical', 'receipt-survives-restart', 'keyboard-and-axe-desktop-mobile']
            report['accounting'] = before
            if live:
                assert live.evidence()['accounting'] == accounting
                report['passed'].append('expired-disabled-and-submission-add-no-provider-transports')
            report['grade_claim'] = 'No pedagogical grading acceptance; receipt is repository publication only.'
            browser.close()
    except Exception as exc:
        report['failed'].append(type(exc).__name__ + ': ' + str(exc)[:600])
        private(root / 'failure.txt', traceback.format_exc())
    finally:
        signal.alarm(0)
        for proc in reversed(processes):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait(timeout=5)
        for log in logs:
            log.close()
        if live:
            live.close()
            report['live_tutoring'] = live.evidence()
            report['model_calls'] = report['transport_requests_to_model_provider'] = live.guard.summary()['dispatch_claims']
        try:
            report['cleanup'] = course.close() if course else True
        except Exception as exc:
            report['failed'].append('cleanup: ' + type(exc).__name__)
        report['elapsed_seconds'] = round(time.monotonic() - started, 2)
        private(root / 'result.json', report)
        print(json.dumps(report, indent=2))
    return 1 if report['failed'] or not report['cleanup'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
