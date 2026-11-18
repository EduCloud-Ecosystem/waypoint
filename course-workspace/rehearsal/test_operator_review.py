import unittest
from types import SimpleNamespace

from operator_review import validate, wait_for_review


class OperatorReviewTests(unittest.TestCase):
    def test_provider_options_and_unbounded_windows_refused(self):
        for seconds in (-1, 121, True, 1.5, '10'):
            with self.assertRaises(ValueError): validate(seconds)
        for options in ({'live_portage': '/repo'}, {'env_file': '/private'}, {'execute_approved': True}):
            with self.assertRaises(ValueError): validate(10, **options)
        validate(0, live_portage='/repo', env_file='/private', execute_approved=True)
        validate(120)

    def test_window_ends_without_extending_deadline_or_claiming_acceptance(self):
        clock = [0.0]
        page = SimpleNamespace(is_closed=lambda: False,
            wait_for_timeout=lambda ms: clock.__setitem__(0, clock[0] + ms / 1000))
        result = wait_for_review(SimpleNamespace(is_connected=lambda: True), page, 2, monotonic=lambda: clock[0])
        self.assertEqual(clock[0], 2)
        self.assertEqual(result['ended_by'], 'deadline')
        self.assertFalse(result['human_acceptance_recorded'])

    def test_closing_browser_ends_early(self):
        page = SimpleNamespace(is_closed=lambda: False)
        result = wait_for_review(SimpleNamespace(is_connected=lambda: False), page, 120)
        self.assertEqual(result['ended_by'], 'operator_closed')

    def test_close_during_browser_wait_is_reported_without_acceptance(self):
        page = SimpleNamespace(closed=False)
        page.is_closed = lambda: page.closed

        def close_during_wait(_):
            page.closed = True
            raise RuntimeError('page closed during wait')

        page.wait_for_timeout = close_during_wait
        result = wait_for_review(SimpleNamespace(is_connected=lambda: True), page, 2)
        self.assertEqual(result['ended_by'], 'operator_closed')
        self.assertFalse(result['human_acceptance_recorded'])

    def test_unrelated_browser_failure_is_not_accepted_as_operator_close(self):
        def fail(_): raise RuntimeError('browser failure')
        page = SimpleNamespace(is_closed=lambda: False, wait_for_timeout=fail)
        with self.assertRaises(RuntimeError):
            wait_for_review(SimpleNamespace(is_connected=lambda: True), page, 1)
