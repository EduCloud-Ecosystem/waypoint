"""Private disposable enrollment controls; never a production provisioner/issuer."""
import hashlib
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import json

from authenticated_fixture import write, _diagnostic_owned, process_identity, service_state
from authority_access import AccessPolicy, publish
from authority_test_pki import TestPKI
from hub.authority_client import AuthorityClient, AuthorityError
from ownership_contract import Authority, digest
from runtime_authority import DockerBackend, RuntimeRefusal, read_record
from worker_credentials import verify_binding, verify_certificate
from worker_enrollment import EnrollmentStore

IDENTITIES = {
    'source': ('worker-one', 'hub:course-main'),
    'peer': ('worker-one', 'hub:course-peer'),
    'receiver': ('worker-two', 'hub:course-main'),
    'guardian-one': ('worker-one', 'guardian'),
    'guardian-two': ('worker-two', 'guardian'),
}


def context(root):
    config = read_record(root / 'runtime.json'); service = read_record(root / 'service.json')
    backend = DockerBackend(config)
    binding = verify_binding(config, service, backend=backend)
    store = EnrollmentStore.open(service['enrollment_directory'], config['authority_id'], binding)
    return config, service, backend, store


def leaf(root, identity, next_leaf=False):
    directory = root / (identity + '-next' if next_leaf else identity) / 'credentials'
    return directory / 'client.crt'


def candidate(root, identity):
    worker, slot = IDENTITIES[identity]
    directory = root / (identity + '-next') / 'credentials'
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    # Only this disposable fixture holds signing material. Registry/CLI never do.
    pki = object.__new__(TestPKI)
    pki.root = root / 'pki'; pki.ca_cert = pki.root / 'ca.pem'; pki.ca_key = pki.root / 'ca.key'
    cert, key = pki.issue(identity + '-rotation')
    for source, name in ((cert, 'client.crt'), (key, 'client.key'), (pki.ca_cert, 'ca.crt')):
        shutil.copyfile(source, directory / name); (directory / name).chmod(0o600)
    config = read_record(root / identity / 'credentials' / 'client.json')
    config.update(certfile=str(directory / 'client.crt'), keyfile=str(directory / 'client.key'), ca_file=str(directory / 'ca.crt'))
    write(directory / 'client.json', config)
    return verify_certificate(directory / 'client.crt', pki.ca_cert)


def apply(root, store, plan, config, service):
    return store.apply(plan['plan_id'], plan['plan_sha256'], lambda: verify_binding(config, service))


def _heartbeat(backend, course):
    with backend.connection() as connection:
        connection.execute('BEGIN'); backend._validate(connection)
        row = connection.execute('SELECT record FROM guardians WHERE endpoint=?',
            (backend.endpoint('worker-one', course),)).fetchone()
        connection.execute('COMMIT')
    return None if row is None else row['record']


def _deferred(result, worker, course):
    return (isinstance(result, dict) and set(result) == {'authorized', 'deferred', 'worker_id', 'course_id'}
        and result['authorized'] is False and result['deferred'] is True
        and result['worker_id'] == worker and result['course_id'] == course)


def _guardian_probe(api, identity, worker, course):
    """Distinguish authenticated acquisition deferral from actual ACL denial."""
    try:
        result = api.call('guardian.observe', identity)
    except AuthorityError as error:
        if error.code == 'authority_access_refused': return {'authorized': False, 'denied': True}
        raise
    if _deferred(result, worker, course): return {'authorized': False, 'deferred': True}
    if (not isinstance(result, dict) or set(result) != {'authorized', 'epoch', 'worker_id', 'course_id'}
            or result['authorized'] is not True or type(result['epoch']) is not int or not 1 <= result['epoch'] <= 10**9
            or result['worker_id'] != worker or result['course_id'] != course):
        raise ValueError('fixture_guardian_probe_invalid')
    return {'authorized': True}


