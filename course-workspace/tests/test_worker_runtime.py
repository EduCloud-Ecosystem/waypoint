"""Worker runtime uses real TLS/SQLite/flocks and injected Docker, no local core DB."""
from copy import deepcopy
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from authority_test_pki import TestPKI
from test_runtime_authority import RuntimeFixture
from runtime_authority import DockerBackend, RuntimeRefusal, RuntimeDispatcher
from worker_channel_state import WorkerJournal, encoded
from worker_transport import WorkerServer, WorkerClient, WorkerError, worker_binding, VERSION
from worker_runtime import WorkerRuntime
import worker_runtime


class WorkerRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(); cls.pki = TestPKI(Path(cls.temporary.name).resolve() / 'pki')
        cls.cert, cls.key = cls.pki.issue('controller')
    @classmethod
    def tearDownClass(cls): cls.temporary.cleanup()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup); self.root = Path(temporary.name).resolve()
        base = self.root / 'source-fixture'; base.mkdir(mode=0o700); self.fixture = RuntimeFixture(base)
        self.worker = 'worker-one'; self.config = deepcopy(self.fixture.config)
        for key in ('workers', 'courses', 'guardians'): self.config[key] = {self.worker: self.config[key][self.worker]}
        self.config['backend_directory'] = str(self.root / 'worker-backend')
        self.backend = DockerBackend(self.config, self.fixture.client, initialize=True, clock=lambda: self.fixture.now)
        self.journal = WorkerJournal.create(self.root / 'journal', self.backend.authority_id, self.worker,
            list(self.config['courses'][self.worker]), worker_binding(self.backend, self.worker))
        self.runtime = WorkerRuntime(self.backend, self.journal, self.worker)
        self.acl = self.root / 'channel.json'
        self.acl.write_bytes(encoded({'version': VERSION, 'authority_id': self.backend.authority_id, 'worker_id': self.worker,
            'controllers': {self.pki.fingerprint(self.cert): ['course-main', 'course-peer']}})); self.acl.chmod(0o600)
        self.callback = None; self.revoked = set(); self.observations = []
        self.server = WorkerServer(('127.0.0.1', 0), self.backend, self.worker, self.pki.server_cert, self.pki.server_key,
            self.pki.ca_cert, self.acl, journal=self.journal, runtime_v2_dispatch=self.runtime.dispatch)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02}, daemon=True); self.thread.start()
        self.addCleanup(self.close)

    def close(self): self.server.shutdown(); self.server.server_close(); self.thread.join(2)
    def client(self):
        return WorkerClient('localhost', self.server.server_address[1], self.backend.authority_id, self.worker,
            self.pki.fingerprint(self.pki.server_cert), self.pki.ca_cert, self.cert, self.key)
    def request(self, course='course-main', epoch=1):
        worker = self.config['workers'][self.worker]
        return {'authority_id': self.backend.authority_id, 'worker_id': self.worker, 'course_id': course,
            'provider_id': worker['provider_id'], 'boot_id': worker['boot_id'], 'epoch': epoch}
    def principal(self, course, guardian=False):
        return {'role': 'guardian' if guardian else 'hub', 'cert_sha256': ('e' if guardian else 'a') * 64,
            'worker_id': self.worker, 'course_id': course}
    def authorize(self, principal, observation):
        self.observations.append(observation.copy())
        if self.callback: self.callback(principal, observation)
        fingerprint = principal['cert_sha256'] if observation['kind'] == 'principal' else observation['cert_sha256']
        if fingerprint in self.revoked: raise ValueError('PRIVATE_TEST_REVOCATION')
    def rpc(self, operation, payload=None, course='course-main', epoch=1):
        guardian = operation == 'guardian.observe'; principal = self.principal(course, guardian)
        try:
            with self.client().session(course, 'observe' if guardian else 'activate', self.request(course, epoch)) as channel:
                return channel.call('runtime_v2', {'operation': operation, 'payload': payload or {}, 'principal': principal},
                    authorize=lambda observation: self.authorize(principal, observation))
        except WorkerError:
            # Denial is sent immediately; deterministic assertions below wait
            # for the worker's existing exact-owned compensation to finish.
            with self.server._guard: handlers = list(self.server._handler_threads)
            for handler in handlers: handler.join(3)
            raise
    def heartbeat(self, course='course-main', epoch=1):
        return self.rpc('guardian.observe', self.backend.guardian_identity(self.worker), course, epoch)
    def spawn(self, course='course-main', epoch=1, subject='alice'):
        return self.rpc('hub.spawn', {'subject': subject, 'environment': self.fixture.env(subject, course=course), 'expected_epoch': epoch}, course, epoch)
    def guardian_row(self, course='course-main'):
        with self.backend.connection() as conn:
            row = conn.execute('SELECT record FROM guardians WHERE endpoint=?', (self.backend.endpoint(self.worker, course),)).fetchone()
        return None if row is None else json.loads(row['record'])
    def refuses(self, callback, code=None):
        with self.assertRaises(WorkerError) as caught: callback()
        if code: self.assertEqual(caught.exception.code, code)

    def test_spawn_poll_stop_reuses_policy_under_held_worker_lock(self):
        self.heartbeat(); result = self.spawn()
        container = self.fixture.client.containers.get(result['server_id'])
        self.assertTrue(container.attrs['State']['Running'])
        self.assertEqual(container.attrs['HostConfig']['Memory'], 512 * 1024**2)
        self.assertEqual(container.attrs['HostConfig']['CpuQuota'], 25000)
        self.assertNotIn('DockerSocket', container.attrs['HostConfig'])
        self.assertEqual(self.rpc('hub.poll', dict(subject='alice', server_id=result['server_id'], epoch=1)), {'running': True, 'exit_code': None})
        self.assertEqual(self.rpc('hub.stop', dict(subject='alice', server_id=result['server_id'], epoch=1)), {'stopped': True})
        self.assertNotIn(result['server_id'], self.fixture.client.containers.items)
        self.assertTrue(any(item['kind'] == 'guardian' for item in self.observations))

    def test_worker_runtime_has_no_local_authority_database(self):
        self.heartbeat(); self.spawn()
        self.assertFalse((self.root / 'authority').exists())
        self.assertFalse(hasattr(self.runtime, 'authority'))

    def test_local_proof_uses_one_fresh_worker_observation(self):
        with patch.object(self.backend, 'inspect_worker', wraps=self.backend.inspect_worker) as inspect:
            self.runtime._local(self.request())
            self.assertEqual(inspect.call_count, 1)
            self.runtime._local(self.request())
            self.assertEqual(inspect.call_count, 2)  # No reuse across proofs.

    def test_local_proof_still_binds_request_incarnation(self):
        for key in ('provider_id', 'boot_id'):
            request = self.request(); request[key] = uuid.uuid4().hex
            with self.subTest(key=key), self.assertRaises(RuntimeRefusal) as denied:
                self.runtime._local(request)
            self.assertEqual(denied.exception.code, 'runtime_worker_incarnation_changed')

    def test_changed_fresh_worker_proof_after_callback_prevents_start(self):
        self.heartbeat(); changed = []; inspect = self.backend.inspect_worker
        def observation(worker):
            value = inspect(worker)
            if changed: value['boot_id'] = 'f' * 32
            return value
        def authorize(principal, observation):
            if observation['kind'] == 'principal' and any(
                    c.attrs['Config']['Labels'].get('educloud.workspace.kind') == 'learner'
                    for c in self.fixture.client.containers.items.values()):
                changed.append(True)
        self.callback = authorize
        with patch.object(self.backend, 'inspect_worker', side_effect=observation):
            self.refuses(self.spawn)
        self.assertTrue(changed)
        created = self.fixture.client.containers.created[-1][0]
        self.assertNotIn('start', created.actions)
        self.assertEqual(self.backend.containers(self.worker, 'course-main'), [])

    def test_local_observation_reuse_preserves_outer_guardian_three_second_budget(self):
        from authority_access import AuthorityServer, ReplayStore, publish
        from hub.authority_client import AuthorityClient, VERSION as ACL_VERSION
        directory = self.root / 'central'; directory.mkdir(mode=0o700)
        acl = directory / 'acl.json'
        publish(acl, {'version': ACL_VERSION, 'authority_id': self.backend.authority_id, 'clients': {
            self.pki.fingerprint(self.cert): {'role': 'guardian', 'workers': [self.worker], 'courses': ['course-main']}}})
        completed = threading.Event()
        def dispatch(principal, operation, course, payload):
            try: return self.rpc(operation, payload, course)
            finally: completed.set()
        central = AuthorityServer(('127.0.0.1', 0), authority_id=self.backend.authority_id,
            certfile=self.pki.server_cert, keyfile=self.pki.server_key, ca_file=self.pki.ca_cert,
            acl_file=acl, replay_store=ReplayStore.create(directory / 'replay', self.backend.authority_id), dispatch=dispatch)
        serving = threading.Thread(target=central.serve_forever, kwargs={'poll_interval': .02}, daemon=True); serving.start()
        def close(): central.shutdown(); central.server_close(); serving.join(2)
        self.addCleanup(close)
        client = AuthorityClient('https://localhost:' + str(central.server_port) + '/v1/authority',
            self.backend.authority_id, self.pki.ca_cert, self.cert, self.key, self.worker, 'course-main')
        self.assertEqual(client.timeout, 3)
        identity = self.backend.guardian_identity(self.worker); inspect = self.fixture.client.info; calls = []
        def bounded_info():
            calls.append(True); time.sleep(.14); return inspect()
        with patch.object(self.fixture.client, 'info', side_effect=bounded_info):
            try: result = client.call('guardian.observe', identity)
            finally: self.assertTrue(completed.wait(4))
        self.assertIs(result['authorized'], True)
        self.assertEqual(len(calls), 19)  # Previously24 calls: outer timeout while inner completed.

    def test_legal_slow_docker_stop_completes_on_same_authenticated_channel(self):
        self.heartbeat(); result = self.spawn()
        container = self.fixture.client.containers.get(result['server_id'])
        original = container.stop
        def slow_stop(timeout):
            self.assertEqual(timeout, 5)
            time.sleep(3.2)
            original(timeout)
        container.stop = slow_stop
        self.assertEqual(self.rpc('hub.stop', dict(subject='alice', server_id=result['server_id'], epoch=1)),
            {'stopped': True})
        self.assertNotIn(result['server_id'], self.fixture.client.containers.items)

    def test_peer_expiry_during_legal_slow_stop_refuses_removal(self):
        self.heartbeat(); result = self.spawn()
        container = self.fixture.client.containers.get(result['server_id'])
        original = container.stop; wall = [time.time()]
        def slow_stop(timeout):
            self.assertEqual(timeout, 5)
            time.sleep(3.2)
            original(timeout)
            wall[0] += 3 * 86400
        container.stop = slow_stop
        with patch('worker_transport.time.time', side_effect=lambda: wall[0]):
            self.refuses(lambda: self.rpc('hub.stop', dict(subject='alice', server_id=result['server_id'], epoch=1)),
                'worker_peer_validity_refused')
        self.assertIs(self.fixture.client.containers.items[result['server_id']], container)
        self.assertFalse(container.attrs['State']['Running'])
        self.assertNotIn(('remove', False, False), container.actions)
        self.assertEqual(container.actions.count(('stop', 5)), 1)

    def test_stop_response_grace_never_extends_absolute_session_deadline(self):
        self.heartbeat(); result = self.spawn()
        container = self.fixture.client.containers.get(result['server_id'])
        original = container.stop
        def slow_stop(timeout):
            time.sleep(.35)
            original(timeout)
        container.stop = slow_stop
        self.server.session_seconds = .15
        self.refuses(lambda: self.rpc('hub.stop', dict(subject='alice', server_id=result['server_id'], epoch=1)),
            'worker_session_expired')
        self.assertIs(self.fixture.client.containers.items[result['server_id']], container)
        self.assertFalse(container.attrs['State']['Running'])
        self.assertNotIn(('remove', False, False), container.actions)
        self.assertEqual(container.actions.count(('stop', 5)), 1)

    def test_non_stop_runtime_retains_ordinary_response_timeout(self):
        original = self.server.runtime_v2_dispatch
        def delayed(request, operation, payload, principal, authorize, guardian_authorize):
            self.assertEqual(operation, 'hub.authorize')
            time.sleep(3.2)
            return original(request, operation, payload, principal, authorize, guardian_authorize)
        self.server.runtime_v2_dispatch = delayed
        self.refuses(lambda: self.rpc('hub.authorize'), 'worker_transport_unavailable')
        self.assertEqual(self.observations, [])
        self.assertEqual(self.backend.containers(self.worker, 'course-main'), [])

    def test_replacement_spawn_allows_existing_legal_docker_stop_grace(self):
        self.heartbeat(); first = self.spawn()
        container = self.fixture.client.containers.get(first['server_id'])
        original = container.stop
        def slow_stop(timeout):
            self.assertEqual(timeout, 5)
            time.sleep(3.2)
            original(timeout)
        container.stop = slow_stop
        environment = self.fixture.env('alice'); environment['JUPYTERHUB_API_TOKEN'] = 'replacement-token'
        result = self.rpc('hub.spawn', {'subject': 'alice', 'environment': environment, 'expected_epoch': 1})
        self.assertNotEqual(first['server_id'], result['server_id'])
        self.assertNotIn(first['server_id'], self.fixture.client.containers.items)
        self.assertEqual(container.actions.count(('stop', 5)), 1)
        replacement = self.fixture.client.containers.get(result['server_id'])
        self.assertTrue(replacement.attrs['State']['Running'])
        self.assertEqual(replacement.actions.count('start'), 1)

    def test_revocation_between_create_and_start_compensates_only_new_container(self):
        self.heartbeat(); self.heartbeat('course-peer'); peer = self.spawn('course-peer')
        def revoke(principal, observation):
            if observation['kind'] == 'principal' and any(c.attrs['Config']['Labels'].get('educloud.workspace.instance') == 'worker-one-course-main'
                    and c.attrs['Config']['Labels'].get('educloud.workspace.kind') == 'learner' for c in self.fixture.client.containers.items.values()):
                self.revoked.add(principal['cert_sha256'])
        self.callback = revoke
        self.refuses(self.spawn)
        self.assertEqual(self.backend.containers(self.worker, 'course-main'), [])
        created = self.fixture.client.containers.created[-1][0]
        self.assertNotIn('start', created.actions); self.assertEqual(created.actions[-1], ('remove', False, False))
        self.assertTrue(self.fixture.client.containers.get(peer['server_id']).attrs['State']['Running'])

    def test_guardian_revocation_between_create_and_start_refuses(self):
        self.heartbeat()
        def revoke(principal, observation):
            if observation['kind'] == 'guardian' and any(c.attrs['Config']['Labels'].get('educloud.workspace.kind') == 'learner'
                    for c in self.fixture.client.containers.items.values()): self.revoked.add(observation['cert_sha256'])
        self.callback = revoke; self.refuses(self.spawn)
        self.assertEqual(self.backend.containers(self.worker, 'course-main'), [])
        self.assertNotIn('start', self.fixture.client.containers.created[-1][0].actions)

    def test_heartbeat_expiry_during_final_round_prevents_start(self):
        self.heartbeat(); calls = []
        def expire(principal, observation):
            if observation['kind'] == 'principal' and any(c.attrs['Config']['Labels'].get('educloud.workspace.kind') == 'learner'
                    for c in self.fixture.client.containers.items.values()):
                calls.append(True)
                if len(calls) == 2: self.fixture.now = 104
        self.callback = expire; self.refuses(self.spawn)
        self.assertNotIn('start', self.fixture.client.containers.created[-1][0].actions)
        self.assertEqual(self.backend.containers(self.worker, 'course-main'), [])

    def test_runtime_drift_during_final_central_start_callback_compensates(self):
        self.heartbeat(); calls = []
        path = Path(self.config['courses'][self.worker]['course-main']['runtime_path'])
        def drift(principal, observation):
            if observation['kind'] == 'principal' and any(c.attrs['Config']['Labels'].get('educloud.workspace.kind') == 'learner'
                    for c in self.fixture.client.containers.items.values()):
                calls.append(True)
                if len(calls) == 2: path.write_bytes(b'changed during central authorization')
        self.callback = drift; self.refuses(self.spawn)
        self.assertNotIn('start', self.fixture.client.containers.created[-1][0].actions)
        self.assertEqual(self.backend.containers(self.worker, 'course-main'), [])

    def test_transport_deadlines_after_final_local_proof_prevent_start(self):
        self.heartbeat(); local = self.runtime._local
        for kind in ('peer_expiry', 'session_expiry', 'closing'):
            wall = [time.time()]; monotonic = [time.monotonic()]; reads = []
            def delayed(request):
                local(request)
                if any(c.attrs['Config']['Labels'].get('educloud.workspace.instance') == 'worker-one-course-main'
                        and c.attrs['Config']['Labels'].get('educloud.workspace.kind') == 'learner'
                        for c in self.fixture.client.containers.items.values()):
                    reads.append(True)
                    if len(reads) == 6:
                        if kind == 'peer_expiry': wall[0] += 3 * 86400
                        elif kind == 'session_expiry': monotonic[0] += 30
                        else: self.server._closing.set()
            with self.subTest(kind=kind), patch('worker_transport.time.time', side_effect=lambda: wall[0]), \
                    patch('worker_transport.time.monotonic', side_effect=lambda: monotonic[0]), \
                    patch.object(self.runtime, '_local', side_effect=delayed):
                self.refuses(self.spawn)
            created = self.fixture.client.containers.created[-1][0]
            self.assertEqual(len(reads), 6); self.assertNotIn('start', created.actions)
            self.assertEqual(self.backend.containers(self.worker, 'course-main'), [])

    def test_peer_expiry_during_new_server_inspection_compensates_started_container(self):
        self.heartbeat(); self.heartbeat('course-peer'); peer = self.spawn('course-peer')
        wall = [time.time()]; original = RuntimeDispatcher.server
        def inspection(container, epoch):
            value = original(container, epoch); wall[0] += 3 * 86400; return value
        with patch('worker_transport.time.time', side_effect=lambda: wall[0]), \
                patch.object(RuntimeDispatcher, 'server', side_effect=inspection):
            self.refuses(self.spawn)
        created = self.fixture.client.containers.created[-1][0]
        self.assertIn('start', created.actions); self.assertNotIn(created.id, self.fixture.client.containers.items)
        self.assertTrue(self.fixture.client.containers.get(peer['server_id']).attrs['State']['Running'])

    def test_watchdog_failure_during_last_root_proof_prevents_start(self):
        import worker_runtime
        self.heartbeat(); hashing = worker_runtime.file_hash; reads = []
        def changed(path):
            value = hashing(path)
            if any(c.attrs['Config']['Labels'].get('educloud.workspace.kind') == 'learner'
                    for c in self.fixture.client.containers.items.values()):
                reads.append(True)
                if len(reads) == 6: self.runtime.failed.set()
            return value
        with patch('worker_runtime.file_hash', side_effect=changed): self.refuses(self.spawn)
        self.assertNotIn('start', self.fixture.client.containers.created[-1][0].actions)
        self.assertEqual(self.backend.containers(self.worker, 'course-main'), [])

    def test_revoked_guardian_observation_cannot_renew_after_identity_read(self):
        identity = self.backend.guardian_identity(self.worker); original = self.backend.guardian_identity
        def revoke(worker):
            result = original(worker); self.revoked.add('e' * 64); return result
        with patch.object(self.backend, 'guardian_identity', side_effect=revoke):
            self.refuses(lambda: self.rpc('guardian.observe', identity))
        self.assertIsNone(self.guardian_row())

    def test_reverse_authorization_never_holds_worker_sqlite_writer(self):
        entered = threading.Event(); release = threading.Event(); rounds = []; outcomes = []
        def delay(principal, observation):
            if principal['course_id'] == 'course-main' and observation['kind'] == 'principal':
                rounds.append(True)
                if len(rounds) == 2: entered.set(); release.wait(3)
        self.callback = delay
        def source():
            try: outcomes.append(self.heartbeat())
            except WorkerError: outcomes.append(False)
        caller = threading.Thread(target=source); caller.start(); self.assertTrue(entered.wait(2))
        try:
            self.assertTrue(self.heartbeat('course-peer')['authorized'])
            self.assertIsNotNone(self.guardian_row('course-peer')); self.assertIsNone(self.guardian_row())
        finally: release.set(); caller.join(3)
        self.assertFalse(caller.is_alive()); self.assertIsNot(outcomes[0], False); self.assertIsNotNone(self.guardian_row())

    def test_heartbeat_writer_contention_does_not_renew_or_defer(self):
        with self.backend.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            try: self.refuses(self.heartbeat, 'worker_channel_refused')
            finally: conn.execute('ROLLBACK')
        self.assertIsNone(self.guardian_row())
        self.assertGreaterEqual(sum(item['kind'] == 'principal' for item in self.observations), 2)

    def heartbeat_write_window_failure(self, kind):
        self.heartbeat(); previous = self.guardian_row(); self.fixture.now = 101
        wall = [time.time()]; connecting = self.backend.connection; validating = self.backend._validate
        writers = set(); observed = []
        @contextmanager
        def traced_connection():
            with connecting() as conn:
                conn.set_trace_callback(lambda statement: writers.add(id(conn)) if statement.startswith('BEGIN IMMEDIATE') else None)
                try: yield conn
                finally: writers.discard(id(conn))
        def delayed(conn):
            validating(conn)
            if id(conn) in writers:
                observed.append(True)
                if kind == 'expiry': wall[0] += 3 * 86400
                elif kind == 'closing': self.server._closing.set()
                else: self.runtime.failed.set()
        with patch('worker_transport.time.time', side_effect=lambda: wall[0]), \
                patch.object(self.backend, 'connection', side_effect=traced_connection), \
                patch.object(self.backend, '_validate', side_effect=delayed):
            self.refuses(self.heartbeat)
        self.assertEqual(observed, [True]); self.assertEqual(self.guardian_row(), previous)

    def test_heartbeat_expiry_after_writer_validation_cannot_renew(self): self.heartbeat_write_window_failure('expiry')
    def test_heartbeat_closing_after_writer_validation_cannot_renew(self): self.heartbeat_write_window_failure('closing')
    def test_heartbeat_watchdog_failure_after_writer_validation_cannot_renew(self): self.heartbeat_write_window_failure('failed')

    def test_remote_heartbeat_requires_separate_scalar_effect_guard(self):
        with self.backend.activation_context(self.request()):
            with self.assertRaises(RuntimeRefusal):
                self.backend.guardian_heartbeat(self.worker, 'course-main', 1, self.backend.guardian_identity(self.worker),
                    'e' * 64, authorize=lambda: None)
        self.assertIsNone(self.guardian_row())

    def test_missing_callback_or_malformed_environment_never_creates(self):
        self.heartbeat(); count = len(self.fixture.client.containers.created)
        principal = self.principal('course-main')
        with self.client().session('course-main', 'activate', self.request()) as channel:
            self.refuses(lambda: channel.call('runtime_v2', {'operation': 'hub.authorize', 'payload': {}, 'principal': principal}))
        self.refuses(lambda: self.rpc('hub.spawn', {'subject': 'alice', 'environment': {'DOCKER_HOST': 'untrusted'}, 'expected_epoch': 1}))
        self.assertEqual(len(self.fixture.client.containers.created), count)

    def test_callback_capability_cannot_escape_thread_or_completed_command(self):
        captured = []; failures = []; original = self.server.runtime_v2_dispatch
        def dispatch(*args):
            captured.append(args)
            def outside():
                try: original(*args)
                except WorkerError: failures.append(True)
            thread = threading.Thread(target=outside); thread.start(); thread.join(1)
            return original(*args)
        self.server.runtime_v2_dispatch = dispatch
        self.rpc('hub.authorize')
        self.assertEqual(failures, [True]); self.refuses(lambda: self.runtime.dispatch(*captured[0]), 'worker_authorization_refused')

    def test_lookalike_held_authority_callables_are_not_capabilities(self):
        self.refuses(lambda: self.runtime.dispatch(self.request(), 'hub.authorize', {}, self.principal('course-main'),
            lambda: None, lambda *args: None), 'worker_authorization_refused')

    def test_observation_acquisition_busy_is_narrow_and_does_not_renew(self):
        identity = self.backend.guardian_identity(self.worker); entered = threading.Event(); release = threading.Event()
        def hold():
            with self.backend.locks([(self.worker, 'course-main')]): entered.set(); release.wait(3)
        holding = threading.Thread(target=hold); holding.start(); self.assertTrue(entered.wait(1))
        try: self.refuses(lambda: self.rpc('guardian.observe', identity), 'worker_observation_busy')
        finally: release.set(); holding.join(2)
        self.assertIsNone(self.guardian_row())
        with patch.object(self.backend, 'guardian_heartbeat', side_effect=RuntimeRefusal('runtime_endpoint_lock_busy')):
            self.refuses(lambda: self.rpc('guardian.observe', identity), 'worker_channel_refused')

    def test_binding_refuses_replaced_home_root_and_runtime_drift(self):
        created = list(self.fixture.client.containers.created)
        with self.client().session('course-main', 'inspect') as channel:
            self.assertEqual(channel.call('binding'), {'binding_sha256': self.journal.binding_sha256})
        root = Path(self.config['courses'][self.worker]['course-main']['home_root']); retained = root.with_name(root.name + '-retained')
        root.rename(retained); root.mkdir(mode=0o700)
        try:
            with self.client().session('course-main', 'inspect') as channel:
                self.refuses(lambda: channel.call('binding'), 'worker_identity_changed')
            self.refuses(self.spawn, 'worker_identity_changed')
            self.assertEqual(self.fixture.client.containers.created, created)
        finally: root.rmdir(); retained.rename(root)
        self.heartbeat()
        Path(self.config['courses'][self.worker]['course-main']['runtime_path']).write_bytes(b'changed immutable runtime')
        self.refuses(self.spawn); self.assertEqual(self.backend.containers(self.worker, 'course-main'), [])

    def test_autonomous_supervisor_stops_expired_course_preserving_fresh_peer(self):
        self.heartbeat(); self.heartbeat('course-peer'); source = self.spawn(); peer = self.spawn('course-peer')
        self.fixture.now = 104; self.heartbeat('course-peer')
        self.runtime.supervision_tick()
        self.assertNotIn(source['server_id'], self.fixture.client.containers.items)
        self.assertTrue(self.fixture.client.containers.get(peer['server_id']).attrs['State']['Running'])
        self.refuses(self.spawn)

    def test_quarantine_and_guardian_process_loss_stop_exact_owned_learners(self):
        self.heartbeat(); result = self.spawn(); self.journal.begin('course-main', uuid.uuid4().hex)
        self.runtime.supervision_tick(); self.assertNotIn(result['server_id'], self.fixture.client.containers.items)
        self.refuses(self.spawn, 'worker_quarantined')
        self.heartbeat('course-peer'); peer = self.spawn('course-peer')
        self.fixture.client.containers.get(self.config['guardians'][self.worker]['container_name']).stop(0)
        self.runtime.supervision_tick(); self.assertNotIn(peer['server_id'], self.fixture.client.containers.items)

    def test_prolonged_enforcement_lock_failure_closes_runtime_without_renewal(self):
        now = [0]; self.runtime.monotonic = lambda: now[0]; entered = threading.Event(); release = threading.Event()
        def hold():
            with self.backend.locks([(self.worker, 'course-main')]): entered.set(); release.wait(3)
        holding = threading.Thread(target=hold); holding.start(); self.assertTrue(entered.wait(1))
        try:
            self.runtime.supervision_tick(); now[0] = 10
            with self.assertRaises(RuntimeRefusal) as caught: self.runtime.supervision_tick()
            self.assertEqual(caught.exception.code, 'runtime_endpoint_enforcement_unavailable')
        finally: release.set(); holding.join(2)
        self.assertTrue(self.runtime.failed.is_set()); self.refuses(lambda: self.rpc('hub.authorize'))

    def test_watchdog_fairness_acquires_actual_periodic_flock_gap(self):
        self.heartbeat(); self.heartbeat('course-peer'); source = self.spawn(); peer = self.spawn('course-peer')
        now = [0.0]; self.runtime.monotonic = lambda: now[0]
        stop = threading.Event(); held = threading.Event(); windows = []
        def holder():
            while not stop.is_set():
                with self.backend.locks([(self.worker, 'course-main')]): held.set(); stop.wait(.3)
                stop.wait(.15)
        thread = threading.Thread(target=holder); thread.start(); original = self.runtime._supervise
        def supervise(course, *, wait_seconds=.1):
            if course == 'course-main': windows.append(wait_seconds)
            return original(course, wait_seconds=wait_seconds)
        try:
            self.assertTrue(held.wait(2))
            with patch.object(self.runtime, '_supervise', side_effect=supervise):
                self.runtime.supervision_tick(); self.assertIn('course-main', self.runtime.busy_since)
                now[0] = .6; self.runtime.supervision_tick()
            self.assertEqual(windows, [.1, 1]); self.assertNotIn('course-main', self.runtime.busy_since)
            self.assertTrue(self.fixture.client.containers.get(source['server_id']).attrs['State']['Running'])
            self.assertTrue(self.fixture.client.containers.get(peer['server_id']).attrs['State']['Running'])
        finally: stop.set(); thread.join(2)


