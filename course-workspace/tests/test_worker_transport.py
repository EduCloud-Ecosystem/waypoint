"""Real disposable mTLS and physical flock evidence; no Docker/provider acceptance."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import socket
import ssl
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from authority_test_pki import TestPKI
from worker_channel_state import WorkerJournal, JournalError, digest, encoded
from worker_transport import (WorkerClient, WorkerServer, WorkerError, VERSION, receive, send, MAX_CALLS,
                              LinuxWorkerBackend, linux_boot_id, journal_binding)
from runtime_authority import DockerBackend


class Backend:
    def __init__(self, root, authority):
        self.authority_id = authority; self.root = root; self.local = threading.local()
        self.worker = {'worker_id': 'one', 'provider_id': 'provider-one', 'boot_id': 'b' * 32,
            'policy_sha256': 'c' * 64, 'memory_budget_mb': 4096, 'cpu_budget': 2,
            'capacity_memory_mb': 8192, 'capacity_cpu': 4}
        self.barriers = {}; self.effects = []; self.running = 0; self.fail_fence = False
        for course in ('course', 'peer'):
            self.barriers[course] = {'authority_id': authority, 'worker_id': 'one', 'course_id': course,
                'barrier_id': uuid.uuid4().hex, 'denied_through_epoch': 0, 'request_sha256': '0' * 64}

    def inspect_worker(self, worker):
        if worker != 'one': raise ValueError()
        return self.worker.copy()

    def inspect_course(self, course, worker):
        if worker != 'one' or course not in self.barriers: raise ValueError()
        return {'running_count': self.running, 'admission_floor': self.barriers[course]['denied_through_epoch'] + 1,
                'retained_tree_sha256': 'a' * 64}

    @contextmanager
    def locks(self, endpoints):
        if endpoints != [('one', 'course')] and endpoints != [('one', 'peer')]: raise ValueError()
        fd = os.open(self.root / (endpoints[0][1] + '.lock'), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX); self.local.held = True
            yield
        finally:
            self.local.held = False; fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)

    def validate_request(self, request, workers):
        if request['authority_id'] != self.authority_id or workers != ['one']: raise ValueError()
        prefix = '' if 'worker_id' in request else ('source_' if request['source_worker_id'] == 'one' else 'receiver_')
        if request[prefix + 'provider_id'] != self.worker['provider_id'] or request[prefix + 'boot_id'] != self.worker['boot_id']: raise ValueError()

    @contextmanager
    def activation_context(self, request):
        with self.locks([('one', request['course_id'])]):
            self.validate_request(request, ['one'])
            if request['epoch'] <= self.barriers[request['course_id']]['denied_through_epoch']: raise ValueError()
            yield

    def fence(self, request, workers):
        assert self.local.held
        self.effects.append('fence')
        barrier = self.barriers[request['course_id']]
        barrier.update(denied_through_epoch=max(barrier['denied_through_epoch'], request['old_epoch']), request_sha256=digest(request))
        if self.fail_fence: raise ValueError('PRIVATE_TEST_SECRET')

    def stop_endpoint(self, worker, course):
        assert self.local.held; self.effects.append('stop'); self.running = 0
    def barrier(self, worker, course): return self.barriers[course].copy()


class WorkerTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(); cls.pki = TestPKI(Path(cls.temporary.name).resolve() / 'pki')
        cls.credentials = {name: cls.pki.issue(name) for name in ('controller', 'rotated', 'unknown')}
        cls.foreign = TestPKI(Path(cls.temporary.name).resolve() / 'foreign'); cls.foreign_credentials = cls.foreign.issue('foreign')
    @classmethod
    def tearDownClass(cls): cls.temporary.cleanup()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup); self.root = Path(temporary.name).resolve()
        self.authority = uuid.uuid4().hex; self.backend = Backend(self.root, self.authority)
        self.journal = WorkerJournal.create(self.root / 'journal', self.authority, 'one', ['course', 'peer'], 'a' * 64)
        self.acl = self.root / 'acl.json'; self.controllers = {self.pki.fingerprint(self.credentials['controller'][0]): ['course', 'peer']}
        self.write_acl(); self.servers = []; self.start(); self.addCleanup(self.close)

    def write_acl(self):
        self.acl.write_bytes(encoded({'version': VERSION, 'authority_id': self.authority, 'worker_id': 'one', 'controllers': self.controllers})); self.acl.chmod(0o600)

    def start(self, runtime=None, lifetime=5):
        self.server = WorkerServer(('127.0.0.1', 0), self.backend, 'one', self.pki.server_cert, self.pki.server_key,
            self.pki.ca_cert, self.acl, journal=self.journal, runtime_dispatch=runtime, timeout=1, session_seconds=lifetime)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        thread.start(); self.servers.append((self.server, thread))

    def close(self):
        for server, thread in self.servers: server.shutdown(); server.server_close(); thread.join(2)
        self.servers = []

    def client(self, role='controller', pin=None, authority=None, worker='one', host='localhost', credentials=None):
        cert, key = credentials or self.credentials[role]
        return WorkerClient(host, self.server.server_address[1], authority or self.authority, worker,
            pin or self.pki.fingerprint(self.pki.server_cert), self.pki.ca_cert, cert, key, timeout=2)

    def activate(self, epoch=1): return {'authority_id': self.authority, 'worker_id': 'one', 'course_id': 'course',
        'provider_id': 'provider-one', 'boot_id': 'b' * 32, 'epoch': epoch}

    def transfer(self, reconciliation=False):
        record = {'authority_id': self.authority, 'transaction_id': uuid.uuid4().hex, 'course_id': 'course',
            'source_worker_id': 'one', 'receiver_worker_id': 'two', 'source_provider_id': 'provider-one',
            'receiver_provider_id': 'provider-two', 'source_boot_id': 'b' * 32, 'receiver_boot_id': 'd' * 32,
            'old_epoch': 1, 'plan_sha256': 'e' * 64}
        if reconciliation: record.update(selected_worker_id='two', original_transaction_id=uuid.uuid4().hex)
        return record

    def refused(self, code, callback):
        with self.assertRaises(WorkerError) as caught: callback()
        self.assertEqual(caught.exception.code, code)

    def lock_available(self):
        fd = os.open(self.root / 'course.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: return False
            return True
        finally: os.close(fd)

    def test_real_tls_session_observations_and_lock_lifetime(self):
        with self.client().session('course', 'inspect') as channel:
            self.assertEqual(channel.call('worker'), self.backend.worker)
            self.assertTrue(self.lock_available())
        with self.client().session('course', 'activate', self.activate()) as channel:
            self.assertEqual(channel.call('check'), {'locked': True}); self.assertFalse(self.lock_available())
            self.assertEqual(channel.call('course')['running_count'], 0)
        deadline = time.monotonic() + 1
        while not self.lock_available() and time.monotonic() < deadline: time.sleep(.01)
        self.assertTrue(self.lock_available()); self.assertEqual(self.backend.effects, [])

    def test_open_acknowledgement_requires_exact_true_boolean(self):
        for acknowledgement in ({'opened': 1}, {'opened': True, 'extra': True}, {'opened': False}, {'opened': 'true'}):
            with self.subTest(acknowledgement=acknowledgement), patch('worker_transport._Channel._response', return_value=acknowledgement):
                def attempt():
                    with self.client().session('course', 'inspect'): self.fail('malformed open acknowledged')
                self.refused('worker_frame_invalid', attempt)
        self.assertEqual(self.backend.effects, [])

    def test_fresh_acl_revocation_and_rotation_on_same_connection(self):
        with self.client().session('course', 'inspect') as channel:
            channel.call('worker'); self.controllers = {}; self.write_acl()
            self.refused('worker_channel_refused', lambda: channel.call('worker'))
        self.controllers[self.pki.fingerprint(self.credentials['rotated'][0])] = ['course']; self.write_acl()
        with self.client('rotated').session('course', 'inspect') as channel: channel.call('worker')
        self.assertEqual(self.backend.effects, [])

    def test_scopes_unknown_certificate_pins_and_authority_refuse(self):
        for client, course in ((self.client('unknown'), 'course'), (self.client(), 'other'),
                              (self.client(pin='0' * 64), 'course'), (self.client(authority='0' * 32), 'course'),
                              (self.client(worker='other'), 'course')):
            with self.subTest(course=course), self.assertRaises(WorkerError):
                with client.session(course, 'inspect'): self.fail('opened unapproved session')
        self.assertEqual(self.backend.effects, [])

    def test_missing_untrusted_certificate_and_hostname_refuse(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); context.load_verify_locations(cafile=self.pki.ca_cert)
        with self.assertRaises((OSError, ssl.SSLError)):
            with socket.create_connection(self.server.server_address, timeout=1) as raw:
                with context.wrap_socket(raw, server_hostname='localhost') as stream: receive(stream, time.monotonic() + 1, 1)
        with self.assertRaises(WorkerError):
            with self.client(credentials=self.foreign_credentials).session('course', 'inspect'): self.fail('untrusted')
        connect = socket.create_connection
        with patch('worker_transport.socket.create_connection', side_effect=lambda *a, **k: connect(self.server.server_address, timeout=1)):
            with self.assertRaises(WorkerError):
                with self.client(host='wrong.test').session('course', 'inspect'): self.fail('wrong hostname')
        self.assertEqual(self.backend.effects, [])

    def test_worker_incarnation_drift_refuses_each_command(self):
        with self.client().session('course', 'inspect') as channel:
            channel.call('worker'); self.backend.worker['boot_id'] = 'f' * 32
            self.refused('worker_identity_changed', lambda: channel.call('worker'))

    def test_worker_integer_to_boolean_drift_refuses_runtime_and_fence(self):
        effects = []
        def runtime(request, operation, payload, authorize):
            authorize(); effects.append(operation); return {}
        for mode in ('activate', 'exclusive'):
            self.close(); self.backend.worker['cpu_budget'] = 1; self.start(runtime=runtime)
            request = self.activate() if mode == 'activate' else self.transfer()
            with self.subTest(mode=mode), self.client().session('course', mode, request) as channel:
                self.backend.worker['cpu_budget'] = True  # Python equality would accept this as 1.
                operation = 'runtime' if mode == 'activate' else 'fence'
                payload = {'operation': 'start', 'payload': {}} if mode == 'activate' else {'request': request, 'reconciliation': False}
                self.refused('worker_identity_changed', lambda: channel.call(operation, payload))
        self.assertEqual(effects, []); self.assertEqual(self.backend.effects, [])
        self.assertFalse(self.journal.state('course')['quarantined'])

    def test_fence_request_boolean_epoch_is_not_an_exact_reviewed_echo(self):
        request = self.transfer()
        with self.client().session('course', 'exclusive', request) as channel:
            self.refused('worker_request_invalid', lambda: channel.call('fence',
                {'request': dict(request, old_epoch=True), 'reconciliation': False}))
        self.assertEqual(self.backend.effects, []); self.assertFalse(self.journal.state('course')['quarantined'])

    def test_inspection_cannot_fence_activate_or_run_runtime(self):
        for op, payload in (('fence', {'request': self.transfer(), 'reconciliation': False}), ('runtime', {'operation': 'start', 'payload': {}}), ('finish', {}), ('check', {})):
            with self.subTest(op=op), self.client().session('course', 'inspect') as channel:
                self.refused('worker_operation_refused', lambda: channel.call(op, payload))
        self.assertEqual(self.backend.effects, []); self.assertFalse(self.journal.state('course')['quarantined'])

    def test_fence_quarantine_survives_disconnect_restart_and_explicit_finish(self):
        request = self.transfer()
        with self.client().session('course', 'exclusive', request) as channel:
            self.assertFalse(self.journal.state('course')['quarantined'])
            proof = channel.call('fence', {'request': request, 'reconciliation': False})
            self.assertEqual(proof['course']['running_count'], 0); self.assertTrue(self.journal.state('course')['quarantined'])
            self.assertEqual(channel.call('proof'), proof)
        self.close(); self.journal = WorkerJournal.open(self.root / 'journal', self.authority, 'one', ['course', 'peer'], 'a' * 64); self.start()
        with self.assertRaises(WorkerError):
            with self.client().session('course', 'activate', self.activate(2)): self.fail('quarantined activated')
        with self.client().session('course', 'exclusive', request) as channel:
            channel.call('fence', {'request': request, 'reconciliation': False}); channel.call('proof')
            self.assertEqual(channel.finish(), {'finished': True})
        self.assertFalse(self.journal.state('course')['quarantined']); self.assertFalse(self.journal.state('peer')['quarantined'])
        with self.client().session('course', 'activate', self.activate(2)) as channel: channel.call('check')

    def test_finish_without_local_fence_and_changed_request_refuse_without_effects(self):
        request = self.transfer()
        with self.client().session('course', 'exclusive', request) as channel:
            self.refused('worker_operation_refused', channel.finish)
        with self.client().session('course', 'exclusive', request) as channel:
            changed = dict(request, old_epoch=2)
            self.refused('worker_request_invalid', lambda: channel.call('fence', {'request': changed, 'reconciliation': False}))
        self.assertEqual(self.backend.effects, []); self.assertFalse(self.journal.state('course')['quarantined'])

    def test_failed_fence_preserves_quarantine_and_no_finish(self):
        self.backend.fail_fence = True; request = self.transfer()
        with self.client().session('course', 'exclusive', request) as channel:
            self.refused('worker_channel_refused', lambda: channel.call('fence', {'request': request, 'reconciliation': False}))
        self.assertTrue(self.journal.state('course')['quarantined']); self.assertEqual(self.backend.effects, ['fence'])

    def test_runtime_callback_fresh_authorize_prevents_revoked_effect(self):
        self.close(); effects = []
        def runtime(request, operation, payload, authorize):
            self.controllers = {}; self.write_acl(); authorize(); effects.append(operation); return {'running': True}
        self.start(runtime=runtime)
        with self.client().session('course', 'activate', self.activate()) as channel:
            self.refused('worker_channel_refused', lambda: channel.call('runtime', {'operation': 'start', 'payload': {}}))
        self.assertEqual(effects, [])

    def test_runtime_default_refuses_and_callback_holds_activation_lock(self):
        with self.client().session('course', 'activate', self.activate()) as channel:
            self.refused('worker_operation_refused', lambda: channel.call('runtime', {'operation': 'start', 'payload': {}}))
        self.close()
        def runtime(request, operation, payload, authorize):
            authorize(); self.assertFalse(self.lock_available()); return {'running': True}
        self.start(runtime=runtime)
        with self.client().session('course', 'activate', self.activate()) as channel:
            self.assertEqual(channel.call('runtime', {'operation': 'start', 'payload': {}}), {'running': True})

    def test_timeout_releases_lock_but_never_clears_fenced_quarantine(self):
        request = self.transfer(); now = [time.monotonic()]
        with patch('worker_transport.time.monotonic', side_effect=lambda: now[0]):
            with self.client().session('course', 'exclusive', request) as channel:
                channel.call('fence', {'request': request, 'reconciliation': False}); channel.call('proof')
                now[0] += 6
                with self.assertRaises(WorkerError): channel.call('proof')
        self.assertTrue(self.journal.state('course')['quarantined']); self.assertTrue(self.lock_available())

    def test_missing_or_corrupt_journal_never_yields_healthy_session_evidence(self):
        for mode in ('inspect', 'exclusive'):
            with self.subTest(mode=mode), self.client().session('course', mode,
                    self.transfer() if mode == 'exclusive' else None) as channel:
                channel.call('worker'); saved = self.journal.sentinel.with_suffix('.saved')
                self.journal.sentinel.rename(saved)
                try: self.refused('worker_channel_refused', lambda: channel.call('course'))
                finally: saved.rename(self.journal.sentinel)
        with self.client().session('course', 'inspect') as channel:
            with sqlite3.connect(self.journal.database) as conn: conn.execute('UPDATE head SET digest=?', ('f' * 64,))
            self.refused('worker_channel_refused', lambda: channel.call('worker'))
        self.assertEqual(self.backend.effects, [])

    def test_strict_sequence_challenge_duplicate_and_nonfinite_frames(self):
        for mutation in ('sequence', 'challenge', 'duplicate', 'nonfinite', 'oversized'):
            with self.subTest(mutation=mutation), self.client().session('course', 'inspect') as channel:
                frame = {k: channel.challenge[k] for k in ('version', 'session_id', 'challenge')}
                frame.update(sequence=1, operation='worker', payload={})
                if mutation == 'sequence': frame['sequence'] = 2
                if mutation == 'challenge': frame['challenge'] = '0' * 64
                raw = encoded(frame)
                if mutation == 'duplicate': raw = raw[:-1] + b',"sequence":1}'
                if mutation == 'nonfinite': raw = raw.replace(b'"payload":{}', b'"payload":{"value":NaN}')
                if mutation == 'oversized': raw = b'x' * 65537
                channel.stream.sendall(struct.pack('!I', len(raw)) + raw)
                with self.assertRaises(WorkerError): channel._response(1)
        self.assertEqual(self.backend.effects, [])

    def test_sequence_replay_and_max_calls_never_repeat_effect(self):
        self.close(); self.start(lifetime=20)
        with self.client().session('course', 'inspect') as channel:
            channel.call('worker')
            send(channel.stream, {k: channel.challenge[k] for k in ('version', 'session_id', 'challenge')} |
                 {'sequence': 1, 'operation': 'worker', 'payload': {}})
            with self.assertRaises(WorkerError): channel._response(1)
        with self.client().session('course', 'inspect') as channel:
            for _ in range(MAX_CALLS): channel.call('state')
            self.refused('worker_session_closed', lambda: channel.call('state'))

    def test_no_secret_logging_on_backend_exception(self):
        self.backend.fail_fence = True; request = self.transfer()
        with self.assertNoLogs('worker_transport'), self.client().session('course', 'exclusive', request) as channel:
            self.refused('worker_channel_refused', lambda: channel.call('fence', {'request': request, 'reconciliation': False}))

    def test_revocation_before_fence_does_not_write_quarantine_or_barrier(self):
        request = self.transfer(); validate = self.backend.validate_request; observations = []
        def revoke_during_validation(*args):
            validate(*args); observations.append(True)
            if len(observations) == 2: self.controllers = {}; self.write_acl()
        with patch.object(self.backend, 'validate_request', side_effect=revoke_during_validation):
            with self.client().session('course', 'exclusive', request) as channel:
                self.refused('worker_channel_refused', lambda: channel.call('fence', {'request': request, 'reconciliation': False}))
        self.assertFalse(self.journal.state('course')['quarantined']); self.assertEqual(self.backend.effects, [])

    def test_untrusted_error_text_cannot_escape_fixed_wire_codes(self):
        self.close()
        def runtime(*args): raise WorkerError('PRIVATE_TEST_SECRET')
        self.start(runtime=runtime)
        with self.client().session('course', 'activate', self.activate()) as channel:
            self.refused('worker_channel_refused', lambda: channel.call('runtime', {'operation': 'start', 'payload': {}}))

    def test_mid_session_peer_expiry_stops_runtime_callback_before_effect(self):
        self.close(); now = [time.time()]; effects = []
        def runtime(request, operation, payload, authorize):
            now[0] += 3 * 86400; authorize(); effects.append(operation); return {}
        self.start(runtime=runtime)
        with patch('worker_transport.time.time', side_effect=lambda: now[0]):
            with self.client().session('course', 'activate', self.activate()) as channel:
                self.refused('worker_peer_validity_refused', lambda: channel.call('runtime', {'operation': 'start', 'payload': {}}))
        self.assertEqual(effects, []); self.assertEqual(self.backend.effects, [])

    def test_peer_expiry_during_final_worker_inspection_prevents_effect(self):
        self.close(); now = [time.time()]; inside_callback = []; effects = []
        inspect = self.backend.inspect_worker
        def slow_inspection(worker):
            result = inspect(worker)
            if inside_callback: now[0] += 3 * 86400
            return result
        def runtime(request, operation, payload, authorize):
            inside_callback.append(True); authorize(); effects.append(operation); return {}
        self.start(runtime=runtime)
        with patch('worker_transport.time.time', side_effect=lambda: now[0]), \
                patch.object(self.backend, 'inspect_worker', side_effect=slow_inspection):
            with self.client().session('course', 'activate', self.activate()) as channel:
                self.refused('worker_peer_validity_refused', lambda: channel.call('runtime', {'operation': 'start', 'payload': {}}))
        self.assertEqual(effects, []); self.assertEqual(self.backend.effects, [])

    def test_session_wallclock_rollback_and_nonfinite_never_fence(self):
        for change in (-1, float('nan'), float('inf'), True):
            now = [time.time()]
            with self.subTest(change=change), patch('worker_transport.time.time', side_effect=lambda: now[0]):
                with self.client().session('course', 'exclusive', self.transfer()) as channel:
                    now[0] = now[0] + change if change == -1 else change
                    self.refused('worker_peer_validity_refused', lambda: channel.call('worker'))
            self.assertFalse(self.journal.state('course')['quarantined'])

    def test_reconciliation_schema_and_barrier_binding_are_exact(self):
        request = self.transfer(True)
        with self.client().session('course', 'exclusive', request) as channel:
            self.refused('worker_request_invalid', lambda: channel.call('fence', {'request': request, 'reconciliation': False}))
        with self.client().session('course', 'exclusive', request) as channel:
            channel.call('fence', {'request': request, 'reconciliation': True})
            self.backend.barriers['course']['request_sha256'] = '0' * 64
            self.refused('worker_fence_unverified', channel.finish)
        self.assertTrue(self.journal.state('course')['quarantined'])

    def test_active_shutdown_is_bounded_and_releases_physical_lock(self):
        with self.client().session('course', 'activate', self.activate()) as channel:
            started = time.monotonic(); self.close(); self.assertLess(time.monotonic() - started, 1)
            with self.assertRaises(WorkerError): channel.call('check')
        deadline = time.monotonic() + 1
        while not self.lock_available() and time.monotonic() < deadline: time.sleep(.01)
        self.assertTrue(self.lock_available())

    def test_shutdown_drains_slow_authorization_and_prevents_late_effect(self):
        self.close(); entered = threading.Event(); release = threading.Event(); enabled = threading.Event()
        effects = []; client_done = []; close_done = []; inspect = self.backend.inspect_worker
        def slow_inspection(worker):
            value = inspect(worker)
            if enabled.is_set(): entered.set(); release.wait(3)
            return value
        def runtime(request, operation, payload, authorize):
            enabled.set(); authorize(); effects.append(operation); return {}
        self.start(runtime=runtime)
        def requesting():
            try:
                with self.client().session('course', 'activate', self.activate()) as channel:
                    channel.call('runtime', {'operation': 'start', 'payload': {}})
            except WorkerError: client_done.append(True)
        def closing():
            try: self.server.server_close(); close_done.append(True)
            except WorkerError: close_done.append(False)
        with patch.object(self.backend, 'inspect_worker', side_effect=slow_inspection):
            caller = threading.Thread(target=requesting); caller.start()
            self.assertTrue(entered.wait(2))
            closer = threading.Thread(target=closing); closer.start()
            try:
                self.assertTrue(self.server._closing.wait(1)); self.assertTrue(closer.is_alive())
                self.assertEqual(close_done, [])
            finally: release.set()
            closer.join(2); caller.join(2)
        self.assertFalse(closer.is_alive()); self.assertFalse(caller.is_alive())
        self.assertEqual(close_done, [True]); self.assertEqual(client_done, [True]); self.assertEqual(effects, [])
        self.assertTrue(self.lock_available()); self.assertFalse(self.server._handler_threads)

    def test_shutdown_cannot_claim_success_for_unresponsive_callback(self):
        self.close(); entered = threading.Event(); release = threading.Event(); client_done = []
        def runtime(request, operation, payload, authorize):
            entered.set(); release.wait(3); return {}
        self.start(runtime=runtime)
        def requesting():
            try:
                with self.client().session('course', 'activate', self.activate()) as channel:
                    channel.call('runtime', {'operation': 'start', 'payload': {}})
            except WorkerError: client_done.append(True)
        caller = threading.Thread(target=requesting); caller.start(); self.assertTrue(entered.wait(2))
        main_thread = threading.current_thread(); real_clock = time.monotonic; observations = []
        def deadline_clock():
            if threading.current_thread() is not main_thread: return real_clock()
            observations.append(True); return 0 if len(observations) == 1 else 100
        try:
            with patch('worker_transport.time.monotonic', side_effect=deadline_clock):
                self.refused('worker_shutdown_incomplete', self.server.server_close)
            self.assertTrue(self.server._handler_threads)
        finally: release.set(); caller.join(2)
        self.assertFalse(caller.is_alive()); self.server.server_close()
        self.assertEqual(client_done, [True]); self.assertFalse(self.server._handler_threads)

    def test_tls_material_private_key_public_write_symlink_and_bounds_refuse(self):
        cert, key = self.credentials['controller']; copy = self.root / 'credential.key'
        copy.write_bytes(key.read_bytes()); copy.chmod(0o644)
        self.refused('worker_channel_refused', lambda: self.client(credentials=(cert, copy)))
        public = self.root / 'public.pem'; public.write_bytes(cert.read_bytes()); public.chmod(0o666)
        self.refused('worker_channel_refused', lambda: self.client(credentials=(public, key)))
        link = self.root / 'link.pem'; link.symlink_to(cert)
        self.refused('worker_channel_refused', lambda: self.client(credentials=(link, key)))
        public.chmod(0o600); public.write_bytes(b'x' * (256 * 1024 + 1))
        self.refused('worker_channel_refused', lambda: self.client(credentials=(public, key)))
        self.refused('worker_channel_refused', lambda: WorkerServer(('127.0.0.1', 0), self.backend, 'one',
            self.pki.server_cert, copy, self.pki.ca_cert, self.acl, journal=self.journal))


class LinuxWorkerIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup); self.root = Path(temporary.name).resolve()
        self.boot = self.root / 'boot'; self.value = uuid.uuid4(); self.boot.write_bytes((str(self.value) + '\n').encode())

    @contextmanager
    def proc(self):
        opening = os.open
        def open_fixed(path, flags):
            self.assertEqual(path, '/proc/sys/kernel/random/boot_id')
            self.assertTrue(flags & os.O_NOFOLLOW)
            return opening(self.boot, flags)
        with patch('worker_transport.sys.platform', 'linux'), patch('worker_transport.os.open', side_effect=open_fixed): yield

    def test_fixed_proc_boot_is_fresh_and_canonical_without_os_fallback(self):
        with self.proc():
            self.assertEqual(linux_boot_id(), self.value.hex)
            changed = uuid.uuid4(); self.boot.write_bytes(str(changed).encode())
            self.assertEqual(linux_boot_id(), changed.hex)
        with patch('worker_transport.sys.platform', 'darwin'), patch('worker_transport.os.open', side_effect=AssertionError('fallback')):
            with self.assertRaises(WorkerError) as caught: linux_boot_id()
            self.assertEqual(caught.exception.code, 'worker_host_identity_refused')

    def test_invalid_oversized_nonascii_and_unavailable_proc_refuse(self):
        for raw in (b'not-a-uuid', b' ' + str(self.value).encode(), b'AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA',
                    str(self.value).encode() + b'\n\n', b'x' * 1000, b'\xff' * 36):
            with self.subTest(raw_length=len(raw)), self.proc():
                self.boot.write_bytes(raw)
                with self.assertRaises(WorkerError): linux_boot_id()
        with patch('worker_transport.sys.platform', 'linux'), patch('worker_transport.os.open', side_effect=OSError('PRIVATE_HOST_PATH')):
            with self.assertRaises(WorkerError) as caught: linux_boot_id()
            self.assertEqual(str(caught.exception), 'worker_host_identity_refused')

    def test_backend_inspection_checks_actual_boot_every_time(self):
        backend = object.__new__(LinuxWorkerBackend); evidence = {'worker_id': 'one', 'boot_id': self.value.hex}
        with patch.object(DockerBackend, 'inspect_worker', return_value=evidence) as inspect, self.proc():
            self.assertEqual(backend.inspect_worker('one'), evidence)
            self.boot.write_bytes(str(uuid.uuid4()).encode())
            with self.assertRaises(WorkerError): backend.inspect_worker('one')
            self.assertEqual(inspect.call_count, 2)

    def test_journal_binding_derived_from_config_roots_and_fresh_worker(self):
        backend = type('Backend', (), {'config': {'approved': True}, 'home_bindings': {'one/course': [1, 2]},
            'inspect_worker': lambda _, worker: {'worker_id': worker, 'boot_id': self.value.hex}})()
        expected = digest({'config': backend.config, 'home_bindings': backend.home_bindings, 'worker': backend.inspect_worker('one')})
        self.assertEqual(journal_binding(backend, 'one'), expected)
        backend.home_bindings['one/course'] = [1, 3]
        self.assertNotEqual(journal_binding(backend, 'one'), expected)


class WorkerJournalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup); self.root = Path(temporary.name).resolve()
        self.authority = uuid.uuid4().hex; self.path = self.root / 'journal'
        self.store = WorkerJournal.create(self.path, self.authority, 'one', ['course', 'peer'], 'a' * 64)
    def reopen(self): return WorkerJournal.open(self.path, self.authority, 'one', ['course', 'peer'], 'a' * 64)

    def test_durable_history_supersession_and_exact_finish(self):
        first, second = uuid.uuid4().hex, uuid.uuid4().hex
        self.store.begin('course', first); self.store.begin('course', first)
        self.store = self.reopen(); self.assertTrue(self.store.state('course')['quarantined'])
        self.store.begin('course', second)
        with self.assertRaises(JournalError): self.store.finish('course', first)
        self.store.finish('course', second); self.assertFalse(self.reopen().state('course')['quarantined'])
        with self.assertRaises(JournalError): self.store.begin('course', first)
        self.assertFalse(self.store.state('peer')['quarantined'])

    def test_missing_replaced_marker_database_and_wrong_binding_refuse(self):
        self.store.begin('course', uuid.uuid4().hex)
        with self.assertRaises(JournalError): WorkerJournal.open(self.path, self.authority, 'one', ['course', 'peer'], 'b' * 64)
        for path in (self.store.sentinel, self.store.database):
            old = path.with_suffix('.saved'); path.rename(old)
            with self.assertRaises(JournalError): self.reopen()
            path.write_bytes(old.read_bytes()); path.chmod(0o600)
            if path == self.store.database:
                with self.assertRaises(JournalError): self.reopen()
            path.unlink(); old.rename(path)
        self.assertTrue(self.reopen().state('course')['quarantined'])

    def test_missing_history_or_state_edit_refuses(self):
        self.store.begin('course', uuid.uuid4().hex)
        with sqlite3.connect(self.store.database) as conn: conn.execute('DELETE FROM events')
        with self.assertRaises(JournalError): self.store.state('course')

    def test_unexpected_schema_trigger_or_oversized_records_refuse(self):
        self.store.begin('course', uuid.uuid4().hex)
        with sqlite3.connect(self.store.database) as conn:
            conn.execute('CREATE TRIGGER unexpected AFTER UPDATE ON state BEGIN DELETE FROM events; END')
        with self.assertRaises(JournalError): self.store.finish('course', uuid.uuid4().hex)
        with sqlite3.connect(self.store.database) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM events').fetchone()[0], 1)
            conn.execute('DROP TRIGGER unexpected'); conn.execute('UPDATE events SET record=?', ('x' * 65537,))
        with self.assertRaises(JournalError): self.reopen()

    def test_oversized_database_refuses_before_sqlite_open(self):
        from worker_channel_state import MAX_DATABASE
        with self.store.database.open('r+b') as target: target.truncate(MAX_DATABASE + 1)
        with patch('worker_channel_state.sqlite3.connect', side_effect=AssertionError('opened oversized database')):
            with self.assertRaises(JournalError): self.reopen()

    def test_concurrent_course_quarantines_preserve_peer_state(self):
        sessions = {course: uuid.uuid4().hex for course in ('course', 'peer')}
        threads = [threading.Thread(target=self.store.begin, args=item) for item in sessions.items()]
        for thread in threads: thread.start()
        for thread in threads: thread.join(2); self.assertFalse(thread.is_alive())
        for course in sessions: self.assertTrue(self.reopen().state(course)['quarantined'])
        self.store.finish('course', sessions['course']); self.assertTrue(self.store.state('peer')['quarantined'])


if __name__ == '__main__': unittest.main()