def _hold_endpoint(root, course, seconds):
    # Disposable holder only: fixed configured worker/course and finite duration.
    if course not in ('course-main', 'course-peer') or type(seconds) is not int or seconds not in (4, 13):
        raise ValueError('fixture_contention_invalid')
    backend = DockerBackend(read_record(root / 'runtime.json'))
    marker = root / 'control' / ('enrollment-lock-' + course + '.json')
    record = read_record(root / 'control' / ('enrollment-holder-' + course + '.json'))
    if record != {'pid': os.getpid(), 'start_identity': process_identity(os.getpid())}:
        raise ValueError('fixture_contention_process_changed')
    deadline = time.monotonic() + 5
    while True:
        original = _heartbeat(backend, course)
        checked_at = _heartbeat_time(original)
        age = backend.clock() - checked_at
        if 0 <= age <= 1: break
        if (not math.isfinite(age) or age < 0
                or age > backend.config['guardians']['worker-one']['heartbeat_seconds']
                or time.monotonic() >= deadline): raise ValueError('fixture_contention_heartbeat_stale')
        time.sleep(.05)  # Readonly readiness: only the actual Guardian renews.
    with backend.locks([('worker-one', course)]):
        original = _heartbeat(backend, course)
        checked_at = _heartbeat_time(original)
        if not 0 <= backend.clock() - checked_at <= 1: raise ValueError('fixture_contention_heartbeat_stale')
        started = time.monotonic()
        ready = {'held': True, 'holder': record, 'seconds': seconds, 'started': started,
            'release_at': started + seconds, 'heartbeat_sha256': digest(original), 'checked_at': checked_at}
        publish(marker, ready)
        time.sleep(seconds)
        # Publish release intent BEFORE unlocking; stale READY cannot prove a
        # hold after exit. Exterior recovery additionally requires holder exit.
        publish(root / 'control' / ('enrollment-release-' + course + '.json'), dict(ready, held=False))


def _heartbeat_time(raw):
    if raw is None: raise ValueError('fixture_heartbeat_missing')
    value = json.loads(raw).get('checked_at')
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('fixture_contention_heartbeat_invalid')
    return value


