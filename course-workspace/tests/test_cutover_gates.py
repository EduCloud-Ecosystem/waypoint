"""Cutover participants cannot escape fencing through another lifecycle path."""
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


class CutoverGateTests(unittest.TestCase):
    def fixture(self, root, name, dangling):
        path = root / name
        if dangling:
            path.symlink_to(root / 'missing-private-record')
        else:
            path.write_text('{}'); path.chmod(0o600)

    def test_pending_and_source_fence_block_lifecycle_before_effects(self):
        for name in ('.cutover-pending.json', '.course-replaced.json'):
            for dangling in (False, True):
                with self.subTest(name=name, dangling=dangling), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp).resolve(); self.fixture(root, name, dangling)
                    actions = [lambda: deployment.settings(root),
                               lambda: deployment.preflight(root, allow_pending=True),
                               lambda: deployment._inspect_course(root, allow_pending=True),
                               lambda: deployment.compose(root, 'up', '-d', 'hub'),
                               lambda: deployment._compose_locked(root, 'restart', 'hub'),
                               lambda: maintenance.environment(root),
                               lambda: upgrade.environment(root),
                               lambda: retirement.environment(root),
                               lambda: retirement.no_conflicts(root, root / 'homes', allow_retirement=True),
                               lambda: resource_amendment.no_other_pending(root, root / 'homes'),
                               lambda: recovery.copy_worker_allocation(root, root / 'bundle'),
                               lambda: recovery.package(root, {}, root / 'bundle'),
                               lambda: recovery.backup_repository(root, {}),
                               lambda: recovery.backup_repository_locked(root, {}),
                               lambda: pilot.start(root, {}),
                               lambda: pilot._start(root, {}, False)]
                    with patch('deployment.subprocess.run') as command, patch('recovery.restic') as backend, \
                            patch('pilot.docker') as docker:
                        for action in actions:
                            with self.assertRaisesRegex(ValueError, 'cutover|replaced'):
                                action()
                        command.assert_not_called(); backend.assert_not_called(); docker.assert_not_called()
                    self.assertFalse((root / 'bundle').exists())

    def test_allocator_legacy_entrypoints_refuse_before_daemon_or_docker(self):
        for name in ('.cutover-pending.json', '.course-replaced.json'):
            for dangling in (False, True):
                with self.subTest(name=name, dangling=dangling), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp).resolve(); self.fixture(root, name, dangling)
                    with patch.object(allocation, 'ROOT_UID', os.geteuid()), \
                            patch.object(allocation, 'worker_lock', side_effect=lambda: nullcontext()), \
                            patch.object(allocation, 'running_containers') as inventory, \
                            patch.object(allocation, 'daemon') as daemon:
                        for action in (lambda: allocation.current(root, {'WORKSPACE_AUTH_MODE':'local-test'}),
                                       lambda: allocation.check(root, {'WORKSPACE_AUTH_MODE':'local-test'}),
                                       lambda: allocation.enroll(root, root / 'policy.json'),
                                       lambda: allocation.release(root)):
                            with self.assertRaises(ValueError): action()
                        with self.assertRaises(ValueError):
                            with allocation.admission(root, {'WORKSPACE_AUTH_MODE':'local-test'}):
                                self.fail('cutover participant entered admission')
                        inventory.assert_not_called(); daemon.assert_not_called()

    def test_stop_and_status_remain_available_for_both_participant_states(self):
        for name in ('.cutover-pending.json', '.course-replaced.json'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve(); self.fixture(root, name, False)
                _, _, env = render('course.example.org', 'https://id.example.org/realms/course',
                                   'course', 'course-one', ['alice-id'], '/srv/homes')
                env.update(WORKSPACE_ENV_FILE=str(root / '.env'), WORKSPACE_OIDC_CLIENT_SECRET='synthetic-only')
                (root / '.env').write_text(''.join(f'{k}={v}\n' for k,v in env.items()))
                (root / '.env').chmod(0o600)
                with patch('deployment.require_native_worker'), patch('deployment.subprocess.run') as command:
                    deployment.compose(root, 'stop', 'hub'); deployment.compose(root, 'ps', '-a')
                self.assertEqual(command.call_count, 2)

    def test_active_history_is_private_and_loss_refuses_before_provider_effects(self):
        import cutover
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); source = root / 'course'; source.mkdir(mode=0o700)
            bundle = root / 'bundle'; bundle.mkdir(mode=0o700)
            _, _, env = render('course.example.org', 'https://id.example.org/realms/course',
                               'course', 'course-one', ['alice-id'], '/srv/homes')
            env.update(WORKSPACE_ENV_FILE=str(source / '.env'), WORKSPACE_OIDC_CLIENT_SECRET='synthetic-only',
                       WORKSPACE_CUTOVER_REVISION='c'*32)
            (source / '.env').write_text(''.join(f'{k}={v}\n' for k,v in env.items()))
            (source / '.env').chmod(0o600)
            key = allocation.path_digest(source)
            record = {'version':cutover.VERSION, 'status':'COMPLETE', 'role':'ACTIVE',
                      'endpoint_sha256':key, 'counterpart_sha256':allocation.path_digest(root / 'previous'),
                      'active_sha256':key, 'claim_sha256':'a'*64, 'tree_sha256':'b'*64,
                      'plan_sha256':'d'*64, 'revision':'c'*32}
            record['digest'] = allocation.digest(record)
            with patch.object(allocation, 'ROOT_UID', os.geteuid()), \
                    patch.object(allocation, 'worker_lock', side_effect=lambda: nullcontext()), \
                    patch.object(allocation, 'current', return_value=({}, {}, {})) as current:
                recovery.private_json(source / cutover.RECEIPT, record)
                self.assertTrue(recovery.copy_cutover_receipt(source, bundle))
                target = bundle / 'cutover-receipt.json'
                self.assertEqual(json.loads(target.read_text()), record)
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                current.assert_called_once()
                # Historical home/resources may change; the receipt stays history.
                env['WORKSPACE_ACTIVE_LIMIT'] = str(int(env['WORKSPACE_ACTIVE_LIMIT']) + 1)
                (source / '.env').write_text(''.join(f'{k}={v}\n' for k,v in env.items()))
                self.assertEqual(recovery.current_cutover_receipt(source), record)
                (source / cutover.RECEIPT).unlink()
                with patch('recovery.restic') as backend, patch('recovery.docker') as docker:
                    with self.assertRaisesRegex(ValueError, 'completed_cutover_history_required'):
                        recovery.backup_repository_locked(source, {})
                    backend.assert_not_called(); docker.assert_not_called()
                self.assertEqual(json.loads(target.read_text()), record)
                replaced = dict(record, role='REPLACED', active_sha256=record['counterpart_sha256'])
                replaced['digest'] = allocation.digest({k:v for k,v in replaced.items() if k!='digest'})
                recovery.private_json(source / cutover.RECEIPT, replaced)
                # No local fence exists here: receipt role independently refuses.
                with patch('recovery.restic') as backend:
                    with self.assertRaisesRegex(ValueError, 'replaced source'):
                        recovery.backup_repository_locked(source, {})
                    backend.assert_not_called()

    def test_fresh_managed_restore_keeps_history_without_active_revision_or_fence(self):
        import cutover
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); destination = root / 'restored'; snapshot = 'a'*64
            _, _, current = render('course.example.org', 'https://id.example.org/realms/course',
                                   'course', 'course-one', ['alice-id'], str(root / 'source-homes'))
            current.update(WORKSPACE_ENV_FILE=str(root / 'source' / '.env'),
                           WORKSPACE_OIDC_CLIENT_SECRET='synthetic-only',
                           WORKSPACE_RESOURCE_REVISION='b'*32, WORKSPACE_CUTOVER_REVISION='c'*32)
            key = allocation.path_digest(root / 'source')
            receipt = {'version':cutover.VERSION, 'status':'COMPLETE', 'role':'ACTIVE',
                       'endpoint_sha256':key, 'counterpart_sha256':allocation.path_digest(root / 'previous'),
                       'active_sha256':key, 'claim_sha256':'a'*64, 'tree_sha256':'b'*64,
                       'plan_sha256':'d'*64, 'revision':'c'*32}
            receipt['digest'] = allocation.digest(receipt); cutover.validate_receipt(receipt)
            meta = {'version':4, 'retirement_protected':False, 'instance':'course-one',
                    'images':{'course':{'id':'sha256:'+'e'*64},'hub':{'id':'sha256:'+'f'*64}}}
            def backend(cfg, *args, **kwargs):
                if args[0] == 'snapshots':
                    return json.dumps([{'id':snapshot,'tags':['educloud-workspace-v1']}])
                bundle = Path(args[args.index('--target') + 1]) / 'bundle'
                (bundle / 'volumes').mkdir(parents=True); (bundle / 'runtime').mkdir()
                (bundle / 'volumes' / 'manifest.json').write_text(json.dumps({'volumes':[]}))
                (bundle / 'worker-allocation.json').write_text('{}')
                (bundle / 'cutover-receipt.json').write_text(json.dumps(receipt))
                return ''
            with patch('recovery.restic', side_effect=backend), \
                    patch('recovery.validate_bundle', return_value=(meta, dict(current))), \
                    patch('recovery.current_institutional_environment', return_value=current), \
                    patch('recovery.docker', return_value=''), patch('recovery.subprocess.run'), \
                    patch('recovery.image_info', side_effect=lambda image: {'Id':image}), \
                    patch('recovery.restore'), patch('homes.provision'), \
                    patch('homes.registry', return_value={'homes':{}}), patch('homes.inspect'):
                restored = recovery.restore_repository({}, snapshot, destination, 18001,
                                                       root / 'replacement-homes', root / 'current.env')
            saved = deployment.read_environment(destination / '.env')
            for revision in ('WORKSPACE_RESOURCE_REVISION','WORKSPACE_CUTOVER_REVISION'):
                self.assertNotIn(revision, restored); self.assertNotIn(revision, saved)
                self.assertIn(revision, current)
            history = destination / 'source-cutover-receipt.json'
            self.assertEqual(json.loads(history.read_text()), receipt)
            self.assertEqual(history.stat().st_mode & 0o777, 0o600)
            for name in (cutover.PENDING, cutover.FENCE, cutover.RECEIPT, allocation.MARKER):
                self.assertFalse((destination / name).exists())
            self.assertTrue((destination / allocation.REQUIRED).is_file())
            with self.assertRaisesRegex(ValueError, 'current worker allocation'):
                deployment.require_current_allocation(destination)

    def test_archived_literal_guards_do_not_need_unshipped_cutover_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); runtime = root / 'runtime'; runtime.mkdir()
            course = root / 'course'; course.mkdir(mode=0o700)
            source = Path(__file__).resolve().parents[1]
            for name in recovery.CURRENT_CODE:
                if name == 'cutover.py': continue
                target = runtime / name; target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / name, target)
            script = '''import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import deployment, allocation, recovery
course=Path(sys.argv[2])
for name in ('.cutover-pending.json','.course-replaced.json'):
    gate=course/name
    gate.symlink_to(course/'missing-record')
    for action in (lambda: deployment.preflight(course,allow_pending=True),
                   lambda: allocation.check(course,{'WORKSPACE_AUTH_MODE':'local-test'}),
                   lambda: recovery.package(course,{},course/'bundle')):
        try: action()
        except ValueError: pass
        else: raise AssertionError('archived entrypoint bypassed cutover gate')
    gate.unlink()
print('ARCHIVED_CUTOVER_GATES_REFUSED')
'''
            result = subprocess.run([sys.executable, '-I', '-c', script, str(runtime), str(course)],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('ARCHIVED_CUTOVER_GATES_REFUSED', result.stdout)


if __name__ == '__main__': unittest.main()
