import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
from unittest.mock import patch
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).parent))
from test_runtime_authority import RuntimeFixture
from runtime_guardian import RuntimeGuardian
from runtime_authority import RuntimeRefusal
from hub.authority_client import AuthorityError


class GuardianTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); os.chmod(self.temp.name, 0o700)
        self.f = RuntimeFixture(self.temp.name)
    def tearDown(self): self.temp.cleanup()

    def client(self, course, failed=False, after_snapshot=None):
        fixture = self.f
        class Client:
            def call(self, operation, payload):
                if after_snapshot: after_snapshot()
                if failed: raise ConnectionError('bounded fixture outage')
                return fixture.dispatcher.dispatch(fixture.principal(course=course, role='guardian'), operation, course, payload)
        return Client()

    def test_authority_outage_stops_exact_course_and_preserves_peer_homes(self):
        f = self.f; first = f.spawn(); peer = f.spawn(course='course-peer')
        guardian = RuntimeGuardian(f.backend, 'worker-one', {'course-main': self.client('course-main', True), 'course-peer': self.client('course-peer')})
        guardian.tick()
        self.assertNotIn(first['server_id'], f.client.containers.items)
        self.assertTrue(f.client.containers.get(peer['server_id']).attrs['State']['Running'])
        self.assertEqual(len(f.client.volume_items), 6)
        with self.assertRaises(Exception): f.spawn()

    def test_failed_old_observation_cannot_remove_newer_docker_id(self):
        f = self.f; first = f.spawn(); later = []
        def replace():
            f.backend.remove(f.client.containers.get(first['server_id']), 'worker-one', 'course-main')
            later.append(f.spawn())
        guardian = RuntimeGuardian(f.backend, 'worker-one', {'course-main': self.client('course-main', True, replace), 'course-peer': self.client('course-peer')})
        guardian.tick()
        self.assertTrue(f.client.containers.get(later[0]['server_id']).attrs['State']['Running'])

    def test_restart_identity_requires_fresh_observation_not_old_heartbeat(self):
        f = self.f
        guardian_container = f.client.containers.get('worker-one-guardian')
        guardian_container.attrs['State']['StartedAt'] = '2026-10-14T00:01:00Z'
        with self.assertRaises(Exception): f.spawn()
        RuntimeGuardian(f.backend, 'worker-one', {'course-main': self.client('course-main'), 'course-peer': self.client('course-peer')}).tick()
        self.assertTrue(f.spawn()['server_id'])

    def test_wrong_guardian_course_set_refused(self):
        with self.assertRaises(Exception): RuntimeGuardian(self.f.backend, 'worker-one', {'course-main': self.client('course-main')})

    def test_authenticated_busy_reply_preserves_epoch_and_ids_without_heartbeat_and_peer_refreshes(self):
        f = self.f; main = f.spawn(); peer = f.spawn(course='course-peer')
        calls = []
        class Busy:
            def call(self, op, payload):
                calls.append(op)
                return {'authorized': False, 'deferred': True, 'worker_id': 'worker-one', 'course_id': 'course-main'}
        guardian = RuntimeGuardian(f.backend, 'worker-one', {'course-main': Busy(), 'course-peer': self.client('course-peer')})
        guardian.epochs['course-main'] = 1
        heartbeat = f.backend.require_guardian('worker-one', 'course-main', 1)
        f.now += 4
        with self.assertLogs('runtime_guardian', level='WARNING') as logs:
            self.assertFalse(guardian.tick_course('course-main'))
        self.assertEqual(logs.output, ['WARNING:runtime_guardian:Guardian snapshot deferred: endpoint_busy'])
        self.assertEqual(guardian.epochs['course-main'], 1); self.assertEqual(guardian.pending_stops, {})
        self.assertEqual(calls, ['guardian.observe'])
        with f.backend.connection() as conn:
            row = conn.execute('SELECT record FROM guardians WHERE endpoint=?', (f.backend.endpoint('worker-one', 'course-main'),)).fetchone()
        import json
        self.assertEqual(json.loads(row['record']), heartbeat)
        with self.assertRaises(RuntimeRefusal): f.backend.require_guardian('worker-one', 'course-main', 1)
        self.assertTrue(f.client.containers.get(main['server_id']).attrs['State']['Running'])
        self.assertTrue(guardian.tick_course('course-peer'))
        self.assertEqual(f.backend.require_guardian('worker-one', 'course-peer', 1)['checked_at'], f.now)
        f.dispatcher.supervision_tick()
        self.assertNotIn(main['server_id'], f.client.containers.items)
        self.assertTrue(f.client.containers.get(peer['server_id']).attrs['State']['Running'])

    def test_malformed_or_wrong_scope_busy_replies_stop_exact_owned_ids(self):
        f = self.f; peer = f.spawn(course='course-peer')
        correct = {'authorized': False, 'deferred': True, 'worker_id': 'worker-one', 'course_id': 'course-main'}
        replies = [dict(correct, worker_id='worker-two'), dict(correct, course_id='course-peer'),
                   dict(correct, epoch=1), dict(correct, authorized=0), dict(correct, deferred=1),
                   dict(correct, deferred=False), {'authorized': False, 'deferred': True}, None]
        for reply in replies:
            with self.subTest(reply=reply):
                f.heartbeat('worker-one', 'course-main'); main = f.spawn()
                class Malformed:
                    def call(self, op, payload): return reply
                guardian = RuntimeGuardian(f.backend, 'worker-one', {'course-main': Malformed(), 'course-peer': self.client('course-peer')})
                with self.assertLogs('runtime_guardian', level='WARNING') as logs:
                    self.assertTrue(guardian.tick_course('course-main'))
                self.assertEqual(logs.output, ['WARNING:runtime_guardian:Guardian observation refused: guardian_observation_invalid'])
                self.assertEqual(guardian.epochs['course-main'], 0); self.assertEqual(guardian.pending_stops, {})
                self.assertNotIn(main['server_id'], f.client.containers.items)
                self.assertTrue(f.client.containers.get(peer['server_id']).attrs['State']['Running'])
                self.assertEqual(len(f.client.volume_items), 6)

    def test_busy_reply_cannot_bypass_existing_failed_exact_id_stop(self):
        f = self.f; main = f.spawn(); calls = []
        class Busy:
            def call(self, op, payload):
                calls.append(op)
                return {'authorized': False, 'deferred': True, 'worker_id': 'worker-one', 'course_id': 'course-main'}
        guardian = RuntimeGuardian(f.backend, 'worker-one', {'course-main': Busy(), 'course-peer': self.client('course-peer')})
        pending = (f.backend.guardian_identity('worker-one'), 1, [(main['server_id'], 1)])
        guardian.pending_stops['course-main'] = pending
        with patch.object(f.backend, 'guardian_failclosed', side_effect=RuntimeRefusal('runtime_endpoint_lock_busy')):
            with self.assertLogs('runtime_guardian', level='WARNING'):
                self.assertFalse(guardian.tick_course('course-main'))
        self.assertEqual(calls, []); self.assertEqual(guardian.pending_stops['course-main'], pending)
        self.assertEqual(guardian.epochs['course-main'], 0)
        with self.assertLogs('runtime_guardian', level='WARNING'):
            self.assertFalse(guardian.tick_course('course-main'))
        self.assertEqual(calls, ['guardian.observe']); self.assertEqual(guardian.pending_stops, {})
        self.assertNotIn(main['server_id'], f.client.containers.items)

    def test_lost_reply_after_late_success_is_not_busy_and_still_stops_exact_ids(self):
        f = self.f; main = f.spawn(); peer = f.spawn(course='course-peer'); base = self.client('course-main')
        class Lost:
            def call(self, op, payload):
                base.call(op, payload)
                raise TimeoutError('PRIVATE_LOST_RESPONSE_CANARY')
        guardian = RuntimeGuardian(f.backend, 'worker-one', {'course-main': Lost(), 'course-peer': self.client('course-peer')})
        with self.assertLogs('runtime_guardian', level='WARNING') as logs:
            self.assertTrue(guardian.tick_course('course-main'))
        self.assertEqual(logs.output, ['WARNING:runtime_guardian:Guardian observation refused: guardian_observation_failed'])
        self.assertNotIn(main['server_id'], f.client.containers.items)
        self.assertTrue(f.client.containers.get(peer['server_id']).attrs['State']['Running'])
        self.assertEqual(guardian.epochs['course-main'], 0); self.assertEqual(guardian.pending_stops, {})

    def test_busy_snapshot_skips_heartbeat_without_exiting_or_affecting_peer(self):
        f = self.f; main = f.spawn(); peer = f.spawn(course='course-peer')
        calls = []
        base = self.client('course-main')
        class Client:
            def call(self, op, payload): calls.append(True); return base.call(op, payload)
        guardian = RuntimeGuardian(f.backend, 'worker-one', {'course-main': Client(), 'course-peer': self.client('course-peer')})
        held = threading.Event(); release = threading.Event()
        def holder():
            with f.backend.locks([('worker-one', 'course-main')]): held.set(); release.wait(10)
        thread = threading.Thread(target=holder); thread.start()
        try:
            self.assertTrue(held.wait(3)); f.now += 4; guardian.tick()
            self.assertEqual(calls, []); self.assertEqual(guardian.epochs['course-main'], 0)
            with self.assertRaises(RuntimeRefusal): f.backend.require_guardian('worker-one', 'course-main', 1)
            self.assertTrue(f.client.containers.get(peer['server_id']).attrs['State']['Running'])
        finally: release.set(); thread.join(5)
        self.assertFalse(thread.is_alive())
        f.dispatcher.supervision_tick()
        self.assertNotIn(main['server_id'], f.client.containers.items)
        self.assertTrue(f.client.containers.get(peer['server_id']).attrs['State']['Running'])

    def test_busy_failed_stop_retains_exact_ids_until_lock_available_without_renewal(self):
        f = self.f; main = f.spawn(); peer = f.spawn(course='course-peer')
        held = threading.Event(); release = threading.Event(); threads = []; calls = []
        def holder():
            with f.backend.locks([('worker-one', 'course-main')]): held.set(); release.wait(10)
        class FailedClient:
            def call(self, op, payload):
                calls.append(True)
                if not threads:
                    thread = threading.Thread(target=holder); threads.append(thread); thread.start()
                    if not held.wait(3): raise AssertionError('fixture failed to hold lock')
                raise ConnectionError('fixture outage')
        guardian = RuntimeGuardian(f.backend, 'worker-one', {'course-main': FailedClient(), 'course-peer': self.client('course-peer')})
        try:
            guardian.tick()
            self.assertEqual(guardian.pending_stops['course-main'][2], [(main['server_id'], 1)])
            f.now += 4; guardian.tick()
            self.assertEqual(len(calls), 1); self.assertEqual(guardian.epochs['course-main'], 0)
            with self.assertRaises(RuntimeRefusal): f.backend.require_guardian('worker-one', 'course-main', 1)
            self.assertTrue(f.client.containers.get(peer['server_id']).attrs['State']['Running'])
        finally:
            release.set()
            for thread in threads: thread.join(5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        guardian.tick()
        self.assertNotIn(main['server_id'], f.client.containers.items)
        self.assertNotIn('course-main', guardian.pending_stops)
        self.assertTrue(f.client.containers.get(peer['server_id']).attrs['State']['Running'])
        self.assertEqual(len(f.client.volume_items), 6)

    def test_observation_warning_is_allowlisted_and_never_exception_text(self):
        f = self.f
        for failure, expected in ((AuthorityError('authority_transport_unavailable'), 'authority_transport_unavailable'),
                                  (RuntimeRefusal('PRIVATE_TEST_CREDENTIAL'), 'guardian_observation_failed')):
            class Failed:
                def call(self, op, payload): raise failure
            guardian = RuntimeGuardian(f.backend, 'worker-one', {'course-main': Failed(), 'course-peer': self.client('course-peer')})
            with self.assertLogs('runtime_guardian', level='WARNING') as captured: guardian.tick()
            self.assertEqual(captured.output, ['WARNING:runtime_guardian:Guardian observation refused: ' + expected])
            self.assertNotIn('PRIVATE_TEST_CREDENTIAL', ''.join(captured.output))

    def test_independent_course_polling_refreshes_peer_while_other_rpc_blocks(self):
        f = self.f; f.spawn(); peer = f.spawn(course='course-peer')
        blocked = threading.Event(); release = threading.Event(); refreshed = threading.Event()
        stop = threading.Event(); errors = []; calls = []
        base = self.client('course-peer')
        class Blocked:
            def call(self, op, payload):
                blocked.set()
                if not release.wait(5): raise AssertionError('fixture release missing')
                return self_outer.client('course-main').call(op, payload)
        self_outer = self
        class Peer:
            def call(self, op, payload):
                calls.append(True)
                if len(calls) == 2: f.now += 4
                value = base.call(op, payload)
                if len(calls) >= 2: refreshed.set()
                return value
        guardian = RuntimeGuardian(f.backend, 'worker-one', {'course-main': Blocked(), 'course-peer': Peer()})
        def run():
            try: guardian.run(stop, interval=0.1)
            except BaseException as error: errors.append(error)
        thread = threading.Thread(target=run); thread.start()
        try:
            self.assertTrue(blocked.wait(2)); self.assertTrue(refreshed.wait(3))
            self.assertEqual(guardian.epochs['course-main'], 0)
            self.assertEqual(f.backend.require_guardian('worker-one', 'course-peer', 1)['checked_at'], f.now)
            with self.assertRaises(RuntimeRefusal) as refused:
                f.backend.require_guardian('worker-one', 'course-main', 1)
            self.assertEqual(refused.exception.code, 'guardian_stale_or_changed')
            self.assertTrue(f.client.containers.get(peer['server_id']).attrs['State']['Running'])
        finally:
            stop.set(); release.set(); thread.join(6)
        self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])

    def test_unexpected_course_enforcer_failure_exits_even_with_other_poll_blocked(self):
        guardian = RuntimeGuardian(self.f.backend, 'worker-one', {'course-main': self.client('course-main'), 'course-peer': self.client('course-peer')})
        blocked = threading.Event(); release = threading.Event(); finished = threading.Event()
        stop = threading.Event()
        def tick(course):
            if course == 'course-peer':
                blocked.set(); release.wait(5); finished.set()
            else:
                if not blocked.wait(2): raise AssertionError('peer worker never started')
                raise RuntimeRefusal('guardian_container_unavailable')
        started = time.monotonic()
        try:
            with patch.object(guardian, 'tick_course', side_effect=tick):
                with self.assertRaises(RuntimeRefusal) as refused: guardian.run(stop, interval=0.1)
            self.assertEqual(refused.exception.code, 'guardian_container_unavailable')
            self.assertTrue(stop.is_set()); self.assertLess(time.monotonic() - started, 2)
        finally: release.set(); self.assertTrue(finished.wait(2))

    def test_poll_thread_count_is_bounded_and_unknown_course_refused(self):
        backend = type('Backend', (), {'config': {'courses': {'worker': {str(i): {} for i in range(17)}}}})()
        with self.assertRaises(RuntimeRefusal): RuntimeGuardian(backend, 'worker', {str(i): None for i in range(17)})
        guardian = RuntimeGuardian(self.f.backend, 'worker-one', {'course-main': self.client('course-main'), 'course-peer': self.client('course-peer')})
        with self.assertRaises(RuntimeRefusal): guardian.tick_course('unapproved-course')

    def test_snapshot_acquires_periodic_real_flock_gap_and_peer_keeps_polling(self):
        f = self.f; stop = threading.Event(); holder_stop = threading.Event()
        held = threading.Event(); source_seen = threading.Event(); peer_seen = threading.Event()
        calls = {'course-main': 0, 'course-peer': 0}; errors = []
        def holder():
            while not holder_stop.is_set():
                with f.backend.locks([('worker-one', 'course-main')]):
                    held.set(); holder_stop.wait(0.25)
                holder_stop.wait(0.12)
        def client(course):
            base = self.client(course)
            class Client:
                def call(self, op, payload):
                    result = base.call(op, payload)
                    if result.get('authorized') is True:
                        calls[course] += 1
                        if calls[course] >= 2: (source_seen if course == 'course-main' else peer_seen).set()
                    return result
            return Client()
        guardian = RuntimeGuardian(f.backend, 'worker-one', {course: client(course) for course in calls})
        def run():
            try: guardian.run(stop, interval=0.1)
            except BaseException as error: errors.append(error)
        owner = threading.Thread(target=holder); owner.start()
        runner = threading.Thread(target=run)
        try:
            self.assertTrue(held.wait(2)); runner.start()
            self.assertTrue(peer_seen.wait(3)); self.assertTrue(source_seen.wait(4))
            self.assertEqual(guardian.epochs, {'course-main': 1, 'course-peer': 1})
            self.assertEqual(f.backend.require_guardian('worker-one', 'course-main', 1)['epoch'], 1)
            self.assertEqual(f.backend.require_guardian('worker-one', 'course-peer', 1)['epoch'], 1)
        finally:
            stop.set(); holder_stop.set(); owner.join(5)
            if runner.ident is not None: runner.join(6)
        self.assertFalse(owner.is_alive()); self.assertFalse(runner.is_alive()); self.assertEqual(errors, [])

    def test_busy_diagnostics_are_fixed_rate_bounded_and_do_not_refresh(self):
        f = self.f; calls = []; clock = [10.0]
        class Client:
            def call(self, op, payload): calls.append(True); raise AssertionError('busy snapshot must not issue RPC')
        guardian = RuntimeGuardian(f.backend, 'worker-one', {'course-main': Client(), 'course-peer': self.client('course-peer')}, monotonic=lambda: clock[0])
        with patch.object(f.backend, 'guardian_snapshot', side_effect=RuntimeRefusal('runtime_endpoint_lock_busy')):
            with self.assertLogs('runtime_guardian', level='WARNING') as logs:
                self.assertFalse(guardian.tick_course('course-main'))
                clock[0] += 4; self.assertFalse(guardian.tick_course('course-main'))
                clock[0] += 1; self.assertFalse(guardian.tick_course('course-main'))
            self.assertEqual(logs.output, ['WARNING:runtime_guardian:Guardian snapshot deferred: endpoint_busy'] * 2)
        self.assertEqual(calls, []); self.assertEqual(guardian.epochs['course-main'], 0)
        self.assertEqual(guardian.pending_stops, {})

    def test_busy_run_cycle_uses_short_delay_without_rpc_retry(self):
        stop = threading.Event(); delays = []; calls = []
        backend = type('Backend', (), {'config': {'courses': {'worker': {'course': {}}}}})()
        guardian = RuntimeGuardian(backend, 'worker', {'course': None})
        def tick(course): calls.append(course); return len(calls) > 1
        original = stop.wait
        def wait(delay):
            if threading.current_thread().name == 'guardian-course-poll':
                delays.append(delay)
                if len(delays) == 2: stop.set()
            return original(0.001)
        with patch.object(guardian, 'tick_course', side_effect=tick), patch.object(stop, 'wait', side_effect=wait):
            guardian.run(stop, interval=1)
        self.assertEqual(delays, [0.1, 1]); self.assertEqual(calls, ['course', 'course'])

    def test_cli_fatal_exception_and_interrupt_emit_fixed_failure_only(self):
        guardian = Path(__file__).resolve().parents[1] / 'runtime_guardian.py'
        for failure in ('RuntimeError', 'KeyboardInterrupt'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                marker = root / 'backend-constructor-called'
                canary = 'guardian-private-error-canary'
                (root / 'sitecustomize.py').write_text(
                    "import runtime_authority\nfrom pathlib import Path\n"
                    "def private_failure(*args, **kwargs):\n"
                    "    Path(" + repr(str(marker)) + ").write_text('called')\n"
                    "    raise " + failure + "('guardian-private-error-canary')\n"
                    "runtime_authority.DockerBackend = private_failure\n")
                config = root / 'config.json'; config.write_text('{}'); config.chmod(0o600)
                env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(root), str(guardian.parent))))
                result = subprocess.run([sys.executable, str(guardian), '--config', str(config),
                    '--worker', 'worker', '--client-config', str(root / 'unused-client.json')],
                    env=env, capture_output=True, text=True, timeout=10)
                self.assertTrue(marker.exists())  # No real Docker constructor or commands ran.
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')
                self.assertEqual(result.stderr.strip(), 'guardian_service_failed')
                self.assertNotIn(canary, result.stdout + result.stderr)
                self.assertNotIn('Traceback', result.stderr)
        result = subprocess.run([sys.executable, str(guardian), '--help'],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertIn('usage:', result.stdout)
        self.assertNotIn('guardian_service_failed', result.stdout + result.stderr)


if __name__ == '__main__': unittest.main()
