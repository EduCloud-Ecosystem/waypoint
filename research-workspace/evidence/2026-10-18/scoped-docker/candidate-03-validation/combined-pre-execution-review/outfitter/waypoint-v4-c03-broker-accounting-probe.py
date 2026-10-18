"""Independent candidate03 pure/scratch probe; no Audit constructor or processes."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path[:0] = ['/tmp/waypoint-v4-c03-broker-pdhkd2yg', '/tmp/waypoint-v4-c03-broker-pdhkd2yg/tests']
import qualify_scoped_docker as q
from test_scoped_docker_qualification import OracleTests, complete_report

def check(value, message):
    if not value:
        raise RuntimeError(message)

forbidden = RuntimeError('No process execution permitted in independent review')
excluded = {'test_exercise_marker_survives_missing_or_changed_output', 'test_exclusive_output_and_fifth_attempt_refuse_without_effect'}
names = [name for name in unittest.defaultTestLoader.getTestCaseNames(OracleTests) if name not in excluded]
with patch.object(q.subprocess, 'Popen', side_effect=forbidden), patch.object(q.subprocess, 'run', side_effect=forbidden), patch.object(q.Audit, '__init__', side_effect=RuntimeError('No marker creation permitted')):
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(OracleTests(name) for name in names))
    check(result.wasSuccessful(), 'Pure oracle suite failed')
    with tempfile.TemporaryDirectory(prefix='waypoint-v4-c03-no-marker-') as temporary:
        audit = object.__new__(q.Audit)
        audit.root = Path(temporary)
        audit.state = audit.root / 'state.private'
        (audit.state / 'broker/outputs').mkdir(parents=True)
        complete = complete_report()
        original = copy.deepcopy(complete['attempts'][0])
        binding = original['binding']
        rid, pid = binding['request_id'], binding['project_id']
        binding['ledger_path'] = str(audit.state / 'broker')
        binding['ledger_identity']['path_sha256'] = q.digest(binding['ledger_path'].encode())
        request = q.planned_request(0, rid, pid, binding['ledger_path'])
        request_bytes = q.canonical(request).encode()
        binding['request_sha256'] = q.digest(request_bytes)
        record = original['ledger_record']
        record['request'] = request
        record['payload_hash'] = q.digest(json.dumps(request, separators=(',', ':')).encode())
        ledger = dict(version=2, owner=binding['ledger_identity']['owner'],
                      authority=dict(authority_id=binding['ledger_identity']['authority_id'], state_path=binding['ledger_path'], admission='active'),
                      projects={project: dict(project_id=project, budget_cpu_seconds=60) for project in complete['projects'].values()}, runs={rid: record})
        (audit.state / 'broker/ledger.json').write_bytes(q.encoded(ledger))
        (audit.state / 'broker/outputs' / (record['output_sha256'] + '.json')).write_bytes(original['diagnostics_utf8'].encode())
        run_dir = audit.state / 'runs' / rid
        run_dir.mkdir(parents=True)
        (run_dir / 'receipt.private.json').write_bytes(q.encoded(original['final_receipt']))
        (run_dir / 'result.json').write_bytes(q.encoded(original['run']['metrics']))
        (audit.root / '1-request.private.json').write_bytes(request_bytes)
        (audit.root / '1-binding.private.json').write_bytes(q.encoded(binding))
        audit.report = dict(status='FAIL', checks={}, artifacts={}, projects=complete['projects'],
                            attempts=[dict(binding=binding, binding_sha256=q.digest(q.encoded(binding)), outcome='DISPATCH_BOUNDARY_ENTERED', dispatch_boundary_entered=True)],
                            charged_cpu_seconds=0, accounting_complete=False, potential_intent_cpu_seconds=30)
        audit.launch_count = 1
        expected_command = ['docker', 'container', 'inspect', record['container_name']]
        calls = []
        def simulated_absence_unavailable(command, **kwargs):
            check(command == expected_command, 'Unexpected simulated command')
            calls.append(command)
            return q.subprocess.CompletedProcess(command, 1, b'[]\n', b'Cannot connect to Docker daemon')
        with patch.object(q.subprocess, 'run', side_effect=simulated_absence_unavailable):
            try:
                audit.settle(original['run'])
            except ValueError as error:
                check(str(error) == 'Container absence was not verified', 'Did not reach expected absence boundary: ' + str(error))
            else:
                raise RuntimeError('Unknown container absence must fail')
        attempt = audit.report['attempts'][0]
        persisted = json.loads((audit.root / 'report.json').read_bytes())
        check(calls == [expected_command], 'Wrong simulated inspection count')
        check(persisted['charged_cpu_seconds'] == 30, 'Known charge not durably retained')
        check(attempt['outcome'] == 'ACCOUNTING_OBSERVED_CLEANUP_NOT_INDEPENDENTLY_VERIFIED', 'Failure phase misleading')
        check(attempt['dispatch_boundary_entered'] is True and audit.report['accounting_complete'] is False, 'Dispatch or incompleteness missing')
        check(not attempt.get('container_absent') and not attempt.get('ledger_verified'), 'Unknown cleanup accepted')
        print(json.dumps(dict(probe='cleanup_unavailable', status='PASS', report_charge=30, retained_receipt_charge=original['final_receipt']['compute']['charged_cpu_seconds'], retained_ledger_charge=record['compute']['charged_cpu_seconds'], outcome=attempt['outcome'], accounting_complete=False), sort_keys=True))
        reduced = copy.deepcopy(ledger)
        reduced['runs'][rid]['compute']['charged_cpu_seconds'] = 0
        audit.observe_accounting(reduced)
        check(audit.report['charged_cpu_seconds'] == 30 and attempt['observed_charged_cpu_seconds'] == 30 and attempt['charge_regression_observed'] is True, 'Reduced charge was not retained/flagged')
        missing = copy.deepcopy(ledger)
        missing['runs'] = {}
        audit.observe_accounting(missing)
        check(audit.report['charged_cpu_seconds'] == 30 and 'NO_RECORD_OBSERVED' in attempt['accounting_observation'], 'Missing record erased charge/exposure')
        mismatched = copy.deepcopy(ledger)
        mismatched['runs'][rid]['owner'] = 'incorrect'
        audit.observe_accounting(mismatched)
        check(audit.report['charged_cpu_seconds'] == 30 and 'IDENTITY_MISMATCH' in attempt['accounting_observation'], 'Wrong identity erased charge')
        for value in (True, 30.0, -1, 31):
            invalid = copy.deepcopy(ledger)
            invalid['runs'][rid]['compute']['charged_cpu_seconds'] = value
            audit.observe_accounting(invalid)
            check(audit.report['charged_cpu_seconds'] == 30 and 'INVALID_AMOUNTS' in attempt['accounting_observation'], 'Invalid amount erased charge')
        print(json.dumps(dict(probe='monotonic_charge_and_unresolved_exposure', status='PASS', lower_charge_flagged=True, missing_record_retains_charge=True, identity_mismatch_retains_charge=True, invalid_amount_cases=4), sort_keys=True))
        audit.launch_count = 4
        with patch.object(q, 'file_hash', side_effect=RuntimeError('Fifth attempt must reject before reading target')):
            try:
                audit.before_launch('unused', 'unused', 'unused')
            except ValueError as error:
                check('ceiling' in str(error), 'Wrong fifth-attempt refusal')
            else:
                raise RuntimeError('Fifth attempt accepted')
        print(json.dumps(dict(probe='fifth_attempt_refuses_before_read_or_dispatch', status='PASS'), sort_keys=True))
print(json.dumps(dict(status='PASS', pure_oracle_tests=len(names), scratch_probes=3, real_process_calls=0, actual_docker_attempts=0, markers_created=0, marker_tests_excluded=sorted(excluded)), sort_keys=True))
