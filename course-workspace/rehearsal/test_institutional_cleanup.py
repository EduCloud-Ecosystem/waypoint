"""Institutional acceptance is conditional on both namespaces being cleaned."""
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from notebook import Notebook
import verify_institutional_recovery as recovery


class BrowserCutoverTests(unittest.TestCase):
    def test_old_cookie_auth_is_checked_before_fresh_process_with_no_cookie_transfer(self):
        for status in (401, 403):
            events = []
            session = Mock()
            session.get.side_effect = lambda *args, **kwargs: (events.append('old-auth-denied') or SimpleNamespace(status_code=status))
            original = Mock()
            original.close.side_effect = lambda: events.append('source-browser-closed')
            original.new_context.side_effect = AssertionError('source browser reused after bridge replacement')
            replacement = Mock()
            playwright = Mock()
            playwright.chromium.launch.side_effect = lambda **kwargs: (events.append('replacement-browser-launched') or replacement)
            self.assertIs(recovery.browser_after_cutover(playwright, original, session, 'https://work.example.test'), replacement)
            self.assertEqual(events, ['old-auth-denied', 'source-browser-closed', 'replacement-browser-launched'])
            session.get.assert_called_once_with('https://work.example.test/hub/api/users/'+recovery.SUBJECTS['bob'], timeout=15, allow_redirects=False)
            playwright.chromium.launch.assert_called_once_with(args=['--host-resolver-rules=MAP *.rehearsal.test 127.0.0.1'])
            original.new_context.assert_not_called()
            replacement.new_context.assert_not_called()
            # Launch accepts only DNS routing: no old browser storage/cookies.
            self.assertNotIn('storage_state', playwright.chromium.launch.call_args.kwargs)

    def test_accepted_old_auth_prevents_cutover_from_hiding_the_regression(self):
        for status in (200, 302, 500):
            session = Mock()
            session.get.return_value.status_code = status
            original, playwright = Mock(), Mock()
            with self.subTest(status=status), self.assertRaisesRegex(AssertionError, 'old session accepted'):
                recovery.browser_after_cutover(playwright, original, session, 'https://work.example.test')
            original.close.assert_not_called()
            playwright.chromium.launch.assert_not_called()

    def test_failed_http_or_browser_launch_propagates_without_retry(self):
        session, original, playwright = Mock(), Mock(), Mock()
        session.get.side_effect = RuntimeError('old-auth probe failed')
        with self.assertRaisesRegex(RuntimeError, 'old-auth probe failed'):
            recovery.browser_after_cutover(playwright, original, session, 'https://work.example.test')
        original.close.assert_not_called()
        playwright.chromium.launch.assert_not_called()
        session.get.side_effect = None
        session.get.return_value.status_code = 401
        playwright.chromium.launch.side_effect = RuntimeError('replacement browser failed')
        with self.assertRaisesRegex(RuntimeError, 'replacement browser failed'):
            recovery.browser_after_cutover(playwright, original, session, 'https://work.example.test')
        original.close.assert_called_once()
        playwright.chromium.launch.assert_called_once()


