"""Worker-local notebook effects and enforcement; no central authority database.

Only the worker channel can create the thread-bound held-admission capability.
Every effect uses fresh controller authorization; local safety cleanup does not.
"""
from contextlib import contextmanager
import json
import logging
import sqlite3
import threading
import time

from runtime_authority import RuntimeDispatcher, RuntimeRefusal, HASH, file_hash, read_record
from worker_transport import (WorkerServer, WorkerJournal, LinuxWorkerBackend, WorkerError,
                              runtime_capability, worker_binding, encoded, ERRORS as WORKER_ERRORS)
from worker_channel_state import JournalError


SUPERVISOR_RUNTIME_CODES = frozenset({
    'runtime_endpoint_enforcement_unavailable', 'runtime_endpoint_lock_busy',
    'runtime_daemon_changed', 'runtime_database_binding_changed',
    'runtime_database_durability_invalid', 'runtime_database_journal_changed',
    'runtime_private_state_invalid', 'runtime_record_bounds',
    'runtime_record_duplicate', 'runtime_record_nonfinite',
    'runtime_container_bounds', 'runtime_container_scope_invalid',
    'runtime_worker_incarnation_changed', 'runtime_worker_unknown',
    'runtime_home_root_changed', 'runtime_home_root_binding_invalid',
    'runtime_quiescence_unverified', 'runtime_lock_invalid'})


def supervisor_failure_code(error):
    """Fixed diagnostics only: exception messages/arguments are never observed."""
    if isinstance(error, WorkerError):
        code = getattr(error, 'code', None)
        return code if type(code) is str and code in WORKER_ERRORS else 'worker_error'
    if isinstance(error, RuntimeRefusal):
        code = getattr(error, 'code', None)
        return code if type(code) is str and code in SUPERVISOR_RUNTIME_CODES else 'runtime_refusal'
    for kind, category in ((JournalError, 'worker_journal_refused'), (sqlite3.Error, 'sqlite_error'),
            (TimeoutError, 'timeout_error'), (OSError, 'os_error'), (KeyError, 'key_error'),
            (TypeError, 'type_error'), (ValueError, 'value_error')):
        if isinstance(error, kind): return category
    # SDK exception names are compared, never emitted from untrusted classes.
    return {('docker.errors', 'NotFound'): 'docker_not_found',
            ('docker.errors', 'APIError'): 'docker_api_error',
            ('docker.errors', 'DockerException'): 'docker_error',
            ('requests.exceptions', 'ReadTimeout'): 'docker_transport_timeout',
            ('requests.exceptions', 'ConnectionError'): 'docker_connection_error'}.get(
                (type(error).__module__, type(error).__name__), 'other_exception')


class _HeldAuthority:
    def __init__(self, runtime, capability): self.runtime, self.capability = runtime, capability

    @contextmanager
    def admission(self, course, worker):
        request = self.capability.request
        if (worker, course) != (request['worker_id'], request['course_id']):
            raise WorkerError('worker_authorization_refused')
        self.capability.check(self.runtime.backend)
        self.runtime._local(request)
        yield {'epoch': request['epoch'], 'worker_id': worker, 'course_id': course}
        self.capability.check(self.runtime.backend)


class _HeldDispatcher(RuntimeDispatcher):
    def __init__(self, authority, backend, guardian_authorize, capability):
        super().__init__(authority, backend, guardian_authorize)
        self.capability = capability

    def server(self, container, epoch):
        result = super().server(container, epoch)
        # Keep expiry after slow Docker inspection inside the existing
        # CREATE/START compensation block, rather than admission's outer exit.
        self.capability.check(self.backend)
        return result


