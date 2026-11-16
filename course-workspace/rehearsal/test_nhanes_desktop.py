"""No runtime: a receipt after restart must use a fresh authenticated session."""
import unittest
from unittest.mock import Mock, patch
import nhanes_desktop as desktop


class RestartReceiptTests(unittest.TestCase):
    def test_reauthenticates_and_preserves_open_notebook_context(self):
        events = []
        course = Mock(url='http://127.0.0.1:12345')
        course.restart.side_effect = lambda: events.append('restart')
        fresh = Mock()
        course.login.side_effect = lambda *args: (events.append('login') or fresh, None)
        fresh.cookies.return_value = [{'name':'synthetic-cookie'}]
        learner = Mock(); browser = object(); receipt = {}
        with patch.object(desktop, 'verify_receipt_after_restart', side_effect=lambda *args: events.append('verify')) as verify:
            desktop.restart_receipt(course, browser, learner, 'synthetic-submission', receipt)
        self.assertEqual(events, ['restart','login','verify'])
        course.login.assert_called_once_with(browser, 'alice', '/student/login?host=forgejo')
        verify.assert_called_once_with(course, fresh, 'synthetic-submission', receipt)
        fresh.cookies.assert_called_once_with(course.url)
        learner.add_cookies.assert_called_once_with(fresh.cookies.return_value)
        learner.close.assert_not_called()
        fresh.close.assert_called_once()

    def test_failed_readback_stays_failed_and_closes_fresh_context(self):
        course = Mock(); fresh = Mock(); learner = Mock()
        course.login.return_value = (fresh, None)
        with patch.object(desktop, 'verify_receipt_after_restart', side_effect=AssertionError('receipt mismatch')):
            with self.assertRaisesRegex(AssertionError, 'receipt mismatch'):
                desktop.restart_receipt(course, object(), learner, 'synthetic-submission', {})
        fresh.close.assert_called_once()
        learner.add_cookies.assert_not_called()


if __name__ == '__main__':unittest.main()
