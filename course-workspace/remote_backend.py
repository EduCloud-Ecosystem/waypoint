"""Opt-in remote ownership backend. Workers retain their own locks and barriers.

The immutable ownership core continues to label its decisions as fixture-only.
This adapter supplies authenticated channels, not cloud identity attestation.
No file contents, host paths, Docker clients or reusable activation grants cross
this interface. Interrupted completion stays quarantined until exact reviewed
apply/resume succeeds. There are deliberately no automatic retries.
"""
from contextlib import contextmanager, ExitStack
import threading

from ownership_contract import (OwnershipError, course_evidence, digest,
                                worker_evidence, HEX32, HEX64, slug)


class RemoteError(OwnershipError):
    pass


class RemoteBackend:
    fixture_only = True
    scope = 'course'

    def __init__(self, authority_id, workers, clients, courses):
        if (not isinstance(authority_id, str) or not HEX32.fullmatch(authority_id)
                or not isinstance(workers, dict) or not 1 <= len(workers) <= 16
                or set(workers) != set(clients) or set(workers) != set(courses)):
            raise RemoteError('remote_configuration_invalid')
        self.authority_id = authority_id
        self.workers, self.courses, self.clients = {}, {}, dict(clients)
        for worker, evidence in workers.items():
            self.workers[worker] = dict(worker_evidence(evidence, worker))
            values = courses[worker]
            if (not isinstance(values, list) or not 1 <= len(values) <= 16
                    or len(set(values)) != len(values) or any(not slug(v) for v in values)
                    or clients[worker].worker_id != worker
                    or clients[worker].authority_id != authority_id):
                raise RemoteError('remote_configuration_invalid')
            self.courses[worker] = tuple(sorted(values))
        self.local = threading.local()

    def _scope(self, worker, course):
        if not slug(worker) or not slug(course) or worker not in self.workers or course not in self.courses[worker]:
            raise RemoteError('remote_endpoint_unknown')

    def _sessions(self):
        return getattr(self.local, 'sessions', {})

    @contextmanager
    def _channel(self, worker, course):
        self._scope(worker, course)
        active = self._sessions().get((worker, course))
        if active is not None:
            yield active
        else:
            with self.clients[worker].session(course, 'inspect') as channel:
                yield channel

    def inspect_worker(self, worker):
        if worker not in self.workers:
            raise RemoteError('remote_endpoint_unknown')
        course = next((c for w, c in self._sessions() if w == worker), self.courses[worker][0])
        with self._channel(worker, course) as channel:
            evidence = worker_evidence(channel.call('worker'), worker)
        if evidence != self.workers[worker]:
            raise RemoteError('remote_worker_binding_changed')
        return dict(evidence)

    def inspect_course(self, course, worker):
        with self._channel(worker, course) as channel:
            # Every remote read is a fresh observation on its authenticated
            # worker; an observation failure is never an empty/stopped course.
            return dict(course_evidence(channel.call('course')))

    def quarantine(self, worker, course):
        with self._channel(worker, course) as channel:
            value = channel.call('state')
        if (not isinstance(value, dict) or set(value) != {'quarantined'}
                or type(value['quarantined']) is not bool):
            raise RemoteError('remote_quarantine_observation_invalid')
        return value['quarantined']

    def _request(self, request):
        if not isinstance(request, dict) or request.get('authority_id') != self.authority_id:
            raise RemoteError('remote_authority_binding_changed')
        if 'worker_id' in request:
            worker = request['worker_id']
            self._scope(worker, request.get('course_id'))
            expected = self.workers[worker]
            if (request.get('provider_id'), request.get('boot_id')) != (expected['provider_id'], expected['boot_id']):
                raise RemoteError('remote_worker_binding_changed')
            return [worker]
        workers = [request.get('source_worker_id'), request.get('receiver_worker_id')]
        if workers[0] == workers[1]:
            raise RemoteError('remote_transfer_scope_invalid')
        for prefix, worker in zip(('source_', 'receiver_'), workers):
            self._scope(worker, request.get('course_id'))
            expected = self.workers[worker]
            if (request.get(prefix + 'provider_id'), request.get(prefix + 'boot_id')) != (expected['provider_id'], expected['boot_id']):
                raise RemoteError('remote_worker_binding_changed')
        return workers

    @contextmanager
    def activation_context(self, request):
        workers = self._request(request)
        if len(workers) != 1 or self._sessions():
            raise RemoteError('remote_activation_context_invalid')
        worker, course = workers[0], request['course_id']
        with self.clients[worker].session(course, 'activate', request) as channel:
            self.local.sessions = {(worker, course): channel}
            self.local.activation = (worker, course)
            try:
                self.inspect_worker(worker)
                yield
            finally:
                self.local.activation = None
                self.local.sessions = {}

    def runtime(self, worker, course, operation, payload):
        self._scope(worker, course)
        if (getattr(self.local, 'activation', None) != (worker, course)
                or operation not in {'hub.authorize', 'hub.spawn', 'hub.poll', 'hub.stop', 'guardian.observe'}
                or not isinstance(payload, dict)):
            raise RemoteError('remote_runtime_requires_live_admission')
        return self._sessions()[(worker, course)].call('runtime', {'operation': operation, 'payload': payload})

    @contextmanager
    def _fencing(self, request, authorize, reconciliation):
        workers = self._request(request)
        if len(workers) != 2 or self._sessions() or not callable(authorize):
            raise RemoteError('remote_fence_context_invalid')
        course = request['course_id']
        view = None
        with ExitStack() as stack:
            sessions = {}
            for worker in sorted(workers):
                sessions[(worker, course)] = stack.enter_context(
                    self.clients[worker].session(course, 'exclusive', request))
            self.local.sessions = sessions
            try:
                authorize()  # Core reads now use these exact held channels.
                if not reconciliation and self.inspect_course(course, workers[1])['running_count']:
                    raise RemoteError('remote_receiver_must_be_stopped')
                # Both endpoints receive independent durable floors. If a
                # channel dies during/after a core write, its quarantine cannot
                # expire into admission, even when its process/host restarts.
                for worker in workers:
                    authorize()
                    sessions[(worker, course)].call('fence', {
                        'request': request, 'reconciliation': reconciliation})
                view = RemoteFenceView(self, request, workers, reconciliation)
                yield view
                view.inspect()
                # This is after core COMPLETE, not an inferred timeout or an
                # unconditional context cleanup. Lost replies require exact
                # apply/resume, including when SQLite is already COMPLETE.
                for worker in workers:
                    acknowledgment = sessions[(worker, course)].finish()
                    if (not isinstance(acknowledgment, dict) or set(acknowledgment) != {'finished'}
                            or acknowledgment['finished'] is not True):
                        raise RemoteError('remote_completion_unverified')
            finally:
                if view is not None:
                    view.active = False
                self.local.sessions = {}

    def fence_context(self, request, *, authorize):
        return self._fencing(request, authorize, False)

    def reconciliation_context(self, request, *, authorize):
        return self._fencing(request, authorize, True)


