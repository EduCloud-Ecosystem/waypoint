"""Readiness follows the Hub's existing redirect, never a competing navigation."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from playwright.sync_api import Error as PlaywrightError

from notebook import Notebook, SUBJECTS


class NotebookNavigationTests(unittest.TestCase):
    def fixture(self, directory):
        book = Notebook(Path(directory))
        subject = SUBJECTS['alice']
        expected = book.origin.replace('://', '://' + subject + '.') + '/user/' + subject + '/'
        page = Mock()
        session = Mock()
        book.http_session = Mock(return_value=session)
        return book, subject, expected, page, session

    def test_login_retries_only_initial_anonymous_get_before_any_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            book, _, _, page, _ = self.fixture(directory)
            target = book.origin + '/hub/login'
            def navigate(url):
                self.assertEqual(url, target)
                page.get_by_role.assert_not_called(); page.locator.assert_not_called()
                if page.goto.call_count < 3:
                    raise PlaywrightError('Page.goto: net::ERR_NETWORK_CHANGED at ' + target + '\nCall log:\n  - navigating')
            page.goto.side_effect = navigate
            with patch('notebook.time.sleep') as sleep, patch('builtins.print') as log:
                book.login(page, 'alice')
            self.assertEqual(page.goto.call_count, 3)
            self.assertEqual(sleep.call_args_list, [unittest.mock.call(0.25), unittest.mock.call(0.25)])
            self.assertEqual(log.call_args_list, [
                unittest.mock.call('Synthetic notebook anonymous login GET network-change retry 1/2.', flush=True),
                unittest.mock.call('Synthetic notebook anonymous login GET network-change retry 2/2.', flush=True)])
            page.get_by_role.assert_called_once_with('button', name='Sign in with')
            self.assertEqual([call.args[0] for call in page.locator.call_args_list],
                             ['input[name="username"]', 'input[name="password"]', 'input[type="submit"],button[type="submit"]'])
            page.wait_for_url.assert_called_once()

    def test_login_other_errors_targets_or_network_code_mentions_do_not_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            book, _, _, page, _ = self.fixture(directory)
            target = book.origin + '/hub/login'
            errors = [PlaywrightError('Page.goto: net::ERR_CONNECTION_REFUSED at ' + target),
                      PlaywrightError('Page.goto: net::ERR_NETWORK_CHANGED_EXTRA at ' + target),
                      PlaywrightError('Page.goto: net::ERR_NETWORK_CHANGED at ' + target + '/other'),
                      PlaywrightError('Page.goto: net::ERR_ABORTED at ' + target + '\nCall log:\n net::ERR_NETWORK_CHANGED'),
                      RuntimeError('Page.goto: net::ERR_NETWORK_CHANGED at ' + target)]
            for error in errors:
                with self.subTest(error=error):
                    page.reset_mock(); page.goto.side_effect = error
                    with patch('notebook.time.sleep') as sleep, patch('builtins.print') as log:
                        with self.assertRaises(type(error)) as result: book.login(page, 'alice')
                    self.assertIs(result.exception, error)
                    page.goto.assert_called_once_with(target)
                    page.get_by_role.assert_not_called(); page.locator.assert_not_called()
                    page.wait_for_url.assert_not_called(); sleep.assert_not_called(); log.assert_not_called()

    def test_login_network_changed_exhausts_two_retries_without_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            book, _, _, page, _ = self.fixture(directory)
            target = book.origin + '/hub/login'
            error = PlaywrightError('Page.goto: net::ERR_NETWORK_CHANGED at ' + target)
            page.goto.side_effect = error
            with patch('notebook.time.sleep') as sleep, patch('builtins.print') as log:
                with self.assertRaises(PlaywrightError) as result: book.login(page, 'alice')
            self.assertIs(result.exception, error)
            self.assertEqual(page.goto.call_count, 3); self.assertEqual(sleep.call_count, 2)
            self.assertEqual(log.call_count, 2)
            page.get_by_role.assert_not_called(); page.locator.assert_not_called(); page.wait_for_url.assert_not_called()

    def test_login_authentication_action_error_is_never_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            book, _, _, page, _ = self.fixture(directory)
            error = PlaywrightError('Page.goto: net::ERR_NETWORK_CHANGED at ' + book.origin + '/hub/login')
            page.get_by_role.return_value.click.side_effect = error
            with patch('notebook.time.sleep') as sleep:
                with self.assertRaises(PlaywrightError) as result: book.login(page, 'alice')
            self.assertIs(result.exception, error)
            page.goto.assert_called_once_with(book.origin + '/hub/login')
            page.locator.assert_not_called(); sleep.assert_not_called()

    def test_hub_auto_redirect_finishes_without_a_second_goto(self):
        with tempfile.TemporaryDirectory() as directory:
            book, subject, expected, page, session = self.fixture(directory)
            spawn = book.origin + '/hub/spawn/' + subject
            def navigate(url):
                # A second navigation would interrupt the pending Hub redirect,
                # reproducing the previous failure instead of hiding it.
                self.assertEqual(url, spawn)
                self.assertEqual(page.goto.call_count, 1)
                page.url = spawn
            page.goto.side_effect = navigate
            def redirected(url, **kwargs):
                self.assertEqual(url, expected + 'lab')
                self.assertEqual(kwargs, {'wait_until':'domcontentloaded', 'timeout':60000})
                page.url = url
            page.wait_for_url.side_effect = redirected
            session.get.side_effect = [
                SimpleNamespace(ok=True, status_code=200, json=lambda: {'server': expected}),
                SimpleNamespace(status_code=200, headers={'Content-Type':'application/json'}, json=lambda: {'type':'directory'}),
            ]
            self.assertEqual(book.ready(page, 'alice'), (session, expected.rstrip('/')))
            page.goto.assert_called_once_with(spawn)
            session.get.assert_called_with(expected + 'api/contents', timeout=10, allow_redirects=False)

    def test_unexpected_server_origin_port_or_learner_never_gets_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            book, subject, expected, page, session = self.fixture(directory)
            invalid = [expected.replace('https:', 'http:'), expected.replace(subject + '.', 'other.'),
                       expected.replace('.work.rehearsal.test', '.work.rehearsal.test.attacker.invalid'),
                       expected.replace('/user/' + subject, '/user/' + SUBJECTS['bob']),
                       expected.replace(':' + str(book.port), ':1'), expected + '?token=unexpected',
                       book.origin + '/user/' + subject + '/']
            for server in invalid:
                with self.subTest(server=server):
                    session.reset_mock(); page.reset_mock()
                    session.get.return_value = SimpleNamespace(ok=True, status_code=200, json=lambda: {'server':server})
                    with self.assertRaisesRegex(AssertionError, 'unexpected learner server URL'):
                        book.ready(page, 'alice')
                    page.wait_for_url.assert_not_called()
                    self.assertEqual(session.get.call_count, 1)

    def test_redirect_failure_propagates_and_html_login_is_not_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            book, _, expected, page, session = self.fixture(directory)
            session.get.return_value = SimpleNamespace(ok=True, status_code=200, json=lambda: {'server':expected})
            page.wait_for_url.side_effect = RuntimeError('navigation failed')
            with self.assertRaisesRegex(RuntimeError, 'navigation failed'):
                book.ready(page, 'alice')
            page.wait_for_url.side_effect = None
            session.get.side_effect = lambda url, **kwargs: (
                SimpleNamespace(ok=True, status_code=200, json=lambda: {'server':expected}) if '/hub/api/' in url
                else SimpleNamespace(status_code=200, headers={'Content-Type':'text/html'}, json=Mock(side_effect=AssertionError('must not parse login HTML'))))
            with patch('notebook.time.sleep'), patch.object(book,'learner_readiness_diagnostics') as diagnostics, self.assertRaisesRegex(AssertionError, 'notebook did not become ready'):
                book.ready(page, 'alice')
            diagnostics.assert_called_once_with({'hub_api':200,'contents':200,'server_present':True})


if __name__ == '__main__': unittest.main()