def _start_holder(root, course, seconds, *, monotonic=time.monotonic, sleep=time.sleep):
    if course not in ('course-main', 'course-peer') or type(seconds) is not int or seconds not in (4, 13):
        raise ValueError('fixture_contention_invalid')
    marker = root / 'control' / ('enrollment-lock-' + course + '.json')
    holder = root / 'control' / ('enrollment-holder-' + course + '.json')
    if any(os.path.lexists(path) for path in (marker, holder, root / 'control' / ('enrollment-release-' + course + '.json'))):
        raise ValueError('fixture_contention_reuse')
    child = subprocess.Popen([sys.executable, 'authenticated_fixture.py', 'enrollment-contention-hold',
        str(root)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    record = {'pid': child.pid, 'start_identity': process_identity(child.pid)}
    publish(holder, record)
    child.stdin.write(json.dumps({'course': course, 'seconds': seconds}).encode()); child.stdin.close()
    deadline = monotonic() + 10  # Initialization/readiness is outside the hold.
    while monotonic() < deadline:
        if child.poll() is not None: raise ValueError('fixture_contention_holder_exited')
        if marker.exists(): return child
        sleep(.05)
    raise ValueError('fixture_contention_unavailable')


def _contention_marker(root, course):
    ready = read_record(root / 'control' / ('enrollment-lock-' + course + '.json'))
    release = root / 'control' / ('enrollment-release-' + course + '.json')
    if release.exists():
        ended = read_record(release)
        if ended != dict(ready, held=False): raise ValueError('fixture_contention_marker_changed')
        return ended
    return ready


def _held_heartbeat(root, backend, course, child, *, monotonic=time.monotonic, process_state=service_state):
    marker = _contention_marker(root, course)
    if (set(marker) != {'held', 'holder', 'seconds', 'started', 'release_at', 'heartbeat_sha256', 'checked_at'}
            or marker['held'] is not True or marker['seconds'] not in (4, 13)
            or type(marker['seconds']) is not int or child.pid != marker['holder'].get('pid')
            or child.poll() is not None or process_state(marker['holder']) in (None, 'Z')
            or any(type(marker[key]) not in (int, float) or not math.isfinite(marker[key]) for key in ('started', 'release_at', 'checked_at'))
            or marker['release_at'] != marker['started'] + marker['seconds']
            or not marker['started'] <= monotonic() < marker['release_at']):
        raise ValueError('fixture_contention_window_invalid')
    current = _heartbeat(backend, course)
    if digest(current) != marker['heartbeat_sha256'] or _heartbeat_time(current) != marker['checked_at']:
        raise ValueError('fixture_contention_heartbeat_changed')
    age = backend.clock() - marker['checked_at']
    if not math.isfinite(age) or age < 0: raise ValueError('fixture_contention_heartbeat_invalid')
    try:
        with backend.locks([('worker-one', course)], wait_seconds=.001):
            raise ValueError('fixture_contention_not_held')
    except RuntimeRefusal as error:
        if error.code != 'runtime_endpoint_lock_busy': raise
    if _contention_marker(root, course) != marker or monotonic() >= marker['release_at']:
        raise ValueError('fixture_contention_window_invalid')
    return marker


def _contention_batch(root, backend, course, seconds, *, monotonic=time.monotonic, sleep=time.sleep,
                      start_holder=_start_holder, process_state=service_state):
    if course not in ('course-main', 'course-peer') or type(seconds) is not int or seconds not in (4, 13):
        raise ValueError('fixture_contention_invalid')
    settings = read_record(root / 'guardian-one' / 'credentials' / 'client.json')
    settings['url'] = 'https://127.0.0.1:8443/v1/authority'
    api = AuthorityClient(worker_id='worker-one', course_id=course, **settings)
    identity = backend.guardian_identity('worker-one')
    child = start_holder(root, course, seconds)
    observations = []
    for _ in range(2):
        _held_heartbeat(root, backend, course, child, monotonic=monotonic, process_state=process_state)
        started = monotonic(); result = api.call('guardian.observe', identity)
        elapsed = 1000 * (monotonic() - started)
        if not _deferred(result, 'worker-one', course) or not 0 <= elapsed < 3000:
            raise ValueError('fixture_contention_probe_invalid')
        _held_heartbeat(root, backend, course, child, monotonic=monotonic, process_state=process_state)
        observations.append({'deferred': True, 'held_before': True, 'held_after': True,
            'heartbeat_unchanged': True, 'elapsed_ms': round(elapsed, 3)})
    expired = False
    if seconds == 13:
        while True:
            marker = _held_heartbeat(root, backend, course, child, monotonic=monotonic, process_state=process_state)
            if backend.clock() - marker['checked_at'] > backend.config['guardians']['worker-one']['heartbeat_seconds']:
                expired = True; break
            sleep(.05)
    return {'observations': observations, 'expiry_while_held': expired}


def _dispatch(root, action, payload):
    if action == 'enrollment-contention-hold':
        _hold_endpoint(root, payload['course'], payload['seconds'])
        return {'released': True}
    if action == 'enrollment-create':
        config = read_record(root / 'runtime.json'); service = read_record(root / 'service.json')
        directory = root / 'control' / 'enrollment'
        EnrollmentStore.create(directory, config['authority_id'], verify_binding(config, service))
        service['enrollment_directory'] = str(directory); write(root / 'service.json', service)
        return {'created': True}
    config, service, backend, store = context(root)
    if action == 'enrollment-contention-batch':
        return _contention_batch(root, backend, payload['course'], payload['seconds'])
    if action == 'enrollment-contention-start':
        _start_holder(root, payload['course'], payload['seconds'])
        return {'held': True}
    if action == 'enrollment-contention-status':
        course = payload['course']
        if course not in ('course-main', 'course-peer'): raise ValueError('fixture_contention_invalid')
        marker = _contention_marker(root, course)
        current = _heartbeat(backend, course)
        expired = (current is not None and backend.clock() - json.loads(current)['checked_at'] >
                   config['guardians']['worker-one']['heartbeat_seconds'])
        return {'held': marker['held'], 'heartbeat_unchanged': digest(current) == marker['heartbeat_sha256'],
                'heartbeat_expired': expired, 'holder_exited': service_state(marker['holder']) in (None, 'Z'),
                'heartbeat_renewed': current is not None and _heartbeat_time(current) > marker['checked_at']}
    if action == 'enrollment-observation-probe':
        course = payload['course']
        if course not in ('course-main', 'course-peer'): raise ValueError('fixture_contention_invalid')
        settings = read_record(root / 'guardian-one' / 'credentials' / 'client.json')
        settings['url'] = 'https://127.0.0.1:8443/v1/authority'
        api = AuthorityClient(worker_id='worker-one', course_id=course, **settings)
        started = time.monotonic()
        result = api.call('guardian.observe', backend.guardian_identity('worker-one'))
        deferred = _deferred(result, 'worker-one', course)
        return {'deferred': deferred, 'elapsed_ms': round(1000 * (time.monotonic() - started), 3)}
    if action == 'enrollment-negative':
        try: store.preview('enroll', 'unapproved-worker', credentials={})
        except Exception: unknown = True
        else: unknown = False
        return {'unknown_worker_refused': unknown}
    if action == 'enrollment-preview':
        worker = payload['worker']
        credentials = {slot: verify_certificate(leaf(root, identity), service['ca_file'])
            for identity, (owner, slot) in IDENTITIES.items() if owner == worker}
        plan = store.preview('enroll', worker, credentials=credentials)
        write(root / ('enrollment-' + worker + '-plan.json'), {'reference': plan, 'credentials': credentials})
        return {'plan_id': plan['plan_id'], 'plan_sha256': plan['plan_sha256']}
    if action == 'enrollment-apply':
        worker = payload['worker']; saved = read_record(root / ('enrollment-' + worker + '-plan.json'))
        credentials = {slot: verify_certificate(leaf(root, identity), service['ca_file'])
            for identity, (owner, slot) in IDENTITIES.items() if owner == worker}
        if credentials != saved['credentials']: raise ValueError('fixture_candidate_changed')
        if payload.get('wrong_digest'):
            try: store.apply(saved['reference']['plan_id'], '0' * 64, lambda: verify_binding(config, service))
            except Exception: return {'refused': True}
            return {'refused': False}
        try: apply(root, store, saved['reference'], config, service)
        except Exception:
            if payload.get('expect_replay'): return {'refused': True}
            raise
        return {'refused': False} if payload.get('expect_replay') else {'enrolled': True}
    if action == 'enrollment-stage':
        identity = payload['identity']; worker, slot = IDENTITIES[identity]
        certificate = candidate(root, identity)
        plan = store.preview('stage', worker, slot=slot, credentials=certificate)
        # Verify actual public leaf again before the reviewed mutation.
        if verify_certificate(leaf(root, identity, True), service['ca_file']) != certificate:
            raise ValueError('fixture_candidate_changed')
        apply(root, store, plan, config, service)
        write(root / ('enrollment-' + identity + '-candidate.json'), certificate)
        return {'staged': True}
    if action == 'enrollment-activate':
        identity = payload['identity']; worker, slot = IDENTITIES[identity]
        certificate = verify_certificate(leaf(root, identity, True), service['ca_file'])
        if certificate != read_record(root / ('enrollment-' + identity + '-candidate.json')):
            raise ValueError('fixture_candidate_changed')
        plan = store.preview('activate', worker, slot=slot, overlap_seconds=payload['overlap_seconds'])
        apply(root, store, plan, config, service)
        return {'activated': True}
    if action == 'enrollment-retire':
        identity = payload['identity']; worker, _ = IDENTITIES[identity]
        fingerprint = verify_certificate(leaf(root, identity), service['ca_file'])['fingerprint']
        plan = store.preview('retire', worker, target_fingerprint=fingerprint)
        apply(root, store, plan, config, service)
        return {'retired': True}
    if action == 'enrollment-install':
        import docker
        identity = payload['identity']; worker, slot = IDENTITIES[identity]
        certificate = verify_certificate(leaf(root, identity, True), service['ca_file'])
        if certificate != read_record(root / ('enrollment-' + identity + '-candidate.json')):
            raise ValueError('fixture_candidate_changed')
        expected = {'role': 'guardian' if slot == 'guardian' else 'hub', 'workers': [worker],
            'courses': sorted(config['courses'][worker]) if slot == 'guardian' else [slot.split(':', 1)[1]]}
        if store.entries().get(certificate['fingerprint']) != expected:
            raise ValueError('fixture_candidate_not_active')
        request = read_record(root / 'fixture.json')['request']
        name = config['guardians'][worker]['container_name'] if slot == 'guardian' else config['courses'][worker][slot.split(':', 1)[1]]['hub_container']
        image = request['controller_image'] if slot == 'guardian' else request['hub_image']
        client = docker.DockerClient(base_url='unix:///var/run/docker.sock', version='auto', timeout=10)
        container = client.containers.get(name); container.reload()
        if slot == 'guardian':
            labels = {'educloud.runtime.authority': config['authority_id'], 'educloud.runtime.worker': worker, 'educloud.runtime.kind': 'guardian'}
        else:
            item = config['courses'][worker][slot.split(':', 1)[1]]
            labels = {'educloud.workspace.instance': item['instance'], 'educloud.workspace.kind': 'hub'}
        _diagnostic_owned(container, name, image, labels)
        container.stop(timeout=10); container.reload()
        if container.attrs['State']['Running']: raise ValueError('fixture_install_running')
        original = root / identity / 'credentials'; previous = root / (identity + '-previous') / 'credentials'
        previous.mkdir(mode=0o700, parents=True, exist_ok=False)
        for target in ('client.crt', 'client.key', 'ca.crt'):
            shutil.copyfile(original / target, previous / target); (previous / target).chmod(0o600)
        old = read_record(original / 'client.json')
        old.update(certfile=str(previous / 'client.crt'), keyfile=str(previous / 'client.key'), ca_file=str(previous / 'ca.crt'))
        write(previous / 'client.json', old)
        for target in ('client.crt', 'client.key'):
            shutil.copyfile(root / (identity + '-next') / 'credentials' / target, original / target)
            (original / target).chmod(0o600)
        container.start()
        return {'installed_and_restarted': True}
    if action == 'enrollment-guardian-probe':
        identity = payload['identity']
        base = identity.removesuffix('-next').removesuffix('-previous'); worker, slot = IDENTITIES[base]
        if slot != 'guardian': raise ValueError('fixture_guardian_identity_invalid')
        settings = read_record(root / identity / 'credentials' / 'client.json')
        settings['url'] = 'https://127.0.0.1:8443/v1/authority'
        api = AuthorityClient(worker_id=worker, course_id='course-main', **settings)
        return _guardian_probe(api, backend.guardian_identity(worker), worker, 'course-main')
    if action == 'enrollment-revoke-worker':
        plan = store.preview('revoke_worker', payload['worker']); apply(root, store, plan, config, service)
        return {'revoked': True}
    if action == 'enrollment-reenroll-refusal':
        worker = payload['worker']
        credentials = {slot: verify_certificate(leaf(root, identity), service['ca_file'])
            for identity, (owner, slot) in IDENTITIES.items() if owner == worker}
        try: store.preview('enroll', worker, credentials=credentials)
        except Exception: return {'refused': True}
        return {'refused': False}
    if action == 'enrollment-checkpoint':
        authority = Authority.open(service['authority_directory'], config['authority_id'], backend)
        proof = {'ownership': digest(authority.inventory()), 'credentials': digest(store.entries())}
        write(root / 'enrollment-checkpoint.json', proof)
        return {'checkpointed': True}
    if action == 'enrollment-checkpoint-proof':
        authority = Authority.open(service['authority_directory'], config['authority_id'], backend)
        proof = {'ownership': digest(authority.inventory()), 'credentials': digest(store.entries())}
        return {'unchanged': proof == read_record(root / 'enrollment-checkpoint.json')}
    raise ValueError('fixture_enrollment_action_invalid')


DIAGNOSTIC_CODES = frozenset({
    'worker_enrollment_already_enrolled', 'worker_enrollment_binding_changed',
    'worker_enrollment_certificate_not_current', 'worker_enrollment_certificate_reuse',
    'worker_enrollment_clock_rollback', 'worker_enrollment_plan_consumed',
    'worker_enrollment_plan_unavailable', 'worker_enrollment_quota_exceeded',
    'worker_enrollment_refused', 'worker_enrollment_rotation_pending',
    'worker_enrollment_stale_plan', 'worker_enrollment_state_unavailable',
    'worker_enrollment_target_unavailable', 'worker_enrollment_worker_unavailable',
    'worker_credentials_refused', 'fixture_enrollment_refused',
})


def dispatch(root, action, payload):
    if action == 'enrollment-failure-status':
        try: code = read_record(root / 'enrollment-failure.json')['code']
        except Exception: code = 'fixture_enrollment_refused'
        return {'code': code if isinstance(code, str) and code in DIAGNOSTIC_CODES else 'fixture_enrollment_refused'}
    try: return _dispatch(root, action, payload)
    except Exception as error:
        code = getattr(error, 'code', None)
        write(root / 'enrollment-failure.json', {'code': code if isinstance(code, str) and code in DIAGNOSTIC_CODES else 'fixture_enrollment_refused'})
        raise
