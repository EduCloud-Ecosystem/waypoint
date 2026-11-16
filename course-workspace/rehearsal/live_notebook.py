"""Operator-owned bridge for one source-fixed synthetic notebook tutoring window.

The real browser, authenticated Belay job, SDK and Portage gateway remain in use.
This module does not accept learner input, mint course membership, or submit work.
"""
import json
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import subprocess
import sys
import time

SOURCE = "print('ok')\n"
QUESTION = 'I predict ok will print. What should I check?'


def notebook_inputs(profile='echo'):
    """Operator-selected source contracts, never user-supplied prompt approval."""
    if profile == 'echo':
        return {'pack': '_skeleton', 'exercise': 'echo-1', 'source': SOURCE,
                'question': QUESTION}
    if profile != 'nhanes-v1':
        raise ValueError('unsupported notebook tutoring profile')
    package = Path(__file__).resolve().parents[1] / 'classroom/info511-nhanes-v1'
    turn = json.loads((package / 'operator/approved-notebook-turn.json').read_text())
    if set(turn) != {'source', 'question'} or not all(isinstance(v, str) and v.strip() for v in turn.values()):
        raise ValueError('invalid source-authored NHANES turn')
    return {'pack': 'datascience', 'exercise': 'info511-nhanes-wrangling', **turn}


def expected_request(belay, env, profile='echo'):
    # Derive the expected envelope from the pinned implementation and source-fixed
    # synthetic inputs, never learn/approve it from incoming browser traffic.
    program = '''
import json, sys
from app.core.registry import get_active_pack
from app.agent.context import build_context
from app.agent.prompts import planner_system, reasoner_system, selfeval_system
p = get_active_pack()
inputs = json.load(sys.stdin)
ex = p.get_exercise(inputs['exercise'])
ctx = build_context({'exercise': ex, 'event': 'chat', 'mode': 'study',
    'stance': 'peer', 'source': inputs['source'],
    'recent': [{'who': 'student', 'text': inputs['question']}]}, {}, 0)
ctx['_exercise_full'] = ex
print(json.dumps({'context': ctx, 'systems': {'planner': planner_system(p.persona, 'peer'),
    'reasoner': reasoner_system(p.persona, 'peer'), 'self_eval': selfeval_system(p.persona, 'peer')}}))
'''
    inputs = notebook_inputs(profile)
    if env.get('TUTOR_PACK') != inputs['pack']:
        raise ValueError('notebook tutoring pack mismatch')
    return json.loads(subprocess.check_output([str(belay / '.venv/bin/python'), '-c', program],
                     input=json.dumps(inputs), env=env, cwd=belay, text=True, timeout=30))


class LiveNotebook:
    def __init__(self, portage, belay, env_file, root, snapshot, document, member, env,
                 *, profile='echo'):
        sys.path.insert(0, str(portage / 'integrations/ai_pilot'))
        from gateway import Gateway, Ledger
        from notebook_adapter import NotebookGuard, notebook_manifest
        from openrouter_adapter import load_key, validate_catalog
        from openrouter_pilot import metadata, serve

        self.stack = ExitStack()
        self.root = root
        self.closed = False
        self.member = member['learner_id']
        self.guard = None
        self.ledger = None
        try:
            if profile == 'nhanes-v1':
                from notebook_adapter import NhanesNotebookGuard, nhanes_manifest
                guard_type, manifest = NhanesNotebookGuard, nhanes_manifest()
            elif profile == 'echo':
                guard_type, manifest = NotebookGuard, notebook_manifest()
            else:
                raise ValueError('unsupported notebook tutoring profile')
            self.project = manifest['project']
            key = load_key(env_file)
            expected = expected_request(belay, env, profile)
            raw = metadata('models/openai/gpt-4.1-mini/endpoints')
            self.guard = guard_type(root / 'provider', validate_catalog(raw),
                                    expected['context'], expected['systems'])
            self.guard.deadline = min(self.guard.deadline, document['authority']['expires_at'])
            self.guard.mono_deadline = time.monotonic() + max(0, self.guard.deadline - time.time())
            self.guard._append({'event': 'course_expiry_bound', 'deadline': self.guard.deadline})
            (root / 'provider/catalog.json').write_text(json.dumps(raw, indent=2))
            # Snapshot identity is exact and immutable for this finite session.
            # Belay also rechecks the export + durable watermark before every call.
            authorization = snapshot.read_bytes()
            secret = secrets.token_urlsafe(32)
            guard = self.guard
            learner = self.member
            project = self.project
            class Authority(BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass

                def do_GET(self):
                    active = False
                    try:
                        active = (self.path == '/internal/member/' + learner
                            and self.headers.get('Authorization') == 'Bearer ' + secret
                            and snapshot.read_bytes() == authorization and guard.remaining() > 0)
                    except OSError:
                        pass
                    data = json.dumps({'active': active, 'project': project}).encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
            authority = serve(self.stack, ThreadingHTTPServer(('127.0.0.1', 0), Authority))
            self.ledger = Ledger(root / 'provider/portage.sqlite')
            self.ledger.project(project, manifest['project_allowance'], 1)
            self.ledger.member(project, learner, manifest['participant_allowance'])
            token = self.ledger.issue(project, subject=learner)
            gateway = Gateway(('127.0.0.1', 0), self.ledger, manifest,
                secrets.token_urlsafe(), key, authority, secret, external_guard=guard)
            origin = serve(self.stack, gateway)
            self.environment = dict(PROVIDER='portage', LLM_BILLING_MODE='metered',
                PORTAGE_DEPLOYMENT='presenter', PORTAGE_INSTITUTION=member['institution_id'],
                PORTAGE_CLASSROOM=member['class_id'], OPENAI_BASE_URL=origin + '/v1',
                OPENAI_API_KEY=token, OPENAI_RESPONSE_FORMAT='json_object',
                OPENAI_REASONING='0', OPENAI_THINKING_MODE='', LLM_TEMPERATURE='0',
                MAX_REFINE='1', MAX_ESCALATE='1', MAX_WORKED_EXAMPLE_RETRY='0')
        except BaseException:
            self.close()
            raise

    def evidence(self):
        return {'accounting': self.guard.summary() if self.guard else None,
                'portage_ledger': self.ledger.usage(self.project, self.member) if self.ledger else None,
                'owned_servers_closed': self.closed,
                'historical_unknown_liability_usd': 0.025,
                'instructional_acceptance': 'pending_human_review',
                'synthetic_only': True}

    def close(self):
        self.stack.close()
        self.closed = True
        if self.guard:
            (self.root / 'provider/result.json').write_text(json.dumps(self.evidence(), indent=2))
