"""Credential replacement must refuse scope/candidate drift before Docker effects."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
import enrollment_fixture as fixture
from hub.authority_client import AuthorityError
from test_runtime_authority import RuntimeFixture


class EnrollmentInstallTests(unittest.TestCase):
    def test_stale_or_symlink_holder_record_refuses_before_process_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); root.chmod(0o700); (root / 'control').mkdir(mode=0o700)
            holder = root / 'control' / 'enrollment-holder-course-main.json'
            for symlink in (False, True):
                if symlink: holder.symlink_to(root / 'absent')
                else: fixture.publish(holder, {'pid': 123, 'start_identity': 'a' * 64})
                try:
                    with patch.object(fixture.subprocess, 'Popen', side_effect=AssertionError('no untracked child')):
                        with self.assertRaisesRegex(ValueError, '^fixture_contention_reuse$'):
                            fixture._start_holder(root, 'course-main', 4)
                finally: holder.unlink()

    def test_batch_startup_is_outside_hold_and_later_normal_renewal_is_not_locked_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); root.chmod(0o700); (root / 'control').mkdir(mode=0o700)
            f = RuntimeFixture(root); now = [0.0]; calls = []; released = threading.Event(); held = threading.Event()
            child = types.SimpleNamespace(pid=123, poll=lambda: None)
            record = {'pid': 123, 'start_identity': 'a' * 64}
            threads = []
            class Client:
                def __init__(self, **options): calls.append('client-ready')
                def call(self, operation, identity):
                    calls.append(operation); now[0] += .25
                    return f.dispatcher.dispatch(f.principal(role='guardian'), operation, 'course-main', identity)
            def start(root, course, seconds):
                self.assertEqual(calls, ['client-ready'])
                now[0] += 20  # Slow child/backend startup must not spend the4s hold.
                f.now += 20; f.heartbeat('worker-one', 'course-main')  # Normal Guardian setup, not batch renewal.
                def owner():
                    with f.backend.locks([('worker-one', course)]):
                        raw = fixture._heartbeat(f.backend, course)
                        ready = {'held': True, 'holder': record, 'seconds': seconds, 'started': now[0],
                            'release_at': now[0] + seconds, 'checked_at': fixture._heartbeat_time(raw),
                            'heartbeat_sha256': fixture.digest(raw)}
                        fixture.publish(root / 'control' / ('enrollment-holder-' + course + '.json'), record)
                        fixture.publish(root / 'control' / ('enrollment-lock-' + course + '.json'), ready)
                        held.set(); released.wait(5)
                        fixture.publish(root / 'control' / ('enrollment-release-' + course + '.json'), dict(ready, held=False))
                thread = threading.Thread(target=owner); threads.append(thread); thread.start()
                self.assertTrue(held.wait(2)); return child
            try:
                with patch.object(fixture, 'read_record', wraps=fixture.read_record) as reader, patch.object(fixture, 'AuthorityClient', Client):
                    real_read = fixture.read_record._mock_wraps
                    reader.side_effect = lambda path: {} if str(path).endswith('client.json') else real_read(path)
                    result = fixture._contention_batch(root, f.backend, 'course-main', 4, start_holder=start,
                        monotonic=lambda: now[0], process_state=lambda _: 'R')
                self.assertEqual(calls, ['client-ready', 'guardian.observe', 'guardian.observe'])
                self.assertEqual(len(result['observations']), 2); self.assertFalse(result['expiry_while_held'])
                self.assertEqual(now[0], 20.5)
                self.assertTrue(all(item['held_before'] and item['held_after'] and item['heartbeat_unchanged'] for item in result['observations']))
            finally:
                released.set()
                for thread in threads: thread.join(5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            now[0] += 5; f.now += 5; f.heartbeat('worker-one', 'course-main')
            with patch.object(fixture, 'context', return_value=(f.config, {}, f.backend, None)), patch.object(fixture, 'service_state', return_value=None):
                status = fixture._dispatch(root, 'enrollment-contention-status', {'course': 'course-main'})
            self.assertFalse(status['held']); self.assertTrue(status['holder_exited']); self.assertTrue(status['heartbeat_renewed'])
            self.assertFalse(status['heartbeat_unchanged'])

    def test_batch_cannot_claim_old_marker_after_release_or_exhausted_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); root.chmod(0o700); (root / 'control').mkdir(mode=0o700)
            f = RuntimeFixture(root); raw = fixture._heartbeat(f.backend, 'course-main')
            ready = {'held': True, 'holder': {'pid': 123, 'start_identity': 'a' * 64}, 'seconds': 4,
                'started': 10, 'release_at': 14, 'checked_at': fixture._heartbeat_time(raw), 'heartbeat_sha256': fixture.digest(raw)}
            fixture.publish(root / 'control' / 'enrollment-lock-course-main.json', ready)
            child = types.SimpleNamespace(pid=123, poll=lambda: None)
            for clock in (14, 20):
                with self.subTest(clock=clock), self.assertRaisesRegex(ValueError, '^fixture_contention_window_invalid$'):
                    fixture._held_heartbeat(root, f.backend, 'course-main', child, monotonic=lambda: clock, process_state=lambda _: 'R')
            # An old READY with a live-looking PID never substitutes for a real flock.
            with self.assertRaisesRegex(ValueError, '^fixture_contention_not_held$'):
                fixture._held_heartbeat(root, f.backend, 'course-main', child, monotonic=lambda: 11, process_state=lambda _: 'R')
            fixture.publish(root / 'control' / 'enrollment-release-course-main.json', dict(ready, held=False))
            f.now += 1; f.heartbeat('worker-one', 'course-main')
            with self.assertRaisesRegex(ValueError, '^fixture_contention_window_invalid$'):
                fixture._held_heartbeat(root, f.backend, 'course-main', child, monotonic=lambda: 11, process_state=lambda _: 'R')

    def test_thirteen_second_batch_reads_expiry_inside_window_without_retry_or_renewal(self):
        now = [0.0]; calls = []; checked = []
        backend = types.SimpleNamespace(clock=lambda: now[0], guardian_identity=lambda _: {},
            config={'guardians': {'worker-one': {'heartbeat_seconds': 10}}})
        class Client:
            def call(self, operation, identity):
                calls.append(operation); now[0] += .1
                return {'authorized': False, 'deferred': True, 'worker_id': 'worker-one', 'course_id': 'course-peer'}
        def held(*args, **kwargs):
            self.assertLess(now[0], 13); checked.append(now[0]); return {'checked_at': 0}
        def sleep(seconds): now[0] += seconds
        with patch.object(fixture, 'read_record', return_value={}), patch.object(fixture, 'AuthorityClient', return_value=Client()), patch.object(fixture, '_held_heartbeat', side_effect=held):
            result = fixture._contention_batch(Path('/unused'), backend, 'course-peer', 13,
                start_holder=lambda *args: object(), monotonic=lambda: now[0], sleep=sleep)
        self.assertTrue(result['expiry_while_held']); self.assertEqual(calls, ['guardian.observe'] * 2)
        self.assertGreater(now[0], 10); self.assertLess(now[0], 13)
        self.assertGreater(len(checked), 4)

    def test_holder_reads_normal_heartbeat_and_keeps_exact_configured_sleep_without_rpc(self):
        for seconds in (4, 13):
            with self.subTest(seconds=seconds), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve(); root.chmod(0o700); (root / 'control').mkdir(mode=0o700)
                f = RuntimeFixture(root); fixture.write(root / 'runtime.json', f.config)
                fixture.publish(root / 'control' / 'enrollment-holder-course-main.json', {'pid': 123, 'start_identity': 'a' * 64})
                f.now += 2; sleeps = []
                def sleep(delay):
                    sleeps.append(delay)
                    if delay == .05: f.heartbeat('worker-one', 'course-main')
                with patch.object(fixture, 'DockerBackend', return_value=f.backend), patch.object(fixture.os, 'getpid', return_value=123), patch.object(fixture, 'process_identity', return_value='a' * 64), patch.object(fixture.time, 'sleep', side_effect=sleep), patch.object(fixture, 'AuthorityClient', side_effect=AssertionError('no heartbeat seeding RPC')):
                    fixture._hold_endpoint(root, 'course-main', seconds)
                self.assertEqual(sleeps, [.05, seconds])
                ready = fixture.read_record(root / 'control' / 'enrollment-lock-course-main.json')
                ended = fixture._contention_marker(root, 'course-main')
                self.assertEqual(ended, dict(ready, held=False)); self.assertEqual(ready['release_at'] - ready['started'], seconds)
                self.assertEqual(ready['checked_at'], f.now)
                self.assertEqual(ready['heartbeat_sha256'], fixture.digest(fixture._heartbeat(f.backend, 'course-main')))

    def test_holder_refuses_expired_future_or_invalid_normal_heartbeat_before_hold(self):
        for delta in (-1, 4):
            with self.subTest(delta=delta), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve(); root.chmod(0o700); (root / 'control').mkdir(mode=0o700)
                f = RuntimeFixture(root); fixture.write(root / 'runtime.json', f.config)
                fixture.publish(root / 'control' / 'enrollment-holder-course-main.json', {'pid': 123, 'start_identity': 'a' * 64})
                f.now += delta
                with patch.object(fixture, 'DockerBackend', return_value=f.backend), patch.object(fixture.os, 'getpid', return_value=123), patch.object(fixture, 'process_identity', return_value='a' * 64), patch.object(fixture.time, 'sleep', side_effect=AssertionError('invalid lease cannot wait or hold')):
                    with self.assertRaisesRegex(ValueError, '^fixture_contention_heartbeat_stale$'):
                        fixture._hold_endpoint(root, 'course-main', 4)
                self.assertFalse((root / 'control' / 'enrollment-lock-course-main.json').exists())
        for value in (None, '{"checked_at":true}', '{"checked_at":NaN}', '{"checked_at":-1}'):
            with self.subTest(value=value), self.assertRaises(ValueError): fixture._heartbeat_time(value)

    def test_guardian_probe_distinguishes_valid_grant_busy_and_actual_access_denial(self):
        class Client:
            def __init__(self, response): self.response = response
            def call(self, op, payload):
                self_outer.assertEqual(op, 'guardian.observe'); self_outer.assertEqual(payload, {'identity': 'synthetic'})
                if isinstance(self.response, Exception): raise self.response
                return self.response
        self_outer = self
        grant = {'authorized': True, 'epoch': 1, 'worker_id': 'worker-two', 'course_id': 'course-main'}
        busy = {'authorized': False, 'deferred': True, 'worker_id': 'worker-two', 'course_id': 'course-main'}
        for response, expected in ((grant, {'authorized': True}), (busy, {'authorized': False, 'deferred': True}),
                                   (AuthorityError('authority_access_refused'), {'authorized': False, 'denied': True})):
            self.assertEqual(fixture._guardian_probe(Client(response), {'identity': 'synthetic'}, 'worker-two', 'course-main'), expected)
        malformed = [dict(grant, authorized=1), dict(grant, epoch=True), dict(grant, worker_id='worker-one'),
            dict(grant, course_id='course-peer'), dict(grant, extra='private'), dict(busy, deferred=1),
            dict(busy, authorized=0), dict(busy, epoch=1), dict(busy, worker_id='worker-one'),
            {'authorized': False}, None]
        for response in malformed:
            with self.subTest(response=response), self.assertRaisesRegex(ValueError, '^fixture_guardian_probe_invalid$'):
                fixture._guardian_probe(Client(response), {'identity': 'synthetic'}, 'worker-two', 'course-main')

    def test_guardian_probe_never_converts_transport_unexpected_or_operation_failures_to_denial(self):
        for error in (AuthorityError('authority_transport_unavailable'), AuthorityError('authority_operation_refused'),
                      AuthorityError('authority_response_invalid'), RuntimeError('PRIVATE_PROBE_CANARY')):
            class Failed:
                def call(self, op, payload): raise error
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)) as caught:
                fixture._guardian_probe(Failed(), {}, 'worker-two', 'course-main')
            self.assertIs(caught.exception, error)

    def test_contention_probe_requires_exact_authenticated_no_grant_shape(self):
        result = {'authorized': False, 'deferred': True, 'worker_id': 'worker-one', 'course_id': 'course-main'}
        self.assertTrue(fixture._deferred(result, 'worker-one', 'course-main'))
        for field, value in [('authorized', 0), ('deferred', 1), ('worker_id', 'worker-two'),
                             ('course_id', 'course-peer'), ('epoch', 1)]:
            changed = dict(result); changed[field] = value
            self.assertFalse(fixture._deferred(changed, 'worker-one', 'course-main'))
        for value in (None, {}, dict(result, authorized=True)):
            self.assertFalse(fixture._deferred(value, 'worker-one', 'course-main'))

    def test_contention_holder_rejects_unconfigured_scope_or_unbounded_duration(self):
        for course, seconds in [('other', 4), ('course-main', 999), ('course-peer', -1)]:
            with patch.object(fixture, 'DockerBackend', side_effect=AssertionError('no provider before scope validation')):
                with self.assertRaisesRegex(ValueError, '^fixture_contention_invalid$'):
                    fixture._hold_endpoint(Path('/unused'), course, seconds)

    def test_other_live_role_worker_course_or_candidate_refused_before_docker(self):
        certificate = {'fingerprint': 'a' * 64, 'spki_sha256': 'b' * 64, 'not_before': 1, 'not_after': 9999999999}
        valid_entry = {'role': 'hub', 'workers': ['worker-one'], 'courses': ['course-main']}
        variants = [None, {'role': 'guardian', 'workers': ['worker-one'], 'courses': ['course-main']},
            {'role': 'hub', 'workers': ['worker-two'], 'courses': ['course-main']},
            {'role': 'hub', 'workers': ['worker-one'], 'courses': ['course-peer']},
            {'role': 'hub', 'workers': ['worker-one'], 'courses': ['course-main', 'course-peer']}]
        for entry in variants + [valid_entry]:
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve(); previous = root / 'source' / 'credentials'
                previous.mkdir(parents=True); old = previous / 'client.crt'; old.write_text('original certificate sentinel')
                candidate = deepcopy(certificate)
                if entry == valid_entry: candidate['fingerprint'] = 'c' * 64
                fixture.write(root / 'enrollment-source-candidate.json', candidate)
                config = {'courses': {'worker-one': {'course-main': {}}}}
                service = {'ca_file': '/unused/ca.pem'}
                store = types.SimpleNamespace(entries=lambda: {} if entry is None else {certificate['fingerprint']: entry})
                docker = types.SimpleNamespace(DockerClient=lambda **kwargs: self.fail('Docker effect before scope verification'))
                with patch.dict(sys.modules, {'docker': docker}), patch.object(fixture, 'context', return_value=(config, service, None, store)), patch.object(fixture, 'verify_certificate', return_value=certificate):
                    with self.assertRaises(ValueError): fixture.dispatch(root, 'enrollment-install', {'identity': 'source'})
                self.assertEqual(old.read_text(), 'original certificate sentinel')
                self.assertFalse((root / 'source-previous').exists())

    def test_failure_diagnostic_never_exposes_exception_or_private_file_value(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            error = ValueError('private fixture exception canary')
            with patch.object(fixture, '_dispatch', side_effect=error):
                with self.assertRaises(ValueError): fixture.dispatch(root, 'enrollment-create', {})
            self.assertEqual(fixture.dispatch(root, 'enrollment-failure-status', {}), {'code': 'fixture_enrollment_refused'})
            fixture.write(root / 'enrollment-failure.json', {'code': 'private fixture file canary'})
            self.assertEqual(fixture.dispatch(root, 'enrollment-failure-status', {}), {'code': 'fixture_enrollment_refused'})


if __name__ == '__main__': unittest.main()