class WorkerRuntime:
    def __init__(self, backend, journal, worker_id, *, monotonic=time.monotonic):
        if (set(backend.config['workers']) != {worker_id} or journal.worker_id != worker_id
                or journal.authority_id != backend.authority_id
                or set(journal.courses) != set(backend.config['courses'][worker_id])
                or journal.binding_sha256 != worker_binding(backend, worker_id)):
            raise WorkerError('worker_identity_changed')
        self.backend, self.journal, self.worker = backend, journal, worker_id
        self.monotonic, self.busy_since, self.failed = monotonic, {}, threading.Event()
        self.runtime_hashes = {course: file_hash(item['runtime_path'])
            for course, item in backend.config['courses'][worker_id].items()}

    def _local(self, request):
        if self.failed.is_set(): raise WorkerError('worker_session_closed')
        if (request['worker_id'] != self.worker or request['authority_id'] != self.backend.authority_id
                or self.journal.binding_sha256 != worker_binding(self.backend, self.worker)):
            raise WorkerError('worker_identity_changed')
        course = request['course_id']; self.backend.validate_request(request, [self.worker])
        if self.journal.state(course)['quarantined']: raise WorkerError('worker_quarantined')
        if self.backend.barrier(self.worker, course)['denied_through_epoch'] >= request['epoch']:
            raise RuntimeRefusal('runtime_epoch_fenced')
        if file_hash(self.backend.course(self.worker, course)['runtime_path']) != self.runtime_hashes[course]:
            raise WorkerError('worker_identity_changed')
        if self.failed.is_set(): raise WorkerError('worker_session_closed')

    def dispatch(self, request, operation, payload, principal, authorize, guardian_authorize):
        capability = runtime_capability(authorize, guardian_authorize, self.backend, request, principal)
        if (principal.get('worker_id') != self.worker or principal.get('course_id') != request['course_id']
                or principal.get('role') != ('guardian' if operation == 'guardian.observe' else 'hub')
                or not isinstance(principal.get('cert_sha256'), str) or not HASH.fullmatch(principal['cert_sha256'])):
            raise WorkerError('worker_authorization_refused')
        def fresh_principal():
            capability.check(self.backend); self._local(request); authorize(); self._local(request); capability.check(self.backend)
        def fresh_guardian(fingerprint, worker, course):
            capability.check(self.backend); self._local(request); guardian_authorize(fingerprint, worker, course)
            self._local(request); capability.check(self.backend)
        authenticated = dict(principal, authorize=fresh_principal)
        dispatcher = _HeldDispatcher(_HeldAuthority(self, capability), self.backend, fresh_guardian, capability)
        if operation == 'guardian.observe':
            if (not isinstance(payload, dict) or set(payload) != {'guardian_container_id', 'start_identity'}
                    or any(not isinstance(value, str) or not HASH.fullmatch(value) for value in payload.values())):
                raise RuntimeRefusal('runtime_payload_invalid')
            # WorkerServer already acquired the short observation context and
            # the physical activation lock. Skip ONLY reacquiring that budget.
            with dispatcher.authority.admission(request['course_id'], self.worker):
                fresh_principal()
                def heartbeat_effect_guard():
                    capability.check(self.backend)
                    if self.failed.is_set(): raise WorkerError('worker_session_closed')
                self.backend.guardian_heartbeat(self.worker, request['course_id'], request['epoch'], payload,
                    principal['cert_sha256'], authorize=fresh_principal, effect_guard=heartbeat_effect_guard)
                return {'authorized': True, 'epoch': request['epoch'], 'worker_id': self.worker, 'course_id': request['course_id']}
        return dispatcher.dispatch(authenticated, operation, request['course_id'], payload)

    def _supervise(self, course, *, wait_seconds=.1):
        with self.backend.locks([(self.worker, course)], wait_seconds=wait_seconds):
            if self.journal.binding_sha256 != worker_binding(self.backend, self.worker):
                raise WorkerError('worker_identity_changed')
            quarantined = self.journal.state(course)['quarantined']
            containers = self.backend.containers(self.worker, course)
            if not containers:
                # This proves only empty physical state, never authorizes a
                # learner or renews a heartbeat. Binding/journal remain fresh.
                return
            valid = False
            try:
                with self.backend.connection() as conn:
                    conn.execute('BEGIN'); self.backend._validate(conn)
                    row = conn.execute('SELECT record FROM guardians WHERE endpoint=?',
                        (self.backend.endpoint(self.worker, course),)).fetchone(); conn.execute('COMMIT')
                record = json.loads(row['record']) if row is not None else {}
                epoch = record.get('epoch')
                if quarantined or type(epoch) is not int or not 1 <= epoch <= 10**9:
                    raise RuntimeRefusal('guardian_unavailable')
                heartbeat = self.backend.require_guardian(self.worker, course, epoch)
                if self.backend.barrier(self.worker, course)['denied_through_epoch'] >= epoch:
                    raise RuntimeRefusal('runtime_epoch_fenced')
                if file_hash(self.backend.course(self.worker, course)['runtime_path']) != self.runtime_hashes[course]:
                    raise WorkerError('worker_identity_changed')
                for container in containers:
                    if self.backend.verified_container(container, self.worker, course, check_policy=True)[1] != epoch:
                        raise RuntimeRefusal('runtime_cached_epoch_invalid')
                self.backend.guardian_deadline(self.worker, course, epoch, heartbeat); valid = True
            except (RuntimeRefusal, WorkerError): pass
            if not valid:
                logging.getLogger(__name__).warning('Worker runtime stopping owned learners: stale_or_changed')
                self.backend._stop_observed(self.worker, course, containers)

    def supervision_tick(self):
        if self.failed.is_set(): raise WorkerError('worker_session_closed')
        try:
            overdue = False
            for course in self.backend.config['courses'][self.worker]:
                first = self.busy_since.get(course)
                age = self.monotonic() - first if first is not None else 0
                wait = min(1, 10 - age) if .5 <= age < 10 else .1
                try: self._supervise(course, wait_seconds=wait)
                except RuntimeRefusal as error:
                    if error.code != 'runtime_endpoint_lock_busy': raise
                    since = self.busy_since.setdefault(course, self.monotonic())
                    overdue = overdue or self.monotonic() - since >= 10
                else: self.busy_since.pop(course, None)
            if overdue: raise RuntimeRefusal('runtime_endpoint_enforcement_unavailable')
        except Exception:
            self.failed.set(); raise

    def stop_owned(self):
        self.failed.set()
        for course in self.backend.config['courses'][self.worker]:
            with self.backend.locks([(self.worker, course)]): self.backend.stop_endpoint(self.worker, course)


