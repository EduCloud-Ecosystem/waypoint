#!/usr/bin/env python3
"""Qualify completed source builds in a disposable application/catalog copy."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

from build_sources import bounded, sha, write
from research_workspace.catalog import source_identifier, validate_snapshot
from research_workspace.evidence import verify_bundle

ROOT = Path(__file__).resolve().parent


def candidate(build, report):
    runtime = build['runtime']
    base = next(e for e in json.loads((ROOT / 'environments.json').read_text())['environments']
                if e['runtime']['id'] == runtime and e['status'] == 'active')
    entry = copy.deepcopy(base)
    version = (2 if runtime == 'python' else 3) + (1 if report['platform'] == 'linux/amd64' else 0) + 2 * (build['repetition'] - 1)
    entry.update(id=source_identifier(entry['recipe'], version, entry['recipe_sha256'], build['image']), version=version, status='candidate',
                 name=f"{runtime.capitalize()} source build {version}", image=build['image'])
    entry['runtime'] = {'id': runtime, 'version': build['observed_runtime_version'],
                        'version_evidence': 'Executed version command in the new source-built image; see build record.'}
    entry['platforms'] = [{'os': 'linux', 'architecture': build['architecture'], 'evidence_status': 'PASS',
                          'evidence_boundary': 'Native clean source compilation and runtime version probe. Workload qualification is recorded separately.'}]
    contract = report['inputs']
    inputs = [
        ('runtime-source', contract['runtimes'][runtime]['url'], contract['runtimes'][runtime]['sha256']),
        ('base-image', build['base_image'], build['base_image'].split('sha256:')[1]),
        ('buildkit-image', contract['buildkit_image'], contract['buildkit_image'].split('sha256:')[1]),
        ('dockerfile', 'source-builds/Dockerfile', build['context_sha256']['Dockerfile']),
        ('build-script', 'source-builds/build.sh', build['context_sha256']['build.sh']),
        ('input-contract', 'source-builds/inputs.json', build['context_sha256']['inputs.json']),
        ('package-inventory', f"{runtime}-{build['repetition']}/packages.tsv", build['package_inventory_sha256']),
        ('build-record', f"{runtime}-{build['repetition']}/build.json", build['record_sha256']),
    ]
    entry['provenance'].update(source_revision=report['source_revision'],
        image_origin='New locally built image. SHA-256 is the local Docker image ID; OCI manifest/config digests are separately recorded. Not the historical opaque image.',
        build_inputs_status='RECORDED', build_inputs=[{'kind': k, 'reference': r, 'sha256': s} for k, r, s in inputs],
        clean_rebuild={'status': 'PASS', 'boundary': 'Runtime compiled from hash-verified official source with network disabled, a pinned Debian base and signed package snapshot, and --no-cache. OS and compiler binaries are inherited, not rebuilt from source. No bit-identical image claim.'},
        execution_evidence={'status': 'NOT RUN', 'reference': 'qualification/verification.json',
                            'boundary': 'Candidate is disabled outside a disposable qualification copy. Build success alone does not authorize promotion.'})
    entry['limitations'].append('Local image IDs require retaining/loading the exact image for replay; a rebuild can have a different identity. Independent human and second-machine acceptance remain NOT RUN.')
    validate_snapshot(entry, entry['recipe'], entry['recipe_sha256'], entry['image'])
    return entry


def qualify(builds_path, broker, output):
    output.mkdir(parents=True, exist_ok=False)
    report = json.loads(builds_path.read_text())
    if report['status'] != 'PASS' or not report.get('builder_stopped'):
        raise ValueError('Require successful clean builds and a stopped owned builder.')
    result = {'status': 'FAIL', 'build_report_sha256': sha(builds_path.read_bytes()), 'runs': [],
              'promotion': 'DISABLED', 'independent_human_second_machine': 'NOT RUN'}
    try:
        for repetition in range(1, report['repetitions'] + 1):
            completed = [b for b in report['builds'] if b['repetition'] == repetition]
            if {b['runtime'] for b in completed} != {'python', 'r'} or len(completed) != 2 or any(b['status'] != 'PASS' for b in completed):
                raise ValueError('Both completed builds are required for every repetition.')
            entries = []
            for build in completed:
                directory = builds_path.parent / f"{build['runtime']}-{repetition}"
                if sha((directory/'build.log').read_bytes()) != build['log_sha256'] or sha((directory/'packages.tsv').read_bytes()) != build['package_inventory_sha256']:
                    raise ValueError('Build evidence changed after completion.')
                for filename, expected in build['context_sha256'].items():
                    if sha((directory/'context'/filename).read_bytes()) != expected:
                        raise ValueError('Recorded build context has changed.')
                build = dict(build, record_sha256=sha((directory/'build.json').read_bytes()))
                entries.append(candidate(build, report))
            target = output / f'repetition-{repetition}'
            target.mkdir()
            write(target / 'candidates.json', {'schema_version': 1, 'environments': entries})
            workspace = target / 'workspace'
            shutil.copytree(ROOT, workspace, ignore=shutil.ignore_patterns('evidence', '__pycache__', '.runtime', '*.pyc'))
            document = json.loads((workspace/'environments.json').read_text())
            # This copy is the only activation surface. Preserve every legacy
            # identity for historical replay; do not edit the source checkout.
            document['environments'] = [e for e in document['environments'] if e['provenance']['build_inputs_status'] == 'NOT RECORDED']
            for entry in document['environments']:
                entry['status'] = 'archived'
            for entry in copy.deepcopy(entries):
                entry['status'] = 'active'
                document['environments'].append(entry)
            write(workspace/'environments.json', document)
            images = {e['runtime']['id']: e['image'] for e in entries}
            command = [sys.executable, str(workspace/'verify.py'), '--broker', str(broker), '--python-image', images['python'],
                       '--r-image', images['r'], '--full-matrix', '--output', str(target/'qualification')]
            row = {'repetition': repetition, 'status': 'FAIL', 'images': images}
            result['runs'].append(row)
            bounded(command, target/'qualification.log', timeout=600, maximum=1024**2)
            verification = json.loads((target/'qualification/verification.json').read_text())
            if verification['status'] != 'PASS' or not verification['all_recorded_runs_cleanup_verified']:
                raise ValueError('Qualification or cleanup failed.')
            row['bundles'] = {}
            for label in ('first-project', 'second-project'):
                payload = (target/'qualification'/f'{label}.zip').read_bytes()
                checked = verify_bundle(payload)
                if checked['run_count'] != 4:
                    raise ValueError('Full matrix was not exported.')
                row['bundles'][label] = checked
            row.update(status='PASS', checks_passed=verification['checks_passed'])
            print(json.dumps({'repetition': repetition, 'status': 'PASS', 'checks': row['checks_passed']}), flush=True)
        result['status'] = 'PASS'
    except Exception as exc:
        result['failure'] = str(exc)
        raise
    finally:
        write(output/'qualification.json', result)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--builds', type=Path, required=True)
    parser.add_argument('--broker', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    qualify(args.builds.resolve(), args.broker.resolve(), args.output.resolve())
