"""Disposable one-daemon remote notebook fixture; never a host provisioner.

Only its short-lived trusted setup/recovery helper has both home namespaces.
The running central service receives neither a Docker socket nor home mounts.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
from types import SimpleNamespace
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))

from authenticated_fixture import (capture_cleanup_owner, cleanup_permissions,
    course_allocation, write)
from authority_access import ReplayStore, VERSION
from authority_test_pki import TestPKI
from ownership_contract import Authority, digest
from runtime_authority import DockerBackend, read_record, open_directory, bounded_names, metadata

WORKERS = ('worker-one', 'worker-two')
ENDPOINTS = (('worker-one', 'course-main', 'source'), ('worker-one', 'course-peer', 'peer'),
             ('worker-two', 'course-main', 'receiver'))


def allocation_for_host(cpu, memory):
    """Budget all three course endpoints and this topology's five controls."""
    if type(cpu) is not int or not 4 <= cpu <= 4096:
        raise ValueError('remote_fixture_cpu_refused')
    if type(memory) is not int or memory < 6656 * 1024**2:
        raise ValueError('remote_fixture_memory_refused')
    if cpu >= 8:
        name, minimum, hub, learner, central, worker = 'eight-cpu-remote', 8, 1.0, .25, 1.0, .75
    elif cpu >= 6:
        name, minimum, hub, learner, central, worker = 'six-cpu-remote', 6, 1.0, .25, .5, .5
    elif cpu == 5:
        name, minimum, hub, learner, central, worker = 'five-cpu-remote', 5, .75, .2, .5, .5
    else:
        name, minimum, hub, learner, central, worker = 'four-cpu-remote', 4, .5, .1, .5, .5
    guardian = .25
    controls = round(central + 2 * worker + 2 * guardian, 6)
    total = round(3 * (hub + learner) + controls, 6)
    if total > cpu or controls < 2 or central < .5 or worker < .5 or guardian < .25:
        raise ValueError('remote_fixture_cpu_refused')
    return dict(name=name, minimum_cpu=minimum, hub_cpu=hub, learner_cpu=learner,
        host_reserve_cpu=controls, reserved_cpu=total, central_cpu=central,
        worker_service_cpu=worker, guardian_cpu=guardian)