def serve(config, journal_directory, *, address, certfile, keyfile, ca_file, acl_file):
    if len(config['workers']) != 1: raise WorkerError('worker_channel_refused')
    worker = next(iter(config['workers'])); backend = LinuxWorkerBackend(config)
    journal = WorkerJournal.open(journal_directory, backend.authority_id, worker,
        list(config['courses'][worker]), worker_binding(backend, worker))
    runtime = WorkerRuntime(backend, journal, worker)
    server = WorkerServer(address, backend, worker, certfile, keyfile, ca_file, acl_file,
        journal=journal, runtime_v2_dispatch=runtime.dispatch)
    stopped = threading.Event(); failures = []
    def supervise():
        while not stopped.is_set():
            try: runtime.supervision_tick()
            except Exception as error:
                failures.append(True); runtime.failed.set()
                try:
                    logging.getLogger(__name__).error('Worker runtime supervisor failed: supervision/%s', supervisor_failure_code(error))
                except Exception:
                    pass  # Diagnostic failures never bypass shutdown or cleanup.
                finally: server.shutdown()
                return
            stopped.wait(.5)
    monitor = threading.Thread(target=supervise, daemon=True)
    try:
        monitor.start(); server.serve_forever(poll_interval=.1)
    finally:
        runtime.failed.set(); stopped.set(); server.server_close()
        monitor.join(timeout=10); runtime.stop_owned()
    if failures or monitor.is_alive(): raise WorkerError('worker_channel_refused')


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Existing single-worker notebook agent; never bootstrap state.')
    for name in ('config', 'journal', 'certfile', 'keyfile', 'ca-file', 'acl-file'): parser.add_argument('--' + name, required=True)
    parser.add_argument('--host', default='127.0.0.1'); parser.add_argument('--port', type=int, default=8444)
    args = parser.parse_args()
    serve(read_record(args.config), args.journal, address=(args.host, args.port), certfile=args.certfile,
        keyfile=args.keyfile, ca_file=args.ca_file, acl_file=args.acl_file)


if __name__ == '__main__':
    try: main()
    except (Exception, KeyboardInterrupt): raise SystemExit('worker_runtime_service_failed') from None
