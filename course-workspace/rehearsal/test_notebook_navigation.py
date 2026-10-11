"""Readiness follows the Hub's existing redirect, never a competing navigation."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

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
                SimpleNamespace(ok=True, json=lambda: {'server': expected}),
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
                    session.get.return_value = SimpleNamespace(ok=True, json=lambda: {'server':server})
                    with self.assertRaisesRegex(AssertionError, 'unexpected learner server URL'):
                        book.ready(page, 'alice')
                    page.wait_for_url.assert_not_called()
                    self.assertEqual(session.get.call_count, 1)

    def test_redirect_failure_propagates_and_html_login_is_not_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            book, _, expected, page, session = self.fixture(directory)
            session.get.return_value = SimpleNamespace(ok=True, json=lambda: {'server':expected})
            page.wait_for_url.side_effect = RuntimeError('navigation failed')
            with self.assertRaisesRegex(RuntimeError, 'navigation failed'):
                book.ready(page, 'alice')
            page.wait_for_url.side_effect = None
            session.get.side_effect = lambda url, **kwargs: (
                SimpleNamespace(ok=True, json=lambda: {'server':expected}) if '/hub/api/' in url
                else SimpleNamespace(status_code=200, headers={'Content-Type':'text/html'}, json=Mock(side_effect=AssertionError('must not parse login HTML'))))
            with patch('notebook.time.sleep'), self.assertRaisesRegex(AssertionError, 'notebook did not become ready'):
                book.ready(page, 'alice')


if __name__ == '__main__': unittest.main()
