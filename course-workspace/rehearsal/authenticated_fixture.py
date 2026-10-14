"""Private disposable Docker fixture helper; never used as a deployment entrypoint."""
import hashlib
import json
import os
import re
import stat
from pathlib import Path
import shutil
import signal
import sys
import time
import uuid

from authority_access import ReplayStore
from authority_test_pki import TestPKI
from hub.authority_client import AuthorityClient, AuthorityError, VERSION, ERRORS
from ownership_contract import digest
from runtime_authority import bootstrap, read_record, open_directory


def write(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open('w') as stream:
        json.dump(value, stream, sort_keys=True)
    path.chmod(0o600)


def process_identity(pid):
    # Fixture-only Linux PID/start binding; not part of the runtime adapter API.
    if type(pid) is not int or pid <= 0: raise ValueError('fixture_process_invalid')
    raw = Path('/proc') / str(pid) / 'stat'
    start = raw.read_text().rsplit(')', 1)[1].split()[19]
    return digest({'pid': pid, 'start': start})


OWNER_VERSION = 'educloud-authenticated-fixture-owner-v1'


def capture_cleanup_owner(root, name):
    """Bind disposal to the original private host directory before bootstrap."""
    if not isinstance(name, str) or not re.fullmatch(r'wa[0-9a-f]{10}', name):
        raise ValueError('fixture_owner_invalid')
    fd = open_directory(root)
    try:
        info = os.fstat(fd)
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('fixture_root_not_private')
        record = dict(version=OWNER_VERSION, name=name, creator_uid=info.st_uid,
            creator_gid=info.st_gid, device=info.st_dev, inode=info.st_ino)
        out = os.open('fixture-owner.json', os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(out, 'w') as stream:
            json.dump(record, stream, sort_keys=True); stream.flush(); os.fsync(stream.fileno())
        os.fsync(fd)
        return record
    finally: os.close(fd)


def _cleanup_record(fd, name, allowed_uids):
    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        info = os.fstat(child)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 8192
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid not in allowed_uids):
            raise ValueError('fixture_cleanup_record_invalid')
        raw = os.read(child, 8193)
        if len(raw) > 8192: raise ValueError('fixture_cleanup_record_invalid')
        return json.loads(raw)
    finally: os.close(child)


def verify_no_service_commands(root):
    # PID metadata may be absent only before launch; do not accept a removed
    # record while the known fixture service command still runs.
    proc = Path('/proc')
    if proc.exists():
        count = 0
        for entry in proc.iterdir():
            if not entry.name.isdigit(): continue
            count += 1
            if count > 4096: raise ValueError('fixture_cleanup_process_bounds')
            try:
                with (entry / 'cmdline').open('rb') as stream: raw = stream.read(8193)
            except FileNotFoundError: continue
            if len(raw) > 8192: raise ValueError('fixture_cleanup_process_bounds')
            args = raw.decode('utf-8', errors='replace').split('\0')
            for index, arg in enumerate(args):
                if (Path(arg).name == 'authenticated_fixture.py' and index + 2 < len(args)
                        and args[index + 1] in {'serve', 'serve-prepared-fault'} and args[index + 2] == str(root)):
                    raise ValueError('fixture_cleanup_service_running')
                if Path(arg).name == 'controller_service.py' and str(Path(root) / 'runtime.json') in args:
                    raise ValueError('fixture_cleanup_service_running')


def service_state(record):
    if (not isinstance(record, dict) or set(record) != {'pid', 'start_identity'}
            or type(record['pid']) is not int or record['pid'] <= 0
            or not isinstance(record['start_identity'], str) or not re.fullmatch(r'[0-9a-f]{64}', record['start_identity'])):
        raise ValueError('fixture_service_record_invalid')
    try: raw = (Path('/proc') / str(record['pid']) / 'stat').read_text().rsplit(')', 1)[1].split()
    except FileNotFoundError: return None
    if digest({'pid': record['pid'], 'start': raw[19]}) != record['start_identity']:
        raise ValueError('fixture_service_replaced')
    return raw[0]


def stop_service(root, *, wait_seconds=5):
    if not 0 < wait_seconds <= 5: raise ValueError('fixture_service_wait_invalid')
    deadline = time.monotonic() + wait_seconds
    try: record = read_record(Path(root) / 'service-pid.json')
    except FileNotFoundError:
        verify_no_service_commands(root)
        return {'stopped': True}
    state = service_state(record)
    if state not in (None, 'Z'):
        if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
            raise ValueError('fixture_service_pidfd_unavailable')
        try: fd = os.pidfd_open(record['pid'], 0)
        except ProcessLookupError: fd = None
        if fd is not None:
            try:
                # A PID reused after this descriptor opened cannot redirect the
                # signal; recheck its private start binding before the effect.
                if service_state(record) not in (None, 'Z'):
                    try: signal.pidfd_send_signal(fd, signal.SIGTERM, None, 0)
                    except ProcessLookupError: pass
            finally: os.close(fd)
    while service_state(record) not in (None, 'Z'):
        if time.monotonic() >= deadline: raise ValueError('fixture_service_stop_timeout')
        time.sleep(0.05)
    verify_no_service_commands(root)
    return {'stopped': True}


def verify_cleanup_quiescent(root_fd, record, client, root):
    name = record['name']
    scopes = [{'label': 'educloud.fixture=' + name}, *({'label': 'educloud.workspace.instance=' + name + '-' + suffix}
        for suffix in ('source', 'peer', 'receiver')), *({'name': '^/' + name + '-' + worker + '-guardian$'}
        for worker in ('worker-one', 'worker-two'))]
    for scope in scopes:
        for container in client.containers.list(all=False, filters=scope):
            container.reload()
            if (container.attrs.get('Name') != '/' + name + '-authority'
                    or container.attrs.get('Config', {}).get('Labels', {}).get('educloud.fixture') != name):
                raise ValueError('fixture_cleanup_participant_running')
    verify_no_service_commands(root)
    try: service = _cleanup_record(root_fd, 'service-pid.json', {os.geteuid(), record['creator_uid']})
    except FileNotFoundError: return
    if (not isinstance(service, dict) or set(service) != {'pid', 'start_identity'}
            or type(service['pid']) is not int or service['pid'] <= 0
            or not isinstance(service['start_identity'], str) or not re.fullmatch(r'[0-9a-f]{64}', service['start_identity'])):
        raise ValueError('fixture_cleanup_service_invalid')
    try:
        raw = (Path('/proc') / str(service['pid']) / 'stat').read_text().rsplit(')', 1)[1].split()
    except FileNotFoundError: return
    identity = digest({'pid': service['pid'], 'start': raw[19]})
    if identity != service['start_identity']:
        raise ValueError('fixture_cleanup_service_replaced')
    if raw[0] != 'Z': raise ValueError('fixture_cleanup_service_running')


def cleanup_permissions(root, client=None):
    """Return only this stopped disposable bind tree to its original creator."""
    root_fd = open_directory(root)
    try:
        initial = os.fstat(root_fd)
        record = _cleanup_record(root_fd, 'fixture-owner.json', {os.geteuid(), initial.st_uid})
        if (not isinstance(record, dict) or set(record) != {'version', 'name', 'creator_uid', 'creator_gid', 'device', 'inode'}
                or record['version'] != OWNER_VERSION or not isinstance(record['name'], str)
                or not re.fullmatch(r'wa[0-9a-f]{10}', record['name'])
                or any(type(record[key]) is not int or not 0 <= record[key] <= 2**32 - 1 for key in ('creator_uid', 'creator_gid'))
                or type(record['device']) is not int or record['device'] < 0
                or type(record['inode']) is not int or record['inode'] <= 0
                or (initial.st_dev, initial.st_ino, initial.st_uid, initial.st_gid)
                    != (record['device'], record['inode'], record['creator_uid'], record['creator_gid'])
                or stat.S_IMODE(initial.st_mode) != 0o700):
            raise ValueError('fixture_cleanup_binding_changed')
        if client is None:
            import docker
            client = docker.DockerClient(base_url='unix:///var/run/docker.sock', version='auto')
        verify_cleanup_quiescent(root_fd, record, client, root)
        count = [0]
        def transfer(fd, depth):
            if depth > 64: raise ValueError('fixture_cleanup_bounds')
            before = os.fstat(fd)
            if before.st_dev != initial.st_dev: raise ValueError('fixture_cleanup_mount_escape')
            with os.scandir(fd) as scan:
                names = []
                for entry in scan:
                    count[0] += 1
                    if count[0] > 20000: raise ValueError('fixture_cleanup_bounds')
                    names.append(entry.name)
            for name in sorted(names):
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if depth == 0 and name in {'control', 'backend'}:
                    if not stat.S_ISDIR(info.st_mode): raise ValueError('fixture_cleanup_mount_invalid')
                    continue  # Exact Linux-volume roots; host mountpoints were preowned.
                if stat.S_ISLNK(info.st_mode): continue  # Parent ownership suffices to unlink; never touch target.
                if info.st_dev != initial.st_dev: raise ValueError('fixture_cleanup_mount_escape')
                if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
                    raise ValueError('fixture_cleanup_entry_invalid')
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if stat.S_ISDIR(info.st_mode): flags |= os.O_DIRECTORY
                child = os.open(name, flags, dir_fd=fd)
                try:
                    actual = os.fstat(child)
                    if (actual.st_dev, actual.st_ino, stat.S_IFMT(actual.st_mode)) != (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)):
                        raise ValueError('fixture_cleanup_entry_changed')
                    if stat.S_ISREG(actual.st_mode) and actual.st_nlink != 1:
                        raise ValueError('fixture_cleanup_hardlink_invalid')
                    if stat.S_ISDIR(actual.st_mode): transfer(child, depth + 1)
                    else:
                        os.fchmod(child, 0o600); os.fchown(child, record['creator_uid'], record['creator_gid'])
                    current = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (actual.st_dev, actual.st_ino):
                        raise ValueError('fixture_cleanup_entry_changed')
                finally: os.close(child)
            final = set()
            with os.scandir(fd) as scan:
                for entry in scan:
                    if len(final) >= 20000: raise ValueError('fixture_cleanup_bounds')
                    final.add(entry.name)
            if final != set(names): raise ValueError('fixture_cleanup_entry_changed')
            os.fchmod(fd, 0o700); os.fchown(fd, record['creator_uid'], record['creator_gid']); os.fsync(fd)
        transfer(root_fd, 0)
        check = open_directory(root)
        try:
            current = os.fstat(check)
            if (current.st_dev, current.st_ino) != (initial.st_dev, initial.st_ino):
                raise ValueError('fixture_cleanup_binding_changed')
        finally: os.close(check)
        return {'ready': True}
    finally: os.close(root_fd)