def validate_request(request):
    required = {'name', 'network', 'edge_network', 'worker_networks', 'ports', 'password',
        'course_image', 'hub_image', 'controller_image'}
    if (not isinstance(request, dict) or set(request) != required
            or not isinstance(request['name'], str) or not re.fullmatch(r'wa[0-9a-f]{10}', request['name'])
            or request['network'] != request['name'] + '-authority-control'
            or request['edge_network'] != request['name'] + '-edge'
            or request['worker_networks'] != {worker: request['name'] + '-' + worker + '-channel' for worker in WORKERS}
            or not isinstance(request['ports'], dict) or set(request['ports']) != {'authority', 'source', 'peer', 'receiver'}
            or any(type(value) is not int or not 1024 <= value <= 65535 for value in request['ports'].values())
            or len(set(request['ports'].values())) != 4
            or not isinstance(request['password'], str) or not 20 <= len(request['password']) <= 128
            or any(not isinstance(request[key], str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', request[key])
                   for key in ('course_image', 'hub_image', 'controller_image'))):
        raise ValueError('remote_fixture_request_refused')
    return request


def mount_plan(root, request):
    """Trusted exact mounts, separately testable without contacting Docker."""
    root = Path(root); validate_request(request); name = request['name']
    return {'central': {name + '-control': {'bind': str(root / 'control'), 'mode': 'rw'}},
        **{worker: {name + '-' + worker + '-backend': {'bind': str(root / 'backend' / worker), 'mode': 'rw'},
            str(root / worker / 'credentials'): {'bind': str(root / worker / 'credentials'), 'mode': 'ro'},
            **{str(root / suffix / 'homes'): {'bind': str(root / suffix / 'homes'), 'mode': 'rw'}
                for w, _, suffix in ENDPOINTS if w == worker},
            '/var/run/docker.sock': {'bind': '/var/run/docker.sock', 'mode': 'rw'}} for worker in WORKERS}}


def initialize(root, request):
    import docker
    from worker_transport import worker_binding, linux_boot_id
    from worker_channel_state import WorkerJournal
    request = validate_request(request); root = Path(root)
    capture_cleanup_owner(root, request['name'])
    (root / 'control').chmod(0o700)
    client = docker.DockerClient(base_url='unix:///var/run/docker.sock', version='auto', timeout=5)
    info = client.info(); allocation = allocation_for_host(info['NCPU'], info['MemTotal'])
    class Empty:
        fixture_only = True
        scope = 'course'
    authority = Authority.create(root / 'control' / 'authority', Empty())
    authority_id = authority.authority_id; name = request['name']
    pki = TestPKI(root / 'pki'); workers = {}; bindings = {}; channels = {}; identities = {}
    profile = dict(active_limit=1, learner_memory_mb=512, learner_cpu=allocation['learner_cpu'],
        memory_mb=1536, cpu=1 + allocation['learner_cpu'])
    # The core keeps its conservative fixed1CPU Hub reservation. Actual lower
    # Hub caps are separately included in the complete daemon budget above.
    # The global check above counts ALL three actual Hub/learner cap pairs,
    # even though the two independently loaded worker configs share one daemon.
    for worker in WORKERS:
        endpoints = [(course, suffix) for w, course, suffix in ENDPOINTS if w == worker]
        worker_root = root / 'backend' / worker
        worker_root.chmod(0o700)
        worker_config = dict(worker_id=worker, provider_id=name + '-' + worker, boot_id=linux_boot_id(),
            policy_sha256=digest({'fixture': name, 'worker': worker}),
            memory_budget_mb=1536 * len(endpoints), capacity_memory_mb=1536 * len(endpoints),
            cpu_budget=profile['cpu'] * len(endpoints), capacity_cpu=profile['cpu'] * len(endpoints))
        courses = {}
        for course, suffix in endpoints:
            instance = name + '-' + suffix; homes = root / suffix / 'homes'; homes.mkdir(mode=0o700, parents=True)
            leaf = homes / (instance + '-home-' + hashlib.sha256(b'alice').hexdigest()[:24])
            leaf.mkdir(mode=0o755); os.chown(leaf, 1000, 1000)
            (leaf / 'work').mkdir(mode=0o755); os.chown(leaf / 'work', 1000, 1000)
            metadata = worker_root / course; metadata.mkdir(mode=0o700)
            for key in ('snapshot', 'runtime'): write(metadata / (key + '.json'), {'synthetic': key, 'course': course})
            hostname = course + '.remote-notebook.invalid'
            courses[course] = dict(instance=instance, logical_identity=dict(origin='https://' + hostname,
                dns_subtree=hostname, issuer='https://identity.remote-notebook.invalid/realms/fixture', client_id=course),
                profile=dict(profile), image_id=request['course_image'], hub_container=instance + '-hub', subjects=['alice'],
                home_root=str(homes), snapshot_path=str(metadata / 'snapshot.json'), runtime_path=str(metadata / 'runtime.json'))
            client.volumes.create(name=leaf.name, driver_opts={'type': 'none', 'o': 'bind', 'device': str(leaf)},
                labels={'educloud.workspace.instance': instance, 'educloud.workspace.kind': 'home'})
        # Lower profiles split the global controls between worker views; each
        # owns its service/Guardian and half the central reserve. Higher hosts
        # retain the existing conservative per-view reserve unchanged.
        local_controls = allocation['host_reserve_cpu']
        if info['NCPU'] < 8:
            local_controls = allocation['worker_service_cpu'] + allocation['guardian_cpu'] + allocation['central_cpu'] / len(WORKERS)
        config = dict(version='educloud-local-runtime-v1', authority_id=authority_id, daemon_id=info['ID'],
            backend_directory=str(worker_root / 'state'), host_reserve_mb=2048, host_reserve_cpu=local_controls,
            workers={worker: worker_config}, courses={worker: courses},
            guardians={worker: dict(container_name=name + '-' + worker + '-guardian',
                image_id=request['controller_image'], heartbeat_seconds=10)})
        backend = DockerBackend(config, client, initialize=True)
        binding = worker_binding(backend, worker); bindings[worker] = binding
        WorkerJournal.create(worker_root / 'journal', authority_id, worker, sorted(courses), binding)
        credentials = root / worker / 'credentials'; credentials.mkdir(mode=0o700, parents=True)
        server_cert, server_key = pki.issue(worker + '-server', 'server', hostname=worker + '.local.test')
        channel_cert, channel_key = pki.issue(worker + '-controller')
        channel_dir = root / 'control' / (worker + '-channel'); channel_dir.mkdir(mode=0o700)
        for directory, values in [(credentials, [(server_cert, 'server.crt'), (server_key, 'server.key'), (pki.ca_cert, 'ca.crt')]),
                                  (channel_dir, [(channel_cert, 'client.crt'), (channel_key, 'client.key'), (pki.ca_cert, 'ca.crt')])]:
            for source, target in values:
                shutil.copyfile(source, directory / target); (directory / target).chmod(0o600)
        write(credentials / 'runtime.json', config)
        write(credentials / 'acl.json', dict(version='educloud-worker-channel-v1', authority_id=authority_id, worker_id=worker,
            controllers={pki.fingerprint(channel_cert): sorted(courses)}))
        channel = dict(host=worker + '.local.test', port=8444, server_sha256=pki.fingerprint(server_cert),
            ca_file=str(channel_dir / 'ca.crt'), certfile=str(channel_dir / 'client.crt'), keyfile=str(channel_dir / 'client.key'))
        channels[worker] = {'evidence': worker_config, 'courses': sorted(courses), 'channel': channel}; workers[worker] = config
    server_cert, server_key = pki.issue('central-server', 'server', hostname='authority.local.test')
    service_credentials = root / 'control' / 'credentials'; service_credentials.mkdir(mode=0o700)
    for source, target in [(server_cert, 'server.crt'), (server_key, 'server.key'), (pki.ca_cert, 'ca.crt')]:
        shutil.copyfile(source, service_credentials / target); (service_credentials / target).chmod(0o600)
    acl = {}; public = root / 'control' / 'public'; public.mkdir(mode=0o700)
    roles = [('operator', 'operator', list(WORKERS), ['course-main', 'course-peer']),
        ('source', 'hub', ['worker-one'], ['course-main']), ('peer', 'hub', ['worker-one'], ['course-peer']),
        ('receiver', 'hub', ['worker-two'], ['course-main']), ('receiver-rotated', 'hub', ['worker-two'], ['course-main']),
        ('guardian-one', 'guardian', ['worker-one'], ['course-main', 'course-peer']),
        ('guardian-two', 'guardian', ['worker-two'], ['course-main'])]
    for identity, role, scopes, courses in roles:
        cert, key = pki.issue(identity); credentials = root / identity / 'credentials'; credentials.mkdir(mode=0o700, parents=True, exist_ok=True)
        for source, target in [(cert, 'client.crt'), (key, 'client.key'), (pki.ca_cert, 'ca.crt')]:
            shutil.copyfile(source, credentials / target); (credentials / target).chmod(0o600)
        shutil.copyfile(cert, public / (identity + '.pem')); (public / (identity + '.pem')).chmod(0o600)
        fingerprint = pki.fingerprint(cert); identities[identity] = fingerprint
        # Runtime identities are managed-only. The operator remains static.
        if role == 'operator': acl[fingerprint] = {'role': role, 'workers': scopes, 'courses': courses}
        write(credentials / 'client.json', dict(url='https://authority.local.test:8443/v1/authority', authority_id=authority_id,
            ca_file=str(credentials / 'ca.crt'), certfile=str(credentials / 'client.crt'), keyfile=str(credentials / 'client.key')))
        if role == 'guardian': write(credentials / 'runtime.json', workers[scopes[0]])
    write(root / 'control' / 'acl.json', {'version': VERSION, 'authority_id': authority_id, 'clients': acl})
    ReplayStore.create(root / 'control' / 'replay', authority_id); (root / 'control' / 'plans').mkdir(mode=0o700)
    control = dict(version='educloud-independent-workers-v1', authority_id=authority_id,
        authority_directory=str(root / 'control' / 'authority'), plan_directory=str(root / 'control' / 'plans'), workers=channels)
    service = dict(bind='0.0.0.0', port=8443, certfile=str(service_credentials / 'server.crt'),
        keyfile=str(service_credentials / 'server.key'), ca_file=str(service_credentials / 'ca.crt'),
        acl_file=str(root / 'control' / 'acl.json'), replay_directory=str(root / 'control' / 'replay'),
        enrollment_directory=str(root / 'control' / 'enrollment'))
    write(root / 'control' / 'remote.json', dict(version='educloud-remote-runtime-v1', control=control, runtime_bindings=bindings, service=service))
    write(root / 'remote-fixture.json', dict(request=request, allocation=allocation, identities=identities,
        workers=workers, global_memory_reserved_mb=6656, global_cpu_reserved=allocation['reserved_cpu']))
    return {'authority_id': authority_id, 'daemon_id': info['ID'], 'cpu': info['NCPU'],
        'memory_bytes': info['MemTotal'], 'allocation': allocation}


def register(root):
    from remote_control import load_backend
    root = Path(root); config = read_record(root / 'control' / 'remote.json'); control = config['control']
    backend = load_backend(control); authority = Authority.open(control['authority_directory'], control['authority_id'], backend)
    for worker in WORKERS: authority.register_worker(worker)
    for course in ('course-main', 'course-peer'): authority.register_course(course, 'worker-one')
    return {'registered': True}


def worker_ready(root):
    """Authenticate exact worker evidence/bindings without ownership writes."""
    from remote_control import load_backend
    from worker_transport import WorkerError
    config = read_record(Path(root) / 'control' / 'remote.json')
    backend = load_backend(config['control']); verified = 0
    for worker in WORKERS:
        try:
            observed = backend.inspect_worker(worker)
            binding = backend.binding(worker, backend.courses[worker][0])
        except WorkerError as exc:
            if exc.code != 'worker_transport_unavailable': raise
            continue
        if observed != backend.workers[worker] or binding != config['runtime_bindings'][worker]:
            raise ValueError('remote_fixture_worker_binding_changed')
        verified += 1
    return {'ready': verified == len(WORKERS), 'verified_workers': verified}


def launch_workers(root):
    import docker
    root = Path(root); fixture = read_record(root / 'remote-fixture.json'); request = fixture['request']; name = request['name']
    client = docker.DockerClient(base_url='unix:///var/run/docker.sock', version='auto', timeout=5)
    mounts = mount_plan(root, request)
    for worker in WORKERS:
        credentials = root / worker / 'credentials'
        client.containers.run(request['controller_image'], name=name + '-' + worker + '-service', detach=True,
            command=['python', 'worker_runtime.py', '--config', str(credentials / 'runtime.json'), '--journal',
                str(root / 'backend' / worker / 'journal'), '--certfile', str(credentials / 'server.crt'),
                '--keyfile', str(credentials / 'server.key'), '--ca-file', str(credentials / 'ca.crt'),
                '--acl-file', str(credentials / 'acl.json'), '--host', '0.0.0.0', '--port', '8444'],
            network=request['worker_networks'][worker], networking_config={
                request['worker_networks'][worker]: client.api.create_endpoint_config(aliases=[worker + '.local.test'])}, volumes=mounts[worker],
            labels={'educloud.fixture': name, 'educloud.remote.worker': worker, 'educloud.remote.kind': 'service'},
            mem_limit=256 * 1024**2, cpu_period=100000, cpu_quota=int(fixture['allocation']['worker_service_cpu'] * 100000),
            restart_policy={'Name': 'no'})
    return {'launched': True}


def launch_users(root):
    import docker
    root = Path(root); fixture = read_record(root / 'remote-fixture.json'); request = fixture['request']
    client = docker.DockerClient(base_url='unix:///var/run/docker.sock', version='auto', timeout=5)
    for worker, course, suffix in ENDPOINTS:
        config = fixture['workers'][worker]; item = config['courses'][worker][course]; credentials = root / suffix / 'credentials'
        env = dict(WORKSPACE_INSTANCE=item['instance'], WORKSPACE_IMAGE=item['image_id'], WORKSPACE_AUTH_MODE='local-test',
            WORKSPACE_PUBLIC_URL='http://127.0.0.1:' + str(request['ports'][suffix]), WORKSPACE_TEST_PASSWORD=request['password'],
            WORKSPACE_MEMORY_MB='512', WORKSPACE_CPU_LIMIT=str(item['profile']['learner_cpu']), WORKSPACE_ACTIVE_LIMIT='1',
            WORKSPACE_IDLE_SECONDS='3600', WORKSPACE_MAX_AGE_SECONDS='7200', WORKSPACE_RUNTIME_MODE='authority-local',
            WORKSPACE_AUTHORITY_URL='https://authority.local.test:8443/v1/authority', WORKSPACE_AUTHORITY_ID=config['authority_id'],
            WORKSPACE_AUTHORITY_WORKER_ID=worker, WORKSPACE_AUTHORITY_COURSE_ID=course,
            WORKSPACE_AUTHORITY_CA=str(credentials / 'ca.crt'), WORKSPACE_AUTHORITY_CERT=str(credentials / 'client.crt'),
            WORKSPACE_AUTHORITY_KEY=str(credentials / 'client.key'))
        hubdata = item['instance'] + '-hubdata'; client.volumes.create(name=hubdata, labels={'educloud.fixture': request['name']})
        hub = client.containers.create(request['hub_image'], name=item['hub_container'], environment=env,
            network=request['edge_network'], ports={'8000/tcp': ('127.0.0.1', request['ports'][suffix])},
            volumes={str(credentials): {'bind': str(credentials), 'mode': 'ro'}, hubdata: {'bind': '/data', 'mode': 'rw'}},
            labels={'educloud.workspace.instance': item['instance'], 'educloud.workspace.kind': 'hub'},
            mem_limit=1024 * 1024**2, cpu_period=100000, cpu_quota=int(fixture['allocation']['hub_cpu'] * 100000),
            restart_policy={'Name': 'no'})
        client.networks.get(request['network']).connect(hub); hub.start()
    for worker, identity in [('worker-one', 'guardian-one'), ('worker-two', 'guardian-two')]:
        config = fixture['workers'][worker]; credentials = root / identity / 'credentials'
        client.containers.run(request['controller_image'], name=config['guardians'][worker]['container_name'], detach=True,
            command=['python', 'runtime_guardian.py', '--config', str(credentials / 'runtime.json'), '--worker', worker,
                '--client-config', str(credentials / 'client.json')], network=request['network'],
            volumes={str(credentials): {'bind': str(credentials), 'mode': 'ro'},
                request['name'] + '-' + worker + '-backend': {'bind': str(root / 'backend' / worker), 'mode': 'rw'},
                '/var/run/docker.sock': {'bind': '/var/run/docker.sock', 'mode': 'rw'}},
            labels={'educloud.runtime.authority': config['authority_id'], 'educloud.runtime.worker': worker, 'educloud.runtime.kind': 'guardian'},
            mem_limit=128 * 1024**2, cpu_period=100000, cpu_quota=int(fixture['allocation']['guardian_cpu'] * 100000),
            restart_policy={'Name': 'no'})
    return {'launched': True}


_COPY_OWNERS = {(0, 0), (1000, 1000)}


def _home_owners(root):
    """Bounded, stable, no-follow metadata for this stopped synthetic home."""
    result = {}; total = 0; fd = open_directory(root); initial = os.fstat(fd)
    def visit(directory, parts):
        before = os.fstat(directory)
        if len(parts) > 64 or before.st_dev != initial.st_dev:
            raise ValueError('remote_copy_tree_bounds')
        def record(parts, info):
            nonlocal total
            if len(result) >= 10000 or (info.st_uid, info.st_gid) not in _COPY_OWNERS:
                raise ValueError('remote_copy_owner_or_bounds_invalid')
            if info.st_mode & 0o6000 or (info.st_mode & stat.S_ISVTX and not stat.S_ISDIR(info.st_mode)):
                raise ValueError('remote_copy_special_permissions')
            if stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1: raise ValueError('remote_copy_hardlink_refused')
                total += info.st_size
                if total > 64 * 1024**2: raise ValueError('remote_copy_tree_bounds')
            result[parts] = (stat.S_IFMT(info.st_mode), info.st_uid, info.st_gid)
        record(parts, before)
        for name in bounded_names(directory, 10000 - len(result)):
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if info.st_dev != initial.st_dev:
                raise ValueError('remote_copy_tree_bounds')
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    if metadata(os.fstat(child)) != metadata(info): raise ValueError('remote_copy_tree_changed')
                    visit(child, parts + (name,))
                finally: os.close(child)
            elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                record(parts + (name,), info)
            else: raise ValueError('remote_copy_special_entry')
            if metadata(os.stat(name, dir_fd=directory, follow_symlinks=False)) != metadata(info):
                raise ValueError('remote_copy_tree_changed')
        if metadata(os.fstat(directory)) != metadata(before): raise ValueError('remote_copy_tree_changed')
    try:
        visit(fd, ())
        current = open_directory(root)
        try:
            if metadata(os.fstat(current)) != metadata(initial): raise ValueError('remote_copy_tree_changed')
        finally: os.close(current)
        return result
    finally: os.close(fd)


def _restore_home_owners(root, owners):
    """Preserve mixed fixture ownership without changing production unpack."""
    observed = _home_owners(root)
    if {parts: value[0] for parts, value in observed.items()} != {parts: value[0] for parts, value in owners.items()}:
        raise ValueError('remote_copy_membership_changed')
    root_fd = open_directory(root); root_info = os.fstat(root_fd)
    try:
        for parts, (kind, uid, gid) in sorted(owners.items(), key=lambda item: len(item[0]), reverse=True):
            if (uid, gid) not in _COPY_OWNERS: raise ValueError('remote_copy_owner_or_bounds_invalid')
            parent = os.dup(root_fd)
            try:
                for part in parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    os.close(parent); parent = child
                    if os.fstat(parent).st_dev != root_info.st_dev: raise ValueError('remote_copy_tree_bounds')
                info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False) if parts else os.fstat(parent)
                if info.st_dev != root_info.st_dev or stat.S_IFMT(info.st_mode) != kind:
                    raise ValueError('remote_copy_tree_changed')
                if stat.S_ISLNK(info.st_mode):
                    os.chown(parts[-1], uid, gid, dir_fd=parent, follow_symlinks=False)
                    after = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                else:
                    item = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent) if parts else os.dup(parent)
                    try:
                        opened = os.fstat(item)
                        if metadata(opened) != metadata(info) or (stat.S_ISREG(opened.st_mode) and opened.st_nlink != 1):
                            raise ValueError('remote_copy_tree_changed')
                        os.fchown(item, uid, gid); after = os.fstat(item)
                    finally: os.close(item)
                if (after.st_dev, after.st_ino, after.st_uid, after.st_gid) != (info.st_dev, info.st_ino, uid, gid):
                    raise ValueError('remote_copy_tree_changed')
            finally: os.close(parent)
        if _home_owners(root) != owners: raise ValueError('remote_copy_owners_unverified')
    finally: os.close(root_fd)