class RecoveryCleanupTests(unittest.TestCase):
    def exercise(self, root, *, fail_volume=False, partial=False):
        source = root / 'source'
        source.mkdir(mode=0o700)
        fixture = Notebook(source)
        source_instance = fixture.instance
        replacement = 'recovered-' + 'a' * 12
        homes = (source / 'homes', root / 'replacement-homes')
        resources = {
            source_instance: {'container': {'source-container'}, 'volume': set(), 'network': {'source-network'}},
            replacement: {'container': {'replacement-container'}, 'volume': {'replacement-volume'}, 'network': {'replacement-network'}},
        }
        mounted = {str(homes[1])}  # Source filesystem was destroyed during the drill.
        unmounts = []
        scopes = []
        # Reproduce the mutation that caused CI to unmount replacement twice.
        fixture.instance = replacement
        fixture.homes = homes[1]
        fixture.mounted = False
        restored = {'WORKSPACE_INSTANCE': replacement}
        if partial:
            (root / 'restored').mkdir(mode=0o700)
            env = root / 'restored/.env'
            env.write_text('WORKSPACE_INSTANCE=' + replacement + '\n')
            env.chmod(0o600)
            restored = None

        def command(args, **kwargs):
            if args[0] == 'umount':
                unmounts.append(args[1])
                if args[1] not in mounted:
                    raise subprocess.CalledProcessError(32, args)
                mounted.remove(args[1])
                return SimpleNamespace(returncode=0, stdout='')
            if args[1] == 'ps' or (len(args) > 2 and args[2] == 'ls'):
                kind = 'container' if args[1] == 'ps' else args[1]
                scope = args[-1].rsplit('=', 1)[1]
                scopes.append(scope)
                return SimpleNamespace(returncode=0, stdout='\n'.join(resources[scope][kind]))
            kind = 'container' if args[1] == 'rm' else args[1]
            if fail_volume and args[-1] == 'replacement-volume':
                return SimpleNamespace(returncode=1, stdout='')
            for resource in resources.values():
                resource[kind].discard(args[-1])
            return SimpleNamespace(returncode=0, stdout='')

        with patch('notebook.subprocess.run', side_effect=command), patch('notebook.os.path.ismount', side_effect=lambda path: str(path) in mounted):
            if fail_volume:
                with self.assertRaises(RuntimeError):
                    recovery.cleanup_rehearsal(fixture, root, source_instance, restored)
            else:
                self.assertTrue(recovery.cleanup_rehearsal(fixture, root, source_instance, restored))
        self.assertEqual(unmounts, [str(homes[1])])
        self.assertFalse(mounted)
        self.assertTrue(all(not values for values in resources[source_instance].values()))
        self.assertIn(source_instance, scopes)
        self.assertIn(replacement, scopes)
        if fail_volume:
            self.assertTrue(resources[replacement]['volume'])
        else:
            self.assertTrue(all(not values for values in resources[replacement].values()))

    def test_replacement_unmounted_exactly_once_and_both_scopes_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            self.exercise(Path(directory))

    def test_replacement_failure_does_not_skip_original_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            self.exercise(Path(directory), fail_volume=True)

    def test_partial_restore_identity_is_recovered_from_private_env(self):
        with tempfile.TemporaryDirectory() as directory:
            self.exercise(Path(directory), partial=True)

    def test_cleanup_error_never_publishes_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'run'
            fixture = Mock(instance='rehearsal-fixture')
            fixture.start.side_effect = RuntimeError('partial start')
            argv = ['verify_institutional_recovery.py', '--output', str(output)]
            with patch.object(recovery.sys, 'argv', argv), patch.object(recovery.sys, 'platform', 'linux'), patch.object(recovery.os, 'geteuid', return_value=0), patch.object(recovery, 'Notebook', return_value=fixture), patch.object(recovery, 'cleanup_rehearsal', side_effect=RuntimeError('cleanup failed')) as cleanup, patch.object(recovery, 'publish_pass') as publish:
                with self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
                    recovery.main()
            cleanup.assert_called_once()
            publish.assert_not_called()
            self.assertFalse((output / 'PASS.json').exists())

    def test_functional_failure_never_publishes_even_when_cleanup_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'run'
            fixture = Mock(instance='rehearsal-fixture')
            fixture.start.side_effect = RuntimeError('functional failure')
            argv = ['verify_institutional_recovery.py', '--output', str(output)]
            with patch.object(recovery.sys, 'argv', argv), patch.object(recovery.sys, 'platform', 'linux'), patch.object(recovery.os, 'geteuid', return_value=0), patch.object(recovery, 'Notebook', return_value=fixture), patch.object(recovery, 'cleanup_rehearsal', return_value=True) as cleanup, patch.object(recovery, 'publish_pass') as publish:
                with self.assertRaisesRegex(RuntimeError, 'functional failure'):
                    recovery.main()
            cleanup.assert_called_once()
            publish.assert_not_called()

    def test_pass_publication_requires_cleanup_and_is_private_atomic_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for value in (False, None, 1):
                with self.assertRaises(ValueError):
                    recovery.publish_pass(root, {'checks': []}, value)
                self.assertFalse((root / 'PASS.json').exists())
            recovery.publish_pass(root, {'checks': ['synthetic']}, True)
            target = root / 'PASS.json'
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            original = target.read_bytes()
            with self.assertRaises(FileExistsError):
                recovery.publish_pass(root, {'checks': []}, True)
            self.assertEqual(original, target.read_bytes())
            self.assertEqual(list(root.iterdir()), [target])


if __name__ == '__main__':
    unittest.main()
