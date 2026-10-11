from contextlib import nullcontext, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
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
import retirement as r
import upgrade as u
from maintenance import write, digest
from public_course import render


class RetirementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='retirement-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.directory = self.root / 'deployment'
        self.directory.mkdir(mode=0o700)
        self.homes = self.root / 'homes'
        self.homes.mkdir()
        self.hub = self.root / 'hub-data'
        self.hub.mkdir()
        self.saved = self.homes / 'retained-home'
        self.saved.mkdir()
        (self.saved / 'saved.py').write_text('print(42)\n')
        (self.hub / 'hub.sqlite').write_text('retained historical Hub data')
        self.images = {'hub': 'sha256:' + 'a' * 64, 'course': 'sha256:' + 'b' * 64}
        _, _, self.env = render('work.example.test', 'https://id.example.test/realms/course', 'course',
                                 'course-test', ['subject-a'], str(self.homes))
        self.env.update(WORKSPACE_HUB_IMAGE=self.images['hub'], WORKSPACE_IMAGE=self.images['course'],
                        WORKSPACE_OIDC_CLIENT_SECRET='PRIVATE-CLIENT-SECRET', WORKSPACE_ENV_FILE=str(self.directory / '.env'))
        write(self.directory / '.env', ''.join(f'{key}={value}\n' for key, value in self.env.items()).encode())
        write(self.directory / 'manifest.json', {'version': 1, 'owners': {'support': 'PRIVATE-SUPPORT'}})
        self.password = self.root / 'backup-password'
        self.password.write_text('PRIVATE-BACKUP-PASSWORD-24chars')
        self.password.chmod(0o600)
        self.config = self.root / 'recovery.json'
        write(self.config, {'version': 1, 'repository': str(self.root / 'restic'), 'password_file': str(self.password),
                            'local_test': True, 'keep_last': 7, 'keep_daily': 7, 'keep_weekly': 4})
        self.plan = self.root / 'retirement-plan.json'
        self.owner = 'PRIVATE-RETENTION-OWNER'
        self.review_after = (datetime.now(timezone.utc).date() + timedelta(days=365)).isoformat()
        self.snapshot_id = 'e' * 64
        self.backups = 0
        self.running = False
        self.snapshot_tags = [r.TAG, r.RETIRED_TAG, 'instance=course-test']
        self.snapshot_host = 'course-test'
        for target, replacement in [('retirement.preflight', self.preflight), ('retirement.home_lock', lambda root: nullcontext()),
                                    ('retirement.backup_repository_locked', self.backup), ('retirement.restic', self.restic),
                                    ('retirement.tool_hash', lambda: 'f' * 64), ('upgrade.state', self.state),
                                    ('upgrade.hub_data', lambda env, images: self.hub), ('upgrade.worker_architecture', lambda: 'amd64'),
                                    ('upgrade.candidate_image', lambda value, arch: value)]:
            mock = patch(target, replacement)
            mock.start()
            self.addCleanup(mock.stop)

    def state(self, directory, env, images):
        if self.running:
            raise ValueError('PRIVATE-running-writer')
        files = u.tree_stamp([self.saved, self.hub])
        return {'env_sha256': digest(env), 'policy_sha256': digest({'env': digest(env), 'quota': 5120}),
                'files_sha256': files['sha256'], 'source_manifest_sha256': u.sha((self.directory / 'manifest.json').read_bytes())}, {
                    'retained_homes': 1, 'file_entries': files['entry_count']}

    def preflight(self, directory):
        if any((directory / name).exists() for name in (r.PENDING, r.MARKER)):
            raise ValueError('retirement-public-gate')
        return {'images': self.images}

    def backup(self, directory, cfg, quiet=False, extra_tags=()):
        self.assertTrue(quiet)
        self.assertEqual(extra_tags, (r.RETIRED_TAG,))
        self.backups += 1
        return self.snapshot_id

    def restic(self, cfg, *args):
        self.assertEqual(args[:2], ('snapshots', '--json'))
        return json.dumps([{'id': self.snapshot_id, 'hostname': self.snapshot_host, 'tags': self.snapshot_tags}])

    def preview(self):
        return r.preview(self.directory, self.config, self.plan, self.owner, self.review_after)

    def apply(self):
        return r.apply(self.directory, self.plan, self.config)

    def test_private_preview_tagged_backup_apply_keeps_env_roster_and_all_files(self):
        env_raw = (self.directory / '.env').read_bytes()
        original_saved = (self.saved / 'saved.py').read_bytes()
        plan = self.preview()
        self.assertEqual(self.backups, 1)
        self.assertEqual(plan['retention_owner'], self.owner)
        self.assertEqual(self.plan.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('PRIVATE-CLIENT', self.plan.read_text())
        receipt = self.apply()
        self.assertEqual(receipt['status'], 'RETIRED')
        self.assertFalse(receipt['deployment_started'])
        self.assertFalse(receipt['files_deleted'])
        self.assertFalse(self.running)
        ledger = r.read_retirement(self.directory)
        self.assertEqual(ledger['retention'], 'pending')
        self.assertEqual(ledger['retention_owner'], self.owner)
        self.assertEqual(ledger['review_after'], self.review_after)
        self.assertEqual((self.directory / r.MARKER).stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.directory / '.env').read_bytes(), env_raw)
        self.assertEqual((self.saved / 'saved.py').read_bytes(), original_saved)
        self.assertTrue((self.hub / 'hub.sqlite').exists())
        self.assertFalse((self.directory / r.PENDING).exists())
        self.assertEqual(self.apply(), receipt)
        self.assertEqual(self.backups, 1)

    def test_interrupt_before_retired_seal_journal_resumes_without_new_backup(self):
        self.preview()
        original_write = r.write
        def interruption(path, value, exclusive=False):
            if path.name == r.MARKER:
                raise OSError('PRIVATE-error')
            original_write(path, value, exclusive)
        with patch('retirement.write', side_effect=interruption):
            with self.assertRaises(r.RetirementError): self.apply()
        self.assertTrue((self.directory / r.PENDING).exists())
        self.assertFalse((self.directory / r.MARKER).exists())
        self.assertEqual(self.apply()['status'], 'RETIRED')
        self.assertFalse((self.directory / r.PENDING).exists())
        self.assertEqual(self.backups, 1)

    def test_interrupt_after_actual_retired_seal_cannot_reopen_and_resumes(self):
        self.preview()
        original_write = r.write
        def interruption(path, value, exclusive=False):
            original_write(path, value, exclusive)
            if path.name == r.MARKER:
                raise RuntimeError('PRIVATE-error')
        with patch('retirement.write', side_effect=interruption):
            with self.assertRaises(r.RetirementError) as error: self.apply()
        self.assertNotIn('PRIVATE', str(error.exception))
        self.assertEqual(r.read_retirement(self.directory)['status'], 'RETIRED')
        self.assertTrue((self.directory / r.PENDING).exists())
        self.assertEqual(self.apply()['status'], 'RETIRED')
        self.assertFalse((self.directory / r.PENDING).exists())

    def test_stale_saved_hub_manifest_environment_config_or_password_refuse(self):
        for kind in ['saved', 'hub', 'manifest', 'env', 'config', 'password']:
            with self.subTest(kind=kind):
                if self.plan.exists(): self.plan.unlink()
                self.preview()
                target = {'saved': self.saved / 'saved.py', 'hub': self.hub / 'hub.sqlite',
                          'manifest': self.directory / 'manifest.json', 'env': self.directory / '.env',
                          'config': self.config, 'password': self.password}[kind]
                original = target.read_bytes()
                if kind == 'env':
                    write(target, original.replace(b'WORKSPACE_ACTIVE_LIMIT=4', b'WORKSPACE_ACTIVE_LIMIT=3'))
                else:
                    target.write_bytes(original + b' ')
                with self.assertRaises(r.RetirementError): self.apply()
                self.assertFalse((self.directory / r.PENDING).exists())
                self.assertFalse((self.directory / r.MARKER).exists())
                write(target, original)

    def test_invalid_owner_date_or_unprotected_wrong_instance_backup_refuse(self):
        for owner, date in [('', self.review_after), ('PRIVATE\nINJECTION', self.review_after), (self.owner, '2026-02-30'),
                            (self.owner, '2000-01-01'), (self.owner, '2999-01-01')]:
            with self.assertRaises(r.RetirementError):
                r.preview(self.directory, self.config, self.plan, owner, date)
        self.assertEqual(self.backups, 0)
        for tags, host in [([r.TAG, 'instance=course-test'], 'course-test'), (self.snapshot_tags, 'foreign-instance')]:
            self.snapshot_tags, self.snapshot_host = tags, host
            with self.assertRaisesRegex(r.RetirementError, 'backup_proof'):
                self.preview()
            self.assertFalse(self.plan.exists())

    def test_changes_during_tagged_backup_refuse_plan_but_do_not_remove_snapshot(self):
        def changing_backup(directory, cfg, quiet=False, extra_tags=()):
            self.backups += 1
            self.assertEqual(extra_tags, (r.RETIRED_TAG,))
            (self.saved / 'saved.py').write_text('print(43)\n')
            return self.snapshot_id
        with patch('retirement.backup_repository_locked', side_effect=changing_backup):
            with self.assertRaisesRegex(r.RetirementError, 'changed_during'):
                self.preview()
        self.assertEqual(self.backups, 1)
        self.assertFalse(self.plan.exists())
        self.assertFalse((self.directory / r.MARKER).exists())

    def test_pending_or_retired_courses_other_operations_live_writers_refuse(self):
        for name in [r.PENDING, r.MARKER, u.PENDING, u.MAINTENANCE_PENDING]:
            write(self.directory / name, {'unknown': 'PRIVATE'})
            with self.assertRaises(r.RetirementError): self.preview()
            (self.directory / name).unlink()
        self.running = True
        with self.assertRaises(r.RetirementError): self.preview()
        self.assertEqual(self.backups, 0)

    def test_unreadable_symlink_fifo_oversized_and_tampered_marker_fail_closed(self):
        marker = self.directory / r.MARKER
        for raw in [b'{"PRIVATE-SECRET"', b'{}', b' ' * (r.MAX_JSON + 1)]:
            write(marker, raw)
            with self.assertRaises(r.RetirementError): r.read_retirement(self.directory)
            marker.unlink()
        marker.symlink_to(self.config)
        with self.assertRaises(r.RetirementError): r.read_retirement(self.directory)
        marker.unlink()
        os.mkfifo(marker, 0o600)
        with self.assertRaises(r.RetirementError): r.read_retirement(self.directory)
        marker.unlink()
        self.preview()
        self.apply()
        record = json.loads(marker.read_text())
        record['retention_owner'] = 'changed'
        write(marker, record)
        with self.assertRaises(r.RetirementError): r.read_retirement(self.directory)
        marker.chmod(0o644)
        with self.assertRaises(r.RetirementError): r.read_retirement(self.directory)

    def test_minimal_restored_retirement_record_supported_and_cannot_apply_old_plan(self):
        self.preview()
        minimal = {'version': r.VERSION, 'status': 'RETIRED', 'source': 'snapshot_restore',
                   'snapshot_id': self.snapshot_id, 'retention': 'pending'}
        write(self.directory / r.MARKER, minimal)
        self.assertEqual(r.read_retirement(self.directory), minimal)
        with self.assertRaises(r.RetirementError): self.apply()
        for field in ['retention', 'source', 'snapshot_id']:
            value = deepcopy(minimal)
            value[field] = 'unexpected'
            write(self.directory / r.MARKER, value)
            with self.assertRaises(r.RetirementError): r.read_retirement(self.directory)

    def test_private_fingerprints_owner_paths_and_dates_never_enter_cli(self):
        captured = io.StringIO()
        with redirect_stdout(captured):
            self.assertEqual(r.main(['preview', '--directory', str(self.directory), '--plan', str(self.plan),
                                     '--recovery-config', str(self.config), '--retention-owner', self.owner,
                                     '--review-after', self.review_after]), 0)
        output = captured.getvalue()
        for secret in ['PRIVATE', str(self.root), self.review_after, 'bindings', 'env_sha256', 'backup_password_sha256']:
            self.assertNotIn(secret, output)
        plan = json.loads(self.plan.read_text())
        self.assertNotIn(plan['bindings']['backup_password_sha256'], output)
        self.assertEqual(json.loads(output)['status'], 'PREVIEWED')
        write(self.plan, b'{"PRIVATE-malformed"')
        captured = io.StringIO()
        with redirect_stdout(captured):
            self.assertEqual(r.main(['apply', '--directory', str(self.directory), '--plan', str(self.plan),
                                     '--recovery-config', str(self.config)]), 1)
        self.assertNotIn('PRIVATE', captured.getvalue())
        self.assertNotIn(str(self.root), captured.getvalue())

    def test_reserved_output_tool_drift_and_exact_journal_required(self):
        with self.assertRaises(r.RetirementError):
            r.preview(self.directory, self.config, self.directory / r.PENDING, self.owner, self.review_after)
        self.assertEqual(self.backups, 0)
        self.preview()
        with patch('retirement.tool_hash', return_value='0' * 64):
            with self.assertRaises(r.RetirementError): self.apply()
        write(self.directory / r.PENDING, {'version': r.VERSION, 'plan': {}, 'ledger': {}})
        with self.assertRaises(r.RetirementError): self.apply()
        self.assertFalse((self.directory / r.MARKER).exists())


if __name__ == '__main__':
    unittest.main()
