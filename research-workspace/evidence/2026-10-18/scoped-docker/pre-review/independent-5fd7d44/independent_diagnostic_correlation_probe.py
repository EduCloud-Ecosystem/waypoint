"""No-provider probe: valid diagnostic hashes still lack receipt correlation."""
import hashlib
import json
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parent
SOURCE = BASE / 'source/research-workspace'
sys.path[:0] = [str(SOURCE), str(SOURCE / 'tests')]
import qualify_scoped_docker as q
from test_scoped_docker_qualification import complete_report, synthetic_manifest

report = complete_report()
diagnostics = BASE / 'mismatched-diagnostics'
diagnostics.mkdir(exist_ok=False)
observations = []
for attempt in report['attempts']:
    index = attempt['binding']['index']
    # This is a well-formed compact-ledger sidecar, with correct size/hash, but
    # it is not the output printed in the final receipt or saved application.
    other = dict(stdout=json.dumps(dict(q.ORACLES[index], mean=-999)), stderr='different stderr')
    content = json.dumps(other, separators=(',', ':')).encode()
    sha = hashlib.sha256(content).hexdigest()
    target = diagnostics / (sha + '.json')
    target.write_bytes(content)
    record = attempt['ledger_record']
    record.update(stdout='', stderr='', output_sha256=sha, output_bytes=len(content))
    record.pop('output_error', None)
    retained = target.read_bytes()
    observations.append(dict(
        request_id=attempt['binding']['request_id'],
        path=str(target), sha256=sha, bytes=len(content),
        size_and_sha256_valid=len(retained) == record['output_bytes'] and hashlib.sha256(retained).hexdigest() == record['output_sha256'],
        valid_json=isinstance(json.loads(retained), dict),
        within_request_output_cap=len(other['stdout'].encode()) + len(other['stderr'].encode()) <= attempt['request']['max_output_bytes'],
        differs_from_receipt=other != {k: attempt['final_receipt'][k] for k in ('stdout', 'stderr')},
    ))

accepted = q.accepted(report, synthetic_manifest())
result = dict(
    candidate='5fd7d445855b191fd2f20eee218a4860961cf38e',
    case='valid-hash-diagnostics-do-not-match-receipt',
    observed_accepted=accepted,
    expected_accepted=False,
    observations=observations,
    scope='Pure synthetic final oracle only. No provider, broker, Docker, production init, or checkpoint executed.',
    production_checkpoint_limit='P1 checkpoint verifies referenced sidecar size/hash, not equality to the separately retained receipt. Each synthetic sidecar here satisfies those size/hash checks.',
)
(BASE / 'mismatched-diagnostics-report.json').write_text(json.dumps(report, indent=2) + '\n')
(BASE / 'mismatched-diagnostics-result.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