def course_allocation(cpu):
    if type(cpu) is not int or cpu < 4: raise ValueError('fixture_cpu_capacity_invalid')
    if cpu >= 5:
        return {'name': 'five-cpu-local', 'learner_cpu': 0.25, 'hub_cpu': 1.0, 'reserved_cpu': 4.5, 'minimum_cpu': 5}
    return {'name': 'four-cpu-ci', 'learner_cpu': 0.1, 'hub_cpu': 1.0, 'reserved_cpu': 4.0, 'minimum_cpu': 4}


def initialize(root, request):
    import docker
    capture_cleanup_owner(root, request['name'])
    client = docker.DockerClient(base_url='unix:///var/run/docker.sock', version='auto')
    info = client.info()
    allocation = course_allocation(info['NCPU'])
    course_cpu = allocation['hub_cpu'] + allocation['learner_cpu']
    reserve_cpu = allocation['reserved_cpu'] - 3 * course_cpu
    name = request['name']
    # SQLite and endpoint locks live on private Linux volumes; homes retain
    # their actual host bindings. Fresh child directories are bootstrapped once.
    (root / 'control').chmod(0o700)
    (root / 'backend').chmod(0o700)
    workers = {}
    for worker, memory, cpu in [('worker-one', 3072, 2 * course_cpu), ('worker-two', 1536, course_cpu)]:
        workers[worker] = dict(worker_id=worker, provider_id=worker, boot_id=uuid.uuid4().hex,
            policy_sha256=digest({'local': name, 'worker': worker}), memory_budget_mb=memory,
            capacity_memory_mb=memory, cpu_budget=cpu, capacity_cpu=cpu)
    courses = {worker: {} for worker in workers}
    resources = []
    p = dict(active_limit=1, learner_memory_mb=512, learner_cpu=allocation['learner_cpu'], memory_mb=1536, cpu=course_cpu)
    for worker, course, suffix in [('worker-one', 'course-main', 'source'),
                                  ('worker-one', 'course-peer', 'peer'),
                                  ('worker-two', 'course-main', 'receiver')]:
        instance = name + '-' + suffix
        directory = root / suffix
        directory.mkdir(mode=0o700)
        homes = directory / 'homes'; homes.mkdir(mode=0o700)
        leaf = homes / (instance + '-home-' + hashlib.sha256(b'alice').hexdigest()[:24])
        leaf.mkdir(mode=0o755); os.chown(leaf, 1000, 1000)
        work = leaf / 'work'; work.mkdir(mode=0o755); os.chown(work, 1000, 1000)
        write(directory / 'snapshot.json', {'fixture': 'preserved-synthetic-coursework', 'course': course})
        write(directory / 'runtime.json', {'fixture': 'fixed-runtime', 'course': course})
        host = course + '.local.invalid'
        item = dict(instance=instance, logical_identity=dict(origin='https://' + host, dns_subtree=host,
            issuer='https://identity.local.invalid/realms/fixture', client_id=course), profile=p,
            image_id=request['course_image'], hub_container=instance + '-hub', subjects=['alice'],
            home_root=str(homes), snapshot_path=str(directory / 'snapshot.json'), runtime_path=str(directory / 'runtime.json'))
        courses[worker][course] = item
        volume = client.volumes.create(name=leaf.name, driver_opts=dict(type='none', o='bind', device=str(leaf)),
            labels={'educloud.workspace.instance': instance, 'educloud.workspace.kind': 'home'})
        resources.append({'kind': 'volume', 'name': volume.name})
    config = dict(version='educloud-local-runtime-v1', authority_id='0' * 32,
        daemon_id=info['ID'], backend_directory=str(root / 'backend' / 'state'), host_reserve_mb=2048,
        host_reserve_cpu=round(reserve_cpu, 6), workers=workers, courses=courses,
        guardians={worker: dict(container_name=name + '-' + worker + '-guardian',
            image_id=request['controller_image'], heartbeat_seconds=10) for worker in workers})
    authority, backend, config = bootstrap(config, str(root / 'control' / 'authority'), client)
    for worker in workers: authority.register_worker(worker)
    authority.register_course('course-main', 'worker-one')
    authority.register_course('course-peer', 'worker-one')
    ReplayStore.create(root / 'control' / 'replay', authority.authority_id)
    (root / 'control' / 'plans').mkdir(mode=0o700)
    pki = TestPKI(root / 'pki')
    cert, key = pki.issue('authority-server', purpose='server', hostname='authority.local.test')
    clients = {}
    identities = {}
    for identity, role, scopes, cs in [
        ('operator', 'operator', list(workers), ['course-main', 'course-peer']),
        ('source', 'hub', ['worker-one'], ['course-main']),
        ('peer', 'hub', ['worker-one'], ['course-peer']),
        ('receiver', 'hub', ['worker-two'], ['course-main']),
        ('guardian-one', 'guardian', ['worker-one'], ['course-main', 'course-peer']),
        ('guardian-two', 'guardian', ['worker-two'], ['course-main'])]:
        directory = root / identity / 'credentials'; directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        c, k = pki.issue(identity)
        for source, target in [(c, 'client.crt'), (k, 'client.key'), (pki.ca_cert, 'ca.crt')]:
            shutil.copyfile(source, directory / target); (directory / target).chmod(0o600)
        fingerprint = pki.fingerprint(c)
        identities[identity] = fingerprint
        clients[fingerprint] = dict(role=role, workers=scopes, courses=cs)
        client_config = dict(url='https://authority.local.test:8443/v1/authority', authority_id=authority.authority_id,
            ca_file=str(directory / 'ca.crt'), certfile=str(directory / 'client.crt'), keyfile=str(directory / 'client.key'))
        write(directory / 'client.json', client_config)
        if role == 'guardian': write(directory / 'runtime.json', config)
    write(root / 'acl.json', dict(version=VERSION, authority_id=authority.authority_id, clients=clients))
    write(root / 'acl-original.json', dict(version=VERSION, authority_id=authority.authority_id, clients=clients))
    write(root / 'runtime.json', config)
    write(root / 'service.json', dict(bind='0.0.0.0', port=8443, authority_directory=str(root / 'control' / 'authority'),
        replay_directory=str(root / 'control' / 'replay'), plan_directory=str(root / 'control' / 'plans'), certfile=str(cert),
        keyfile=str(key), ca_file=str(pki.ca_cert), acl_file=str(root / 'acl.json')))
    write(root / 'fixture.json', dict(request=request, allocation=allocation, identities=identities, resources=resources))
    return {'authority_id': authority.authority_id, 'daemon_id': info['ID'], 'memory_bytes': info['MemTotal'], 'cpu': info['NCPU'], 'allocation': allocation}


