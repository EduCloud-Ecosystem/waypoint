import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recovery import load_config, private_json, restic, retention, restore_repository, write_status, backup_repository, validate_bundle, BASE_CODE, digest, copy_source_manifest


class RecoveryTests(unittest.TestCase):
    def test_stopped_old_hub_cannot_back_up_new_image_configuration(self):
        from recovery import package
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {'WORKSPACE_INSTANCE':'course-one', 'WORKSPACE_IMAGE':'sha256:'+'b'*64,
                   'WORKSPACE_HUB_IMAGE':'sha256:'+'c'*64}
            with patch('recovery.docker', return_value=json.dumps([{'Image':'sha256:'+'a'*64}])), \
                    patch('recovery.image_info', return_value={'Id':'sha256:'+'c'*64}), \
                    patch('recovery.subprocess.run') as export, patch('recovery.checkpoint') as checkpoint:
                with self.assertRaisesRegex(ValueError, 'Hub image differs'):
                    package(root, env, root/'bundle')
                export.assert_not_called()
                checkpoint.assert_not_called()

    def test_backup_lock_and_upgrade_marker_refuse_before_backend(self):
        from pilot import locked
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with locked(root), patch('recovery.settings') as settings, patch('recovery.restic') as backend:
                with self.assertRaisesRegex(ValueError, 'operation is in progress'):
                    backup_repository(root, {})
                settings.assert_not_called()
                backend.assert_not_called()
            (root / '.upgrade-pending.json').symlink_to(root / 'missing-journal')
            with patch('recovery.restic') as backend:
                with self.assertRaisesRegex(ValueError, 'upgrade is incomplete'):
                    backup_repository(root, {})
                backend.assert_not_called()

    def test_private_backend_scope_suppresses_child_diagnostics_and_restores_fds(self):
        import subprocess
        code = '''
import os, subprocess, sys
from recovery import private_backend_output
try:
    with private_backend_output():
        print('private-python-diagnostic')
        os.write(2, b'private-native-diagnostic\\n')
        subprocess.run([sys.executable, '-c', "print('private-child-diagnostic')"], check=True)
        raise ValueError('control')
except ValueError:
    pass
print('public-after-scope')
os.write(2, b'public-stderr-after-scope\\n')
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, 'public-after-scope\n')
        self.assertEqual(result.stderr, 'public-stderr-after-scope\n')

    def test_source_manifest_is_private_historical_context_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original, bundle, replacement = [root / name for name in ('original', 'bundle', 'replacement')]
            for directory in (original, bundle, replacement):
                directory.mkdir()
            raw = '{"version":1,"owners":{"restore":"course-ops"}}\n'
            private_json(original / 'manifest.json', json.loads(raw))
            copy_source_manifest(original, bundle)
            copy_source_manifest(bundle, replacement)
            self.assertEqual((bundle / 'source-manifest.json').read_bytes(), (replacement / 'source-manifest.json').read_bytes())
            self.assertEqual((replacement / 'source-manifest.json').stat().st_mode & 0o777, 0o600)
            self.assertFalse((replacement / 'manifest.json').exists())
            with self.assertRaises(FileExistsError):
                copy_source_manifest(bundle, replacement)

    def test_source_manifest_rejects_links_special_files_and_oversized_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, target = root / 'source', root / 'target'
            source.mkdir(); target.mkdir()
            path = source / 'manifest.json'
            path.symlink_to(root / 'absent')
            with self.assertRaises(OSError):
                copy_source_manifest(source, target)
            path.unlink()
            os.mkfifo(path, 0o600)
            with self.assertRaises(ValueError):
                copy_source_manifest(source, target)
            path.unlink()
            path.write_bytes(b' ' * (256 * 1024 + 1)); path.chmod(0o600)
            with self.assertRaises(ValueError):
                copy_source_manifest(source, target)
            self.assertFalse((target / 'source-manifest.json').exists())

    def fixture(self, root):
        key = root / 'password'
        key.write_text('synthetic-unit-test-password-do-not-use')
        key.chmod(0o600)
        cfg = {'version': 1, 'repository': str(root / 'repo'),
               'local_test': True, 'password_file': str(key)}
        path = root / 'recovery.json'
        private_json(path, cfg)
        return path, cfg

    def test_old_bundles_remain_recoverable_and_corruption_is_refused(self):
        with tempfile.TemporaryDirectory() as d, patch('recovery.docker', return_value=json.dumps({'Architecture': 'x86_64'})):
            root = Path(d)
            (root / 'runtime').mkdir()
            (root / 'volumes').mkdir()
            for name in BASE_CODE:
                (root / 'runtime' / name).write_text('fixture runtime')
            (root / 'images.tar').write_bytes(b'fixture image archive')
            (root / 'volumes/runtime.json').write_text('{}')
            archive = root / 'volumes/hub-data.tar.gz'
            archive.write_bytes(b'fixture volume')
            (root / 'volumes/manifest.json').write_text(json.dumps({'version': 1, 'instance': 'trial-old',
                'volumes': [{'suffix': 'hub-data', 'kind': 'hub', 'sha256': digest(archive)}]}))
            env = {'WORKSPACE_AUTH_MODE': 'local-test', 'WORKSPACE_INSTANCE': 'trial-old'}
            (root / 'environment.json').write_text(json.dumps(env))
            meta = {'version': 1, 'instance': 'trial-old', 'images': {'hub': {
                'id': 'sha256:' + 'a' * 64, 'os': 'linux', 'architecture': 'amd64'}}}
            def inventory():
                meta['files'] = {str(p.relative_to(root)): digest(p) for p in root.rglob('*')
                                 if p.is_file() and p.name != 'bundle.json'}
                (root / 'bundle.json').write_text(json.dumps(meta))
            inventory()
            self.assertEqual(validate_bundle(root)[1], env)
            (root / 'images.tar').write_bytes(b'corrupted')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                validate_bundle(root)
            inventory()
            meta['images']['hub']['architecture'] = 'arm64'
            (root / 'bundle.json').write_text(json.dumps(meta))
            with self.assertRaisesRegex(ValueError, 'architecture'):
                validate_bundle(root)
            env['WORKSPACE_HOME_ROOT'] = '/srv/homes'
            (root / 'environment.json').write_text(json.dumps(env))
            inventory()
            with self.assertRaisesRegex(ValueError, 'quota recovery runtime'):
                validate_bundle(root)
            (root / 'linked').symlink_to(root / 'images.tar')
            with self.assertRaisesRegex(ValueError, 'links'):
                validate_bundle(root)

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
