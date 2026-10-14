"""Private disposable enrollment controls; never a production provisioner/issuer."""
import hashlib
from pathlib import Path
import shutil

from authenticated_fixture import write, _diagnostic_owned
from authority_access import AccessPolicy
from authority_test_pki import TestPKI
from hub.authority_client import AuthorityClient, AuthorityError
from ownership_contract import Authority, digest
from runtime_authority import DockerBackend, read_record
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


def _dispatch(root, action, payload):
    if action == 'enrollment-create':
        config = read_record(root / 'runtime.json'); service = read_record(root / 'service.json')
        directory = root / 'control' / 'enrollment'
        EnrollmentStore.create(directory, config['authority_id'], verify_binding(config, service))
        service['enrollment_directory'] = str(directory); write(root / 'service.json', service)
        return {'created': True}
    config, service, backend, store = context(root)
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
        try: api.call('guardian.observe', backend.guardian_identity(worker))
        except AuthorityError: return {'authorized': False}
        return {'authorized': True}
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
