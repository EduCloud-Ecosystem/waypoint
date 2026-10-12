"""A partial resource change must not escape through another lifecycle path."""
from contextlib import nullcontext
import os
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import allocation
import deployment
import maintenance
import pilot
import recovery
import retirement
import resource_amendment
import upgrade
from public_course import render


class ResourceAmendmentGateTests(unittest.TestCase):
    def fixture(self, root, dangling=False):
        pending = root / '.resource-amendment-pending.json'
        if dangling:
            pending.symlink_to(root / 'missing-record')
        else:
            pending.write_text('{}')
            pending.chmod(0o600)

    def test_pending_and_dangling_gate_blocks_all_activation_and_policy_readers(self):
        for dangling in (False, True):
            with self.subTest(dangling=dangling), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve(); self.fixture(root, dangling)
                operations = [lambda: deployment.settings(root),
                              lambda: deployment.preflight(root, allow_pending=True),
                              lambda: deployment._inspect_course(root, allow_pending=True),
                              lambda: deployment.compose(root, '--profile', 'build', 'up', '-d', 'hub'),
                              lambda: deployment._compose_locked(root, 'restart', 'hub'),
                              lambda: maintenance.environment(root),
                              lambda: upgrade.environment(root),
                              lambda: retirement.environment(root),
                              lambda: retirement.no_conflicts(root, root / 'homes', allow_retirement=True),
                              lambda: recovery.copy_worker_allocation(root, root / 'bundle'),
                              lambda: recovery.package(root, {}, root / 'bundle'),
                              lambda: recovery.backup_repository(root, {}),
                              lambda: pilot.start(root, {}),
                              lambda: pilot._start(root, {}, False)]
                with patch('deployment.subprocess.run') as command, patch('recovery.restic') as restic, \
                        patch('pilot.docker') as docker:
                    for operation in operations:
                        with self.subTest(operation=operation):
                            with self.assertRaisesRegex(ValueError, 'resource amendment'):
                                operation()
                    command.assert_not_called(); restic.assert_not_called(); docker.assert_not_called()
                self.assertFalse((root / 'bundle').exists())

    def test_allocator_legacy_and_managed_entrypoints_do_not_ignore_gate(self):
        for dangling in (False, True):
            with self.subTest(dangling=dangling), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve(); self.fixture(root, dangling)
                env = {'WORKSPACE_AUTH_MODE': 'local-test'}
                with patch.object(allocation, 'ROOT_UID', os.geteuid()), \
                        patch.object(allocation, 'worker_lock', side_effect=lambda: nullcontext()), \
                        patch.object(allocation, 'running_containers') as inventory, \
                        patch.object(allocation, 'daemon') as daemon:
                    for operation in (lambda: allocation.current(root, env),
                                      lambda: allocation.check(root, env),
                                      lambda: allocation.enroll(root, root / 'policy.json'),
                                      lambda: allocation.release(root)):
                        with self.assertRaisesRegex(allocation.AllocationError, 'unfinished_resource_amendment'):
                            operation()
                    with self.assertRaisesRegex(allocation.AllocationError, 'unfinished_resource_amendment'):
                        with allocation.admission(root, env):
                            self.fail('pending amendment entered admission')
                    inventory.assert_not_called(); daemon.assert_not_called()

    def test_stop_and_status_keep_working_while_resource_amendment_is_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); self.fixture(root)
            _, _, env = render('course.example.org', 'https://id.example.org/realms/course',
                               'course', 'course-one', ['alice-id'], '/srv/homes')
            env.update(WORKSPACE_ENV_FILE=str(root / '.env'), WORKSPACE_OIDC_CLIENT_SECRET='synthetic-only')
            (root / '.env').write_text(''.join(f'{k}={v}\n' for k,v in env.items()))
            (root / '.env').chmod(0o600)
            with patch('deployment.require_native_worker'), patch('deployment.subprocess.run') as command:
                deployment.compose(root, 'stop', 'hub')
                deployment.compose(root, 'ps', '-a')
            self.assertEqual(command.call_count, 2)

    def test_completed_receipt_backup_is_private_and_matches_current_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); source = root / 'course'; source.mkdir(mode=0o700)
            bundle = root / 'bundle'; bundle.mkdir(mode=0o700)
            _, _, env = render('course.example.org', 'https://id.example.org/realms/course',
                               'course', 'course-one', ['alice-id'], '/srv/homes')
            env.update(WORKSPACE_ENV_FILE=str(source / '.env'), WORKSPACE_OIDC_CLIENT_SECRET='synthetic-only',
                       WORKSPACE_RESOURCE_REVISION='c'*32)
            raw = ''.join(f'{k}={v}\n' for k,v in env.items())
            (source / '.env').write_text(raw); (source / '.env').chmod(0o600)
            with patch.object(allocation, 'ROOT_UID', os.geteuid()), \
                    patch.object(allocation, 'home_filesystem_uuid', return_value='00000000-0000-0000-0000-000000000001'):
                claim = allocation.claim(source, env)
                plan = {'digest':'a'*64, 'directory_sha256':allocation.path_digest(source),
                        'old_claim':claim, 'new_claim':claim, 'peak_claim':claim, 'new_env':raw}
                receipt = resource_amendment.receipt_record(plan)
                marker = {'version':allocation.VERSION, 'status':'ENROLLED', 'config_file':str(root / 'policy.json'),
                          'worker_id':'synthetic-worker', 'policy_sha256':'a'*64, 'daemon_sha256':'b'*64,
                          'claim_sha256':allocation.digest(claim), 'directory_sha256':allocation.path_digest(source)}
                recovery.private_json(source / allocation.MARKER, marker)
                recovery.private_json(source / resource_amendment.RECEIPT, receipt)
                self.assertTrue(recovery.copy_resource_amendment_receipt(source, bundle))
                target = bundle / 'resource-amendment-receipt.json'
                self.assertEqual(json.loads(target.read_text()), receipt)
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                changed = dict(env, WORKSPACE_ACTIVE_LIMIT=str(int(env['WORKSPACE_ACTIVE_LIMIT']) + 1))
                (source / '.env').write_text(''.join(f'{k}={v}\n' for k,v in changed.items()))
                with self.assertRaisesRegex(ValueError, 'differs from current course enrollment'):
                    recovery.copy_resource_amendment_receipt(source, bundle)
                self.assertEqual(json.loads(target.read_text()), receipt)
                # Lost local history must stop before a repository call or image export.
                (source / resource_amendment.RECEIPT).unlink()
                with patch('recovery.restic') as restic, patch('recovery.docker') as docker:
                    with self.assertRaisesRegex(ValueError, 'completed_resource_history_required'):
                        recovery.backup_repository_locked(source, {})
                    with self.assertRaisesRegex(ValueError, 'completed_resource_history_required'):
                        recovery.copy_resource_amendment_receipt(source, bundle)
                    restic.assert_not_called(); docker.assert_not_called()

    def test_archived_gates_do_not_import_unshipped_amendment_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); runtime = root / 'runtime'; runtime.mkdir()
            course = root / 'course'; course.mkdir(mode=0o700); self.fixture(course)
            source = Path(__file__).resolve().parents[1]
            for name in recovery.CURRENT_CODE:
                if name == 'resource_amendment.py':
                    continue
                target = runtime / name; target.parent.mkdir(exist_ok=True, parents=True)
                shutil.copyfile(source / name, target)
            script = '''import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import deployment, allocation, recovery
course=Path(sys.argv[2])
for action in (lambda: deployment.preflight(course,allow_pending=True),
               lambda: allocation.check(course,{'WORKSPACE_AUTH_MODE':'local-test'}),
               lambda: recovery.package(course,{},course/'bundle')):
    try: action()
    except ValueError: pass
    else: raise AssertionError('archived entrypoint bypassed pending gate')
print('ARCHIVED_GATES_REFUSED')
'''
            result = subprocess.run([sys.executable, '-I', '-c', script, str(runtime), str(course)],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('ARCHIVED_GATES_REFUSED', result.stdout)


if __name__ == '__main__':
    unittest.main()
