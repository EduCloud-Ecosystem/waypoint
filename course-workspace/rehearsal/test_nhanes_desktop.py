"""Offline controls for NHANES first use, manual sign-in and receipt recovery."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from cairn_journey import BrowserError, BrowserTimeout, Course, SignInNotStarted
import nhanes_desktop as desktop


class FirstUseControls(unittest.TestCase):
    def setUp(self):
        self.quiet = {'control_jobs': 0, 'control_attempts': 0}
        self.tutor = Mock(live=None)
        self.tutor.counts.return_value = self.quiet.copy()
        workspace = TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.notebook = Path(workspace.name) / 'starter.ipynb'
        self.original = b'{"cells": []}\n'
        self.notebook.write_bytes(self.original)

    def verify_navigation(self):
        desktop.verify_passive_navigation(self.tutor, self.quiet, self.notebook, self.original)

    def test_navigation_rejects_nonzero_counts_even_when_unchanged(self):
        for key in self.quiet:
            with self.subTest(key=key):
                self.quiet[key] = 1
                self.tutor.counts.return_value = self.quiet.copy()
                with self.assertRaisesRegex(ValueError, 'zero tutor jobs and attempts'):
                    self.verify_navigation()
                self.quiet[key] = 0
                with self.assertRaisesRegex(ValueError, 'zero tutor jobs and attempts'):
                    self.verify_navigation()

    def test_navigation_rejects_dispatch_with_zero_control_jobs(self):
        self.tutor.live = Mock()
        self.tutor.live.evidence.return_value = {'accounting': {'dispatch_claims': 1}}
        with self.assertRaisesRegex(ValueError, 'zero provider dispatches'):
            self.verify_navigation()

    def test_navigation_rejects_changed_saved_bytes(self):
        self.notebook.write_bytes(self.original + b'\n')
        with self.assertRaisesRegex(ValueError, 'changed the saved starter notebook'):
            self.verify_navigation()

    def test_offline_completion_rejects_jobs_and_attempts_independently(self):
        for key in self.quiet:
            with self.subTest(key=key):
                self.tutor.counts.return_value = dict(self.quiet, **{key: 1})
                with self.assertRaisesRegex(ValueError, 'offline completion'):
                    desktop.verify_offline_completion(self.tutor)


class ManualSignInTests(unittest.TestCase):
    def test_closure_during_setup_is_distinct_from_a_form_failure(self):
        course = Mock(forge='http://127.0.0.1:18001', learner_passwords={'alice': 'synthetic'})
        for stage in ('navigate', 'prefill', 'focus'):
            for closed in (False, True):
                with self.subTest(stage=stage, closed=closed):
                    browser = Mock()
                    page = browser.new_context.return_value.new_page.return_value
                    page.url = course.forge + '/user/login'
                    page.is_closed.return_value = closed
                    browser.is_connected.return_value = True
                    steps = {'navigate': page.goto, 'prefill': page.locator.return_value.fill,
                             'focus': page.bring_to_front}
                    steps[stage].side_effect = BrowserTimeout('synthetic setup interruption')
                    with self.assertRaises(SignInNotStarted if closed else BrowserTimeout):
                        Course.login(course, browser, 'alice', '/me', manual_sign_in=True)
                    page.wait_for_url.assert_not_called()

    def test_only_unsubmitted_wait_or_close_is_an_incomplete_review(self):
        course = Mock(forge='http://127.0.0.1:18001', learner_passwords={'alice': 'synthetic'})
        url = course.forge + '/user/login'
        for submitted, closed, error, outcome in [
            (False, False, BrowserTimeout, SignInNotStarted),
            (False, True, BrowserError, SignInNotStarted),
            (False, False, BrowserError, BrowserError),
            (True, False, BrowserTimeout, BrowserTimeout),
            (True, True, BrowserError, BrowserError),
        ]:
            with self.subTest(submitted=submitted, closed=closed, error=error):
                browser = Mock()
                page = browser.new_context.return_value.new_page.return_value
                page.url = url
                page.is_closed.return_value = closed
                browser.is_connected.return_value = True

                def wait(*args, **kwargs):
                    if submitted:
                        page.on.call_args.args[1](SimpleNamespace(method='POST', url=url))
                    raise error('synthetic browser result')

                page.wait_for_url.side_effect = wait
                with patch('builtins.print'), self.assertRaises(outcome):
                    Course.login(course, browser, 'alice', '/me', manual_sign_in=True)


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
