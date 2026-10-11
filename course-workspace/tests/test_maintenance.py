import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import homes
import maintenance as m
from public_course import render


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.directory = self.base / 'deployment'
        self.directory.mkdir(mode=0o700)
        self.root = self.base / 'homes'
        self.root.mkdir()
        _, _, self.env = render('course.example.org', 'https://id.example.org/realms/course', 'course', 'course-test', ['alice', 'bob'], str(self.root))
        self.env.update(WORKSPACE_HOME_QUOTA_MB='64', WORKSPACE_HOME_INODE_LIMIT='1000', WORKSPACE_OIDC_CLIENT_SECRET='private-secret', WORKSPACE_ENV_FILE=str(self.directory / '.env'))
        m.write(self.directory / '.env', ''.join(f'{k}={v}\n' for k, v in self.env.items()).encode())
        self.data = {'version': 1, 'next_id': 10003, 'homes': {}}
        self.volumes, self.blocks, self.inodes = {}, {}, {}
        self.running, self.refs, self.fail_create = False, False, False
        for i, subject in enumerate(['alice', 'bob', 'carol']):
            name = 'course-test-home-' + hashlib.sha256(subject.encode()).hexdigest()[:24]
            entry = dict(instance='course-test', project_id=10000+i, quota_mb=64, inode_limit=1000)
            self.data['homes'][name] = entry
            (self.root / name).mkdir()
            (self.root / name / 'saved.txt').write_text(subject + ' retained work')
            wanted = homes.expected_volume(self.root, name, entry)
            self.volumes[name] = dict(Driver='local', Labels=wanted['labels'], Options=wanted['options'])
            self.blocks[10000+i] = dict(used=32, hard=65536)
            self.inodes[10000+i] = dict(used=3, hard=1000)
        m.write(self.root / '.educloud-projects.json', self.data)
        for target, replacement in [('maintenance.require_native_worker', lambda: None), ('homes.storage', lambda root: None), ('homes.run', self.command), ('homes.limits', self.limits), ('homes.docker', self.docker), ('maintenance.docker', self.docker)]:
            p = patch(target, replacement)
            p.start()
            self.addCleanup(p.stop)
        self.plan = self.base / 'plan.json'

    def limits(self, root, kind):
        return self.blocks if kind == 'b' else self.inodes

    def command(self, *args):
        if args[0] == 'findmnt':
            return json.dumps({'filesystems': [dict(target=str(self.root), source='/dev/loop-test', uuid='same-volume', fstype='xfs')]})
        if args[0] == 'xfs_io':
            return 'projid = ' + str(self.data['homes'][Path(args[-1]).name]['project_id'])
        if args[0] == 'xfs_quota':
            command = args[3]
            project = int(command.split()[-1])
            self.blocks[project]['hard'] = int(re.search(r'bhard=(\d+)m', command)[1])*1024
            self.inodes[project]['hard'] = int(re.search(r'ihard=(\d+)', command)[1])
            return ''
        raise AssertionError(args)

    def docker(self, *args):
        if args[0] == 'ps':
            if args[-1].startswith('volume='):
                return 'container-ref' if self.refs else ''
            return 'running-hub' if self.running else ''
        if args[:2] == ('volume', 'ls'):
            return '\n'.join(self.volumes)
        if args[:2] == ('volume', 'inspect'):
            return json.dumps([self.volumes[args[-1]]])
        if args[:2] == ('volume', 'rm'):
            del self.volumes[args[-1]]
            return args[-1]
        if args[:2] == ('volume', 'create'):
            if self.fail_create:
                self.fail_create = False
                raise OSError('injected interrupted create')
            value = dict(Driver='local', Labels={}, Options={})
            for i, token in enumerate(args):
                if token in ('--label', '--opt'):
                    key, content = args[i+1].split('=', 1)
                    value['Labels' if token == '--label' else 'Options'][key] = content
            self.volumes[args[-1]] = value
            return args[-1]
        raise AssertionError(args)

    def preview(self, subjects=None, quota=96):
        return m.preview(self.directory, subjects or ['bob', 'carol'], quota, 2000, self.plan)

    def test_preview_private_and_apply_retains_removed_homes(self):
        plan = self.preview()
        self.assertNotIn('private-secret', self.plan.read_text())
        self.assertEqual(self.plan.stat().st_mode & 0o777, 0o600)
        self.assertEqual(plan['added_subjects'], ['carol'])
        self.assertEqual(plan['removed_subjects'], ['alice'])
        m.apply(self.directory, self.plan)
        self.assertEqual(m.environment(self.directory)['WORKSPACE_ALLOWED_SUBJECTS'], 'bob,carol')
        self.assertEqual(len(self.volumes), 3)
        for name in self.data['homes']:
            self.assertTrue((self.root/name/'saved.txt').read_text().endswith('retained work'))
            self.assertEqual(self.volumes[name]['Labels']['educloud.workspace.quota-bytes'], str(96*1024**2))
        self.assertFalse((self.directory/m.PENDING).exists())
        self.assertFalse((self.root/m.ROOT_PENDING).exists())
        self.assertTrue(m.read(self.directory/'.maintenance-receipt.json')['plan_digest'])

    def test_pending_upgrade_blocks_roster_quota_changes_before_inspection(self):
        (self.directory / '.upgrade-pending.json').symlink_to(self.directory / 'absent-journal')
        with patch('maintenance.snapshot') as snapshot:
            with self.assertRaisesRegex(ValueError, 'upgrade is incomplete'):
                self.preview()
            snapshot.assert_not_called()

    def test_special_operation_lock_refused_before_blocking_open(self):
        import os
        os.mkfifo(self.directory / 'operation.lock', 0o600)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            self.preview()

    def test_failure_blocks_health_and_exact_plan_resume(self):
        self.preview()
        self.fail_create = True
        with self.assertRaisesRegex(OSError, 'interrupted'):
            m.apply(self.directory, self.plan)
        with self.assertRaisesRegex(ValueError, 'unfinished maintenance'):
            homes.inspect(self.root, 'course-test')
        self.assertTrue((self.directory/m.PENDING).exists())
        m.apply(self.directory, self.plan)
        self.assertEqual(len(self.volumes), 3)
        self.assertFalse((self.directory/m.PENDING).exists())

    def test_stale_usage_plan_and_foreign_mount_refused(self):
        self.preview()
        self.blocks[10000]['used'] += 1
        with self.assertRaisesRegex(ValueError, 'stale'):
            m.apply(self.directory, self.plan)
        self.assertFalse((self.directory/m.PENDING).exists())
        self.blocks[10000]['used'] -= 1
        self.volumes[next(iter(self.volumes))]['Options']['device'] = '/foreign'
        with self.assertRaisesRegex(ValueError, 'ownership'):
            m.apply(self.directory, self.plan)

    def test_active_foreign_reference_and_shrink_below_usage_refused(self):
        self.running = True
        with self.assertRaisesRegex(ValueError, 'stop the Hub'):
            self.preview()
        self.running, self.refs = False, True
        with self.assertRaisesRegex(ValueError, 'references'):
            self.preview()
        self.refs = False
        self.blocks[10000]['used'] = 40*1024
        with self.assertRaisesRegex(ValueError, 'below actual'):
            self.preview(quota=32)
        self.assertFalse(self.plan.exists())

    def test_digest_tampering_identity_rebinding_and_unprovisioned_roster_refused(self):
        plan = self.preview()
        plan['subjects'] = ['mallory']
        m.write(self.plan, plan)
        with self.assertRaisesRegex(ValueError, 'digest'):
            m.apply(self.directory, self.plan)
        self.plan.unlink()
        with self.assertRaisesRegex(ValueError, 'provisioned'):
            self.preview(subjects=['dave'])
        m.write(m.identity_path(self.directory), dict(m.identity(self.env), WORKSPACE_OIDC_ISSUER='https://other.example.org/realms/course'))
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.preview()

    def test_symlink_home_and_plan_overwrite_refused(self):
        self.preview()
        with self.assertRaises(FileExistsError):
            self.preview()
        name = next(iter(self.data['homes']))
        path = self.root/name
        path.rename(self.root/'retained')
        path.symlink_to(self.root/'retained')
        with self.assertRaisesRegex(ValueError, 'replaced'):
            m.apply(self.directory, self.plan)

    def test_started_after_preview_refuses_without_pending_journal(self):
        self.preview()
        self.running = True
        with self.assertRaisesRegex(ValueError, 'stop the Hub'):
            m.apply(self.directory, self.plan)
        self.assertFalse((self.directory/m.PENDING).exists())
        self.assertFalse((self.root/m.ROOT_PENDING).exists())

    def test_dangling_marker_and_native_worker_refuse_before_plan(self):
        (self.root/m.ROOT_PENDING).symlink_to(self.root/'absent')
        with self.assertRaisesRegex(ValueError, 'unfinished'):
            self.preview()
        (self.root/m.ROOT_PENDING).unlink()
        with patch('maintenance.require_native_worker', side_effect=ValueError('remote daemon')):
            with self.assertRaisesRegex(ValueError, 'remote daemon'):
                self.preview()
        self.assertFalse(self.plan.exists())

    def test_inode_shrink_and_changed_resume_volume_refused(self):
        self.inodes[10000]['used'] = 2200
        with self.assertRaisesRegex(ValueError, 'below actual'):
            self.preview()
        self.inodes[10000]['used'] = 3
        self.preview()
        self.fail_create = True
        with self.assertRaises(OSError):
            m.apply(self.directory, self.plan)
        name = next(iter(self.volumes))
        self.volumes[name]['Options']['device'] = '/foreign'
        with self.assertRaisesRegex(ValueError, 'foreign'):
            m.apply(self.directory, self.plan)
        self.assertTrue((self.directory/m.PENDING).exists())

    def test_concurrent_deployment_lock_refuses_preview(self):
        with m.locked(self.directory):
            with self.assertRaisesRegex(ValueError, 'operation'):
                self.preview()


if __name__ == '__main__':
    unittest.main()
