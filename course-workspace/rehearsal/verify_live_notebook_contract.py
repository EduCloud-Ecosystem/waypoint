"""Offline seam check: real Belay orchestration into the finite Portage profile."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from live_notebook import expected_request, SOURCE, QUESTION

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--belay', required=True, type=Path)
p.add_argument('--portage', required=True, type=Path)
a = p.parse_args()
os.environ['TUTOR_PACK'] = '_skeleton'
os.environ['STORE_BACKEND'] = 'memory'
sys.path.insert(0, str(a.belay / 'backend'))
sys.path.insert(0, str(a.portage / 'src/portage'))
from app.agent.orchestrator import run_turn
from app.core.registry import get_active_pack
from app.store import InMemoryStore
from notebook_adapter import NotebookGuard, notebook_manifest
from pilot_ledger import prepare_request

expected = expected_request(a.belay, {**os.environ, 'PYTHONPATH': str(a.belay / 'backend')})
with tempfile.TemporaryDirectory() as d:
    guard = NotebookGuard(Path(d)/'guard', {'observed_at': time.time()}, expected['context'], expected['systems'])
    class SyntheticProvider:
        def json(self, *, role, tier, system, user, max_tokens, **kwargs):
            body = {'model': 'code_large' if tier == 'strong' else 'code_small',
                'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
                'max_tokens': max_tokens, 'temperature': 0.0, 'response_format': {'type': 'json_object'}}
            _, reserved = prepare_request(body, notebook_manifest()['models'], 'chat/completions')
            name = guard.start(body, role)
            guard.claim(name)
            guard.observe(name, {'provider': 'OpenAI', 'model': 'openai/gpt-4.1-mini',
                'usage': {'total_tokens': 20, 'cost': .0001}})
            assert guard.finish(name, complete=True)
            print(json.dumps({'stage': role, 'byte_reservation': reserved, 'offline': True}))
            if role == 'planner':
                return {'intervention': 'co_reason', 'confidence': .9}
            if role == 'reasoner':
                return {'message': 'What output do you expect?', 'confidence': .9}
            return {'needs_revision': False, 'confidence': .9, 'leak_risk': 'none'}
    result = run_turn({'participant_id': 'synthetic001',
        'exercise': get_active_pack().get_exercise('echo-1'), 'event': 'chat', 'mode': 'study',
        'stance': 'peer', 'source': SOURCE, 'recent': [{'who':'student', 'text':QUESTION}]},
        SyntheticProvider(), InMemoryStore())
    assert result['message'] and guard.summary()['dispatch_claims'] == 3
    print(json.dumps({'passed': True, 'actual_provider_requests': 0, 'simulated_stages': 3}))
