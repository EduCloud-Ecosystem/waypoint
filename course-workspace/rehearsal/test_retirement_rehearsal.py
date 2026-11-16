"""Retirement acceptance cannot hide incomplete closure, export, or cleanup controls."""
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import shutil
import unittest
from unittest.mock import Mock, patch

import verify_retirement as r


def report():
    return {'waypoint_revision':'a'*40,'fixture_sha256':'b'*64,'exporter_sha256':'c'*64,
            'source_manifest_sha256':'d'*64,'snapshot_id':'e'*64,'source_export_sha256':'f'*64,
            'restored_export_sha256':'a'*64,'images':{'hub':'sha256:'+'b'*64,'course':'sha256:'+'c'*64},
            'counts':{'retained_homes':2,'work_files':4,'work_bytes':100,'outside_work_entries':10},
            'checks':sorted(r.CHECKS)}


class RetirementAcceptanceTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('restic'), 'real Restic is required for protected-retention acceptance')
    def test_real_retention_keeps_protected_course_and_foreign_host(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); password=root/'password'
            password.write_text('synthetic-retention-test-only'); password.chmod(0o600)
            cfg={'repository':str(root/'repository'),'password_file':str(password),'local_test':True}
            r.restic(cfg,'init')
            protected=root/'protected'; protected.write_text('retained coursework')
            foreign=root/'foreign'; foreign.write_text('another course')
            control=root/'control'; control.write_text('disposable control')
            def snapshot(path,host,*tags):
                output=r.restic(cfg,'backup','--json','--host',host,*tags,str(path))
                return next(json.loads(line)['snapshot_id'] for line in output.splitlines()
                            if json.loads(line).get('message_type')=='summary')
            protected_id=snapshot(protected,'synthetic-course','--tag',r.RETIRED_TAG)
            foreign_id=snapshot(foreign,'foreign-course')
            r.verify_protected_retention(cfg,'synthetic-course',protected_id,control)
            rows=json.loads(r.restic(cfg,'snapshots','--json'))
            self.assertEqual({row['id'] for row in rows},{protected_id,foreign_id})
            r.restic(cfg,'check','--read-data')
            r.restic(cfg,'restore',protected_id,'--target',str(root/'restored'),'--verify')
            restored=list((root/'restored').rglob('protected'))
            self.assertEqual(len(restored),1)
            self.assertEqual(restored[0].read_text(),'retained coursework')

    def test_retention_failure_or_missing_protected_snapshot_cannot_pass(self):
        backup=json.dumps({'message_type':'summary','snapshot_id':'b'*64})
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); cfg={'repository':str(root/'repository'),'local_test':True}
            for remaining in ([],[{'id':'b'*64}],[{'id':'a'*64},{'id':'b'*64}]):
                with self.subTest(remaining=remaining), patch.object(r,'restic',side_effect=[backup,'',json.dumps(remaining)]):
                    with self.assertRaises(AssertionError):r.verify_protected_retention(cfg,'synthetic-course','a'*64,root/'control')
            with patch.object(r,'restic',side_effect=[backup,RuntimeError('retention refused')]):
                with self.assertRaises(RuntimeError):r.verify_protected_retention(cfg,'synthetic-course','a'*64,root/'control')

    def test_fixture_retention_refuses_foreign_or_nonlocal_repository_before_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for repository, local in ((str(root / 'repository'), False), (str(root.parent / 'foreign'), True),
                                      ('sftp:example.invalid:/repository', True), (str(root), True),
                                      (str(root / '..' / 'foreign'), True)):
                with self.subTest(repository=repository), patch.object(r, 'restic') as restic:
                    with self.assertRaises(ValueError):
                        r.forget_unprotected_fixture_snapshots({'repository': repository, 'local_test': local}, root)
                    with self.assertRaises(ValueError):
                        r.verify_protected_retention({'repository': repository, 'local_test': local},
                            'synthetic-course', 'a'*64, root/'control')
                    restic.assert_not_called()

    @unittest.skipUnless(shutil.which('restic'), 'installed Restic required for fresh local repository regression')
    def test_real_fixture_retention_keeps_protected_bytes_and_removes_other_path_group(self):
        with tempfile.TemporaryDirectory(prefix='retirement-retention-control-') as directory:
            root = Path(directory).resolve(); root.chmod(0o700)
            password = root / 'password'; password.write_text(secrets.token_urlsafe(32)); password.chmod(0o600)
            cfg = {'repository': str(root / 'repository'), 'password_file': str(password), 'local_test': True}
            retained = root / 'protected'; retained.mkdir()
            saved = retained / 'saved.txt'; saved.write_text('synthetic protected coursework\n')
            control = root / 'unprotected.txt'; control.write_text('synthetic unprotected control\n')
            r.restic(cfg, 'init')
            def backup(path, tag):
                output = r.restic(cfg, 'backup', '--json', '--tag', tag, str(path))
                return next(json.loads(line)['snapshot_id'] for line in output.splitlines()
                            if json.loads(line).get('message_type') == 'summary')
            protected_id = backup(retained, r.RETIRED_TAG)
            control_id = backup(control, 'synthetic-unprotected-control')
            before = json.loads(r.restic(cfg, 'snapshots', '--json'))
            self.assertEqual({row['id'] for row in before}, {protected_id, control_id})
            self.assertEqual(len({tuple(row['paths']) for row in before}), 2)
            # Actual CLI execution distinguishes the corrected policy from the
            # old default grouping, which refuses the control-only group.
            with patch.object(r, 'restic', wraps=r.restic) as command:
                r.forget_unprotected_fixture_snapshots(cfg, root)
            self.assertNotIn('--unsafe-allow-remove-all', command.call_args.args)
            after = json.loads(r.restic(cfg, 'snapshots', '--json'))
            self.assertEqual({row['id'] for row in after}, {protected_id})
            self.assertIn(r.RETIRED_TAG, after[0]['tags'])
            self.assertEqual(r.restic(cfg, 'dump', protected_id, str(saved)), saved.read_text())
            r.restic(cfg, 'check', '--read-data')
            # A broken/missing keep tag must still fail closed rather than
            # acquire a general permission to delete the whole snapshot group.
            with patch.object(r, 'RETIRED_TAG', 'synthetic-missing-protected-tag'):
                with self.assertRaises(RuntimeError): r.forget_unprotected_fixture_snapshots(cfg, root)
            remaining = json.loads(r.restic(cfg, 'snapshots', '--json'))
            self.assertEqual({row['id'] for row in remaining}, {protected_id})

    def test_all_controls_and_cleanup_are_required_and_publication_is_private_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            for check in r.CHECKS:
                value=report(); value['checks'].remove(check)
                with self.subTest(check=check), self.assertRaises(ValueError): r.publish_pass(root,value,True)
                self.assertFalse((root/'RETIREMENT-PASS.json').exists())
            for cleanup in (False,None,1):
                with self.assertRaises(ValueError): r.publish_pass(root,report(),cleanup)
            r.publish_pass(root,report(),True)
            path=root/'RETIREMENT-PASS.json'; original=path.read_bytes()
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            self.assertEqual(json.loads(original)['export_scope'],'learner-work-directories')
            with self.assertRaises(FileExistsError): r.publish_pass(root,report(),True)
            self.assertEqual(path.read_bytes(),original)
    def test_private_fields_missing_retained_home_and_corrupt_values_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); cases=[]
            for key in ('environment','roster','retention_owner','export_path'):
                value=report(); value[key]='private'; cases.append(value)
            value=report(); value['counts']['retained_homes']=1; cases.append(value)
            value=report(); value['snapshot_id']='latest'; cases.append(value)
            value=report(); value['images']['hub']='hub:latest'; cases.append(value)
            for value in cases:
                with self.assertRaises(ValueError): r.publish_pass(root,value,True)
            self.assertEqual(list(root.iterdir()),[])
    def test_partial_start_and_failed_cleanup_never_publish(self):
        for cleanup_failure in (False,True):
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve(); fixture=Mock(instance='rehearsal-owned')
                fixture.start.side_effect=RuntimeError('partial fixture start')
                cleanup=Mock(side_effect=RuntimeError('cleanup failed')) if cleanup_failure else Mock(return_value=True)
                argv=['verify_retirement.py','--output',str(root/'run')]
                with patch.object(r.sys,'argv',argv),patch.object(r.sys,'platform','linux'),patch.object(r.os,'geteuid',return_value=0),patch.object(r,'_source',return_value={'revision':'a'*40,'dirty':False}),patch.object(r,'Notebook',return_value=fixture),patch.object(r,'cleanup_rehearsal',cleanup),patch.object(r,'publish_pass') as publish:
                    with self.assertRaises(RuntimeError): r.main()
                cleanup.assert_called_once(); publish.assert_not_called()
                self.assertFalse((root/'run/RETIREMENT-PASS.json').exists())
    def test_negative_controls_propagate_unexpected_failures(self):
        r.refused(Mock(side_effect=ValueError('retired')))
        with self.assertRaises(AssertionError): r.refused(Mock())
        with self.assertRaises(OSError): r.refused(Mock(side_effect=OSError('unexpected transport failure')))
    def test_actual_legacy_validator_refuses_intrinsic_v3_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            def restore(cfg,*args):
                target=Path(args[args.index('--target')+1]); (target/'bundle').mkdir()
                (target/'bundle/bundle.json').write_text(json.dumps({'version':3}))
                return ''
            with patch.object(r,'restic',side_effect=restore): r.verify_legacy_refusal({},'a'*64,root)
            self.assertFalse(list(root.glob('.legacy-proof-*')))
    def test_atomic_write_failure_leaves_no_success_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            with patch.object(r.os,'link',side_effect=OSError('synthetic failure')):
                with self.assertRaises(OSError): r.publish_pass(root,report(),True)
            self.assertEqual(list(root.iterdir()),[])
    def test_optimized_python_refuses_without_output_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'run'
            result=subprocess.run([sys.executable,'-O',r.__file__,'--output',str(output)],capture_output=True,text=True,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'),timeout=30)
            self.assertNotEqual(result.returncode,0); self.assertRegex(result.stderr,r'optimized Python|Python assertions')
            self.assertFalse(output.exists())


if __name__=='__main__': unittest.main()
