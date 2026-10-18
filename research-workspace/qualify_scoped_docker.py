"""One reviewed four-attempt local v4 exercise. Preparation never dispatches.

The fixed exercise marker is deliberately separate from the output directory.
There is no resume, retry, alternative output, refund, image pull or build mode.
This is qualification instrumentation, not a production capacity/grant system.
"""
import argparse
import copy
import datetime as dt
import gzip
import hashlib
import http.cookiejar
import io
import json
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import urllib.error
import urllib.request
import zipfile

from research_workspace import finite_authority as finite, recovery
from research_workspace.evidence import MAX_ARCHIVE_BYTES, verify_bundle
from research_workspace.identity import Identity
from research_workspace.runtime import Broker, strict_json, validate_receipt
from research_workspace.server import Application, Handler, Server
from research_workspace.store import canonical
PINS = Path(__file__).resolve().parent / 'qualification/broker-upgrade/pins.json'

HERE = Path(__file__).resolve().parent
BASE = '0efc6fd734e7f812cabc3822d2ea93b8c1063752'
EXERCISE = 'waypoint-v4-docker-20261018-01'
DESTINATION = Path('/Users/gregchism/Documents/Codex/2026-10-18') / EXERCISE
# Retained PR44 preparation for the accepted current Outfitter16 broker.
BROKER = Path('/private/var/folders/kl/3pnz2w2j2858c3gfr4429gn40000gn/T/educloud-broker-upgrade-build-_kdcsi3d/qualified/candidate')
BROKER_SHA = '1bf1ce898d6d10e574ab7b2f79a830fa94fa740867e208470bfc15790dc41bb1'
BROKER_COMMIT = '5446438985e31cb739f2048a2aa2176918831a7e'
IMAGES = {
    'python': 'python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36',
    'r': 'r-base@sha256:fa1972f31def171b83e0911e947ab8b57db143f0fc8a67af4c0d5ac329041646',
}
CAPS = dict(cpus=1, memory_mb=256, pids_limit=64, timeout_seconds=30, max_output_bytes=65536)
MATRIX = [
    dict(slot='first-python-baseline', project='first', runtime='python', condition='baseline', fixture='public-fixture.csv'),
    dict(slot='first-r-alternative', project='first', runtime='r', condition='alternative', fixture='public-fixture.csv'),
    dict(slot='second-r-baseline', project='second', runtime='r', condition='baseline', fixture='second-project.csv'),
    dict(slot='second-python-alternative', project='second', runtime='python', condition='alternative', fixture='second-project.csv'),
]
# Hand-calculated fixture answers, independent of the production result helper.
ORACLES = [
    dict(condition='baseline', count=10, mean=18, groups=[dict(group='alpha', count=5, mean=22), dict(group='beta', count=5, mean=14)]),
    dict(condition='alternative', count=6, mean=8.5, groups=[dict(group='alpha', count=3, mean=3), dict(group='beta', count=3, mean=14)]),
    dict(condition='baseline', count=10, mean=9, groups=[dict(group='comparison', count=5, mean=12), dict(group='control', count=5, mean=6)]),
    dict(condition='alternative', count=6, mean=7.5, groups=[dict(group='comparison', count=3, mean=9), dict(group='control', count=3, mean=6)]),
]
REQUIRED = {
    'fresh_v4_cli', 'anonymous_refused', 'source_candidates_disabled',
    'two_projects', 'queued_cancel_zero_dispatch', 'current_access_zero_dispatch',
    'oidc_refused', 'first_export', 'second_export', 'removed_member_export_refused',
    'four_exact_settlements', 'server_closed', 'restore_refused', 'final_ledger_120',
    *(row['slot'] for row in MATRIX),
}
FORMAT = 'waypoint-v4-docker-qualification-v1'


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\n'


def digest(content):
    return hashlib.sha256(content).hexdigest()


