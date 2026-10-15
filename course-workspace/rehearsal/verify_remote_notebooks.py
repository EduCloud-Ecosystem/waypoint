"""Actual Python/R over two worker channels on ONE disposable Docker daemon."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(Path(__file__).resolve().parent)]
import evidence
from smoke import kernel, change, xsrf
from verify_authenticated_runtime import command, port, wait, configured_cpu_limit, request_notebook_start
from remote_notebook_fixture import allocation_for_host, mount_plan, WORKERS, ENDPOINTS

VERSION = 'waypoint-remote-notebook-rehearsal-v1'
SCOPE = 'actual-local-docker-one-daemon-two-worker-services-synthetic-notebooks'
PHASES = (
    'central_has_no_socket_homes_or_worker_state',
    'disjoint_worker_state_guardian_mounts_and_actual_caps',
    'managed_enrollment_and_nonowner_role_scope_refusal',
    'source_and_peer_real_python_r_saved_bytes',
    'central_service_loss_guardians_stop_and_retain_homes',
    'local_recovery_helper_stages_exact_stopped_home',
    'post_fence_interruption_quarantines_both_endpoints',
    'stale_approval_refused_fresh_reconciliation_preserves_peer',
    'receiver_real_python_r_retained_work_no_automatic_start',
    'restarted_source_hub_cannot_revive',
    'worker_channel_loss_stops_receiver_preserves_peer',
    'worker_service_restart_retains_floors_journal_and_work',
    'guardian_loss_worker_watchdog_stops_receiver_preserves_peer',
    'managed_hub_rotation_staged_inert_overlap_and_retire',
    'rotated_hub_restarts_with_real_python_r_and_work',
    'guardian_revocation_stops_only_its_worker',
    'owned_cleanup_and_final_source_verified',
)
CONTROLLER_FILES = ('ownership_contract.py', 'authority_access.py', 'authority_operator.py',
    'runtime_authority.py', 'runtime_guardian.py', 'worker_transport.py', 'worker_channel_state.py',
    'worker_runtime.py', 'remote_backend.py', 'remote_control.py', 'remote_runtime_service.py',
    'remote_credentials.py', 'worker_enrollment.py', 'worker_credentials.py', 'volume_archive.py')
CODE_FILES = frozenset(CONTROLLER_FILES) | {'hub/' + name for name in ('authority_client.py', 'authority_spawner.py',
    'home_policy.py', 'jupyterhub_config.py', 'settings.py')} | {'tests/authority_test_pki.py',
    'rehearsal/authenticated_fixture.py', 'rehearsal/authenticated_evidence.py',
    'rehearsal/remote_notebook_fixture.py', 'rehearsal/verify_remote_notebooks.py',
    'rehearsal/test_remote_notebook_fixture.py', 'rehearsal/verify_authenticated_runtime.py', 'smoke.py'}
FLAGS = {'actual_python_r': False, 'actual_mtls_worker_channels': False, 'one_docker_daemon': True,
    'actual_two_host_isolation': False, 'independent_host_file_transfer': False, 'encrypted_backup': False,
    'off_host_backup': False, 'production_ready': False, 'course_capacity_acceptance': False}


def observed_flags(phases):
    if not isinstance(phases, list): raise ValueError('remote_notebook_phases_invalid')
    return dict(FLAGS, actual_python_r=PHASES[3] in phases, actual_mtls_worker_channels=PHASES[2] in phases)


def require_refusal(reply, expected):
    if (not isinstance(reply, dict) or set(reply) != {'ok', 'refused', 'code'}
            or reply['ok'] is not False or reply['refused'] is not True or reply['code'] != expected):
        raise ValueError('remote_notebook_expected_refusal_missing')


def exercise_kernels(session, base, invoke=kernel):
    # smoke.kernel's actual protocol requires this exact execution sentinel.
    invoke(session, base, 'alice', 'python3', "print('CONTINUITY_OK')")
    invoke(session, base, 'alice', 'ir', "cat('CONTINUITY_OK\\n')")


def await_worker_readiness(probe, *, timeout=45, monotonic=time.monotonic, sleep=time.sleep):
    """Only explicit transport-startup unavailability can become a retry."""
    if type(timeout) not in (int, float) or not 0 < timeout <= 45:
        raise ValueError('worker_readiness_timeout_invalid')
    deadline = monotonic() + timeout
    while True:
        value = probe()
        if (not isinstance(value, dict) or set(value) != {'ready', 'verified_workers'}
                or type(value['ready']) is not bool or type(value['verified_workers']) is not int
                or not 0 <= value['verified_workers'] <= len(WORKERS)
                or value['ready'] != (value['verified_workers'] == len(WORKERS))):
            raise ValueError('worker_readiness_reply_invalid')
        if value['ready']: return
        remaining = deadline - monotonic()
        if remaining <= 0: raise RuntimeError('authenticated_workers_not_ready')
        sleep(min(.25, remaining))


def verify_source_login_refused(session, base, password):
    session.get(base + '/hub/login', timeout=10).raise_for_status()
    rejected = session.post(base + '/hub/login', data={'username': 'alice', 'password': password,
        '_xsrf': xsrf(session, base + '/hub/login')}, timeout=20, allow_redirects=False)
    if rejected.status_code != 403: raise ValueError('source_login_was_not_denied')
    redirected = session.get(base + '/hub/spawn/alice', timeout=20)
    destination, expected = urlsplit(redirected.url), urlsplit(base)
    if (destination.scheme, destination.netloc, destination.path) != (expected.scheme, expected.netloc, '/hub/login'):
        raise ValueError('source_spawn_was_not_denied')


def require_owned_container(value, name, images, authority_id):
    actual = value.get('Name', '').lstrip('/'); labels = value.get('Config', {}).get('Labels', {})
    image = value.get('Image'); matched = False
    if actual == name + '-authority' or actual in {name + '-' + worker + '-service' for worker in WORKERS}:
        matched = image == images['controller'] and labels.get('educloud.fixture') == name
    elif actual in {name + '-' + worker + '-guardian' for worker in WORKERS}:
        worker = next(worker for worker in WORKERS if actual == name + '-' + worker + '-guardian')
        matched = (image == images['controller'] and labels.get('educloud.runtime.authority') == authority_id
            and authority_id is not None and labels.get('educloud.runtime.worker') == worker
            and labels.get('educloud.runtime.kind') == 'guardian')
    else:
        subject = hashlib.sha256(b'alice').hexdigest()[:24]
        for worker, course, suffix in ENDPOINTS:
            instance = name + '-' + suffix
            if actual == instance + '-hub':
                matched = image == images['hub'] and labels.get('educloud.workspace.instance') == instance and labels.get('educloud.workspace.kind') == 'hub'
            elif actual == instance + '-learner-' + subject:
                matched = (image == images['course'] and authority_id is not None
                    and labels.get('educloud.workspace.instance') == instance and labels.get('educloud.workspace.kind') == 'learner'
                    and labels.get('educloud.runtime.authority') == authority_id and labels.get('educloud.runtime.worker') == worker
                    and labels.get('educloud.runtime.course') == course and labels.get('educloud.runtime.subject') == subject
                    and isinstance(labels.get('educloud.runtime.epoch'), str)
                    and re.fullmatch(r'[1-9][0-9]{0,9}', labels['educloud.runtime.epoch'])
                    and int(labels['educloud.runtime.epoch']) <= 10**9)
            if matched: break
    if not matched: raise ValueError('fixture_container_ownership_unverified')
    return value['Id']


def same_running_enforcer(before, after):
    return (before['Id'] == after['Id'] and before['Image'] == after['Image']
        and before['State']['StartedAt'] == after['State']['StartedAt'] and after['State']['Running'] is True)


def wait_for_learner_removal(name, suffix, invoke=command, wait_for=wait):
    """Explicit Hub stop is complete only after stopped containers are removed."""
    wait_for(lambda: not invoke('ps', '-aq', '--filter',
        'label=educloud.workspace.instance=' + name + '-' + suffix,
        '--filter', 'label=educloud.workspace.kind=learner').split(), 30)


def failure_context(error, phases):
    """Bounded diagnostic identifiers only; never messages, URLs or replies."""
    known = (AssertionError, RuntimeError, ValueError, OSError, KeyError, TypeError,
        subprocess.TimeoutExpired, subprocess.CalledProcessError, KeyboardInterrupt,
        requests.exceptions.HTTPError, requests.exceptions.ConnectionError,
        requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout,
        requests.exceptions.TooManyRedirects)
    result = {'phase': PHASES[len(phases)] if len(phases) < len(PHASES) else 'finalization',
        'exception': type(error).__name__ if type(error) in known else 'OtherFailure', 'lines': []}
    trace = error.__traceback__
    while trace is not None:
        if trace.tb_frame.f_code.co_filename == __file__:
            result['lines'].append(trace.tb_lineno)
        trace = trace.tb_next
    result['lines'] = result['lines'][-8:]
    if isinstance(error, requests.exceptions.HTTPError):
        status = getattr(error.response, 'status_code', None)
        if type(status) is int and 100 <= status <= 599: result['http_status'] = status
    return result


def validate_report(value):
    required = {'version', 'status', 'scope', 'phases', 'cleanup_verified', 'acceptance_eligible',
        'flags', 'source', 'code_sha256', 'images', 'host', 'allocation'}
    if (not isinstance(value, dict) or not required <= set(value) <= required | {'failure'}
            or value['version'] != VERSION or value['scope'] != SCOPE or value['status'] not in ('PASS', 'FAIL')
            or value['flags'] != observed_flags(value.get('phases', []))
            or any(type(value['flags'][key]) is not bool for key in FLAGS)
            or type(value['cleanup_verified']) is not bool or type(value['acceptance_eligible']) is not bool
            or not isinstance(value['phases'], list) or value['phases'] != list(PHASES[:len(value['phases'])])
            or not isinstance(value['source'], str) or not re.fullmatch(r'[0-9a-f]{40}', value['source'])
            or not isinstance(value['code_sha256'], dict) or set(value['code_sha256']) != CODE_FILES
            or any(not isinstance(item, str) or not evidence.HASH.fullmatch(item) for item in value['code_sha256'].values())
            or not isinstance(value['images'], dict) or set(value['images']) != {'course', 'hub', 'controller'}
            or any(not isinstance(item, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', item) for item in value['images'].values())
            or not isinstance(value['host'], dict) or set(value['host']) != {'cpu', 'memory_bytes'}
            or value['allocation'] != allocation_for_host(value['host']['cpu'], value['host']['memory_bytes'])):
        raise ValueError('remote_notebook_report_invalid')
    if any(type(value['allocation'][key]) is bool for key in value['allocation']): raise ValueError('remote_notebook_report_invalid')
    if value['status'] == 'PASS':
        if (value['phases'] != list(PHASES) or value['cleanup_verified'] is not True
                or value['acceptance_eligible'] is not True or 'failure' in value): raise ValueError('remote_notebook_report_invalid')
    elif value['acceptance_eligible'] is not False or value.get('failure') not in {
            'RuntimeError', 'AssertionError', 'ValueError', 'OSError', 'KeyError', 'TypeError',
            'TimeoutExpired', 'CalledProcessError', 'KeyboardInterrupt', 'OtherFailure'}:
        raise ValueError('remote_notebook_report_invalid')
    return value


def assert_fresh_namespace(name, request, invoke=command):
    containers = {name + '-authority', *(name + '-' + worker + '-' + kind for worker in WORKERS for kind in ('service', 'guardian')),
        *(name + '-' + suffix + '-hub' for _, _, suffix in ENDPOINTS)}
    volumes = {name + '-control', *(name + '-' + worker + '-backend' for worker in WORKERS),
        *(name + '-' + suffix + ending for _, _, suffix in ENDPOINTS
            for ending in ('-hubdata', '-home-' + hashlib.sha256(b'alice').hexdigest()[:24]))}
    if containers & set(invoke('ps', '-a', '--format', '{{.Names}}').split()): raise ValueError('fixture_namespace_exists')
    for kind, expected in [('network', {request['network'], request['edge_network'], *request['worker_networks'].values()}), ('volume', volumes)]:
        if expected & set(invoke(kind, 'ls', '--format', '{{.Name}}').split()): raise ValueError('fixture_namespace_exists')
    for kind in ('container', 'network', 'volume'):
        if invoke(kind, 'ls', '-aq' if kind == 'container' else '-q', '--filter', 'label=educloud.fixture=' + name):
            raise ValueError('fixture_namespace_exists')
        for _, _, suffix in ENDPOINTS:
            if invoke(kind, 'ls', '-aq' if kind == 'container' else '-q', '--filter', 'label=educloud.workspace.instance=' + name + '-' + suffix):
                raise ValueError('fixture_namespace_exists')


def run(args):
    source = evidence._source(ROOT)
    if source['dirty']: raise ValueError('clean_source_required')
    output = Path(args.output)
    if os.path.lexists(output): raise ValueError('fresh_output_required')
    output.mkdir(mode=0o700, parents=True)
    root = Path(tempfile.mkdtemp(prefix='waypoint-remote-notebooks-')).resolve()
    for path in (root / 'control', root / 'backend', *(root / 'backend' / worker for worker in WORKERS)):
        path.mkdir(mode=0o700, exist_ok=True)
    name = 'wa' + uuid.uuid4().hex[:10]; central = name + '-authority'
    phases = []; resources = False; cleaned = False; failure = None; authority_id = None
    images = {role: command('image', 'inspect', getattr(args, role + '_image'), '--format', '{{.Id}}')
              for role in ('course', 'hub', 'controller')}
    info = json.loads(command('info', '--format', '{{json .}}')); host = {'cpu': info['NCPU'], 'memory_bytes': info['MemTotal']}
    allocation = allocation_for_host(host['cpu'], host['memory_bytes'])
    ports = {key: port() for key in ('authority', 'source', 'peer', 'receiver')}
    request = {'name': name, 'network': name + '-authority-control', 'edge_network': name + '-edge',
        'worker_networks': {worker: name + '-' + worker + '-channel' for worker in WORKERS}, 'ports': ports,
        'password': secrets.token_urlsafe(32), **{key + '_image': image for key, image in images.items()}}
    volumes = [name + '-control', *(name + '-' + worker + '-backend' for worker in WORKERS)]
    bases = {key: 'http://127.0.0.1:' + str(ports[key]) for key in ('source', 'peer', 'receiver')}
    proof = {path: evidence.sha256_file(ROOT / path) for path in CODE_FILES}
    report = {'version': VERSION, 'status': 'FAIL', 'scope': SCOPE, 'phases': phases, 'flags': dict(FLAGS),
        'source': source['revision'], 'code_sha256': proof, 'images': images, 'host': host, 'allocation': allocation,
        'cleanup_verified': False, 'acceptance_eligible': False}
    def phase(value):
        assert value == PHASES[len(phases)]; phases.append(value); print(value + ': PASS', flush=True)
    def inspect(container): return json.loads(command('inspect', container))[0]
    def helper(action, payload=None):
        options = ['run', '--rm', '-i', '--network', 'none', '--memory', '256m', '--cpus', str(2 * allocation['learner_cpu']),
            '--mount', 'type=bind,src=' + str(root) + ',dst=' + str(root),
            '--mount', 'type=volume,src=' + volumes[0] + ',dst=' + str(root / 'control') + ',volume-nocopy']
        for worker in WORKERS:
            options += ['--mount', 'type=volume,src=' + name + '-' + worker + '-backend,dst=' + str(root / 'backend' / worker) + ',volume-nocopy']
        options += ['--mount', 'type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock', images['controller'],
            'python', 'remote_notebook_fixture.py', action, str(root)]
        return json.loads(command(*options, data=json.dumps(payload or {})))
    def central_helper(action, payload=None):
        return json.loads(command('exec', '-i', central, 'python', 'remote_notebook_fixture.py', action, str(root), data=json.dumps(payload or {})))
    def rpc(operation, payload=None, identity='operator', worker='worker-one', course='course-main'):
        directory = root / identity / 'credentials'
        return json.loads(command('run', '--rm', '-i', '--network', request['network'], '--memory', '128m',
            '--cpus', str(allocation['learner_cpu']), '--mount', 'type=bind,src=' + str(directory) + ',dst=' + str(directory) + ',readonly',
            images['controller'], 'python', 'remote_notebook_fixture.py', 'rpc', str(directory),
            data=json.dumps({'operation': operation, 'payload': payload or {}, 'worker': worker, 'course': course})))
    def learners(suffix): return command('ps', '-q', '--filter', 'label=educloud.workspace.instance=' + name + '-' + suffix,
        '--filter', 'label=educloud.workspace.kind=learner').split()
    def ready(suffix):
        worker = 'worker-two' if suffix == 'receiver' else 'worker-one'; course = 'course-peer' if suffix == 'peer' else 'course-main'
        runtime = root / worker / 'credentials/runtime.json'
        value = json.loads(command('exec', name + '-' + worker + '-service', 'python', 'remote_notebook_fixture.py', 'worker-status', str(runtime)))
        return value[course]['ready']
    def start_service():
        command('start', central)
        command('exec', '-d', central, 'python', 'remote_notebook_fixture.py', 'serve-fenced-fault', str(root))
        wait(lambda: rpc('ownership.inventory')['ok'], 45)
    def notebook(suffix):
        wait(lambda: ready(suffix), 30); base = bases[suffix]; session = requests.Session(); session.max_redirects = 8
        session.get(base + '/hub/login', timeout=10).raise_for_status()
        session.post(base + '/hub/login', data={'username': 'alice', 'password': request['password'], '_xsrf': xsrf(session, base + '/hub/login')},
            timeout=30, allow_redirects=False).raise_for_status()
        request_notebook_start(session, base, previous_runtime_stopped=not learners(suffix))
        def established():
            session.get(base + '/user/alice/lab', timeout=10)
            response = session.get(base + '/user/alice/api/contents', timeout=10)
            return response.status_code == 200 and 'application/json' in response.headers.get('Content-Type', '')
        wait(established, 180); return session
    def read(session, suffix):
        value = session.get(bases[suffix] + '/user/alice/api/contents/coursework.txt', timeout=10)
        value.raise_for_status(); assert value.json()['content'] == 'synthetic remote-channel coursework\n'
    def kernels(session, suffix):
        exercise_kernels(session, bases[suffix])
    def stop(session, suffix):
        change(session, 'DELETE', bases[suffix] + '/hub/api/users/alice/server').raise_for_status()
        wait_for_learner_removal(name, suffix)
    def credential(payload):
        review = central_helper('credential-preview', payload)
        return central_helper('credential-apply', {key: review[key] for key in ('review_id', 'review_sha256')})
    try:
        # Execute exact reviewed source bytes, not merely a matching tag.
        mapping = {str(ROOT / path): '/srv/authority/' + path for path in CONTROLLER_FILES}
        mapping.update({str(ROOT / 'hub/authority_client.py'): '/srv/authority/hub/authority_client.py',
            str(ROOT / 'tests/authority_test_pki.py'): '/srv/authority/authority_test_pki.py',
            **{str(ROOT / 'rehearsal' / path): '/srv/authority/' + path for path in
                ('authenticated_fixture.py', 'authenticated_evidence.py', 'remote_notebook_fixture.py')}})
        for image, files in [(images['controller'], mapping), (images['hub'], {str(path): '/srv/jupyterhub/' + path.name for path in (ROOT / 'hub').glob('*.py')})]:
            expected = {target: evidence.sha256_file(source) for source, target in files.items()}
            probe = 'import sys,json,hashlib;from pathlib import Path;print(json.dumps({p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in json.loads(sys.argv[1])}))'
            assert json.loads(command('run', '--rm', '--network', 'none', '--entrypoint', 'python', image, '-c', probe, json.dumps(list(expected)))) == expected
        assert_fresh_namespace(name, request)
        resources = True
        for network in [request['network'], request['edge_network'], *request['worker_networks'].values()]:
            command('network', 'create', *(['--internal'] if network != request['edge_network'] else []), '--label', 'educloud.fixture=' + name, network)
        for volume in volumes: command('volume', 'create', '--label', 'educloud.fixture=' + name, volume)
        observed = helper('bootstrap', request); assert observed['allocation'] == allocation
        authority_id = observed['authority_id']
        helper('launch-workers')
        command('run', '-d', '--name', central, '--network', request['network'], '--network-alias', 'authority.local.test',
            '--label', 'educloud.fixture=' + name, '--mount', 'type=volume,src=' + volumes[0] + ',dst=' + str(root / 'control') + ',volume-nocopy',
            '--memory', '256m', '--cpus', str(allocation['central_cpu']), images['controller'], 'python', '-c', 'import time;time.sleep(86400)')
        for network in request['worker_networks'].values(): command('network', 'connect', network, central)
        central_mounts = inspect(central)['Mounts']; assert len(central_mounts) == 1 and central_mounts[0]['Destination'] == str(root / 'control')
        assert central_mounts[0]['Name'] == volumes[0]; phase(PHASES[0])
        expected_mounts = mount_plan(root, request)
        for worker in WORKERS:
            service = inspect(name + '-' + worker + '-service')
            assert {mount['Destination'] for mount in service['Mounts']} == {value['bind'] for value in expected_mounts[worker].values()}
            assert configured_cpu_limit(service['HostConfig']) == allocation['worker_service_cpu']
            assert service['HostConfig']['Memory'] == 256 * 1024**2
        assert configured_cpu_limit(inspect(central)['HostConfig']) == allocation['central_cpu']
        await_worker_readiness(lambda: central_helper('worker-ready'))
        central_helper('register'); central_helper('enrollment-create')
        for worker in WORKERS: assert credential({'action': 'enroll', 'worker': worker})['status'] == 'COMPLETE'
        start_service(); helper('launch-users')
        for worker in WORKERS:
            guardian = inspect(name + '-' + worker + '-guardian')
            assert configured_cpu_limit(guardian['HostConfig']) == allocation['guardian_cpu']
            assert all('/homes' not in mount['Destination'] and 'control' not in mount['Destination'] for mount in guardian['Mounts'])
        phase(PHASES[1])
        assert rpc('hub.authorize', identity='source')['ok'] is True
        assert all(value == {'quarantined': False} for value in central_helper('worker-quarantine').values())
        require_refusal(rpc('hub.authorize', identity='source', worker='worker-two'), 'authority_access_refused')
        require_refusal(rpc('hub.authorize', identity='receiver', worker='worker-two'), 'authority_operation_refused')
        require_refusal(rpc('ownership.inventory', identity='source'), 'authority_access_refused')
        assert rpc('hub.authorize', identity='source')['ok'] is True
        assert all(value == {'quarantined': False} for value in central_helper('worker-quarantine').values()); phase(PHASES[2])
        sessions = {suffix: notebook(suffix) for suffix in ('source', 'peer')}
        for suffix, session in sessions.items():
            kernels(session, suffix)
            change(session, 'PUT', bases[suffix] + '/user/alice/api/contents/coursework.txt',
                json={'type': 'file', 'format': 'text', 'content': 'synthetic remote-channel coursework\n'}).raise_for_status()
            read(session, suffix)
            hub = inspect(name + '-' + suffix + '-hub'); learner = inspect(learners(suffix)[0])
            assert not any(mount['Destination'] == '/var/run/docker.sock' or '/homes' in mount['Destination'] for mount in hub['Mounts'])
            limits = learner['HostConfig']; assert limits['ReadonlyRootfs'] and limits['PidsLimit'] == 128 and not limits['Privileged']
            assert limits['Memory'] == limits['MemorySwap'] == 512 * 1024**2 and configured_cpu_limit(limits) == allocation['learner_cpu']
            assert len(learner['Mounts']) == 1 and learner['Mounts'][0]['Destination'] == '/home/learner'
            assert len(learner['NetworkSettings']['Networks']) == 1 and not any('AUTHORITY' in item for item in learner['Config']['Env'])
        phase(PHASES[3])
        command('stop', central); wait(lambda: not learners('source') and not learners('peer'), 25)
        start_service(); sessions = {suffix: notebook(suffix) for suffix in ('source', 'peer')}
        for suffix, session in sessions.items(): read(session, suffix)
        phase(PHASES[4])
        stop(sessions['source'], 'source'); assert helper('copy-home') == {'copied': True, 'local_recovery_staging': True, 'network_file_transfer': False}
        peer_ids = learners('peer'); read(sessions['peer'], 'peer'); phase(PHASES[5])
        plan = rpc('ownership.preview', {'receiver_worker_id': 'worker-two'}); assert plan['ok'] is True
        reference = {key: plan['result'][key] for key in ('plan_id', 'plan_sha256')}
        require_refusal(rpc('ownership.apply', reference), 'authority_operation_refused')
        assert rpc('ownership.inventory')['result']['pending'] is True
        assert all(value == {'quarantined': True} for value in central_helper('worker-quarantine').values())
        command('restart', name + '-worker-two-service')
        wait(lambda: all(value == {'quarantined': True} for value in central_helper('worker-quarantine').values()), 30)
        for worker in WORKERS:
            require_refusal(rpc('hub.authorize', identity='source' if worker == 'worker-one' else 'receiver', worker=worker), 'authority_operation_refused')
        assert learners('peer') == peer_ids; read(sessions['peer'], 'peer'); phase(PHASES[6])
        helper('drift-snapshot'); require_refusal(rpc('ownership.apply', reference), 'authority_operation_refused')
        recovery = rpc('ownership.reconcile_preview', {'selected_worker_id': 'worker-two'}); assert recovery['ok'] is True
        ref = {key: recovery['result'][key] for key in ('plan_id', 'plan_sha256')}
        completed = rpc('ownership.reconcile_apply', ref); assert completed['ok'] is True
        assert all(value == {'quarantined': False} for value in central_helper('worker-quarantine').values())
        assert learners('peer') == peer_ids; kernels(sessions['peer'], 'peer'); read(sessions['peer'], 'peer'); phase(PHASES[7])
        assert not learners('receiver'); receiver = notebook('receiver'); kernels(receiver, 'receiver'); read(receiver, 'receiver'); phase(PHASES[8])
        assert rpc('hub.authorize', identity='receiver', worker='worker-two')['ok'] is True
        command('restart', name + '-source-hub'); require_refusal(rpc('hub.authorize', identity='source'), 'authority_operation_refused')
        wait(lambda: requests.get(bases['source'] + '/hub/login', timeout=3).status_code == 200, 60)
        verify_source_login_refused(requests.Session(), bases['source'], request['password'])
        assert rpc('hub.authorize', identity='receiver', worker='worker-two')['ok'] is True
        assert not learners('source') and learners('peer') == peer_ids; phase(PHASES[9])
        worker_two = name + '-worker-two-service'; guardian_two = name + '-worker-two-guardian'
        guardian_before = inspect(guardian_two)
        command('kill', '--signal', 'KILL', require_owned_container(inspect(worker_two), name, images, authority_id))
        wait(lambda: not learners('receiver'), 25)
        assert same_running_enforcer(guardian_before, inspect(guardian_two))
        assert learners('peer') == peer_ids; read(sessions['peer'], 'peer'); phase(PHASES[10])
        command('start', worker_two); receiver = notebook('receiver'); read(receiver, 'receiver'); phase(PHASES[11])
        worker_before = inspect(worker_two)
        command('kill', '--signal', 'KILL', require_owned_container(inspect(guardian_two), name, images, authority_id))
        wait(lambda: not learners('receiver'), 25)
        assert same_running_enforcer(worker_before, inspect(worker_two))
        assert learners('peer') == peer_ids; read(sessions['peer'], 'peer'); phase(PHASES[12])
        command('start', guardian_two); receiver = notebook('receiver'); read(receiver, 'receiver')
        stage = {'worker': 'worker-two', 'slot': 'hub:course-main', 'identity': 'receiver-rotated'}
        assert rpc('hub.authorize', identity='receiver', worker='worker-two')['ok'] is True
        require_refusal(rpc('hub.authorize', identity='receiver-rotated', worker='worker-two'), 'authority_access_refused')
        credential(dict(stage, action='stage')); require_refusal(rpc('hub.authorize', identity='receiver-rotated', worker='worker-two'), 'authority_access_refused')
        assert rpc('hub.authorize', identity='receiver', worker='worker-two')['ok'] is True
        credential(dict(stage, action='activate', overlap_seconds=300))
        for identity in ('receiver', 'receiver-rotated'): assert rpc('hub.authorize', identity=identity, worker='worker-two')['ok'] is True
        credential({'action': 'retire', 'worker': 'worker-two', 'target_identity': 'receiver'})
        require_refusal(rpc('hub.authorize', identity='receiver', worker='worker-two'), 'authority_access_refused')
        assert rpc('hub.authorize', identity='receiver-rotated', worker='worker-two')['ok'] is True; phase(PHASES[13])
        # Old certificate is retired: trusted fixture stops its exact learner
        # through the newly authorized identity, then installs while Hub is off.
        learner = inspect(learners('receiver')[0])
        assert rpc('hub.stop', {'subject': 'alice', 'server_id': learner['Id'], 'epoch': completed['result']['epoch']},
            identity='receiver-rotated', worker='worker-two')['ok'] is True
        wait_for_learner_removal(name, 'receiver'); command('stop', name + '-receiver-hub')
        helper('install-rotated-hub'); command('start', name + '-receiver-hub')
        receiver = notebook('receiver'); kernels(receiver, 'receiver'); read(receiver, 'receiver'); assert learners('peer') == peer_ids; phase(PHASES[14])
        credential({'action': 'revoke', 'worker': 'worker-two', 'target_identity': 'guardian-two'})
        wait(lambda: not learners('receiver'), 25); assert learners('peer') == peer_ids; kernels(sessions['peer'], 'peer'); read(sessions['peer'], 'peer'); phase(PHASES[15])
    except BaseException as exc:
        print('FAILURE_CONTEXT ' + json.dumps(failure_context(exc, phases), sort_keys=True), flush=True)
        failure = type(exc).__name__ if type(exc).__name__ in {'RuntimeError', 'AssertionError', 'ValueError', 'OSError', 'KeyError', 'TypeError',
            'TimeoutExpired', 'CalledProcessError', 'KeyboardInterrupt'} else 'OtherFailure'
    finally:
        try:
            if resources:
                # Invocation-owned participant containers only; the privileged
                # cleanup helper runs AFTER every runtime participant is gone.
                for suffix in ('source', 'peer', 'receiver'):
                    for container in command('ps', '-aq', '--filter', 'label=educloud.workspace.instance=' + name + '-' + suffix).split():
                        command('rm', '-f', require_owned_container(inspect(container), name, images, authority_id))
                for container in [central, *(name + '-' + worker + '-service' for worker in WORKERS),
                        *(name + '-' + worker + '-guardian' for worker in WORKERS)]:
                    if container in command('ps', '-a', '--format', '{{.Names}}').split():
                        command('rm', '-f', require_owned_container(inspect(container), name, images, authority_id))
                if (root / 'fixture-owner.json').exists(): helper('cleanup-permissions')
                for kind, flag in [('network', 'label=educloud.fixture=' + name), ('volume', 'label=educloud.fixture=' + name)]:
                    for item in command(kind, 'ls', '-q', '--filter', flag).split(): command(kind, 'rm', item)
                for suffix in ('source', 'peer', 'receiver'):
                    for kind in ('network', 'volume'):
                        for item in command(kind, 'ls', '-q', '--filter', 'label=educloud.workspace.instance=' + name + '-' + suffix).split(): command(kind, 'rm', item)
                assert not command('ps', '-aq', '--filter', 'label=educloud.fixture=' + name)
                for kind in ('network', 'volume'): assert not command(kind, 'ls', '-q', '--filter', 'label=educloud.fixture=' + name)
                for _, _, suffix in ENDPOINTS:
                    for kind in ('container', 'network', 'volume'):
                        assert not command(kind, 'ls', '-aq' if kind == 'container' else '-q', '--filter', 'label=educloud.workspace.instance=' + name + '-' + suffix)
            shutil.rmtree(root); cleaned = True
        except Exception: cleaned = False
    report['cleanup_verified'] = cleaned
    report['flags'] = observed_flags(phases)
    if failure is None and cleaned:
        try:
            assert evidence._source(ROOT) == source and {path: evidence.sha256_file(ROOT / path) for path in CODE_FILES} == proof
            phase(PHASES[16]); report.update(status='PASS', acceptance_eligible=True)
        except Exception: failure = 'ValueError'
    if report['status'] != 'PASS': report['failure'] = failure or 'RuntimeError'
    validate_report(report)
    from authority_access import publish
    publish(output.resolve() / 'REMOTE-NOTEBOOKS.json', report)
    if report['status'] != 'PASS': raise RuntimeError('remote_notebook_rehearsal_failed')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('course', 'hub', 'controller'): parser.add_argument('--' + key + '-image', default='waypoint-authority-' + key + ':local')
    parser.add_argument('--output', required=True)
    try: run(parser.parse_args())
    except BaseException: raise SystemExit('remote_notebook_rehearsal_failed') from None
