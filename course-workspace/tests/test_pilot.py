import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pilot import initialize, settings, locked, compose


class PilotTests(unittest.TestCase):
    def test_restored_allocation_gate_blocks_trial_start_and_prefixed_compose(self):
        from pilot import start
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'.worker-allocation-required.json').symlink_to(root/'missing')
            with patch('pilot.docker') as docker, patch('pilot.subprocess.run') as run:
                with self.assertRaisesRegex(ValueError,'cannot start'): start(root,{})
                with self.assertRaisesRegex(ValueError,'current worker allocation'): compose(root,'--profile','build','up','hub')
                docker.assert_not_called(); run.assert_not_called()
    def test_retirement_marker_cannot_start_through_legacy_trial_entrypoint(self):
        from pilot import start
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.course-retired.json').symlink_to(root / 'missing-record')
            with patch('pilot.docker') as docker, patch('pilot.compose') as compose:
                with self.assertRaisesRegex(ValueError, 'cannot start'):
                    start(root, {}, build=True)
                docker.assert_not_called()
                compose.assert_not_called()
    def test_private_initialization_is_not_destructive(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'trial'
            original = initialize(path)
            self.assertEqual(settings(path), original)
            self.assertEqual((path / 'pilot.env').stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                initialize(path)
            self.assertEqual(settings(path), original)
            (path / 'pilot.env').chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'private'):
                settings(path)

    def test_public_or_larger_trial_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'trial'
            initialize(path)
            file = path / 'pilot.env'
            original = file.read_text()
            for before, after in [('127.0.0.1', '0.0.0.0'), ('ACTIVE_LIMIT=1', 'ACTIVE_LIMIT=4')]:
                file.write_text(original.replace(before, after))
                with self.assertRaises(ValueError):
                    settings(path)

    def test_operations_are_serialized(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            with locked(path):
                with self.assertRaisesRegex(ValueError, 'in progress'):
                    with locked(path):
                        self.fail('lock should exclude concurrent operation')
            with locked(path):
                pass

    def test_shell_cannot_redirect_compose_trial(self):
        with patch.dict(os.environ, {'WORKSPACE_PORT': '9999', 'COMPOSE_PROJECT_NAME': 'other'}):
            with patch('pilot.subprocess.run') as run:
                compose(Path('/private/trial'), 'stop', 'hub')
        self.assertNotIn('WORKSPACE_PORT', run.call_args.kwargs['env'])
        self.assertNotIn('COMPOSE_PROJECT_NAME', run.call_args.kwargs['env'])


if __name__ == '__main__':
    unittest.main()