def copy_home(root):
    """One-shot LOCAL recovery staging, not a worker-channel file transfer."""
    from volume_archive import pack, unpack
    root = Path(root); fixture = read_record(root / 'remote-fixture.json')
    backends = {worker: DockerBackend(config) for worker, config in fixture['workers'].items()}
    items = [fixture['workers'][worker]['courses'][worker]['course-main'] for worker in WORKERS]
    before = {}
    for worker in WORKERS:
        backend = backends[worker]
        if backend.containers(worker, 'course-main'): raise ValueError('remote_copy_participants_not_stopped')
        before[worker] = backend.inspect_course('course-main', worker)
    leafs = []
    for item in items:
        leafs.append(Path(item['home_root']) / (item['instance'] + '-home-' + hashlib.sha256(b'alice').hexdigest()[:24]))
    source, target = leafs
    # First staging only. Never replace existing receiver coursework or hidden
    # state merely to make transfer/rollback equality pass.
    if (target.is_symlink() or sorted(path.name for path in target.iterdir()) != ['work']
            or (target / 'work').is_symlink() or not (target / 'work').is_dir() or any((target / 'work').iterdir())):
        raise ValueError('remote_copy_receiver_not_fresh')
    owners = _home_owners(source)
    with tempfile.TemporaryDirectory(prefix='remote-notebook-local-snapshot-') as temporary:
        archive = Path(temporary) / 'snapshot.tar.gz'; pack(source, archive, 0, 0)
        os.rmdir(target / 'work'); unpack(target, archive, 1000, 1000)
        _restore_home_owners(target, owners)
    after = {worker: backends[worker].inspect_course('course-main', worker) for worker in WORKERS}
    if after['worker-one'] != before['worker-one']:
        raise ValueError('remote_copy_source_changed')
    if after['worker-one']['retained_tree_sha256'] != after['worker-two']['retained_tree_sha256']:
        raise ValueError('remote_copy_equality_unverified')
    return {'copied': True, 'local_recovery_staging': True, 'network_file_transfer': False}


