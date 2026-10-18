"""Synthetic shared-projection drift: HTTP and export agree but prelaunch does not."""
import copy
import io
import json
from pathlib import Path
import sys
import zipfile
from unittest.mock import patch

BASE = Path(__file__).resolve().parent
SOURCE = BASE / 'source/research-workspace'
sys.path[:0] = [str(SOURCE), str(SOURCE / 'tests')]
import qualify_scoped_docker as q
from research_workspace.evidence import build_bundle
from test_scoped_docker_qualification import SimulatedWiringTests

results = []
for case in ('input', 'recipe'):
    paths = list((SOURCE / ('examples' if case == 'input' else 'recipes')).glob('*.csv' if case == 'input' else 'recipe.*'))
    hashes = {q.digest(path.read_bytes()): q.digest(path.read_bytes() + b'\n') for path in paths}
    original = q.HTTP.request
    structural = []

    def mutate_run(run):
        changed = copy.deepcopy(run)
        if changed.get('status') == 'succeeded':
            key = case + '_sha256'
            changed[key] = hashes[changed[key]]
            if case == 'recipe':
                changed['environment']['specification']['recipe_sha256'] = changed[key]
        return changed

    def changed_request(client, method, path, *args, **kwargs):
        status, payload = original(client, method, path, *args, **kwargs)
        if status != 200:
            return status, payload
        if isinstance(payload, dict) and 'project_id' in payload and payload.get('status') == 'succeeded':
            return status, mutate_run(payload)
        if not path.endswith('/export'):
            return status, payload
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            project = q.strict_json(archive.read('project.json'))
            runs = q.strict_json(archive.read('runs.json'))
            files = {name: archive.read(name) for name in archive.namelist() if name.startswith('runs/')}
        changed_runs = []
        for run in runs:
            if run['status'] == 'succeeded':
                name = 'input.csv' if case == 'input' else ('recipe.py' if run['recipe'] == 'summary-python' else 'recipe.R')
                files['runs/' + run['id'] + '/' + name] += b'\n'
            changed_runs.append(mutate_run(run))
        changed = build_bundle(project, changed_runs, files)
        structural.append(q.verify_bundle(changed)['valid'])
        return status, changed

    with patch.object(q.HTTP, 'request', changed_request):
        report = SimulatedWiringTests().simulated()
    (BASE / ('lineage-' + case + '-report.json')).write_text(json.dumps(report, indent=2) + '\n')
    results.append(dict(case=case, status=report['status'], failure=report.get('failure'), attempts=len(report['attempts']),
                        charged_cpu_seconds=report['charged_cpu_seconds'], structural_exports_valid=structural,
                        mismatch_count=sum(attempt['binding'][case + '_sha256'] != attempt['run'][case + '_sha256']
                                           for attempt in report['attempts'] if 'run' in attempt)))

(BASE / 'lineage-projection-results.json').write_text(json.dumps(dict(
    candidate='09418f231187a5465ba9d0b8546b3b59514959e9',
    scope='Only temporary synthetic fixture. All CLI/provider/Docker/checkpoint effects mocked; production state and sources unchanged.',
    expected='FAIL: final records and exported bytes must retain the prelaunch input/recipe hashes.',
    results=results), indent=2) + '\n')
print(json.dumps(results, indent=2))
