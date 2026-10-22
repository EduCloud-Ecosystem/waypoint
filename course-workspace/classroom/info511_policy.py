# SPDX-License-Identifier: Apache-2.0
"""Pure bounded instructor-policy assembly; no services, models or notebooks."""
import hashlib
import json
from pathlib import Path
import re
from assignment_profiles import LEGACY, get_profile


def read_json(path, limit=1024 * 1024):
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('bounded JSON input exceeded')
    return json.loads(raw)


def template_files(belay_source, image, *, profile=LEGACY):
    """Stage only pinned policy/source inputs, never operator answers."""
    profile = get_profile(profile)
    package = profile.package
    binding = read_json(package / 'operator/checker-binding.json')
    if not isinstance(binding.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', binding['sha256']):
        raise ValueError('assignment checker is not bound; review and pin before running')
    raw = (belay_source / binding['path_in_belay']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != binding['sha256']:
        raise ValueError('B1 checker changed; review and rebind before running')
    task = read_json(package / 'task.json')
    if task.get('task_id') != profile.exercise or task.get('task_version') != profile.task:
        raise ValueError('assignment profile and package differ')
    if profile.key == 'nhanes-v1':
        datasets = {}
        expected_paths = {'learner/data/DEMO_L_selected.csv', 'learner/data/BPXO_L_selected.csv'}
        if len(task['datasets']) != 2 or {entry['path'] for entry in task['datasets']} != expected_paths:
            raise ValueError('exact NHANES input pair required')
        for entry in task['datasets']:
            raw_data = (package / entry['path']).read_bytes()
            if hashlib.sha256(raw_data).hexdigest() != entry['sha256'] or binding['source_files'].get(Path(entry['path']).name) != entry['sha256']:
                raise ValueError('NHANES input changed')
            datasets[entry['path'].removeprefix('learner/')] = raw_data.decode()
        if set(datasets) != {'data/DEMO_L_selected.csv', 'data/BPXO_L_selected.csv'}:
            raise ValueError('exact NHANES input pair required')
        grading = {'version':'1', 'image':image, 'tests':[{'name':profile.correctness_test,
            'run':'python -I -B /cairn-policy/grade_csv.py','points':4}],
            'limits':{'timeout':20000000000,'memory_mb':256,'cpus':1,'network':'none'}}
        return {'grading.json':json.dumps(grading),
            'grade_csv.py':(package / 'operator/grade_csv.py').read_text(),
            profile.checker_module:raw.decode(), 'checker-binding.json':json.dumps(binding),
            'README.md':(package / 'learner/README.md').read_text(), **datasets}
    data = (package / task['dataset']['path']).read_bytes()
    if hashlib.sha256(data).hexdigest() != task['dataset']['sha256']:
        raise ValueError('synthetic input changed')
    # A real four-point correctness check, not a pass-all/full-rubric substitute.
    grading = {'version': '1', 'image': image, 'tests': [{
        'name': 'synthetic-csv-cell-preservation-only',
        'run': 'python -I -B /cairn-policy/grade_csv.py', 'points': 4}],
        'limits': {'timeout': 20000000000, 'memory_mb': 256, 'cpus': 1, 'network': 'none'}}
    return {'grading.json': json.dumps(grading),
            'grade_csv.py': (package / 'operator/grade_csv.py').read_text(),
            'tidy_check.py': raw.decode(),
            'checker-binding.json': json.dumps(binding),
            'data/info511-inflation-synthetic.csv': data.decode(),
            'README.md': (package / 'learner/README.md').read_text()}