def file_hash(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def exclusive(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())
    directory = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def observe(argv, timeout=20):
    # Only bounded local read-only commands are used for prerequisite inspection.
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    require(result.returncode == 0, 'Required local observation unavailable: ' + argv[0])
    require(len(result.stdout) <= 2 * 1024 * 1024, 'Oversized prerequisite observation')
    return result.stdout


def source_hashes():
    files = {p for folder in ('research_workspace', 'recipes', 'static', 'examples')
             for p in (HERE / folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    files.update(HERE / name for name in ('qualify_scoped_docker.py', 'tests/test_scoped_docker_qualification.py',
                 'environments.json', 'images.lock.json', 'qualification/broker-upgrade/pins.json',
                 'evidence/2026-10-18/broker-upgrade/verification.json'))
    return {p.relative_to(HERE).as_posix(): file_hash(p) for p in sorted(files)}


def prerequisites(bound=None):
    require(file_hash(BROKER) == BROKER_SHA and os.access(BROKER, os.X_OK), 'Reviewed existing broker unavailable')
    pin = strict_json(PINS.read_bytes())['candidate']
    identity = strict_json((HERE / 'evidence/2026-10-18/broker-upgrade/verification.json').read_bytes())['provenance']['candidate']
    attestation_path = BROKER.with_name('candidate.provenance.json')
    attestation = strict_json(attestation_path.read_bytes())
    metadata = observe(['go', 'version', '-m', str(BROKER)]).decode().splitlines()[1:]
    require(identity['source_commit'] == BROKER_COMMIT and identity['sha256'] == BROKER_SHA
            and identity['source_sha256'] == pin['source_sha256'] and identity['source_tree'] == pin['tree']
            and identity['build_manifest_sha256'] == file_hash(attestation_path)
            and identity['build_metadata'] == metadata, 'Accepted broker provenance mismatch')
    require(attestation['format'] == 'waypoint-broker-build-v1' and attestation['pin'] == pin
            and attestation['binary_sha256'] == BROKER_SHA and attestation['build_metadata'] == metadata
            and attestation['pins_sha256'] == file_hash(PINS), 'Retained broker preparation mismatch')
    for name, expected in pin['source_sha256'].items():
        require(file_hash(BROKER.parent / 'candidate-source' / name) == expected, 'Retained compiled source changed')
    if bound is None:
        require(not os.environ.get('DOCKER_HOST') and not os.environ.get('DOCKER_CONTEXT'), 'Use the reviewed local Docker context without overrides')
        context = observe(['docker', 'context', 'show']).decode().strip()
    else:
        context = bound['context']
        require(os.environ.get('DOCKER_HOST') == bound['endpoint'] and not os.environ.get('DOCKER_CONTEXT'), 'Execution endpoint changed')
    endpoint = strict_json(observe(['docker', 'context', 'inspect', context]))[0]['Endpoints']['docker']['Host']
    if bound is not None: require(endpoint == bound['endpoint'], 'Reviewed context endpoint changed')
    require(endpoint.startswith('unix://'), 'A local Docker socket is required')
    info = strict_json(observe(['docker', 'info', '--format', '{{json .}}']))
    require(info['NCPU'] >= 1 and info['MemTotal'] >= 256 * 1024**2, 'Existing local capacity prerequisites unavailable')
    require(all(info[x] is True for x in ('MemoryLimit', 'CpuCfsQuota', 'PidsLimit')), 'Required Docker caps unavailable')
    # No stopping or cleaning other workloads; concurrent Outfitter work closes this exercise.
    running = observe(['docker', 'ps', '--format', '{{.Names}}']).decode().splitlines()
    require(not any(n.startswith('outfitter-') for n in running), 'Another Outfitter container is active; NOT RUN')
    images = {}
    for runtime, image in IMAGES.items():
        details = strict_json(observe(['docker', 'image', 'inspect', image]))[0]
        require(details['Os'] == 'linux' and details['Architecture'] == 'arm64', 'Reviewed platform unavailable')
        images[runtime] = dict(reference=image, id=details['Id'], platform='linux/arm64')
    return dict(context=context, endpoint=endpoint, endpoint_sha256=digest(endpoint.encode()), daemon_id_sha256=digest(info['ID'].encode()),
                server_version=info['ServerVersion'], ncpu=info['NCPU'], memory_bytes=info['MemTotal'], images=images,
                broker_sha256=BROKER_SHA, broker_source_commit=BROKER_COMMIT, broker_provenance=identity,
                broker_build_metadata=observe(['go', 'version', '-m', str(BROKER)]).decode().splitlines()[1:],
                capacity_boundary='Existing local machine with required caps; no free-capacity, quota or termination guarantee.')


def prepare(destination):
    destination = Path(destination).absolute()
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    sources = source_hashes()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        for name in sources:
            content = (HERE / name).read_bytes()
            item = tarfile.TarInfo(name); item.size = len(content); item.mode = 0o644
            archive.addfile(item, io.BytesIO(content))
    archive = gzip.compress(buffer.getvalue(), mtime=0)
    exclusive(destination / 'source.tar.gz', archive)
    manifest = dict(format=FORMAT, exercise=EXERCISE, output=str(DESTINATION),
                    restart_marker=str(DESTINATION.with_suffix('.started.json')),
                    source_commit=observe(['git', '-C', str(HERE), 'rev-parse', 'HEAD']).decode().strip(),
                    runtime_base=BASE, sources=sources, source_archive_sha256=digest(archive),
                    broker_path=str(BROKER), broker_sha256=BROKER_SHA, broker_source_commit=BROKER_COMMIT,
                    broker_evidence='evidence/2026-10-18/broker-upgrade/verification.json',
                    images=IMAGES, attempts=MATRIX, oracles=ORACLES, limits=CAPS,
                    attempt_limit=4, conservative_cpu_seconds=120, project_budget_cpu_seconds=60,
                    required_controls=sorted(REQUIRED), status='PENDING_REVIEW',
                    execution_review='Coordinating and Outfitter accounting review required before run.',
                    quota_enforced=False, available_capacity='UNKNOWN', fifth_headroom='FAIL_UNCHANGED')
    try:
        manifest['prerequisites'] = prerequisites()
    except Exception as error:
        manifest.update(status='NOT RUN', prerequisite_error=str(error))
    exclusive(destination / 'manifest.json', encoded(manifest))
    return dict(status=manifest['status'], manifest_sha256=file_hash(destination / 'manifest.json'))


def validate_manifest(manifest):
    expected = dict(format=FORMAT, exercise=EXERCISE, output=str(DESTINATION),
                    restart_marker=str(DESTINATION.with_suffix('.started.json')), runtime_base=BASE,
                    broker_path=str(BROKER), broker_sha256=BROKER_SHA, broker_source_commit=BROKER_COMMIT,
                    images=IMAGES, attempts=MATRIX, oracles=ORACLES, limits=CAPS, attempt_limit=4,
                    conservative_cpu_seconds=120, project_budget_cpu_seconds=60,
                    required_controls=sorted(REQUIRED), quota_enforced=False,
                    available_capacity='UNKNOWN', fifth_headroom='FAIL_UNCHANGED')
    require(all(encoded(manifest.get(k)) == encoded(v) for k, v in expected.items()), 'Manifest differs from the finite exercise')
    require(manifest.get('status') == 'PENDING_REVIEW', 'Prerequisites did not qualify for execution')
    require(manifest['sources'] == source_hashes(), 'Reviewed source bytes changed')


def oracle_matches(value, expected):
    if type(expected) is dict:
        return (type(value) is dict and set(value) == set(expected)
                and ('count' not in value or type(value['count']) is int)
                and all(oracle_matches(value[k], v) for k, v in expected.items()))
    if type(expected) is list:
        return type(value) is list and len(value) == len(expected) and all(oracle_matches(a, b) for a, b in zip(value, expected))
    if type(expected) in (int, float):
        return type(value) in (int, float) and math.isfinite(value) and math.isclose(value, expected, rel_tol=1e-12, abs_tol=1e-9)
    return type(value) is type(expected) and value == expected


def settled(receipt):
    try:
        return (receipt['admitted'] is True and receipt['execution_started'] is True
                and receipt['cleanup_verified'] is True and receipt['unresolved_liability'] is False
                and receipt['status'] == 'succeeded' and type(receipt['exit_code']) is int and receipt['exit_code'] == 0
                and encoded(receipt['compute']) == encoded(dict(reserved_cpu_seconds=30, charged_cpu_seconds=30, provenance='estimated_upper_bound')))
    except (KeyError, TypeError, ValueError):
        return False


def ledger_identity(data, path):
    require(data['version'] == 2 and data['authority']['state_path'] == str(path), 'Wrong ledger path/version')
    require(data['authority']['admission'] == 'active' and not data['authority'].get('restore') and not data['authority'].get('fence'), 'Ledger is not a fresh active authority')
    return dict(owner=data['owner'], authority_id=data['authority']['authority_id'], path_sha256=digest(str(path).encode()))


def planned_request(index, rid, pid, state):
    plan = MATRIX[index]
    directory = Path(state).parent / 'runs' / rid
    command = (['python', '/recipe/recipe.py'] if plan['runtime'] == 'python' else ['Rscript', '--vanilla', '/recipe/recipe.R']) + ['/input/data.csv', plan['condition']]
    # In the reviewed Go Request struct's field order, also used by Request.Hash.
    return dict(schema_version=1, request_id=rid, project_id=pid, image=IMAGES[plan['runtime']], command=command,
                input_dir=str(directory / 'input'), recipe_dir=str(directory / 'recipe'), **CAPS,
                isolation='reviewed-local', allow_supervised_reaper=True)


def verify_projects(data, projects):
    require(set(projects) == {'first', 'second'} and len(set(projects.values())) == 2, 'Exactly two project identities required')
    expected = {pid: dict(project_id=pid, budget_cpu_seconds=60) for pid in projects.values()}
    require(set(data) == {'version', 'owner', 'authority', 'projects', 'runs'}, 'Unexpected ledger authority population')
    require(encoded(data['projects']) == encoded(expected), 'Ledger project population or budgets differ from manifest')
    return digest(encoded(expected))


def read_diagnostic(state, record):
    reference = record.get('output_sha256')
    require(type(reference) is str and len(reference) == 64 and all(c in '0123456789abcdef' for c in reference), 'Missing diagnostic reference')
    size = record.get('output_bytes')
    require(type(size) is int and 0 < size <= 6 * CAPS['max_output_bytes'] + 512, 'Invalid diagnostic byte count')
    path = Path(state) / 'outputs' / (reference + '.json')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        info = os.fstat(source.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_size == size, 'Diagnostic is not the retained bounded regular file')
        content = source.read(size + 1)
    require(len(content) == size and digest(content) == reference, 'Diagnostic byte/hash mismatch')
    return content


def verify_diagnostic(record, receipt, content):
    require(type(content) is bytes and len(content) <= 6 * CAPS['max_output_bytes'] + 512, 'Invalid diagnostic content')
    require(type(record.get('output_bytes')) is int and len(content) == record['output_bytes']
            and digest(content) == record.get('output_sha256'), 'Ledger diagnostic binding differs')
    require(not record.get('output_error') and not receipt.get('output_error'), 'Diagnostic persistence/retrieval failed')
    expected = dict(stdout=receipt['stdout'], stderr=receipt['stderr'])
    require(encoded(strict_json(content)) == encoded(expected), 'Receipt differs from retained ledger diagnostics')
    require(sum(len(v.encode()) for v in expected.values()) <= CAPS['max_output_bytes'], 'Combined diagnostic output exceeds cap')


def verify_settlement(binding, request, receipt, data, run, diagnostics, projects):
    """Join the generic receipt to retained prelaunch, ledger and application evidence."""
    require(verify_projects(data, projects) == binding['project_authority_sha256'], 'Project authority changed')
    require(binding['project_id'] == projects[MATRIX[binding['index']]['project']], 'Wrong planned project')
    validate_receipt(receipt, run)
    require(settled(receipt), 'Partial/unknown/zero-charge receipt is not qualification success')
    planned = planned_request(binding['index'], binding['request_id'], binding['project_id'], binding['ledger_path'])
    require(encoded(request) == encoded(planned) and digest(canonical(request).encode()) == binding['request_sha256'], 'Request differs from the fixed matrix or captured bytes')
    record = data['runs'][binding['request_id']]
    require(record['payload_hash'] == digest(json.dumps(planned, separators=(',', ':')).encode()), 'Broker payload hash differs')
    require(type(record['broker_pid']) is int and record['broker_pid'] > 0, 'Missing broker process identity')
    require(ledger_identity(data, Path(binding['ledger_path'])) == binding['ledger_identity'], 'Ledger identity changed')
    for key in ('request_id', 'project_id', 'image'):
        require(record[key] == receipt[key] == request[key] == binding[key], 'Receipt/request/ledger identity mismatch')
    verify_diagnostic(record, receipt, diagnostics)
    require(record['owner'] == binding['ledger_identity']['owner'], 'Wrong request owner')
    require(encoded(record['request']) == encoded(request), 'Ledger request differs from prelaunch bytes')
    require(settled(record) and record['compute'] == receipt['compute'], 'Ledger must independently retain the full charge')
    require(record['container_name'] == 'outfitter-' + digest((record['owner'] + ':' + binding['request_id']).encode())[:32], 'Wrong owned container')
    require(record['create_attempted'] is True and record['create_finished'] is True and record['create_ambiguous'] is False, 'Creation remains uncertain')
    require(run['id'] == binding['request_id'] and run['project_id'] == binding['project_id'] and run['status'] == 'succeeded'
            and run['cleanup_verified'] is True and run['compute']['unresolved_liability'] is False
            and encoded({k: run['compute'][k] for k in receipt['compute']}) == encoded(receipt['compute']), 'Application accounting differs')
    require(oracle_matches(run['metrics'], ORACLES[binding['index']]) and oracle_matches(strict_json(receipt['stdout']), ORACLES[binding['index']]), 'Independent fixture result mismatch')
    return record['container_name']


PROJECT_FIELDS = {'id', 'name', 'question', 'success_criterion', 'created_at', 'ends_at', 'budget_cpu_seconds', 'status'}
RUN_FIELDS = {'id', 'project_id', 'recipe', 'condition', 'status', 'created_at', 'started_at', 'finished_at',
              'input_sha256', 'recipe_sha256', 'environment', 'parameters', 'metrics', 'error', 'cleanup_verified', 'compute'}


def verify_public_export(payload, project, expected_runs, result_hashes):
    require(type(payload) is bytes and len(payload) <= MAX_ARCHIVE_BYTES, 'Export exceeds bound')
    verified = verify_bundle(payload)
    require(verified['valid'] is True and verified['run_count'] == len(expected_runs) and bool(expected_runs), 'Incomplete public export')
    expected = {run['id']: {key: run[key] for key in RUN_FIELDS} for run in expected_runs}
    require(len(expected) == len(expected_runs), 'Duplicate expected export run')
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        require(encoded(strict_json(archive.read('project.json'))) == encoded({key: project[key] for key in PROJECT_FIELDS}), 'Export project differs from captured HTTP project')
        runs = strict_json(archive.read('runs.json'))
        require(len(runs) == len(expected) and {run['id'] for run in runs} == set(expected), 'Export omitted or added run history')
        for run in runs:
            saved = expected[run['id']]
            require(encoded(run) == encoded(saved), 'Export status/accounting/lineage/metrics differ from captured HTTP record')
            prefix = 'runs/' + run['id'] + '/'
            recipe = 'recipe.py' if run['recipe'] == 'summary-python' else 'recipe.R'
            require(digest(archive.read(prefix + 'input.csv')) == saved['input_sha256'], 'Export input differs')
            require(digest(archive.read(prefix + recipe)) == saved['recipe_sha256'], 'Export recipe differs')
            if saved['status'] == 'succeeded':
                result = archive.read(prefix + 'result.json')
                require(digest(result) == result_hashes[run['id']] and oracle_matches(strict_json(result), saved['metrics']), 'Export result differs from captured bytes/oracle')
            else:
                require(prefix + 'result.json' not in archive.namelist(), 'Refused run acquired a result')
    return dict(project_id=project['id'], run_ids=sorted(expected),
                project_sha256=digest(encoded({key: project[key] for key in PROJECT_FIELDS})),
                records_sha256=digest(encoded(expected)), archive_sha256=digest(payload))


def accepted(report, manifest):
    try:
        require(report['format'] == FORMAT and report['exercise'] == EXERCISE and 'failure' not in report, 'Wrong/failed report')
        require(set(report['checks']) == REQUIRED and all(value is True for value in report['checks'].values()), 'Incomplete or additional controls')
        attempts = report['attempts']
        require(len(attempts) == 4 and len({a['binding']['request_id'] for a in attempts}) == 4, 'Exactly four distinct attempts required')
        for index, (attempt, plan) in enumerate(zip(attempts, MATRIX)):
            binding = attempt['binding']
            require(binding['index'] == index and binding['slot'] == plan['slot'] and binding['exercise'] == EXERCISE, 'Wrong attempt order')
            require(binding['broker_sha256'] == BROKER_SHA and binding['image'] == IMAGES[plan['runtime']], 'Wrong binary/image')
            require(binding['broker_source_commit'] == BROKER_COMMIT
                    and binding['source_commit'] == manifest['source_commit']
                    and binding['source_archive_sha256'] == manifest['source_archive_sha256']
                    and binding['image_id'] == manifest['prerequisites']['images'][plan['runtime']]['id']
                    and binding['ledger_path'] == str(Path(manifest['output']) / 'state.private/broker'), 'Wrong source, image or ledger binding')
            require(attempt['binding_sha256'] == digest(encoded(binding)), 'Changed binding')
            require(attempt.get('dispatch_boundary_entered') is True and attempt.get('outcome') == 'succeeded'
                    and not attempt.get('charge_regression_observed'), 'Incomplete dispatch or observed charge regression')
            require(type(attempt.get('observed_charged_cpu_seconds')) is int and attempt['observed_charged_cpu_seconds'] == 30
                    and type(attempt.get('observed_reserved_cpu_seconds')) is int and attempt['observed_reserved_cpu_seconds'] == 30,
                    'Missing full correlated accounting observation')
            require(settled(attempt['final_receipt']) and attempt['ledger_verified'] is True and attempt['container_absent'] is True, 'Unsettled attempt')
            require(all(attempt['final_receipt'][k] == binding[k] for k in ('request_id', 'project_id', 'image')), 'Generic receipt cannot pass')
            require(oracle_matches(attempt['metrics'], ORACLES[index]), 'Wrong numerical oracle')
            require(binding['manifest_sha256'] == report['manifest_sha256'] and attempt['ledger_identity'] == binding['ledger_identity'], 'Different manifest/ledger')
            identity = attempt['ledger_identity']
            ledger = dict(version=2, owner=identity['owner'], authority=dict(authority_id=identity['authority_id'],
                          state_path=binding['ledger_path'], admission='active'),
                          projects={pid: dict(project_id=pid, budget_cpu_seconds=60) for pid in report['projects'].values()},
                          runs={binding['request_id']: attempt['ledger_record']})
            verify_settlement(binding, attempt['request'], attempt['final_receipt'], ledger, attempt['run'],
                              attempt['diagnostics_utf8'].encode(), report['projects'])
        identities = [a['binding']['ledger_identity'] for a in attempts]
        require(all(x == identities[0] for x in identities), 'Multiple ledger authorities')
        pids = [a['binding']['project_id'] for a in attempts]
        require(pids[0] == pids[1] and pids[2] == pids[3] and pids[0] != pids[2], 'Wrong project matrix')
        require(type(report['charged_cpu_seconds']) is int and report['charged_cpu_seconds'] == 120
                and report['accounting_complete'] is True, 'Full conservative charges required')
        return True
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return False


class Audit:
    def __init__(self, manifest, manifest_sha):
        self.manifest = manifest
        self.root = Path(manifest['output'])
        self.state = self.root / 'state.private'
        # Claim the entire exercise once, independent of whether output survives.
        exclusive(Path(manifest['restart_marker']), encoded(dict(exercise=EXERCISE, manifest_sha256=manifest_sha,
                  output=str(self.root), status='STARTED_NO_RESUME', attempt_limit=4)))
        self.root.mkdir(mode=0o700, exist_ok=False)
        self.report = dict(format=FORMAT, exercise=EXERCISE, status='FAIL', manifest_sha256=manifest_sha,
                           checks={}, attempts=[], refusal_controls={}, projects={}, project_records={}, artifacts={}, attempt_limit=4, conservative_cpu_seconds=120,
                           charged_cpu_seconds=0, accounting_complete=False, potential_intent_cpu_seconds=0, quota_enforced=False, available_capacity='UNKNOWN',
                           fifth_headroom='FAIL_UNCHANGED', failure_boundary='Interrupted/partial exercise requires manual review; no resume or new allowance.')
        exclusive(self.root / 'manifest.json', encoded(manifest))
        self.save()
        self.pending_launch = None
        self.launch_count = 0

    def save(self):
        recovery.atomic_json(self.root / 'report.json', self.report)

    def check(self, name, passed):
        require(name in REQUIRED and name not in self.report['checks'], 'Unexpected/duplicate check')
        self.report['checks'][name] = passed is True
        self.save()
        require(passed is True, 'Qualification control failed: ' + name)

    def artifact(self, name, content):
        exclusive(self.root / name, content)
        self.report['artifacts'][name] = dict(sha256=digest(content), bytes=len(content))
        self.save()

    def ledger(self):
        return strict_json((self.state / 'broker/ledger.json').read_bytes())

    def observe_accounting(self, data=None):
        """Retain monotonic known charges even when a later check cannot finish.

        These are correlated ledger observations, not an accepted outcome or
        independent cleanup assertion. Missing observations leave exposure open.
        """
        data = self.ledger() if data is None else data
        for attempt in self.report['attempts']:
            binding = attempt['binding']; record = data.get('runs', {}).get(binding['request_id'])
            if record is None:
                attempt['accounting_observation'] = 'NO_RECORD_OBSERVED; exposure not resolved'
                continue
            if (ledger_identity(data, Path(binding['ledger_path'])) != binding['ledger_identity']
                    or record.get('owner') != binding['ledger_identity']['owner']
                    or any(record.get(key) != binding[key] for key in ('request_id', 'project_id', 'image'))
                    or digest(canonical(record.get('request')).encode()) != binding['request_sha256']):
                attempt['accounting_observation'] = 'IDENTITY_MISMATCH; prior known charge retained'
                continue
            compute = record.get('compute', {})
            reserved = compute.get('reserved_cpu_seconds'); charged = compute.get('charged_cpu_seconds')
            if type(reserved) is not int or type(charged) is not int or not 0 <= charged <= reserved <= 30:
                attempt['accounting_observation'] = 'INVALID_AMOUNTS; prior known charge retained'
                continue
            previous = attempt.get('observed_charged_cpu_seconds', 0)
            attempt.update(observed_charged_cpu_seconds=max(previous, charged),
                           observed_reserved_cpu_seconds=max(attempt.get('observed_reserved_cpu_seconds', 0), reserved),
                           ledger_compute_observation=copy.deepcopy(compute),
                           ledger_cleanup_verified_observation=record.get('cleanup_verified'),
                           ledger_unresolved_liability_observation=record.get('unresolved_liability'),
                           accounting_observation='CORRELATED_LEDGER_OBSERVATION')
            if charged < previous:
                attempt['charge_regression_observed'] = True
            if attempt.get('outcome') != 'succeeded':
                attempt['outcome'] = 'ACCOUNTING_OBSERVED_CLEANUP_NOT_INDEPENDENTLY_VERIFIED'
        self.report['charged_cpu_seconds'] = max(self.report['charged_cpu_seconds'], sum(a.get('observed_charged_cpu_seconds', 0) for a in self.report['attempts']))
        self.save()

    def prepare_attempt(self, run):
        index = len(self.report['attempts'])
        require(index < 4, 'Four-attempt ceiling exhausted; no fifth attempt')
        require(self.pending_launch is None, 'Previous planned launch is incomplete')
        plan = MATRIX[index]
        executable = str(BROKER); state = str(self.state / 'broker')
        directory = self.state / 'runs' / run['id']
        filename = 'recipe.py' if plan['runtime'] == 'python' else 'recipe.R'
        request = planned_request(index, run['id'], run['project_id'], state)
        request_bytes = canonical(request).encode()
        request_path = directory / 'request.private.json'
        require(run['recipe'] == 'summary-' + plan['runtime'] and run['condition'] == plan['condition'], 'Wrong planned run')
        require(file_hash(directory / 'input/data.csv') == file_hash(HERE / 'examples' / plan['fixture']) == run['input_sha256'], 'Wrong fixture bytes')
        require(file_hash(directory / 'recipe' / filename) == run['recipe_sha256'], 'Wrong recipe bytes')
        require(Path(executable) == BROKER and file_hash(executable) == BROKER_SHA and Path(state) == self.state / 'broker', 'Wrong binary or state')
        data = self.ledger()
        project_hash = verify_projects(data, self.report['projects'])
        require(set(data['runs']) == {a['binding']['request_id'] for a in self.report['attempts']}, 'Unexpected prior ledger runs')
        require(all(settled(r) for r in data['runs'].values()), 'Prior attempt is unsettled; stop without retry')
        binding = dict(exercise=EXERCISE, index=index, slot=plan['slot'], request_id=run['id'], project_id=run['project_id'],
                       image=request['image'], image_id=self.manifest['prerequisites']['images'][plan['runtime']]['id'],
                       manifest_sha256=self.report['manifest_sha256'],
                       source_commit=self.manifest['source_commit'], source_archive_sha256=self.manifest['source_archive_sha256'],
                       broker_sha256=BROKER_SHA, broker_source_commit=BROKER_COMMIT, ledger_path=state,
                       ledger_identity=ledger_identity(data, Path(state)), project_authority_sha256=project_hash, request_sha256=digest(request_bytes),
                       input_sha256=run['input_sha256'], recipe_sha256=run['recipe_sha256'])
        # Persist outside SQLite owners, before Worker enters its final fence.
        # A preparation/spawn failure consumes this slot; there is no resume.
        self.artifact(f'{index + 1}-request.private.json', request_bytes)
        self.artifact(f'{index + 1}-ledger-before.private.json', encoded(data))
        self.artifact(f'{index + 1}-binding.private.json', encoded(binding))
        self.report['attempts'].append(dict(binding=binding, binding_sha256=digest(encoded(binding)), outcome='UNKNOWN_BEFORE_LAUNCH', dispatch_boundary_entered=False))
        self.report['potential_intent_cpu_seconds'] = 30 * len(self.report['attempts'])
        self.save()
        self.pending_launch = dict(executable=executable, state=state, request_path=str(request_path), request_bytes=request_bytes)

    def before_launch(self, executable, state, request_path):
        require(self.launch_count < 4, 'Four-attempt ceiling exhausted; no fifth attempt')
        pending = self.pending_launch
        require(pending is not None, 'Unexpected dispatch outside the four planned runs')
        require((executable, state, request_path) == (pending['executable'], pending['state'], pending['request_path']), 'Wrong final dispatch target')
        require(Path(request_path).read_bytes() == pending['request_bytes'] and file_hash(executable) == BROKER_SHA, 'Final request or binary differs from prepared identity')
        # This read-only assertion runs inside the production dispatch reservation.
        # All evidence writes preceded Worker.execute; no artifact writer is held
        # inside a SQLite owner. Consumption is permanent even if Popen raises.
        self.pending_launch = None
        self.launch_count += 1
        self.report['attempts'][-1].update(dispatch_boundary_entered=True, outcome='DISPATCH_BOUNDARY_ENTERED')

    def settle(self, run):
        attempt = self.report['attempts'][-1]; binding = attempt['binding']; index = binding['index']
        request_bytes = (self.root / f'{index + 1}-request.private.json').read_bytes()
        require(digest(request_bytes) == binding['request_sha256'], 'Prelaunch request changed')
        require(file_hash(self.root / f'{index + 1}-binding.private.json') == attempt['binding_sha256'], 'Prelaunch binding changed')
        require(file_hash(BROKER) == binding['broker_sha256'], 'Broker changed during execution')
        receipt_bytes = (self.state / 'runs' / run['id'] / 'receipt.private.json').read_bytes()
        receipt = strict_json(receipt_bytes); data = self.ledger()
        self.observe_accounting(data)
        self.artifact(f'{index + 1}-receipt.private.json', receipt_bytes)
        self.artifact(f'{index + 1}-ledger-after.private.json', encoded(data))
        self.artifact(f'{index + 1}-run.json', encoded(run))
        diagnostics = read_diagnostic(self.state / 'broker', data['runs'][run['id']])
        self.artifact(f'{index + 1}-diagnostics.private.json', diagnostics)
        result_bytes = (self.state / 'runs' / run['id'] / 'result.json').read_bytes()
        self.artifact(f'{index + 1}-result.json', result_bytes)
        name = verify_settlement(binding, strict_json(request_bytes), receipt, data, run, diagnostics, self.report['projects'])
        # Require an explicit absent result at the same observed local endpoint.
        observed = subprocess.run(['docker', 'container', 'inspect', name], capture_output=True, timeout=10)
        absence = dict(command=['docker', 'container', 'inspect', name], exit_code=observed.returncode,
                       stdout=observed.stdout.decode(), stderr=observed.stderr.decode())
        self.artifact(f'{index + 1}-container-observation.json', encoded(absence))
        require(observed.returncode == 1 and strict_json(observed.stdout) == []
                and observed.stderr.decode().strip() == 'Error response from daemon: No such container: ' + name, 'Container absence was not verified')
        attempt.update(outcome='succeeded', final_receipt=receipt, ledger_verified=True, container_absent=True, metrics=run['metrics'],
                       request=strict_json(request_bytes), run=run, ledger_record=data['runs'][binding['request_id']],
                       ledger_identity=ledger_identity(data, Path(binding['ledger_path'])),
                       diagnostics_utf8=diagnostics.decode(), result_sha256=digest(result_bytes))
        self.observe_accounting(data)
        self.save()


class ObservedBroker(Broker):
    def __init__(self, executable, state, audit):
        super().__init__(executable, state)
        self.audit = audit

    def command(self, *args):
        if args and args[0] == 'run':
            require(len(args) == 3 and args[1] == '--request', 'Unexpected run invocation')
            self.audit.before_launch(self.executable, self.state, args[2])
        return super().command(*args)


class HTTP:
    def __init__(self, base):
        self.base = base
        self.connection = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, method, path, data=None, key=None):
        headers = {'Content-Type': 'application/json', 'X-Research-Request': '1'}
        if key: headers['Idempotency-Key'] = key
        request = urllib.request.Request(self.base + path, data=encoded(data) if data is not None else None, method=method, headers=headers)
        try:
            response = self.connection.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read(MAX_ARCHIVE_BYTES + 1)
            require(len(raw) <= MAX_ARCHIVE_BYTES, 'HTTP response exceeds bounded archive size')
            return response.code, raw if response.headers.get_content_type() == 'application/zip' else strict_json(raw)

    def expect(self, code, method, path, data=None, key=None):
        status, value = self.request(method, path, data, key)
        require(status == code, f'Unexpected HTTP status {status} for {method} {path}')
        return value


def cli(*args):
    result = subprocess.run([sys.executable, '-m', 'research_workspace', *map(str, args)], cwd=HERE,
                            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(HERE)},
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    require(result.returncode == 0, 'Production CLI refused initialization/user setup')
    return strict_json(result.stdout)


def exercise(audit):
    server = thread = None
    failure = None
    try:
        # Credentials stay in memory; only the new disposable state stores hashes.
        owner_key = cli('init', '--state', audit.state, '--broker', BROKER, '--subject', 'owner',
                        '--python-image', IMAGES['python'], '--r-image', IMAGES['r'], '--total-budget-cpu-seconds', 120, '--scoped-sqlite')['access_key']
        editor_key = cli('user-add', '--state', audit.state, '--subject', 'editor')['access_key']
        app = Application(audit.state)
        app.broker = ObservedBroker(BROKER, audit.state / 'broker', audit)
        app.worker.broker = app.broker  # Same production Worker, no worker loop.
        audit.check('fresh_v4_cli', app.store.profile_version == 4 and app.config['mode'] == finite.SCOPED_MODE
                    and type(app.config['total_budget_cpu_seconds']) is int and app.config['total_budget_cpu_seconds'] == 120)
        server = Server(('127.0.0.1', 0), Handler); server.app = app
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        owner = HTTP(f'http://127.0.0.1:{server.server_port}'); editor = HTTP(owner.base)
        audit.check('anonymous_refused', owner.request('GET', '/api/projects')[0] == 401)
        owner.expect(200, 'POST', '/api/session', {'access_key': owner_key})
        editor.expect(200, 'POST', '/api/session', {'access_key': editor_key})
        del owner_key, editor_key
        environments = owner.expect(200, 'GET', '/api/environments')['environments']
        candidates = [e for e in environments if e['status'] == 'candidate']
        audit.check('source_candidates_disabled', len(candidates) == 4 and all(e['available'] is False for e in candidates))
        projects = {}
        for label in ('first', 'second'):
            project = owner.expect(201, 'POST', '/api/projects', dict(name='Synthetic ' + label,
                question='Do the fixed fixture summaries match independent answers?',
                success_criterion='Four bounded runs retain full charges and verified cleanup.',
                ends_at=(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat(), budget_cpu_seconds=60), label)
            require(type(project['budget_cpu_seconds']) is int and project['budget_cpu_seconds'] == 60, 'HTTP project allowance differs')
            projects[label] = project['id']
            audit.report['project_records'][label] = copy.deepcopy(project)
        audit.report['projects'] = copy.deepcopy(projects)
        verify_projects(audit.ledger(), projects)
        audit.artifact('projects.json', encoded(projects))
        audit.check('two_projects', len(set(projects.values())) == 2)
        pid = projects['first']; route = f'/api/projects/{pid}'
        data = dict(recipe='summary-python', condition='baseline', csv=(HERE / 'examples/public-fixture.csv').read_text(), parameters={})
        before = encoded(audit.ledger())
        cancelled = owner.expect(202, 'POST', route + '/runs', data, 'queued-cancel')
        owner.expect(200, 'POST', route + '/runs/' + cancelled['id'] + '/cancel', {})
        require(app.store.claim() is None, 'Queued cancellation unexpectedly claimed')
        saved = owner.expect(200, 'GET', route + '/runs/' + cancelled['id'])
        audit.report['refusal_controls']['queued_cancel'] = saved
        audit.check('queued_cancel_zero_dispatch', saved['status'] == 'cancelled' and saved['compute']['charged_cpu_seconds'] == 0
                    and saved['cleanup_verified'] is True and not audit.report['attempts'] and audit.launch_count == 0 and encoded(audit.ledger()) == before)
        owner.expect(200, 'POST', route + '/members', dict(subject='editor', role='editor'))
        refused = editor.expect(202, 'POST', route + '/runs', data, 'current-access')
        claimed = app.store.claim(); require(claimed['id'] == refused['id'], 'Wrong refusal-control claim')
        owner.expect(200, 'DELETE', route + '/members/editor')
        app.worker.execute(claimed)
        saved = owner.expect(200, 'GET', route + '/runs/' + refused['id'])
        audit.report['refusal_controls']['current_access'] = saved
        audit.check('current_access_zero_dispatch', saved['status'] == 'cancelled' and saved['compute']['charged_cpu_seconds'] == 0
                    and saved['cleanup_verified'] is True and not audit.report['attempts'] and audit.launch_count == 0 and encoded(audit.ledger()) == before)
        try:
            Identity({}, app.store)
        except ValueError as error:
            audit.check('oidc_refused', 'not qualified' in str(error))
        else:
            audit.check('oidc_refused', False)
        for index, plan in enumerate(MATRIX):
            pid = projects[plan['project']]
            run = owner.expect(202, 'POST', f'/api/projects/{pid}/runs', dict(recipe='summary-' + plan['runtime'], condition=plan['condition'],
                    csv=(HERE / 'examples' / plan['fixture']).read_text(), parameters={}), plan['slot'])
            claimed = app.store.claim(); require(claimed['id'] == run['id'], 'Unexpected queued run')
            audit.prepare_attempt(copy.deepcopy(claimed))
            app.worker.execute(claimed)
            require(len(audit.report['attempts']) == audit.launch_count == index + 1, 'Planned execution did not launch exactly once')
            saved = owner.expect(200, 'GET', f'/api/projects/{pid}/runs/{run["id"]}')
            audit.settle(saved)
            audit.check(plan['slot'], True)
        audit.check('four_exact_settlements', len(audit.report['attempts']) == 4 and audit.report['charged_cpu_seconds'] == 120)
        audit.report['exports'] = {}
        for label, pid in projects.items():
            payload = owner.expect(200, 'GET', f'/api/projects/{pid}/export')
            expected_runs = [a['run'] for a in audit.report['attempts'] if a['binding']['project_id'] == pid]
            if label == 'first': expected_runs += list(audit.report['refusal_controls'].values())
            result_hashes = {a['binding']['request_id']: a['result_sha256'] for a in audit.report['attempts']}
            audit.report['exports'][label] = verify_public_export(payload, audit.report['project_records'][label], expected_runs, result_hashes)
            audit.artifact(label + '-project.zip', payload)
            audit.check(label + '_export', True)
        audit.check('removed_member_export_refused', editor.request('GET', route + '/export')[0] == 403)
    except BaseException as error:
        failure = error
        # Persist failure BEFORE server shutdown or any further observation.
        audit.report['failure'] = dict(type=type(error).__name__, message=str(error))
        audit.report['status'] = 'FAIL'; audit.save()
        try:
            audit.observe_accounting()
        except BaseException as observation_error:
            audit.report['accounting_observation_error'] = type(observation_error).__name__
            audit.save()
    finally:
        if server is not None:
            try:
                server.shutdown(); server.server_close()
                if thread is not None: thread.join(timeout=5)
                audit.check('server_closed', thread is None or not thread.is_alive())
            except BaseException as error:
                if failure is None: failure = error
                audit.report.setdefault('failure', dict(type=type(error).__name__, message=str(error)))
                audit.save()
    if failure is not None:
        # No cancellation/reconciliation mutation, refunds, retry, or fabricated
        # settlement. The private ledger and every prelaunch intent survive.
        return audit.report
    try:
        checkpoint = recovery.checkpoint(audit.state, audit.root / 'checkpoint.private')
        replacement = audit.root / 'replacement-never-created.private'
        try:
            recovery.restore(audit.root / 'checkpoint.private', replacement,
                             trusted_sha256=checkpoint['checkpoint_sha256'], broker=str(BROKER))
        except ValueError as error:
            audit.check('restore_refused', 'not qualified' in str(error) and not replacement.exists())
        else:
            audit.check('restore_refused', False)
        final = audit.ledger()
        require(set(final['runs']) == {a['binding']['request_id'] for a in audit.report['attempts']}, 'Unexpected final execution')
        for attempt in audit.report['attempts']:
            verify_settlement(attempt['binding'], attempt['request'], attempt['final_receipt'], final, attempt['run'],
                              read_diagnostic(audit.state / 'broker', final['runs'][attempt['binding']['request_id']]), audit.report['projects'])
        audit.artifact('final-ledger.private.json', encoded(final))
        audit.check('final_ledger_120', all(settled(r) for r in final['runs'].values())
                    and sum(r['compute']['charged_cpu_seconds'] for r in final['runs'].values()) == 120)
        require(prerequisites(bound=audit.manifest['prerequisites']) == audit.manifest['prerequisites'], 'Dependency or local endpoint changed')
        require(source_hashes() == audit.manifest['sources'], 'Source bytes changed during exercise')
        for name, reference in audit.report['artifacts'].items():
            require(file_hash(audit.root / name) == reference['sha256'], 'Retained artifact changed: ' + name)
        audit.report['accounting_complete'] = True
        require(accepted(audit.report, audit.manifest), 'Final oracle refused qualification')
        audit.report['status'] = 'PASS'
    except BaseException as error:
        audit.report['failure'] = dict(type=type(error).__name__, message=str(error))
        audit.report['status'] = 'FAIL'
        audit.report['accounting_complete'] = False
    audit.save()
    return audit.report


def run(manifest_path, reviewed_sha):
    manifest_path = Path(manifest_path)
    require(file_hash(manifest_path) == reviewed_sha, 'Manifest differs from reviewed SHA256')
    manifest = strict_json(manifest_path.read_bytes())
    validate_manifest(manifest)
    require(file_hash(manifest_path.parent / 'source.tar.gz') == manifest['source_archive_sha256'], 'Captured source changed')
    # Check the one-time marker BEFORE dependencies. An interrupted attempt has
    # no new execution path, even if someone moves or removes its output folder.
    require(not Path(manifest['restart_marker']).exists(), 'Exercise already started. Status-only/manual review; no resume.')
    require(not Path(manifest['output']).exists(), 'Output must be exclusive and fresh')
    require(prerequisites() == manifest['prerequisites'], 'Reviewed prerequisites changed; NOT RUN')
    audit = Audit(manifest, reviewed_sha)
    # Bind all broker/watchdog Docker children to the reviewed local socket.
    # Initial preflight refused overrides, and the prior environment is restored.
    os.environ['DOCKER_HOST'] = manifest['prerequisites']['endpoint']
    previous = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(InterruptedError('SIGTERM: no resume')))
    try:
        return exercise(audit)
    finally:
        signal.signal(signal.SIGTERM, previous)
        os.environ.pop('DOCKER_HOST', None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    preparation = commands.add_parser('prepare'); preparation.add_argument('--output', required=True)
    execution = commands.add_parser('run'); execution.add_argument('--manifest', required=True); execution.add_argument('--reviewed-sha256', required=True)
    commands.add_parser('status')
    args = parser.parse_args()
    if args.command == 'prepare':
        print(json.dumps(prepare(args.output))); return 0
    if args.command == 'status':
        marker = DESTINATION.with_suffix('.started.json')
        print(json.dumps(dict(exercise=EXERCISE, started=marker.exists(), report=str(DESTINATION / 'report.json'),
                              execution='NO RESUME; manual review' if marker.exists() else 'NOT RUN'))); return 0
    report = run(args.manifest, args.reviewed_sha256)
    print(json.dumps(dict(status=report['status'], attempts=len(report['attempts']), charged_cpu_seconds=report['charged_cpu_seconds'],
                          accounting_complete=report['accounting_complete'], potential_intent_cpu_seconds=report['potential_intent_cpu_seconds'])))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
