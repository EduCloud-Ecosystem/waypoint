import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recovery import load_config, private_json, restic, retention, restore_repository, write_status, backup_repository


class RecoveryTests(unittest.TestCase):
    def fixture(self, root):
        key = root / 'password'
        key.write_text('synthetic-unit-test-password-do-not-use')
        key.chmod(0o600)
        cfg = {'version': 1, 'repository': str(root / 'repo'),
               'local_test': True, 'password_file': str(key)}
        path = root / 'recovery.json'
        private_json(path, cfg)
        return path, cfg

    def test_private_configuration_and_explicit_local_mode(self):
        with tempfile.TemporaryDirectory() as d:
            path, cfg = self.fixture(Path(d))
            self.assertEqual(load_config(path)['keep_last'], 7)
            for change in [{'local_test': False}, {'keep_last': 0},
                           {'repository': 's3:http://bucket.example/course'},
                           {'repository': 's3:https://user:secret@bucket.example/course'}]:
                private_json(path, dict(cfg, **change))
                with self.assertRaises(ValueError):
                    load_config(path)
            private_json(path, cfg)
            Path(cfg['password_file']).chmod(0o644)
            with self.assertRaises(ValueError):
                load_config(path)

    def test_shell_cannot_override_repository_or_password(self):
        cfg = {'repository': '/synthetic/repo', 'password_file': '/private/key'}
        with patch.dict(os.environ, {'RESTIC_PASSWORD': 'wrong', 'RESTIC_REPOSITORY': '/other'}), \
                patch('recovery.subprocess.run') as run:
            run.return_value.returncode = 0
            restic(cfg, 'snapshots')
        env = run.call_args.kwargs['env']
        self.assertNotIn('RESTIC_PASSWORD', env)
        self.assertEqual(env['RESTIC_PASSWORD_FILE'], '/private/key')
        self.assertEqual(env['RESTIC_REPOSITORY'], '/synthetic/repo')

    def test_restore_refuses_ambiguous_ids_and_existing_targets_before_backend(self):
        with tempfile.TemporaryDirectory() as d, patch('recovery.restic') as backend:
            for snapshot, destination in [('latest', Path(d) / 'fresh'), ('a' * 64, Path(d))]:
                with self.assertRaises(ValueError):
                    restore_repository({}, snapshot, destination, 18000)
            backend.assert_not_called()

    def test_backend_diagnostics_do_not_echo_credentials(self):
        with patch('recovery.subprocess.run') as run:
            run.return_value.returncode = 12
            run.return_value.stderr = 'sensitive backend details'
            with self.assertRaises(RuntimeError) as error:
                restic({'repository': '/repo', 'password_file': '/key'}, 'check')
            self.assertNotIn('sensitive', str(error.exception))

    def test_failed_backup_preserves_last_success_and_does_not_stop_on_bad_credentials(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            write_status(root, 'backup', True, 'a' * 64)
            with patch('recovery.settings', return_value={}), \
                    patch('recovery.restic', side_effect=RuntimeError('wrong key')), \
                    patch('recovery.package') as package:
                with self.assertRaises(RuntimeError):
                    backup_repository(root, {})
            package.assert_not_called()
            status = json.loads((root / 'recovery-status.json').read_text())
            self.assertFalse(status['success'])
            self.assertEqual(status['snapshot_id'], 'a' * 64)
            self.assertIn('last_success', status)

    def test_retention_is_scoped_and_defaults_to_preview(self):
        cfg = {'keep_last': 7, 'keep_daily': 7, 'keep_weekly': 4}
        with patch('recovery.restic') as run:
            retention(cfg, 'trial-example')
        args = run.call_args.args
        self.assertIn('--dry-run', args)
        self.assertNotIn('--prune', args)
        self.assertIn('educloud-workspace-v1,instance=trial-example', args)
        self.assertIn('--host', args)


if __name__ == '__main__':
    unittest.main()