def rpc(credentials, payload):
    from hub.authority_client import AuthorityClient, AuthorityError
    if (not isinstance(payload, dict) or set(payload) != {'operation', 'payload', 'worker', 'course'}
            or payload['worker'] not in WORKERS or payload['course'] not in ('course-main', 'course-peer')
            or not isinstance(payload['payload'], dict)):
        raise ValueError('remote_fixture_rpc_invalid')
    settings = read_record(Path(credentials) / 'client.json')
    # Match the real Hub client's bounded wait for a legal Docker stop(5).
    # This is one request, never a replay/retry of a state-changing operation.
    if payload['operation'] == 'hub.stop': settings = dict(settings, timeout=10)
    api = AuthorityClient(worker_id=payload['worker'], course_id=payload['course'], **settings)
    try: return {'ok': True, 'result': api.call(payload['operation'], payload['payload'])}
    except AuthorityError as exc: return {'ok': False, 'refused': True, 'code': exc.code}


def worker_status(runtime_path):
    backend = DockerBackend(read_record(runtime_path), inspect_roots=False)
    worker = next(iter(backend.config['workers'])); result = {}
    for course in backend.config['courses'][worker]:
        try:
            with backend.connection() as conn:
                conn.execute('BEGIN'); backend._validate(conn)
                row = conn.execute('SELECT record FROM guardians WHERE endpoint=?', (backend.endpoint(worker, course),)).fetchone()
                conn.execute('COMMIT')
            if row is None: raise ValueError('not_ready')
            record = json.loads(row[0]); backend.require_guardian(worker, course, record['epoch'])
            result[course] = {'ready': True}
        except Exception: result[course] = {'ready': False}
    return result