def launch(root):
    import docker
    config = read_record(root / 'runtime.json'); fixture = read_record(root / 'fixture.json')
    request = fixture['request']; client = docker.DockerClient(base_url='unix:///var/run/docker.sock', version='auto')
    for worker, courses in config['courses'].items():
        for course, item in courses.items():
            suffix = item['instance'].rsplit('-', 1)[1]
            credentials = root / suffix / 'credentials'
            env = dict(WORKSPACE_INSTANCE=item['instance'], WORKSPACE_IMAGE=item['image_id'],
                WORKSPACE_AUTH_MODE='local-test', WORKSPACE_PUBLIC_URL='http://127.0.0.1:' + str(request['ports'][suffix]),
                WORKSPACE_TEST_PASSWORD=request['password'], WORKSPACE_MEMORY_MB=str(item['profile']['learner_memory_mb']),
                WORKSPACE_CPU_LIMIT=str(item['profile']['learner_cpu']), WORKSPACE_ACTIVE_LIMIT=str(item['profile']['active_limit']), WORKSPACE_IDLE_SECONDS='3600', WORKSPACE_MAX_AGE_SECONDS='7200',
                WORKSPACE_RUNTIME_MODE='authority-local', WORKSPACE_AUTHORITY_URL='https://authority.local.test:8443/v1/authority',
                WORKSPACE_AUTHORITY_ID=config['authority_id'], WORKSPACE_AUTHORITY_WORKER_ID=worker,
                WORKSPACE_AUTHORITY_COURSE_ID=course, WORKSPACE_AUTHORITY_CA=str(credentials / 'ca.crt'),
                WORKSPACE_AUTHORITY_CERT=str(credentials / 'client.crt'), WORKSPACE_AUTHORITY_KEY=str(credentials / 'client.key'))
            hubdata = item['instance'] + '-hubdata'
            client.volumes.create(name=hubdata, labels={'educloud.fixture': request['name']})
            hub = client.containers.create(request['hub_image'], name=item['hub_container'], detach=True,
                environment=env, network=request['edge_network'], ports={'8000/tcp': ('127.0.0.1', request['ports'][suffix])},
                volumes={str(credentials): {'bind': str(credentials), 'mode': 'ro'}, hubdata: {'bind': '/data', 'mode': 'rw'}},
                labels={'educloud.workspace.instance': item['instance'], 'educloud.workspace.kind': 'hub'},
                mem_limit=1024 * 1024**2, cpu_period=100000, cpu_quota=int(fixture['allocation']['hub_cpu'] * 100000), restart_policy={'Name': 'no'})
            client.networks.get(request['network']).connect(hub)
            hub.start()
        identity = 'guardian-one' if worker == 'worker-one' else 'guardian-two'
        credentials = root / identity / 'credentials'
        client.containers.run(request['controller_image'], name=config['guardians'][worker]['container_name'], detach=True,
            command=['python', 'runtime_guardian.py', '--config', str(credentials / 'runtime.json'), '--worker', worker,
                     '--client-config', str(credentials / 'client.json')], network=request['network'],
            volumes={str(credentials): {'bind': str(credentials), 'mode': 'ro'},
                request['name'] + '-backend': {'bind': str(root / 'backend'), 'mode': 'rw'},
                '/var/run/docker.sock': {'bind': '/var/run/docker.sock', 'mode': 'rw'}},
            labels={'educloud.runtime.authority': config['authority_id'], 'educloud.runtime.worker': worker, 'educloud.runtime.kind': 'guardian'},
            mem_limit=128 * 1024**2, cpu_period=100000, cpu_quota=10000, restart_policy={'Name': 'no'})
    return {'launched': True}