class WorkerSupervisorDiagnosticsTests(unittest.TestCase):
    def drive_failure(self, error):
        actions = []; shutdown = threading.Event(); failed = threading.Event()
        def tick():
            actions.append('tick')
            raise error
        def close_server(): actions.append('close')
        def request_shutdown(): actions.append('shutdown'); shutdown.set()
        def serving(**kwargs): self.assertTrue(shutdown.wait(2))
        def stop_owned():
            self.assertTrue(failed.is_set())
            actions.append('stop_owned')
        runtime = SimpleNamespace(failed=failed, supervision_tick=tick, dispatch=None, stop_owned=stop_owned)
        server = SimpleNamespace(shutdown=request_shutdown, serve_forever=serving, server_close=close_server)
        config = {'workers': {'worker-one': {}}, 'courses': {'worker-one': {'course-main': {}}}}
        backend = SimpleNamespace(authority_id='a' * 32)
        with patch.object(worker_runtime, 'LinuxWorkerBackend', return_value=backend), \
                patch.object(worker_runtime.WorkerJournal, 'open', return_value=object()), \
                patch.object(worker_runtime, 'worker_binding', return_value='b' * 64), \
                patch.object(worker_runtime, 'WorkerRuntime', return_value=runtime), \
                patch.object(worker_runtime, 'WorkerServer', return_value=server):
            with self.assertRaises(WorkerError) as refused:
                worker_runtime.serve(config, 'PRIVATE_JOURNAL', address=('127.0.0.1', 1),
                    certfile='PRIVATE_CERT', keyfile='PRIVATE_KEY', ca_file='PRIVATE_CA', acl_file='PRIVATE_ACL')
        self.assertEqual(refused.exception.code, 'worker_channel_refused')
        self.assertEqual(actions, ['tick', 'shutdown', 'close', 'stop_owned'])
        self.assertTrue(failed.is_set())

    def run_failure(self, error, expected):
        with self.assertLogs('worker_runtime', level='ERROR') as captured:
            self.drive_failure(error)
        self.assertEqual(captured.output,
            ['ERROR:worker_runtime:Worker runtime supervisor failed: supervision/' + expected])
        self.assertNotIn('PRIVATE', ''.join(captured.output))

    def test_supervisor_reports_only_known_codes_and_still_closes_and_stops(self):
        for error, code in [(RuntimeRefusal('runtime_endpoint_enforcement_unavailable'), 'runtime_endpoint_enforcement_unavailable'),
                (WorkerError('worker_identity_changed'), 'worker_identity_changed')]:
            with self.subTest(code=code): self.run_failure(error, code)

    def test_supervisor_never_logs_unknown_codes_or_secret_exception_data(self):
        unknown_worker = WorkerError('worker_channel_refused'); unknown_worker.code = 'PRIVATE_WORKER_CODE'
        private_type = type('PRIVATE_EXCEPTION_CLASS', (Exception,), {})
        for error, code in [(RuntimeRefusal('PRIVATE_RUNTIME_CODE'), 'runtime_refusal'),
                (unknown_worker, 'worker_error'), (ValueError('PRIVATE_VALUE', {'PRIVATE_KEY': 'PRIVATE_TOKEN'}), 'value_error'),
                (private_type('PRIVATE_ARGUMENT'), 'other_exception')]:
            with self.subTest(category=code): self.run_failure(error, code)

    def test_unknown_exception_code_property_is_never_read(self):
        class PrivateException(Exception):
            @property
            def code(self): raise AssertionError('PRIVATE_PROPERTY_MUST_NOT_BE_READ')
        self.run_failure(PrivateException('PRIVATE_ARGUMENT'), 'other_exception')

    def test_diagnostic_classifier_or_logging_failure_cannot_prevent_shutdown(self):
        for target in ('supervisor_failure_code', 'logging'):
            with self.subTest(target=target):
                if target == 'supervisor_failure_code':
                    with patch.object(worker_runtime, target, side_effect=RuntimeError('PRIVATE_CLASSIFIER')):
                        self.drive_failure(RuntimeError('PRIVATE_ORIGINAL'))
                else:
                    with patch.object(worker_runtime.logging.Logger, 'error', side_effect=RuntimeError('PRIVATE_HANDLER')):
                        self.drive_failure(RuntimeError('PRIVATE_ORIGINAL'))


if __name__ == '__main__': unittest.main()