class RemoteFenceView:
    def __init__(self, backend, request, workers, reconciliation):
        self.backend, self.request, self.workers = backend, request, workers
        self.reconciliation, self.active = reconciliation, True

    def inspect(self):
        if not self.active:
            raise RemoteError('remote_fence_context_expired')
        result = {}
        for worker in self.workers:
            self.backend.inspect_worker(worker)
            channel = self.backend._sessions()[(worker, self.request['course_id'])]
            acknowledgment = channel.call('check')
            if (not isinstance(acknowledgment, dict) or set(acknowledgment) != {'locked'}
                    or acknowledgment['locked'] is not True):
                raise RemoteError('remote_endpoint_exclusion_unverified')
            proof = channel.call('proof')
            if not isinstance(proof, dict) or set(proof) != {'barrier', 'course'}:
                raise RemoteError('remote_fence_proof_invalid')
            barrier, course = proof['barrier'], course_evidence(proof['course'])
            expected = {'authority_id', 'worker_id', 'course_id', 'barrier_id',
                        'denied_through_epoch', 'request_sha256'}
            if (not isinstance(barrier, dict) or set(barrier) != expected
                    or barrier['authority_id'] != self.backend.authority_id
                    or barrier['worker_id'] != worker or barrier['course_id'] != self.request['course_id']
                    or not isinstance(barrier['barrier_id'], str) or not HEX32.fullmatch(barrier['barrier_id'])
                    or not isinstance(barrier['request_sha256'], str) or not HEX64.fullmatch(barrier['request_sha256'])
                    or barrier['request_sha256'] != digest(self.request)
                    or type(barrier['denied_through_epoch']) is not int
                    or not self.request['old_epoch'] <= barrier['denied_through_epoch'] <= 10**9
                    or self.reconciliation and barrier['denied_through_epoch'] != self.request['old_epoch']
                    or course['running_count'] != 0):
                raise RemoteError('remote_fence_proof_invalid')
            result[worker] = {'barrier_id': barrier['barrier_id'], 'barrier_sha256': digest(barrier),
                'denied_through_epoch': barrier['denied_through_epoch'], 'observed_running_count': 0}
        if self.reconciliation:
            return {'request_sha256': digest(self.request), 'scope': 'course', 'endpoints': result}
        return dict(result[self.workers[0]], request_sha256=digest(self.request), scope='course')