DIAGNOSTIC_LIMIT = 256 * 1024
SUPERVISOR_CODES = frozenset({'guardian_unavailable', 'guardian_stale_or_changed',
    'guardian_container_unavailable', 'runtime_cached_epoch_invalid', 'runtime_container_policy_invalid', 'runtime_supervision_refused'})
OBSERVATION_CODES = ERRORS | frozenset({'guardian_observation_invalid', 'guardian_unavailable',
    'guardian_stale_or_changed', 'guardian_container_unavailable', 'runtime_cached_epoch_invalid', 'guardian_observation_failed'})
AUTHORITY_FAILURES = ERRORS | frozenset({'fixture_service_refused', 'runtime_endpoint_enforcement_unavailable', 'authority_supervision_failed'})
STAGES = {'ownership': 'checking ownership', 'environment': 'preparing environment',
          'create': 'creating runtime', 'created': 'runtime created'}


def _diagnostic_counts(raw, prefix, codes):
    counts = {code: 0 for code in sorted(codes)}
    for line in raw.splitlines():
        position = line.find(prefix)
        if position < 0: continue
        code = line[position + len(prefix):].strip()
        if code in counts: counts[code] = min(10000, counts[code] + 1)
    return counts


def _diagnostic_owned(container, name, image, labels):
    container.reload()
    actual = container.attrs
    if (actual.get('Name') != '/' + name or actual.get('Image') != image
            or {key: value for key, value in (actual.get('Config', {}).get('Labels') or {}).items()
                if key.startswith('educloud.')} != labels):
        raise ValueError('fixture_diagnostic_scope_invalid')