def credentials(root, action, payload):
    """Invoke the real reviewed remote CLI implementation in central state."""
    import remote_credentials
    from authority_access import publish
    root = Path(root); control = root / 'control'; config = control / 'remote.json'
    if action == 'enrollment-create' and payload == {}:
        return remote_credentials.execute(SimpleNamespace(config=str(config), command='create'))
    if action == 'credential-apply':
        if (set(payload) != {'review_id', 'review_sha256'} or not re.fullmatch(r'[0-9a-f]{32}', payload['review_id'])
                or not re.fullmatch(r'[0-9a-f]{64}', payload['review_sha256'])): raise ValueError('remote_credential_review_invalid')
        return remote_credentials.execute(SimpleNamespace(config=str(config), command='apply',
            plan_file=str(control / 'reviews' / (payload['review_id'] + '.json')), review_sha256=payload['review_sha256']))
    if action != 'credential-preview': raise ValueError('remote_credential_action_invalid')
    allowed = {'action', 'worker', 'slot', 'identity', 'overlap_seconds', 'target_identity'}
    if not isinstance(payload, dict) or set(payload) - allowed or payload.get('worker') not in WORKERS:
        raise ValueError('remote_credential_request_invalid')
    worker = payload['worker']; operation = payload['action']; review = uuid.uuid4().hex
    directory = control / 'reviews'; directory.mkdir(mode=0o700, exist_ok=True)
    sources = None
    if operation == 'enroll':
        choices = ({'guardian': 'guardian-one', 'hub:course-main': 'source', 'hub:course-peer': 'peer'}
            if worker == 'worker-one' else {'guardian': 'guardian-two', 'hub:course-main': 'receiver'})
    elif operation in ('stage', 'activate'):
        if worker != 'worker-two' or payload.get('slot') != 'hub:course-main' or payload.get('identity') != 'receiver-rotated':
            raise ValueError('remote_credential_rotation_scope_invalid')
        choices = {'hub:course-main': 'receiver-rotated'}
    else: choices = {}
    if choices:
        sources = directory / (review + '-sources.json')
        publish(sources, {slot: str(control / 'public' / (identity + '.pem')) for slot, identity in choices.items()})
    fingerprint = None
    if operation in ('retire', 'revoke'):
        from worker_credentials import verify_certificate
        identity = payload.get('target_identity')
        if identity not in ('receiver', 'guardian-two'): raise ValueError('remote_credential_revoke_scope_invalid')
        service = read_record(config)['service']
        fingerprint = verify_certificate(control / 'public' / (identity + '.pem'), service['ca_file'])['fingerprint']
    args = SimpleNamespace(config=str(config), command='preview', action=operation, worker=worker,
        slot=payload.get('slot'), credentials=str(sources) if sources is not None else None,
        overlap_seconds=payload.get('overlap_seconds'), target_fingerprint=fingerprint,
        output=str(directory / (review + '.json')))
    result = remote_credentials.execute(args)
    return {'review_id': review, 'review_sha256': result['review_sha256'], 'status': result['status']}


