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
    def test_managed_restore_gate_precedes_usable_environment_and_home_allocation(self):
        from deployment import require_current_allocation
        for retired in (False, True):
            with self.subTest(retired=retired), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); destination=root/'restored'; snapshot_id='a'*64
                saved={'WORKSPACE_AUTH_MODE':'oidc','WORKSPACE_HOME_ROOT':'/source/homes'}
                rows=json.dumps([{'id':snapshot_id,'tags':['educloud-workspace-v1']}])
                with patch('recovery.restic',side_effect=[rows,'']), \
                        patch('recovery.validate_bundle',return_value=({'version':4,'retirement_protected':retired,'images':{}},saved)), \
                        patch('recovery.current_institutional_environment',return_value={'WORKSPACE_ALLOWED_SUBJECTS':'bob'}), \
                        patch('recovery.docker',side_effect=RuntimeError('before allocation')):
                    with self.assertRaisesRegex(RuntimeError,'before allocation'):
                        restore_repository({},snapshot_id,destination,18000,root/'homes',root/'current.env')
                gate=json.loads((destination/'.worker-allocation-required.json').read_text())
                self.assertEqual(gate, {'version':'educloud-worker-allocation-required-v1','source':'snapshot_restore'})
                self.assertFalse((destination/'.env').exists())
                self.assertFalse((destination/'.worker-allocation.json').exists())
                self.assertEqual((destination/'.course-retired.json').exists(),retired)
                with self.assertRaisesRegex(ValueError,'current worker allocation'):
                    require_current_allocation(destination)

    def test_managed_historical_binding_is_private_and_cannot_silently_downgrade(self):
        import allocation
        from recovery import copy_worker_allocation
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve(); source=root/'source'; source.mkdir(mode=0o700)
            bundle=root/'bundle'; bundle.mkdir(mode=0o700); authority=root/'authority'
            record={'version':allocation.VERSION,'status':'ENROLLED','config_file':str(root/'policy.json'),
                    'worker_id':'synthetic-worker','policy_sha256':'a'*64,'daemon_sha256':'b'*64,
                    'claim_sha256':'c'*64,'directory_sha256':allocation.path_digest(source)}
            with patch.object(allocation,'AUTHORITY',authority), patch.object(allocation,'ROOT_UID',os.geteuid()):
                self.assertFalse(copy_worker_allocation(source,bundle))
                authority.mkdir(mode=0o700)
                with self.assertRaisesRegex(ValueError,'marker is missing'):
                    copy_worker_allocation(source,bundle)
                private_json(source/allocation.MARKER,record)
                self.assertTrue(copy_worker_allocation(source,bundle))
                self.assertEqual(json.loads((bundle/'worker-allocation.json').read_text()),record)
                self.assertEqual((bundle/'worker-allocation.json').stat().st_mode&0o777,0o600)
                self.assertFalse((bundle/'authority.json').exists())
                self.assertFalse((bundle/'ledger.json').exists())
                (bundle/'worker-allocation.json').unlink()
                private_json(source/allocation.MARKER,dict(record,status='PENDING'))
                with self.assertRaisesRegex(ValueError,'enrollment is incomplete'):
                    copy_worker_allocation(source,bundle)
                private_json(source/allocation.REQUIRED,{'version':'educloud-worker-allocation-required-v1','source':'snapshot_restore'})
                with self.assertRaisesRegex(ValueError,'current worker enrollment'):
                    copy_worker_allocation(source,bundle)

    def test_v4_validation_requires_managed_state_and_allocator_runtime(self):
        from recovery import CURRENT_CODE
        from public_course import render
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve(); (root/'runtime').mkdir(); (root/'volumes').mkdir()
            source_code=Path(__file__).resolve().parents[1]
            for name in CURRENT_CODE:
                target=root/'runtime'/name; target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(source_code/name,target)
            (root/'images.tar').write_bytes(b'synthetic image')
            (root/'volumes/runtime.json').write_text('{}')
            archive=root/'volumes/hub-data.tar.gz'; archive.write_bytes(b'synthetic volume')
            (root/'volumes/manifest.json').write_text(json.dumps({'version':1,'instance':'course-one',
                'volumes':[{'suffix':'hub-data','kind':'hub','sha256':digest(archive)}]}))
            _,_,env=render('course.example.org','https://id.example.org/realms/course','course','course-one',['bob-id'],'/srv/homes')
            env['WORKSPACE_OIDC_CLIENT_SECRET']='synthetic-secret'; env['WORKSPACE_ENV_FILE']='/srv/course/.env'
            (root/'environment.json').write_text(json.dumps(env))
            (root/'worker-allocation.json').write_text('{}')
            meta={'version':4,'retirement_protected':False,'instance':'course-one','images':{
                'hub':{'id':'sha256:'+'a'*64,'os':'linux','architecture':'amd64'}}}
            def save():
                meta['files']={str(p.relative_to(root)):digest(p) for p in root.rglob('*') if p.is_file() and p.name!='bundle.json'}
                (root/'bundle.json').write_text(json.dumps(meta))
            save()
            with patch('recovery.docker',return_value=json.dumps({'Architecture':'x86_64'})):
                self.assertEqual(validate_bundle(root)[0]['version'],4)
                for invalid in (None,1,'false'):
                    meta['retirement_protected']=invalid; save()
                    with self.assertRaisesRegex(ValueError,'explicit retirement state'): validate_bundle(root)
                meta['retirement_protected']=False
                # The new amendment CLI is optional historical runtime context;
                # old managed v4 snapshots did not contain it.
                optional=root/'runtime/resource_amendment.py'; raw=optional.read_bytes()
                optional.unlink(); save()
                self.assertEqual(validate_bundle(root)[0]['version'],4)
                optional.write_bytes(raw); save()
                for name in ('worker-allocation.json','runtime/allocation.py'):
                    path=root/name; raw=path.read_bytes(); path.unlink(); save()
                    with self.assertRaisesRegex(ValueError,'incomplete recovery bundle'): validate_bundle(root)
                    path.write_bytes(raw); save()
            # Archived control imports are self-contained. Required gate refuses
            # activation before invoking Docker even with otherwise valid config.
            import subprocess
            closed=root/'closed'; closed.mkdir(); private_json(closed/'.worker-allocation-required.json',
                {'version':'educloud-worker-allocation-required-v1','source':'snapshot_restore'})
            env['WORKSPACE_ENV_FILE']=str(closed/'.env')
            (closed/'.env').write_text(''.join(k+'='+v+'\n' for k,v in env.items())); (closed/'.env').chmod(0o600)
            result=subprocess.run([sys.executable,str(root/'runtime/deployment.py'),'start','--directory',str(closed)],capture_output=True,text=True,timeout=10)
            self.assertNotEqual(result.returncode,0); self.assertIn('current worker allocation',result.stderr)
            self.assertNotIn('ModuleNotFoundError',result.stderr)

    def test_retirement_snapshot_seals_destination_before_partial_restore_can_start(self):
        from recovery import RETIRED_TAG
        from deployment import require_active_course
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / 'restored'
            snapshot_id = 'a' * 64
            saved = {'WORKSPACE_AUTH_MODE':'oidc', 'WORKSPACE_HOME_ROOT':'/source/homes'}
            rows = json.dumps([{'id':snapshot_id,'tags':['educloud-workspace-v1',RETIRED_TAG]}])
            env = {'WORKSPACE_ALLOWED_SUBJECTS':'bob', 'WORKSPACE_ENV_FILE':'placeholder'}
            with patch('recovery.restic', side_effect=[rows,'']), \
                    patch('recovery.validate_bundle', return_value=({'images':{}},saved)), \
                    patch('recovery.current_institutional_environment', return_value=env), \
                    patch('recovery.docker', side_effect=RuntimeError('failure before volume allocation')):
                with self.assertRaisesRegex(RuntimeError, 'failure before volume'):
                    restore_repository({},snapshot_id,destination,18000,root/'homes',root/'current.env')
            self.assertTrue((destination/'.course-retired.json').is_file())
            self.assertFalse((destination/'.env').exists())
            with self.assertRaisesRegex(ValueError, 'course is retired'):
                require_active_course(destination)

    def test_protected_bundle_version_seals_even_without_retirement_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); destination=root/'restored'; snapshot_id='a'*64
            saved={'WORKSPACE_AUTH_MODE':'oidc','WORKSPACE_HOME_ROOT':'/source/homes'}
            rows=json.dumps([{'id':snapshot_id,'tags':['educloud-workspace-v1']}])
            with patch('recovery.restic',side_effect=[rows,'']), \
                    patch('recovery.validate_bundle',return_value=({'version':3,'images':{}},saved)), \
                    patch('recovery.current_institutional_environment',return_value={'WORKSPACE_ALLOWED_SUBJECTS':'bob'}), \
                    patch('recovery.docker',side_effect=RuntimeError('before home allocation')):
                with self.assertRaisesRegex(RuntimeError,'before home allocation'):
                    restore_repository({},snapshot_id,destination,18000,root/'homes',root/'current.env')
            self.assertEqual(json.loads((destination/'.course-retired.json').read_text())['status'],'RETIRED')
            self.assertFalse((destination/'.env').exists())

    def test_retired_trial_snapshot_refused_before_usable_trial_environment(self):
        from recovery import RETIRED_TAG
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); destination = root/'restored'
            snapshot_id = 'a'*64
            rows = json.dumps([{'id':snapshot_id,'tags':['educloud-workspace-v1',RETIRED_TAG]}])
            with patch('recovery.restic', side_effect=[rows,'']), \
                    patch('recovery.validate_bundle', return_value=({'images':{}},{'WORKSPACE_AUTH_MODE':'local-test'})), \
                    patch('recovery.initialize') as initialize:
                with self.assertRaisesRegex(ValueError, 'institutional course'):
                    restore_repository({},snapshot_id,destination,18000)
                initialize.assert_not_called()
            self.assertFalse(destination.exists())

    def test_prepared_retirement_backup_keeps_protection_tag_without_starting(self):
        from recovery import backup_repository_locked, RETIRED_TAG
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {'WORKSPACE_INSTANCE':'course-one'}
            with patch('recovery.settings', return_value=env), patch('recovery.package') as package, \
                    patch('recovery.restic', side_effect=['',json.dumps({'message_type':'summary','snapshot_id':'a'*64}), '']) as backend:
                self.assertEqual(backup_repository_locked(root,{},extra_tags=(RETIRED_TAG,)), 'a'*64)
            args = backend.call_args_list[1].args
            self.assertIn(RETIRED_TAG, args)
            self.assertTrue(package.call_args.kwargs['protected'])
            self.assertFalse((root / '.course-retired.json').exists())

    def test_completed_retired_backup_automatically_keeps_protection_tag(self):
        from recovery import backup_repository_locked, RETIRED_TAG
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private_json(root/'.course-retired.json', {'version':'educloud-retirement-v1','status':'RETIRED'})
            with patch('recovery.settings', return_value={'WORKSPACE_INSTANCE':'course-one'}), \
                    patch('recovery.package'), patch('recovery.restic', side_effect=['',
                    json.dumps({'message_type':'summary','snapshot_id':'a'*64}), '']) as backend:
                self.assertEqual(backup_repository_locked(root, {}), 'a'*64)
            self.assertIn(RETIRED_TAG, backend.call_args_list[1].args)

    def test_pending_retirement_blocks_backup_before_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'.retirement-pending.json').symlink_to(root/'missing')
            with patch('recovery.restic') as backend:
                with self.assertRaisesRegex(ValueError, 'retirement is incomplete'):
                    backup_repository(root, {})
                backend.assert_not_called()

    def test_stopped_old_hub_cannot_back_up_new_image_configuration(self):
        from recovery import package
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {'WORKSPACE_INSTANCE':'course-one', 'WORKSPACE_IMAGE':'sha256:'+'b'*64,
                   'WORKSPACE_HUB_IMAGE':'sha256:'+'c'*64}
            with patch('recovery.settings', return_value=env), \
                    patch('recovery.docker', return_value=json.dumps([{'Image':'sha256:'+'a'*64}])), \
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
        self.assertIn('--keep-tag', args)
        self.assertIn('educloud-retired-v1', args)


if __name__ == '__main__':
    unittest.main()
