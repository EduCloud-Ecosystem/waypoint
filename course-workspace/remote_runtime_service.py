"""Opt-in socketless authority service for managed worker-local notebook runtimes."""
import argparse
from copy import deepcopy
import ipaddress
from pathlib import Path

from authority_access import AccessPolicy, AuthorityServer, ReplayStore, read_private
from authority_operator import OperatorDispatcher
from ownership_contract import Authority, OwnershipError, digest, HEX64
from remote_backend import RemoteObservationBusy
from remote_control import load_backend
from worker_credentials import _verified_public_ca, _read_pinned_ca
from worker_enrollment import EnrollmentStore

VERSION = 'educloud-remote-runtime-v1'
OPERATIONS = {'hub.authorize', 'hub.spawn', 'hub.poll', 'hub.stop', 'guardian.observe'}


def refuse():
    raise OwnershipError('remote_runtime_refused')


def validate_config(config):
    if (not isinstance(config, dict) or set(config) != {'version', 'control', 'runtime_bindings', 'service'}
            or config['version'] != VERSION or not isinstance(config['service'], dict)):
        refuse()
    backend = load_backend(config['control'])
    pins, service = config['runtime_bindings'], config['service']
    if (not isinstance(pins, dict) or set(pins) != set(backend.workers)
            or any(not isinstance(v, str) or not HEX64.fullmatch(v) for v in pins.values())
            or set(service) != {'bind', 'port', 'certfile', 'keyfile', 'ca_file', 'acl_file',
                               'replay_directory', 'enrollment_directory'}
            or not isinstance(service['bind'], str) or not 1 <= len(service['bind']) <= 253
            or type(service['port']) is not int or not 1 <= service['port'] <= 65535
            or any(not isinstance(v, str) or not Path(v).is_absolute()
                   for k, v in service.items() if k not in ('bind', 'port'))):
        refuse()
    return backend


def verify_binding(config, *, backend=None, authority=None, observe=True):
    """Bind trusted configuration/registry, optionally adding live worker proof."""
    backend = backend or validate_config(config)
    control = config['control']
    authority = authority or Authority.open(control['authority_directory'], control['authority_id'], backend)
    observations = {}
    for worker in sorted(backend.workers):
        observations[worker] = backend.inspect_worker(worker) if observe else backend.workers[worker]
        if observe and backend.binding(worker, backend.courses[worker][0]) != config['runtime_bindings'][worker]:
            refuse()
    ca, _, _ = _verified_public_ca(config['service']['ca_file'])
    workers = {}
    with authority._connection() as conn:
        conn.execute('BEGIN'); authority._validate(conn)
        for worker, evidence in observations.items():
            saved = authority._worker(conn, worker, evidence)
            courses = list(backend.courses[worker])
            for course in courses:
                authority._course(conn, course)
            workers[worker] = {'provider_id': saved['provider_id'], 'boot_id': saved['boot_id'],
                               'evidence_sha256': digest(saved), 'courses': courses}
        conn.execute('COMMIT')
    # Existing registry schema is reused. Its root-binding digest here binds
    # the aggregate of worker-proved config/home bindings, never central paths.
    return {'runtime_config_sha256': digest(config), 'ca_sha256': ca,
            'home_root_bindings_sha256': digest(config['runtime_bindings']), 'workers': workers}


def local_binding_verifier(config_path, config, binding):
    """Pure controller-local callback: never RPC while a worker awaits our reply."""
    approved = digest(config)
    ca, raw, der = _verified_public_ca(config['service']['ca_file'])
    if (approved != binding['runtime_config_sha256'] or ca != binding['ca_sha256']
            or digest(config['runtime_bindings']) != binding['home_root_bindings_sha256']):
        refuse()
    def verify(worker, course):
        if (digest(config) != approved or digest(read_private(config_path)) != approved
                or worker not in binding['workers'] or course not in binding['workers'][worker]['courses']):
            refuse()
        _read_pinned_ca(config['service']['ca_file'], ca, (raw, der))
    return verify


