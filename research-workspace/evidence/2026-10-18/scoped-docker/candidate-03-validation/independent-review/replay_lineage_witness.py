"""Replay the byte-identical D5 witness, optionally removing one check in memory."""
import ast
import hashlib
import json
from pathlib import Path
import runpy
import sys

BASE = Path(__file__).resolve().parent
SOURCE = BASE / 'source/research-workspace'
mode = sys.argv[1]
if mode not in ('fixed', 'reverted'):
    raise ValueError('Only synthetic fixed/reverted modes are permitted')
sys.path[:0] = [str(SOURCE), str(SOURCE / 'tests')]
import qualify_scoped_docker as q

removed = []
if mode == 'reverted':
    tree = ast.parse((SOURCE / 'qualify_scoped_docker.py').read_text())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'verify_settlement')
    kept = []
    for statement in function.body:
        matching = any(isinstance(n, ast.Constant) and n.value == 'Final HTTP lineage differs from prelaunch binding or fixed matrix'
                       for n in ast.walk(statement))
        if matching:
            removed.append(dict(lineno=statement.lineno, end_lineno=statement.end_lineno))
        else:
            kept.append(statement)
    if len(removed) != 1:
        raise ValueError('Expected exactly the new D5 require statement')
    function.body = kept
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    namespace = dict(q.__dict__)
    exec(compile(module, '<in-memory D5 check reversion>', 'exec'), namespace)
    q.verify_settlement = namespace['verify_settlement']

path = BASE / mode / 'independent_lineage_projection_probe.py'
runpy.run_path(str(path), run_name='__main__')
result = json.loads((path.parent / 'lineage-projection-results.json').read_text())
expected_status = 'FAIL' if mode == 'fixed' else 'PASS'
expected_attempts = 1 if mode == 'fixed' else 4
expected_charge = 30 if mode == 'fixed' else 120
for item in result['results']:
    if (item['status'], item['attempts'], item['charged_cpu_seconds']) != (expected_status, expected_attempts, expected_charge):
        raise AssertionError(item)
    if mode == 'fixed' and 'Final HTTP lineage' not in item['failure']['message']:
        raise AssertionError(item)
summary = dict(candidate='fe4788c47a378e6234648dc4131ec4b870a8e713', mode=mode,
               original_witness_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
               historical_script_candidate_label=result['candidate'],
               actual_source=str(SOURCE), in_memory_removed_statements=removed,
               expectations_met=True, results=result['results'],
               scope='Temporary synthetic fixture only. No source files changed; reversion is in-memory function substitution only.')
(BASE / mode / 'replay-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
print(json.dumps(summary, indent=2))
