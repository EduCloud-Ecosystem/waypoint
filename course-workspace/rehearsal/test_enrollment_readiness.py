"""Startup transport polling must never mask a logical authorization denial."""
import unittest
import contextlib
import io
import json
from verify_worker_enrollment import await_hub_authorization, await_guardian_authorization, verify_contention_batch, await_contention_release


def authorized():
    return {'ok': True, 'result': {'authorized': True, 'epoch': 1,
        'worker_id': 'worker-one', 'course_id': 'course-main'}}


class EnrollmentReadinessTests(unittest.TestCase):
    def test_contention_release_requires_exit_and_fresh_heartbeat_and_keeps_bounded_status(self):
        status = {'held': True, 'holder_exited': False, 'heartbeat_renewed': False,
            'heartbeat_unchanged': True, 'heartbeat_expired': False}
        states = [dict(status), dict(status), dict(status, held=False),
            dict(status, held=False, holder_exited=True),
            dict(status, held=False, holder_exited=True, heartbeat_renewed=True, heartbeat_unchanged=False)]
        replies = iter(states); sleeps = []; output = io.StringIO()
        with contextlib.redirect_stdout(output):
            await_contention_release(lambda: next(replies), sleep=sleeps.append)
        self.assertEqual(len(sleeps), 4)
        records = [json.loads(line.removeprefix('CONTENTION_STATUS ')) for line in output.getvalue().splitlines()]
        self.assertEqual(records, [states[0], *states[2:]])

    def test_contention_probe_error_is_immediate_and_private_message_is_not_logged(self):
        output = io.StringIO(); calls = []
        def broken(): calls.append(True); raise OSError('PRIVATE_PROBE_CANARY')
        with contextlib.redirect_stdout(output), self.assertRaisesRegex(RuntimeError, '^fixture_contention_status_unavailable$'):
            await_contention_release(broken, sleep=lambda _: self.fail('probe errors never retry'))
        self.assertEqual(calls, [True]); self.assertNotIn('PRIVATE_PROBE_CANARY', output.getvalue())
        self.assertEqual(output.getvalue(), 'CONTENTION_STATUS_REFUSAL {"category": "OSError"}\n')

    def test_contention_malformed_or_expired_read_never_becomes_waited_success(self):
        valid = {'held': False, 'holder_exited': True, 'heartbeat_renewed': True,
            'heartbeat_unchanged': False, 'heartbeat_expired': False}
        for value in (None, {}, dict(valid, held=0), dict(valid, heartbeat_unchanged=True), dict(valid, private='PRIVATE_STATUS_CANARY')):
            output = io.StringIO()
            with self.subTest(value=value), contextlib.redirect_stdout(output), self.assertRaisesRegex(ValueError, '^fixture_contention_status_invalid$'):
                await_contention_release(lambda: value, sleep=lambda _: self.fail('malformed response never retries'))
            self.assertEqual(output.getvalue(), '')
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, '^fixture_contention_heartbeat_expired$'):
            await_contention_release(lambda: dict(valid, heartbeat_expired=True), sleep=lambda _: self.fail('expired heartbeat never retries'))

    def test_contention_release_timeout_retains_false_conjunct_without_extending_budget(self):
        now = [0.0]; calls = []
        status = {'held': False, 'holder_exited': True, 'heartbeat_renewed': False,
            'heartbeat_unchanged': True, 'heartbeat_expired': False}
        def probe(): calls.append(True); return dict(status)
        def sleep(seconds): now[0] += seconds
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaisesRegex(RuntimeError, '^fixture_contention_release_timeout$'):
            await_contention_release(probe, timeout=.5, monotonic=lambda: now[0], sleep=sleep)
        self.assertEqual(now[0], .5); self.assertEqual(len(calls), 3)
        self.assertEqual(json.loads(output.getvalue().removeprefix('CONTENTION_STATUS ')), status)
        for timeout in (True, 0, -1, 11, float('nan'), float('inf')):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(ValueError, '^fixture_contention_timeout_invalid$'):
                await_contention_release(lambda: self.fail('invalid timeout must not probe'), timeout=timeout)

    def test_contention_probe_cannot_report_late_success_after_the_wait_budget(self):
        now = [0.0]
        def slow():
            now[0] = 11
            return {'held': False, 'holder_exited': True, 'heartbeat_renewed': True,
                'heartbeat_unchanged': False, 'heartbeat_expired': False}
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, '^fixture_contention_release_timeout$'):
            await_contention_release(slow, monotonic=lambda: now[0], sleep=lambda _: self.fail('no retries'))

    def test_batch_requires_exact_two_held_busy_unchanged_heartbeat_proofs(self):
        from copy import deepcopy
        value = {'observations': [{'deferred': True, 'held_before': True, 'held_after': True,
            'heartbeat_unchanged': True, 'elapsed_ms': 100.0}] * 2, 'expiry_while_held': False}
        verify_contention_batch(value, expiry=False)
        for key in ('deferred', 'held_before', 'held_after', 'heartbeat_unchanged'):
            altered = deepcopy(value); altered['observations'][0][key] = False
            with self.subTest(key=key), self.assertRaises(AssertionError): verify_contention_batch(altered, expiry=False)
        for bad in (True, 3000, -1, float('nan')):
            altered = deepcopy(value); altered['observations'][0]['elapsed_ms'] = bad
            with self.subTest(bad=bad), self.assertRaises(AssertionError): verify_contention_batch(altered, expiry=False)
        altered = deepcopy(value); altered['observations'].pop()
        with self.assertRaises(AssertionError): verify_contention_batch(altered, expiry=False)
        with self.assertRaises(AssertionError): verify_contention_batch(value, expiry=True)
    def test_guardian_positive_retries_only_explicit_authenticated_busy(self):
        replies = iter([{'authorized': False, 'deferred': True}] * 2 + [{'authorized': True}])
        sleeps = []
        await_guardian_authorization(lambda: next(replies), sleep=sleeps.append)
        self.assertEqual(sleeps, [.1, .1])

    def test_guardian_denial_malformed_and_transport_failures_never_retry(self):
        for reply in ({'authorized': False, 'denied': True}, {'authorized': False}, {'authorized': 1},
                      {'authorized': False, 'deferred': 1}, {'authorized': 0, 'deferred': True},
                      {'authorized': False, 'deferred': True, 'epoch': 1}, None):
            with self.subTest(reply=reply), self.assertRaisesRegex(ValueError, '^fixture_guardian_probe_invalid$'):
                await_guardian_authorization(lambda: reply, sleep=lambda _: self.fail('not a busy reply'))
        error = TimeoutError('PRIVATE_RESPONSE_CANARY')
        def failed(): raise error
        with self.assertRaises(TimeoutError) as caught:
            await_guardian_authorization(failed, sleep=lambda _: self.fail('transport cannot retry'))
        self.assertIs(caught.exception, error)

    def test_guardian_busy_retries_are_bounded_and_timeout_typed(self):
        now = [0.0]; calls = []
        def busy(): calls.append(True); return {'authorized': False, 'deferred': True}
        def sleep(seconds): now[0] += seconds
        with self.assertRaisesRegex(RuntimeError, '^fixture_guardian_probe_busy$'):
            await_guardian_authorization(busy, timeout=.2, monotonic=lambda: now[0], sleep=sleep)
        self.assertEqual(len(calls), 3); self.assertEqual(now[0], .2)
        for timeout in (True, 0, -1, 11, float('nan'), float('inf')):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(ValueError, '^fixture_guardian_probe_timeout_invalid$'):
                await_guardian_authorization(lambda: self.fail('bad timeout must refuse before probe'), timeout=timeout)

    def test_transport_then_exact_current_authorization(self):
        replies = iter([{'ok': False, 'refused': True, 'code': 'authority_transport_unavailable'}, authorized()])
        sleeps = []
        await_hub_authorization(lambda: next(replies), sleep=sleeps.append)
        self.assertEqual(sleeps, [0.2])

    def test_logical_denial_and_unknown_failure_are_immediate(self):
        for reply in ({'ok': False, 'code': 'authority_access_refused'},
                      {'ok': False, 'code': 'authority_operation_refused'}, None, {}, {'ok': 1},
                      {'ok': False, 'code': 'authority_transport_unavailable'},
                      {'ok': False, 'refused': 1, 'code': 'authority_transport_unavailable'},
                      {'ok': False, 'refused': True, 'code': 'authority_transport_unavailable', 'result': {'authorized': False}}):
            with self.subTest(reply=reply), self.assertRaises(AssertionError):
                await_hub_authorization(lambda: reply, sleep=lambda _: self.fail('denial must not retry'))
        reply = authorized(); reply['code'] = 'authority_access_refused'
        with self.assertRaises(AssertionError): await_hub_authorization(lambda: reply)

    def test_success_requires_exact_scope_boolean_epoch_and_no_deferred_grant(self):
        for field, value in [('authorized', 1), ('epoch', True), ('epoch', 0),
                             ('worker_id', 'worker-two'), ('course_id', 'course-peer'), ('deferred', True)]:
            reply = authorized(); reply['result'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(AssertionError):
                await_hub_authorization(lambda: reply)

    def test_continuous_transport_failure_is_bounded(self):
        now = [0.0]; calls = []
        def call(): calls.append(True); return {'ok': False, 'refused': True, 'code': 'authority_transport_unavailable'}
        def sleep(seconds): now[0] += seconds
        with self.assertRaisesRegex(RuntimeError, '^fixture_hub_startup_unavailable$'):
            await_hub_authorization(call, timeout=0.4, monotonic=lambda: now[0], sleep=sleep)
        self.assertEqual(len(calls), 3)


if __name__ == '__main__': unittest.main()
