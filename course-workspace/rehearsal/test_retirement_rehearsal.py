"""Retirement acceptance cannot hide incomplete closure, export, or cleanup controls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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
