"""Startup transport polling must never mask a logical authorization denial."""
import unittest
from verify_worker_enrollment import await_hub_authorization, await_guardian_authorization


def authorized():
    return {'ok': True, 'result': {'authorized': True, 'epoch': 1,
        'worker_id': 'worker-one', 'course_id': 'course-main'}}


class EnrollmentReadinessTests(unittest.TestCase):
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
