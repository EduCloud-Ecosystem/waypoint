"""Private disposable Docker fixture helper; never used as a deployment entrypoint."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import uuid

import docker
from authority_access import ReplayStore
from authority_test_pki import TestPKI
from hub.authority_client import AuthorityClient, AuthorityError, VERSION
from ownership_contract import digest
from runtime_authority import bootstrap, read_record


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


def initialize(root, request):
    client = docker.DockerClient(base_url='unix:///var/run/docker.sock', version='auto')
    info = client.info()
    name = request['name']
    # SQLite and endpoint locks live on private Linux volumes; homes retain
    # their actual host bindings. Fresh child directories are bootstrapped once.
    (root / 'control').chmod(0o700)
    (root / 'backend').chmod(0o700)
    workers = {}
    for worker, memory, cpu in [('worker-one', 3072, 2.2), ('worker-two', 1536, 1.1)]:
        workers[worker] = dict(worker_id=worker, provider_id=worker, boot_id=uuid.uuid4().hex,
            policy_sha256=digest({'local': name, 'worker': worker}), memory_budget_mb=memory,
            capacity_memory_mb=memory, cpu_budget=cpu, capacity_cpu=cpu)
    courses = {worker: {} for worker in workers}
    resources = []
    p = dict(active_limit=1, learner_memory_mb=512, learner_cpu=0.1, memory_mb=1536, cpu=1.1)
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
        host_reserve_cpu=0.7, workers=workers, courses=courses,
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
    write(root / 'fixture.json', dict(request=request, identities=identities, resources=resources))
    return {'authority_id': authority.authority_id, 'daemon_id': info['ID'], 'memory_bytes': info['MemTotal'], 'cpu': info['NCPU']}


def launch(root):
    config = read_record(root / 'runtime.json'); fixture = read_record(root / 'fixture.json')
    request = fixture['request']; client = docker.DockerClient(base_url='unix:///var/run/docker.sock', version='auto')
    for worker, courses in config['courses'].items():
        for course, item in courses.items():
            suffix = item['instance'].rsplit('-', 1)[1]
            credentials = root / suffix / 'credentials'
            env = dict(WORKSPACE_INSTANCE=item['instance'], WORKSPACE_IMAGE=item['image_id'],
                WORKSPACE_AUTH_MODE='local-test', WORKSPACE_PUBLIC_URL='http://127.0.0.1:' + str(request['ports'][suffix]),
                WORKSPACE_TEST_PASSWORD=request['password'], WORKSPACE_MEMORY_MB='512', WORKSPACE_CPU_LIMIT='0.1',
                WORKSPACE_ACTIVE_LIMIT='1', WORKSPACE_IDLE_SECONDS='3600', WORKSPACE_MAX_AGE_SECONDS='7200',
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
                mem_limit=1024 * 1024**2, cpu_period=100000, cpu_quota=100000, restart_policy={'Name': 'no'})
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


def main():
    action, directory = sys.argv[1:3]; root = Path(directory)
    payload = json.load(sys.stdin) if action in {'bootstrap', 'rpc', 'revoke'} else {}
    if action == 'bootstrap': result = initialize(root, payload)
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
    elif action == 'runtime-status':
        result = read_record(root / 'runtime-failure.json') if (root / 'runtime-failure.json').exists() else {'code': None}
    elif action == 'guardian-status':
        from runtime_authority import DockerBackend
        from authority_access import AccessPolicy
        from ownership_contract import Authority
        config = read_record(root / 'runtime.json'); backend = DockerBackend(config)
        policy = AccessPolicy(root / 'acl.json', config['authority_id'])
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
        record = read_record(root / 'service-pid.json')
        if process_identity(record['pid']) != record['start_identity']: raise ValueError('fixture_service_replaced')
        os.kill(record['pid'], signal.SIGTERM); result = {'stopped': True}
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
        # Only this invocation's root is passed by the controlling harness.
        for path in root.rglob('*'):
            if not path.is_symlink(): path.chmod(0o700 if path.is_dir() else 0o600)
        result = {'ready': True}
    else: raise ValueError('fixture_action_invalid')
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == '__main__': main()