def install_rotated_hub(root):
    import docker
    root = Path(root); fixture = read_record(root / 'remote-fixture.json'); request = fixture['request']
    item = fixture['workers']['worker-two']['courses']['worker-two']['course-main']
    client = docker.DockerClient(base_url='unix:///var/run/docker.sock', version='auto', timeout=5)
    hub = client.containers.get(item['hub_container']); hub.reload()
    if (hub.attrs['State']['Running'] is not False or hub.attrs['Image'] != request['hub_image']
            or hub.attrs['Config']['Labels'].get('educloud.workspace.instance') != item['instance']):
        raise ValueError('remote_rotation_hub_not_stopped')
    backend = DockerBackend(fixture['workers']['worker-two'])
    if backend.containers('worker-two', 'course-main'): raise ValueError('remote_rotation_learner_not_stopped')
    for filename in ('client.crt', 'client.key'):
        source = root / 'receiver-rotated' / 'credentials' / filename
        target = root / 'receiver' / 'credentials' / filename
        shutil.copyfile(source, target); target.chmod(0o600)
    return {'installed': True, 'live_hot_reload': False}


def serve(root, *, interrupt=False):
    import ownership_contract
    from remote_runtime_service import serve as service
    previous = ownership_contract.fault_hook
    fired = []
    def fault(stage, _):
        if stage == 'fenced' and not fired:
            fired.append(True); raise RuntimeError('fixture_post_fence_interruption')
    if interrupt: ownership_contract.fault_hook = fault
    try: service(str(Path(root) / 'control' / 'remote.json'))
    finally: ownership_contract.fault_hook = previous


