"""Capture synthetic failure/accounting and pure binding control results."""
import copy
import json
from pathlib import Path
import sys
from unittest.mock import Mock

BASE = Path(__file__).resolve().parent
SOURCE = BASE / 'source/research-workspace'
sys.path[:0] = [str(SOURCE), str(SOURCE / 'tests')]
import qualify_scoped_docker as q
from test_scoped_docker_qualification import SimulatedWiringTests, complete_report, synthetic_manifest

results = []
cases = [
    ('baseline', {}),
    ('absence-failure', dict(absence_failure=True)),
    ('numeric-export', dict(export_mutation='numeric')),
    ('unknown-export', dict(export_mutation='unknown')),
    ('self-consistent-different-diagnostics', dict(bad_diagnostics='different')),
    ('interrupted-second-intent', dict(interrupt=True, interrupt_at=1)),
]
for name, arguments in cases:
    report = SimulatedWiringTests().simulated(**arguments)
    (BASE / (name + '-control-report.json')).write_text(json.dumps(report, indent=2) + '\n')
    expected = 'PASS' if name == 'baseline' else 'FAIL'
    results.append(dict(name=name, actual_status=report['status'], expected_status=expected,
                        matched=report['status'] == expected, charge=report['charged_cpu_seconds'],
                        accounting_complete=report['accounting_complete'],
                        attempts=len(report['attempts']), failure=report.get('failure')))

# A valid diagnostic hash/size and report retention must still fail on differing
# streams, independently of the external-file reader.
report = complete_report()
for attempt in report['attempts']:
    content = q.encoded(dict(stdout='different output', stderr='different stderr'))
    attempt['ledger_record'].update(output_sha256=q.digest(content), output_bytes=len(content))
    attempt['diagnostics_utf8'] = content.decode()
accepted = q.accepted(report, synthetic_manifest())
(BASE / 'self-consistent-diagnostic-oracle-report.json').write_text(json.dumps(report, indent=2) + '\n')
results.append(dict(name='self-consistent-diagnostic-oracle', accepted=accepted, matched=accepted is False))

# Missing/extra/native populations and noninteger budgets are all refused.
projects = {'first': 'a' * 32, 'second': 'b' * 32}
good = dict(version=2, owner='c' * 32, authority={}, runs={},
            projects={pid: dict(project_id=pid, budget_cpu_seconds=60) for pid in projects.values()})
for name in ('missing-project', 'wrong-map-id', 'float-budget', 'bool-budget', 'native-population'):
    data = copy.deepcopy(good)
    if name == 'missing-project': data['projects'].pop(projects['second'])
    elif name == 'wrong-map-id': data['projects'][projects['first']]['project_id'] = projects['second']
    elif name == 'float-budget': data['projects'][projects['first']]['budget_cpu_seconds'] = 60.0
    elif name == 'bool-budget': data['projects'][projects['first']]['budget_cpu_seconds'] = True
    else: data['native_accounts'] = {'unplanned': {}}
    try:
        q.verify_projects(data, projects)
    except ValueError as error:
        results.append(dict(name=name, rejected=True, matched=True, reason=str(error)))
    else:
        results.append(dict(name=name, rejected=False, matched=False))

audit = object.__new__(q.Audit)
audit.report = complete_report()
audit.save = Mock()
attempt = audit.report['attempts'][0]
binding = attempt['binding']
identity = binding['ledger_identity']
data = dict(version=2, owner=identity['owner'], authority=dict(authority_id=identity['authority_id'],
            state_path=binding['ledger_path'], admission='active'), runs={binding['request_id']: copy.deepcopy(attempt['ledger_record'])})
data['runs'][binding['request_id']]['compute']['charged_cpu_seconds'] = 0
audit.observe_accounting(data)
regression = bool(attempt.get('charge_regression_observed'))
data['runs'] = {}
audit.observe_accounting(data)
(BASE / 'monotonic-accounting-control-report.json').write_text(json.dumps(audit.report, indent=2) + '\n')
results.append(dict(name='charge-regression-and-disappearance', charge=audit.report['charged_cpu_seconds'],
                    regression_latched=regression, accounting_observation=attempt['accounting_observation'],
                    accepted=q.accepted(audit.report, synthetic_manifest()),
                    matched=regression and audit.report['charged_cpu_seconds'] == 120 and not q.accepted(audit.report, synthetic_manifest())))

result = dict(candidate='09418f231187a5465ba9d0b8546b3b59514959e9',
              scope='Temporary synthetic stores and pure objects only; no real provider, broker, Docker, production CLI or checkpoint.',
              controls=results, all_expected=all(row['matched'] for row in results))
(BASE / 'independent-failure-control-results.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