def _diagnostic_logs(container):
    stream = container.logs(stream=True, follow=False, tail=2000)
    raw = bytearray(); truncated = False
    try:
        for chunks, chunk in enumerate(stream, 1):
            if chunks > 16384:
                truncated = True; break
            if not isinstance(chunk, bytes): raise ValueError('fixture_diagnostic_log_invalid')
            remaining = DIAGNOSTIC_LIMIT - len(raw)
            raw.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True; break
    finally:
        close = getattr(stream, 'close', None)
        if close is not None: close()
    return raw.decode('utf-8', errors='replace'), truncated


def failure_status(root, client=None):
    """Read-only bounded fixed counters; raw log lines never leave this helper."""
    config = read_record(root / 'runtime.json'); fixture = read_record(root / 'fixture.json')
    request = fixture['request']
    name = request.get('name')
    expected_courses = {'worker-one': {'course-main': 'source', 'course-peer': 'peer'}, 'worker-two': {'course-main': 'receiver'}}
    if (not isinstance(name, str) or not re.fullmatch(r'wa[0-9a-f]{10}', name)
            or set(config['courses']) != set(expected_courses) or set(config['guardians']) != set(expected_courses)):
        raise ValueError('fixture_diagnostic_scope_invalid')
    for worker, courses in expected_courses.items():
        if set(config['courses'][worker]) != set(courses): raise ValueError('fixture_diagnostic_scope_invalid')
        for course, suffix in courses.items():
            item = config['courses'][worker][course]
            if item['instance'] != name + '-' + suffix or item['hub_container'] != name + '-' + suffix + '-hub':
                raise ValueError('fixture_diagnostic_scope_invalid')
        if config['guardians'][worker]['container_name'] != name + '-' + worker + '-guardian':
            raise ValueError('fixture_diagnostic_scope_invalid')
    if client is None:
        import docker
        client = docker.DockerClient(base_url='unix:///var/run/docker.sock', version='auto', timeout=3)
    result = {'version': 'educloud-authenticated-fixture-diagnostics-v1', 'authority_failure': 'none',
        'supervisor': {'available': False, 'truncated': False, 'reason_counts': {code: 0 for code in sorted(SUPERVISOR_CODES)}},
        'hubs': {}, 'guardians': {}, 'learners': {}}
    try:
        code = read_record(root / 'service-failure.json').get('failure')
        result['authority_failure'] = code if code in AUTHORITY_FAILURES else 'unknown'
    except FileNotFoundError: pass
    except Exception: result['authority_failure'] = 'unavailable'
    try:
        fd = os.open(root / 'supervision.log', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600): raise ValueError('fixture_diagnostic_log_invalid')
            start = max(0, info.st_size - DIAGNOSTIC_LIMIT)
            os.lseek(fd, start, os.SEEK_SET); raw = os.read(fd, DIAGNOSTIC_LIMIT).decode('utf-8', errors='replace')
            if start: raw = raw.partition('\n')[2]
            result['supervisor'] = {'available': True, 'truncated': bool(start),
                'reason_counts': _diagnostic_counts(raw, 'Runtime supervision stopping owned learners: ', SUPERVISOR_CODES)}
        finally: os.close(fd)
    except Exception: pass
    for worker, courses in config['courses'].items():
        for course, item in courses.items():
            suffix = item['instance'].rsplit('-', 1)[1]
            hub = {'available': False, 'truncated': False, 'startup_stage_counts': {key: 0 for key in STAGES},
                'authority_error_counts': {code: 0 for code in sorted(ERRORS)}, 'http_startup_timeout_count': 0}
            try:
                container = client.containers.get(item['hub_container'])
                _diagnostic_owned(container, item['hub_container'], request['hub_image'],
                    {'educloud.workspace.instance': item['instance'], 'educloud.workspace.kind': 'hub'})
                raw, truncated = _diagnostic_logs(container)
                hub.update(available=True, truncated=truncated)
                hub['startup_stage_counts'] = {key: min(10000, raw.count('Authority notebook startup: ' + value)) for key, value in STAGES.items()}
                hub['authority_error_counts'] = _diagnostic_counts(raw, 'AuthorityError: ', ERRORS)
                hub['http_startup_timeout_count'] = min(10000, sum('server never showed up at ' in line and '. Giving up.' in line for line in raw.splitlines()))
            except Exception: pass
            result['hubs'][suffix] = hub
            learners = {'available': False, 'states': []}
            try:
                values = client.containers.list(all=True, filters={'label': [
                    'educloud.workspace.instance=' + item['instance'], 'educloud.workspace.kind=learner']})
                if len(values) > len(item['subjects']): raise ValueError('fixture_diagnostic_bounds')
                states = []
                for container in values:
                    container.reload(); labels = container.attrs['Config'].get('Labels') or {}
                    key = labels.get('educloud.runtime.subject'); epoch = labels.get('educloud.runtime.epoch')
                    if (key not in {hashlib.sha256(subject.encode()).hexdigest()[:24] for subject in item['subjects']}
                            or not isinstance(epoch, str) or not epoch.isdecimal() or not 1 <= int(epoch) <= 10**9
                            or str(int(epoch)) != epoch): raise ValueError('fixture_diagnostic_scope_invalid')
                    _diagnostic_owned(container, item['instance'] + '-learner-' + key, item['image_id'],
                        {'educloud.workspace.instance': item['instance'], 'educloud.workspace.kind': 'learner',
                         'educloud.runtime.authority': config['authority_id'], 'educloud.runtime.worker': worker,
                         'educloud.runtime.course': course, 'educloud.runtime.subject': key, 'educloud.runtime.epoch': epoch})
                    state = container.attrs['State']
                    if (type(state['Running']) is not bool or type(state['OOMKilled']) is not bool
                            or type(state['ExitCode']) is not int or not -2**31 <= state['ExitCode'] < 2**31):
                        raise ValueError('fixture_diagnostic_state_invalid')
                    states.append({key: state[key] for key in ('Running', 'OOMKilled', 'ExitCode')})
                learners = {'available': True, 'states': states}
            except Exception: pass
            result['learners'][suffix] = learners
        guardian = {'available': False, 'truncated': False, 'observation_reason_counts': {code: 0 for code in sorted(OBSERVATION_CODES)},
            'snapshot_busy_count': 0, 'stop_busy_count': 0}
        try:
            item = config['guardians'][worker]; container = client.containers.get(item['container_name'])
            _diagnostic_owned(container, item['container_name'], item['image_id'],
                {'educloud.runtime.authority': config['authority_id'], 'educloud.runtime.worker': worker, 'educloud.runtime.kind': 'guardian'})
            raw, truncated = _diagnostic_logs(container)
            guardian.update(available=True, truncated=truncated)
            guardian['observation_reason_counts'] = _diagnostic_counts(raw, 'Guardian observation refused: ', OBSERVATION_CODES)
            guardian['snapshot_busy_count'] = min(10000, raw.count('Guardian snapshot deferred: endpoint_busy'))
            guardian['stop_busy_count'] = min(10000, raw.count('Guardian stop deferred: endpoint_busy'))
        except Exception: pass
        result['guardians'][worker] = guardian
    return result