def main():
    if len(sys.argv) not in (3, 4): raise ValueError('remote_fixture_action_invalid')
    action, root = sys.argv[1], Path(sys.argv[2])
    raw = sys.argv[3] if len(sys.argv) == 4 else sys.stdin.read(65537)
    if len(raw.encode()) > 65536: raise ValueError('remote_fixture_payload_bound')
    payload = json.loads(raw) if raw else {}
    if action == 'bootstrap': result = initialize(root, payload)
    elif action == 'register' and payload == {}: result = register(root)
    elif action == 'worker-ready' and payload == {}: result = worker_ready(root)
    elif action == 'worker-quarantine' and payload == {}:
        from remote_control import load_backend
        config = read_record(root / 'control' / 'remote.json'); backend = load_backend(config['control'])
        result = {worker: {'quarantined': backend.quarantine(worker, 'course-main')} for worker in WORKERS}
    elif action == 'launch-workers' and payload == {}: result = launch_workers(root)
    elif action == 'launch-users' and payload == {}: result = launch_users(root)
    elif action == 'copy-home' and payload == {}: result = copy_home(root)
    elif action == 'rpc': result = rpc(root, payload)
    elif action == 'worker-status' and payload == {}: result = worker_status(root)
    elif action in ('credential-preview', 'credential-apply', 'enrollment-create'): result = credentials(root, action, payload)
    elif action == 'install-rotated-hub' and payload == {}: result = install_rotated_hub(root)
    elif action in ('serve', 'serve-fenced-fault') and payload == {}:
        serve(root, interrupt=action == 'serve-fenced-fault'); return
    elif action == 'drift-snapshot' and payload == {}:
        fixture = read_record(root / 'remote-fixture.json')
        for worker in WORKERS:
            path = fixture['workers'][worker]['courses'][worker]['course-main']['snapshot_path']
            write(path, {'synthetic': 'fresh-review-required', 'course': 'course-main'})
        result = {'changed': True}
    elif action == 'cleanup-permissions' and payload == {}: result = cleanup_permissions(root)
    else: raise ValueError('remote_fixture_action_invalid')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try: main()
    except Exception: raise SystemExit('remote_notebook_fixture_failed') from None