class RemoteRuntimeDispatcher:
    def __init__(self, authority, backend, guardian_authorize, runtime_bindings=None):
        self.authority, self.backend = authority, backend
        self.guardian_authorize = guardian_authorize
        self.runtime_bindings = deepcopy(runtime_bindings)

    def _current(self, worker, course):
        # Local SQL only. Calling any backend method here would deadlock the
        # worker's same-channel authorization challenge under its held lock.
        with self.authority._connection() as conn:
            conn.execute('BEGIN'); self.authority._validate(conn)
            self.authority._worker(conn, worker, self.backend.workers[worker])
            current = dict(self.authority._course(conn, course))
            conn.execute('COMMIT')
        return current

    @staticmethod
    def _result(operation, value, worker, course, epoch):
        if not isinstance(value, dict):
            refuse()
        if operation in ('hub.authorize', 'guardian.observe'):
            if (set(value) != {'authorized', 'worker_id', 'course_id', 'epoch'}
                    or value['authorized'] is not True or type(value['epoch']) is not int
                    or value['epoch'] != epoch or value['worker_id'] != worker or value['course_id'] != course):
                refuse()
        elif operation == 'hub.spawn':
            if (set(value) != {'epoch', 'server_id', 'address', 'port'}
                    or type(value['epoch']) is not int or value['epoch'] != epoch
                    or type(value['port']) is not int or value['port'] != 8888
                    or not isinstance(value['server_id'], str) or not HEX64.fullmatch(value['server_id'])
                    or not isinstance(value['address'], str)):
                refuse()
            ipaddress.ip_address(value['address'])
        elif operation == 'hub.stop':
            if set(value) != {'stopped'} or value['stopped'] is not True:
                refuse()
        elif (set(value) != {'running', 'exit_code'} or type(value['running']) is not bool
              or value['exit_code'] is not None and type(value['exit_code']) is not int):
            refuse()
        return value

    def _dispatch(self, principal, operation, course, payload):
        worker = principal['worker_id']
        with self.authority.admission(course, worker) as view:
            if self.runtime_bindings is not None and self.backend.binding(worker, course) != self.runtime_bindings[worker]:
                refuse()
            principal['authorize']()
            expected = self._current(worker, course)
            if expected['owner'] != worker or expected['pending'] is not None or expected['epoch'] != view['epoch']:
                refuse()
            actor = {key: principal[key] for key in ('role', 'cert_sha256', 'worker_id', 'course_id')}
            def authorize(challenge):
                if challenge == {'kind': 'principal'}:
                    guardian = None
                elif (isinstance(challenge, dict) and set(challenge) == {'kind', 'cert_sha256'}
                      and challenge['kind'] == 'guardian' and isinstance(challenge['cert_sha256'], str)
                      and HEX64.fullmatch(challenge['cert_sha256'])):
                    guardian = challenge['cert_sha256']
                else:
                    refuse()
                principal['authorize']()
                if self._current(worker, course) != expected:
                    refuse()
                if guardian is not None:
                    self.guardian_authorize(guardian, worker, course)
                principal['authorize']()
                if self._current(worker, course) != expected:
                    refuse()
            result = self.backend.runtime_v2(worker, course, operation, payload, actor, authorize)
            authorize({'kind': 'principal'})
            return self._result(operation, result, worker, course, view['epoch'])

    def dispatch(self, principal, operation, course, payload):
        if (not isinstance(principal, dict) or not isinstance(operation, str) or operation not in OPERATIONS
                or principal.get('role') != ('guardian' if operation == 'guardian.observe' else 'hub')
                or principal.get('course_id') != course or not callable(principal.get('authorize'))
                or not isinstance(principal.get('cert_sha256'), str) or not HEX64.fullmatch(principal['cert_sha256'])
                or not isinstance(payload, dict)):
            refuse()
        worker = principal.get('worker_id')
        self.backend._scope(worker, course)
        principal['authorize']()
        if operation != 'guardian.observe':
            return self._dispatch(principal, operation, course, payload)
        if (set(payload) != {'guardian_container_id', 'start_identity'}
                or any(not isinstance(v, str) or not HEX64.fullmatch(v) for v in payload.values())):
            refuse()
        with self.backend.guardian_observation_budget() as token:
            try:
                return self._dispatch(principal, operation, course, payload)
            except RemoteObservationBusy as error:
                if error.token is not token or error.endpoint != (worker, course):
                    raise
                principal['authorize']()
                return {'authorized': False, 'deferred': True, 'worker_id': worker, 'course_id': course}


def serve(config_path):
    config = deepcopy(read_private(config_path))
    backend = validate_config(config)
    control, service = config['control'], config['service']
    authority = Authority.open(control['authority_directory'], control['authority_id'], backend)
    binding = verify_binding(config, backend=backend, authority=authority, observe=False)
    enrollment = EnrollmentStore.open(service['enrollment_directory'], backend.authority_id, binding)
    verify = local_binding_verifier(config_path, config, binding)
    policy = AccessPolicy(service['acl_file'], backend.authority_id, enrollment=enrollment, binding_verifier=verify)
    runtime = RemoteRuntimeDispatcher(authority, backend,
        lambda fingerprint, worker, course: policy.authorize_fingerprint(fingerprint, 'guardian', worker, course),
        config['runtime_bindings'])
    operator = OperatorDispatcher(authority, control['plan_directory'])
    def dispatch(principal, operation, course, payload):
        original = principal['authorize']
        def authorize():
            verify(principal['worker_id'], course)
            original()
        bound = dict(principal, authorize=authorize)
        authorize()
        return (operator if operation.startswith('ownership.') else runtime).dispatch(bound, operation, course, payload)
    server = AuthorityServer((service['bind'], service['port']), authority_id=backend.authority_id,
        certfile=service['certfile'], keyfile=service['keyfile'], ca_file=service['ca_file'],
        acl_file=service['acl_file'], replay_store=ReplayStore.open(service['replay_directory'], backend.authority_id),
        dispatch=dispatch, access_policy=policy)
    try:
        server.serve_forever(poll_interval=.1)
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    try:
        serve(args.config)
    except (Exception, KeyboardInterrupt):
        raise SystemExit('remote_runtime_service_failed') from None


if __name__ == '__main__':
    main()