def main():
    action, directory = sys.argv[1:3]; root = Path(directory)
    payload = json.load(sys.stdin) if action in {'bootstrap', 'rpc', 'revoke'} or action.startswith('enrollment-') else {}
    if action.startswith('enrollment-'):
        from enrollment_fixture import dispatch
        result = dispatch(root, action, payload)
    elif action == 'bootstrap': result = initialize(root, payload)
    elif action == 'launch': result = launch(root)
    elif action in {'serve', 'serve-prepared-fault'}:
        import logging
        diagnostic_fd = os.open(root / 'supervision.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        diagnostic_handler = logging.StreamHandler(os.fdopen(diagnostic_fd, 'a'))
        logging.getLogger('runtime_authority').addHandler(diagnostic_handler)
        from controller_service import serve
        from runtime_authority import RuntimeDispatcher
        original_dispatch = RuntimeDispatcher.dispatch
        def diagnose(self, principal, operation, course, payload):
            try: return original_dispatch(self, principal, operation, course, payload)
            except Exception as exc:
                import re
                code = getattr(exc, 'code', 'fixture_runtime_refused')
                if not isinstance(code, str) or not re.fullmatch('[a-z_]{1,80}', code): code = 'fixture_runtime_refused'
                if operation == 'hub.spawn':
                    write(root / 'runtime-failure.json', {'operation': operation, 'code': code, 'type': type(exc).__name__})
                raise
        RuntimeDispatcher.dispatch = diagnose
        (root / 'service-failure.json').unlink(missing_ok=True)
        write(root / 'service-pid.json', {'pid': os.getpid(), 'start_identity': process_identity(os.getpid())})
        if action == 'serve-prepared-fault':
            import ownership_contract
            fired = []
            def fault(stage, transaction):
                if stage == 'prepared' and not fired:
                    fired.append(True)
                    raise RuntimeError('disposable_fixture_prepared_interruption')
            ownership_contract.fault_hook = fault
        try:
            serve(read_record(root / 'runtime.json'), read_record(root / 'service.json'))
        except Exception as exc:
            import re
            cause = exc.__cause__ or exc
            code = getattr(cause, 'code', 'authority_service_failed')
            if not isinstance(code, str) or not re.fullmatch('[a-z_]{1,80}', code): code = 'authority_service_failed'
            write(root / 'service-failure.json', {'failure': code})
            raise SystemExit('disposable_fixture_service_failed') from None
        return
    elif action == 'service-status':
        result = read_record(root / 'service-failure.json') if (root / 'service-failure.json').exists() else {'failure': None}
    elif action == 'failure-status':
        result = failure_status(root)
    elif action == 'runtime-status':
        result = read_record(root / 'runtime-failure.json') if (root / 'runtime-failure.json').exists() else {'code': None}
    elif action == 'guardian-status':
        from runtime_authority import DockerBackend
        from authority_access import AccessPolicy
        from ownership_contract import Authority
        config = read_record(root / 'runtime.json'); backend = DockerBackend(config)
        service = read_record(root / 'service.json')
        enrollment = None
        if 'enrollment_directory' in service:
            from worker_credentials import verify_binding, make_runtime_binding_verifier
            from worker_enrollment import EnrollmentStore
            enrollment = EnrollmentStore.open(service['enrollment_directory'], config['authority_id'],
                verify_binding(config, service, backend=backend))
        binding_verifier = None if enrollment is None else make_runtime_binding_verifier(config, service, backend, enrollment.binding)
        policy = AccessPolicy(root / 'acl.json', config['authority_id'], enrollment=enrollment, binding_verifier=binding_verifier)
        authority = Authority.open(root / 'control' / 'authority', config['authority_id'], backend)
        result = {}
        for course in ('course-main', 'course-peer'):
            try:
                with authority._connection() as connection:
                    connection.execute('BEGIN'); authority._validate(connection)
                    current = authority._course(connection, course); connection.execute('COMMIT')
                record = backend.require_guardian(current['owner'], course, current['epoch'])
                policy.authorize_fingerprint(record['cert_sha256'], 'guardian', current['owner'], course)
                result[course] = {'ready': True}
            except Exception as exc:
                result[course] = {'ready': False, 'code': getattr(exc, 'code', 'fixture_guardian_unavailable')}
    elif action == 'stop-service':
        result = stop_service(root)
    elif action == 'rpc':
        identity = payload.pop('identity', 'operator')
        settings = read_record(root / identity / 'credentials' / 'client.json')
        # The fixture operator runs inside the service container; use its
        # certificate-verified loopback SAN. Hubs/guardians use private DNS.
        settings['url'] = 'https://127.0.0.1:8443/v1/authority'
        api = AuthorityClient(worker_id=payload.pop('worker_id', 'worker-one'),
            course_id=payload.pop('course_id', 'course-main'), **settings)
        try: result = {'ok': True, 'result': api.call(payload['operation'], payload.get('payload', {}))}
        except AuthorityError as exc: result = {'ok': False, 'refused': True, 'code': exc.code}
        except Exception: result = {'ok': False, 'refused': True}
    elif action == 'copy-home':
        config = read_record(root / 'runtime.json')
        source = config['courses']['worker-one']['course-main']; target = config['courses']['worker-two']['course-main']
        for child in Path(source['home_root']).iterdir():
            dest = Path(target['home_root']) / child.name.replace(source['instance'], target['instance'], 1)
            shutil.rmtree(dest)
            shutil.copytree(child, dest, symlinks=True)
            for path in [dest, *dest.rglob('*')]:
                if not path.is_symlink():
                    original = child / path.relative_to(dest); info = original.lstat(); os.chown(path, info.st_uid, info.st_gid)
        result = {'copied': True}
    elif action == 'drift-snapshot':
        for suffix in ('source', 'receiver'): write(root / suffix / 'snapshot.json', {'fixture': 'reviewed-new-snapshot'})
        result = {'changed': True}
    elif action == 'revoke':
        acl = read_record(root / 'acl.json'); fixture = read_record(root / 'fixture.json')
        acl['clients'].pop(fixture['identities'][payload['identity']]); write(root / 'acl.json', acl)
        result = {'revoked': True}
    elif action == 'restore-acl':
        write(root / 'acl.json', read_record(root / 'acl-original.json')); result = {'restored': True}
    elif action == 'history-proof':
        from ownership_contract import Authority
        from runtime_authority import DockerBackend
        config = read_record(root / 'runtime.json'); backend = DockerBackend(config)
        authority = Authority.open(root / 'control' / 'authority', config['authority_id'], backend)
        authority.inventory()  # Validates every durable event and pending/recovery link.
        with authority._connection() as connection:
            rows = connection.execute('SELECT status FROM transfers ORDER BY transaction_id').fetchall()
            result = {'transfer_records': len(rows), 'statuses': sorted(row['status'] for row in rows)}
    elif action == 'cleanup-permissions':
        result = cleanup_permissions(root)
    else: raise ValueError('fixture_action_invalid')
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == '__main__': main()
