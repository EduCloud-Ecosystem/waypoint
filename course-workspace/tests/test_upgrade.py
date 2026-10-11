from contextlib import nullcontext, redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import upgrade as u
from maintenance import write, digest
from public_course import render
from rehearsal import evidence


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='upgrade-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.directory = self.root / 'deployment'
        self.directory.mkdir(mode=0o700)
        self.homes = self.root / 'homes'
        self.homes.mkdir()
        self.hub = self.root / 'hub-data'
        self.hub.mkdir()
        (self.hub / 'hub.sqlite').write_bytes(b'stopped-saved-db')
        self.names = ['course-test-home-' + hashlib.sha256(value.encode()).hexdigest()[:24] for value in ['subject-a', 'retained-subject']]
        for name in self.names:
            (self.homes / name).mkdir()
            (self.homes / name / 'saved.py').write_text('print(42)\n')
        _, _, self.env = render('work.example.test', 'https://id.example.test/realms/course', 'course',
                                 'course-test', ['subject-a'], str(self.homes))
        self.old = {'hub': 'sha256:' + 'a' * 64, 'course': 'sha256:' + 'b' * 64}
        self.new = {'hub': 'sha256:' + 'c' * 64, 'course': 'sha256:' + 'd' * 64}
        self.env.update(WORKSPACE_HUB_IMAGE=self.old['hub'], WORKSPACE_IMAGE=self.old['course'],
                        WORKSPACE_OIDC_CLIENT_SECRET='SENTINEL-PRIVATE-CLIENT', WORKSPACE_ENV_FILE=str(self.directory / '.env'))
        write(self.directory / '.env', ''.join(f'{key}={value}\n' for key, value in self.env.items()).encode())
        write(self.directory / 'manifest.json', {'version': 1, 'owners': {'support': 'Synthetic support'}})
        self.password = self.root / 'backup-password'
        self.password.write_text('SENTINEL-PRIVATE-BACKUP-24chars')
        self.password.chmod(0o600)
        self.config = self.root / 'recovery.json'
        write(self.config, {'version': 1, 'repository': str(self.root / 'restic'), 'password_file': str(self.password),
                            'local_test': True, 'keep_last': 7, 'keep_daily': 7, 'keep_weekly': 4})
        self.canary = self.root / 'PASS.json'
        now = datetime.now(timezone.utc).isoformat()
        hash64 = 'e' * 64
        p = {'cairn': {'revision': 'a' * 40, 'dirty': False}, 'waypoint': {'revision': 'b' * 40, 'dirty': False},
             'cairn_binary_sha256': hash64, 'lti_binary_sha256': hash64, 'axe_core_sha256': hash64,
             'dashboard_bundle_sha256': hash64, 'dashboard_file_count': 4,
             'fixture_version': 'course-pilot-v4', 'fixture_files_sha256': {name: evidence.sha256_file(ROOT / 'rehearsal' / name) for name in evidence.FIXTURE_FILES},
             'image_ids': {role: self.new.get(role, 'sha256:' + 'e' * 64) for role in evidence.IMAGE_ROLES},
             'hardware': {'os': 'Linux', 'architecture': 'x86_64', 'cpu_count': 4, 'memory_bytes': 16 * 1024**3},
             'runtime': {'python': '3.12.9', 'docker_server': '29.0.0'}}
        self.evidence = {'version': 4, 'status': 'PASS', 'scope': 'full-linux', 'cleanup_verified': True,
                         'provenance': p, 'phases': {phase: True for phase in evidence.REQUIRED_PHASES['full-linux']},
                         'started_at': now, 'finished_at': now, 'duration_seconds': 10,
                         'checks': ['synthetic-check'], 'metrics': {'model_calls': 0, 'course_errors': 0},
                         'course_manifest_sha256': hash64}
        write(self.canary, self.evidence)
        self.plan = self.root / 'upgrade-plan.json'
        self.snapshot_id = 'f' * 64
        self.backups = 0
        self.foreign_writer = False
        self.hub_running = False
        self.candidate_arch = 'amd64'
        self.backup_rows = None
        for target, replacement in [('upgrade.preflight', self.preflight), ('upgrade.home_lock', lambda root: nullcontext()),
                                    ('upgrade.snapshot', self.snapshot), ('upgrade.stopped', self.stopped),
                                    ('upgrade.docker_json', self.docker_json), ('backup.docker', self.docker),
                                    ('upgrade.backup_repository_locked', self.backup), ('upgrade.restic', self.restic),
                                    ('upgrade.tool_hash', lambda: '9' * 64)]:
            mock = patch(target, replacement)
            mock.start()
            self.addCleanup(mock.stop)

    def preflight(self, directory, allow_pending=False):
        if (directory / u.PENDING).exists() and not allow_pending:
            raise ValueError('SENTINEL-private-pending-diagnostic')
        env = u.environment(directory)
        return {'images': {'hub': env['WORKSPACE_HUB_IMAGE'], 'course': env['WORKSPACE_IMAGE']}}

    def snapshot(self, directory, env):
        return {'env_sha256': digest(env), 'homes': {name: {'quota_mb': 5120} for name in self.names},
                'filesystem': {'source': 'synthetic-uuid'}, 'identity': {'instance': 'course-test'}}

    def stopped(self, instance, names, resizing):
        if self.hub_running:
            raise ValueError('SENTINEL-private-running-diagnostic')

    def docker_json(self, *args):
        if args[0] == 'info':
            return {'OSType': 'linux', 'Architecture': 'amd64'}
        if args[:2] == ('image', 'inspect'):
            return [{'Id': args[-1], 'Os': 'linux', 'Architecture': self.candidate_arch}]
        if args[:2] == ('container', 'inspect'):
            return [{'Name': '/course-test-hub', 'Image': self.old['hub'], 'State': {'Running': self.hub_running},
                     'Config': {'Labels': {'educloud.workspace.instance': 'course-test', 'educloud.workspace.kind': 'hub'}},
                     'Mounts': [{'Type': 'volume', 'Name': 'course-test-hub-data', 'Destination': '/data'}]}]
        if args[:2] == ('volume', 'inspect'):
            return [{'Driver': 'local', 'Options': {}, 'Mountpoint': str(self.hub),
                     'Labels': {'educloud.workspace.instance': 'course-test', 'educloud.workspace.kind': 'hub'}}]
        raise AssertionError(args)

    def docker(self, *args):
        if args[0] == 'ps':
            return 'unlabelled-live-writer' if self.foreign_writer else ''
        raise AssertionError(args)

    def backup(self, directory, cfg, quiet=False):
        self.assertTrue(quiet)
        self.backups += 1
        return self.snapshot_id

    def restic(self, cfg, *args):
        self.assertEqual(args[:2], ('snapshots', '--json'))
        return json.dumps(self.backup_rows if self.backup_rows is not None else
                          [{'id': self.snapshot_id, 'hostname': 'course-test', 'tags': [u.TAG, 'instance=course-test']}])

    def preview(self):
        return u.preview(self.directory, self.new['hub'], self.new['course'], self.canary, self.config, self.plan)

    def apply(self):
        return u.apply(self.directory, self.plan, self.canary, self.config)

    def test_preview_fresh_backup_and_apply_only_pins_keeps_stopped_files_and_roster(self):
        plan = self.preview()
        self.assertEqual(self.backups, 1)
        self.assertEqual(plan['new_images'], self.new)
        self.assertEqual(self.plan.stat().st_mode & 0o777, 0o600)
        raw = self.plan.read_text()
        self.assertNotIn(str(self.root), raw)
        self.assertNotIn('subject-a', raw)
        self.assertNotIn('SENTINEL', raw)
        before_manifest = (self.directory / 'manifest.json').read_bytes()
        receipt = self.apply()
        self.assertEqual(receipt['status'], 'APPLIED')
        self.assertFalse(receipt['deployment_started'])
        self.assertFalse(self.hub_running)
        env = u.environment(self.directory)
        expected = dict(self.env, WORKSPACE_HUB_IMAGE=self.new['hub'], WORKSPACE_IMAGE=self.new['course'])
        self.assertEqual(env, expected)
        self.assertEqual((self.directory / 'manifest.json').read_bytes(), before_manifest)
        for name in self.names:
            self.assertEqual((self.homes / name / 'saved.py').read_text(), 'print(42)\n')
        self.assertFalse((self.directory / u.PENDING).exists())

    def test_changed_saved_file_equal_size_and_restored_mtime_is_stale(self):
        self.preview()
        saved = self.homes / self.names[0] / 'saved.py'
        metadata = saved.stat()
        saved.write_text('print(43)\n')
        os.utime(saved, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        with self.assertRaises(u.UpgradeError):
            self.apply()
        self.assertFalse((self.directory / u.PENDING).exists())

    def test_hub_data_and_original_manifest_changes_make_plan_stale(self):
        for target in [self.hub / 'hub.sqlite', self.directory / 'manifest.json']:
            with self.subTest(target=target.name):
                if self.plan.exists():
                    self.plan.unlink()
                self.preview()
                target.write_bytes(target.read_bytes() + b' changed')
                with self.assertRaises(u.UpgradeError):
                    self.apply()
                self.assertFalse((self.directory / u.PENDING).exists())

    def test_config_password_canary_tool_and_environment_drift_refuse(self):
        for kind in ['config', 'password', 'canary', 'tool', 'env']:
            with self.subTest(kind=kind):
                if self.plan.exists():
                    self.plan.unlink()
                self.preview()
                if kind == 'config':
                    original = self.config.read_bytes()
                    self.config.write_bytes(original + b' ')
                elif kind == 'password':
                    original = self.password.read_bytes()
                    self.password.write_bytes(original + b'x')
                elif kind == 'canary':
                    original = self.canary.read_bytes()
                    self.canary.write_bytes(original + b' ')
                elif kind == 'env':
                    original = (self.directory / '.env').read_bytes()
                    write(self.directory / '.env', original.replace(b'WORKSPACE_ACTIVE_LIMIT=4', b'WORKSPACE_ACTIVE_LIMIT=3'))
                with patch('upgrade.tool_hash', return_value='8' * 64 if kind == 'tool' else '9' * 64):
                    with self.assertRaises(u.UpgradeError):
                        self.apply()
                if kind == 'config': self.config.write_bytes(original)
                elif kind == 'password': self.password.write_bytes(original)
                elif kind == 'canary': self.canary.write_bytes(original)
                elif kind == 'env': write(self.directory / '.env', original)
                self.assertFalse((self.directory / u.PENDING).exists())

    def test_wrong_backup_and_candidate_arch_missing_mutable_images_refuse(self):
        self.backup_rows = [{'id': self.snapshot_id, 'hostname': 'other', 'tags': [u.TAG, 'instance=course-test']}]
        with self.assertRaisesRegex(u.UpgradeError, 'backup_mismatch'):
            self.preview()
        self.assertFalse(self.plan.exists())
        self.backup_rows = None
        self.candidate_arch = 'arm64'
        with self.assertRaisesRegex(u.UpgradeError, 'incompatible'):
            self.preview()
        self.candidate_arch = 'amd64'
        with self.assertRaisesRegex(u.UpgradeError, 'immutable'):
            u.preview(self.directory, 'hub:latest', self.new['course'], self.canary, self.config, self.plan)
        with patch('upgrade.docker_json', side_effect=ValueError('PRIVATE-REGISTRY-TOKEN')):
            with self.assertRaises(u.UpgradeError) as error:
                self.preview()
        self.assertNotIn('PRIVATE', str(error.exception))

    def test_live_hub_or_foreign_hub_volume_writer_refuse_before_backup(self):
        for key in ['hub_running', 'foreign_writer']:
            setattr(self, key, True)
            with self.assertRaises(u.UpgradeError):
                self.preview()
            setattr(self, key, False)
        self.assertEqual(self.backups, 0)
        self.assertFalse(self.plan.exists())

    def test_incomplete_dirty_wrong_image_model_calls_or_errors_canary_refuse(self):
        variants = [lambda v: v.update(scope='local-subset'), lambda v: v.update(cleanup_verified=False),
                    lambda v: v['phases'].pop(next(iter(v['phases']))),
                    lambda v: v['provenance']['waypoint'].update(dirty=True),
                    lambda v: v['provenance']['fixture_files_sha256'].update(notebook_py='f' * 64),
                    lambda v: v['provenance']['fixture_files_sha256'].update({'notebook.py': 'f' * 64}),
                    lambda v: v['provenance']['image_ids'].update(hub=self.old['hub']),
                    lambda v: v['metrics'].update(model_calls=1), lambda v: v['metrics'].update(course_errors=1),
                    lambda v: v.update(extra_private='SENTINEL-secret')]
        for mutate in variants:
            with self.subTest(mutate=mutate):
                value = deepcopy(self.evidence)
                mutate(value)
                write(self.canary, value)
                with self.assertRaises(u.UpgradeError):
                    self.preview()
                self.assertFalse(self.plan.exists())
        self.assertEqual(self.backups, 0)

    def test_changes_during_backup_refuse_publication(self):
        def changing_backup(directory, cfg, quiet=False):
            (self.homes / self.names[1] / 'saved.py').write_text('print(99)\n')
            return self.snapshot_id
        with patch('upgrade.backup_repository_locked', side_effect=changing_backup):
            with self.assertRaisesRegex(u.UpgradeError, 'state_changed_during_backup'):
                self.preview()
        self.assertFalse(self.plan.exists())

    def test_failure_after_env_write_resumes_exact_private_journal(self):
        self.preview()
        original_write = u.write
        def interruption(path, value, exclusive=False):
            original_write(path, value, exclusive)
            if path.name == '.env':
                raise RuntimeError('SENTINEL-PRIVATE-interruption')
        with patch('upgrade.write', side_effect=interruption):
            with self.assertRaises(u.UpgradeError) as error:
                self.apply()
        self.assertNotIn('SENTINEL', str(error.exception))
        pending = self.directory / u.PENDING
        self.assertTrue(pending.exists())
        self.assertEqual(pending.stat().st_mode & 0o777, 0o600)
        self.assertEqual(u.environment(self.directory)['WORKSPACE_HUB_IMAGE'], self.new['hub'])
        receipt = self.apply()
        self.assertEqual(receipt['status'], 'APPLIED')
        self.assertFalse(pending.exists())
        self.assertEqual(self.backups, 1)

    def test_interruption_before_write_and_drift_while_pending_refuse_until_resolved(self):
        self.preview()
        original_write = u.write
        def interruption(path, value, exclusive=False):
            if path.name == '.env':
                raise OSError('private-error')
            original_write(path, value, exclusive)
        with patch('upgrade.write', side_effect=interruption):
            with self.assertRaises(u.UpgradeError): self.apply()
        self.assertEqual(u.environment(self.directory), self.env)
        target = self.hub / 'hub.sqlite'
        target.write_bytes(b'unexpectedchangeddata')
        with self.assertRaises(u.UpgradeError): self.apply()
        self.assertTrue((self.directory / u.PENDING).exists())

    def test_plan_tampering_private_mode_symlink_fifo_and_no_raw_cli_diagnostics(self):
        self.preview()
        raw = self.plan.read_bytes()
        plan = json.loads(raw)
        plan['new_images']['hub'] = self.old['hub']
        write(self.plan, plan)
        with self.assertRaisesRegex(u.UpgradeError, 'digest'):
            self.apply()
        write(self.plan, raw)
        self.plan.chmod(0o644)
        with self.assertRaises(u.UpgradeError): self.apply()
        self.plan.chmod(0o600)
        self.canary.unlink()
        os.mkfifo(self.canary, 0o600)
        with self.assertRaises(u.UpgradeError): self.apply()
        self.canary.unlink()
        self.canary.symlink_to(self.config)
        with self.assertRaises(u.UpgradeError): self.apply()
        self.canary.unlink()
        self.canary.write_text('{"PRIVATE-SECRET"')
        captured = io.StringIO()
        with redirect_stdout(captured):
            self.assertEqual(u.main(['apply', '--directory', str(self.directory), '--plan', str(self.plan),
                                     '--canary', str(self.canary), '--recovery-config', str(self.config)]), 1)
        self.assertNotIn('PRIVATE-SECRET', captured.getvalue())
        self.assertNotIn(str(self.root), captured.getvalue())

    def test_pending_preview_existing_plan_and_special_saved_files_refuse(self):
        write(self.directory / u.PENDING, {'private': 'SENTINEL'})
        with self.assertRaises(u.UpgradeError): self.preview()
        (self.directory / u.PENDING).unlink()
        self.preview()
        with self.assertRaises(u.UpgradeError): self.preview()
        self.plan.unlink()
        os.mkfifo(self.homes / self.names[0] / 'special', 0o600)
        with self.assertRaises(u.UpgradeError): self.preview()

    def test_large_supported_roster_interrupted_journal_can_resume(self):
        self.env['WORKSPACE_ALLOWED_SUBJECTS'] = ','.join('subject-' + str(index).zfill(4) + '-' + 'a' * 110 for index in range(1000))
        write(self.directory / '.env', ''.join(f'{key}={value}\n' for key, value in self.env.items()).encode())
        self.assertGreater((self.directory / '.env').stat().st_size, u.MAX_JSON)
        self.assertEqual(len(u.environment(self.directory)['WORKSPACE_ALLOWED_SUBJECTS'].split(',')), 1000)
        self.preview()
        original = u.write
        def interrupt(path, value, exclusive=False):
            original(path, value, exclusive)
            if path.name == '.env':
                raise RuntimeError('synthetic-interruption')
        with patch('upgrade.write', side_effect=interrupt):
            with self.assertRaises(u.UpgradeError):
                self.apply()
        self.assertGreater((self.directory / u.PENDING).stat().st_size, u.MAX_JSON)
        self.assertEqual(self.apply()['status'], 'APPLIED')
        self.assertEqual(len(u.environment(self.directory)['WORKSPACE_ALLOWED_SUBJECTS'].split(',')), 1000)

    def test_preview_cli_exposes_only_public_summary_not_private_fingerprints(self):
        captured = io.StringIO()
        with redirect_stdout(captured):
            result = u.main(['preview', '--directory', str(self.directory), '--plan', str(self.plan),
                             '--canary', str(self.canary), '--recovery-config', str(self.config),
                             '--hub-image', self.new['hub'], '--course-image', self.new['course']])
        self.assertEqual(result, 0)
        output = captured.getvalue()
        summary = json.loads(output)
        self.assertEqual(summary['status'], 'PREVIEWED')
        self.assertEqual(summary['snapshot_id'], self.snapshot_id)
        self.assertEqual(set(summary), {'version', 'status', 'plan_digest', 'old_images', 'new_images',
                                       'counts', 'snapshot_id', 'scope', 'deployment_started'})
        for private in ['backup_password_sha256', 'env_sha256', 'bindings', str(self.root), 'SENTINEL']:
            self.assertNotIn(private, output)
        plan = json.loads(self.plan.read_text())
        self.assertNotIn(plan['bindings']['backup_password_sha256'], output)
        self.assertNotIn(plan['bindings']['env_sha256'], output)

    def test_changed_legacy_config_read_refuses_before_backup(self):
        config = json.loads(self.config.read_text())
        config['repository'] = str(self.root / 'different-restic')
        with patch('upgrade.load_config', return_value=config):
            with self.assertRaisesRegex(u.UpgradeError, 'changed_during_read'):
                self.preview()
        self.assertEqual(self.backups, 0)
        self.assertFalse(self.plan.exists())

    def test_oversized_serialized_journal_refuses_before_mutation(self):
        self.preview()
        with patch('upgrade.MAX_JOURNAL_BYTES', 1):
            with self.assertRaisesRegex(u.UpgradeError, 'journal_budget'):
                self.apply()
        self.assertFalse((self.directory / u.PENDING).exists())
        self.assertEqual(u.environment(self.directory), self.env)

    def test_saved_tree_budget_is_enforced_before_large_listing(self):
        with patch('upgrade.MAX_ENTRIES', 1):
            with self.assertRaisesRegex(u.UpgradeError, 'saved_tree_budget'):
                u.tree_stamp([self.homes / self.names[0]])

    def test_raw_candidate_id_cannot_resolve_to_different_identity(self):
        original = self.docker_json
        def changed_identity(*args):
            result = original(*args)
            if args[:2] == ('image', 'inspect'):
                result[0]['Id'] = self.old['hub']
            return result
        with patch('upgrade.docker_json', side_effect=changed_identity):
            with self.assertRaisesRegex(u.UpgradeError, 'identity_changed'):
                self.preview()
        self.assertEqual(self.backups, 0)

    def test_reserved_plan_and_saved_tree_destinations_refuse_before_backup(self):
        for target in [self.directory / '.upgrade-pending.json', self.directory / '.maintenance-pending.json',
                       self.directory / 'source-manifest.json', self.homes / self.names[0] / 'plan.json',
                       self.hub / 'plan.json']:
            with self.subTest(target=target.name):
                with self.assertRaises(u.UpgradeError):
                    u.preview(self.directory, self.new['hub'], self.new['course'], self.canary, self.config, target)
                self.assertFalse(target.exists())
        self.assertEqual(self.backups, 0)

    def test_canary_config_and_password_path_rebinding_refuse_identical_bytes(self):
        self.preview()
        other = self.root / 'other-PASS.json'
        write(other, self.canary.read_bytes())
        with self.assertRaises(u.UpgradeError):
            u.apply(self.directory, self.plan, other, self.config)
        other_config = self.root / 'other-recovery.json'
        write(other_config, self.config.read_bytes())
        with self.assertRaises(u.UpgradeError):
            u.apply(self.directory, self.plan, self.canary, other_config)


if __name__ == '__main__':
    unittest.main()
