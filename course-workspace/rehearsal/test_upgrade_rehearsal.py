"""Upgrade acceptance is withheld for incomplete controls, cleanup, or source checks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import verify_upgrade as rehearsal


def report():
    return {'waypoint_revision':'a'*40, 'canary_sha256':'b'*64,
            'upgrade_fixture_sha256':'c'*64, 'source_manifest_sha256':'d'*64,
            'old_images':{'hub':'sha256:'+'a'*64, 'course':'sha256:'+'b'*64},
            'new_images':{'hub':'sha256:'+'c'*64, 'course':'sha256:'+'d'*64},
            'snapshot_id':'e'*64, 'checks':sorted(rehearsal.CHECKS)}


class UpgradeEvidenceTests(unittest.TestCase):
    def test_publication_requires_all_controls_and_verified_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for check in rehearsal.CHECKS:
                value = report(); value['checks'].remove(check)
                with self.subTest(check=check), self.assertRaises(ValueError):
                    rehearsal.publish_pass(root, value, True)
                self.assertFalse((root/'UPGRADE-PASS.json').exists())
            for cleanup in (False, None, 1):
                with self.assertRaises(ValueError): rehearsal.publish_pass(root, report(), cleanup)
                self.assertFalse((root/'UPGRADE-PASS.json').exists())
            rehearsal.publish_pass(root, report(), True)
            path = root/'UPGRADE-PASS.json'
            saved = json.loads(path.read_text())
            self.assertEqual(saved['scope'], 'same-worker-synthetic-image-upgrade')
            self.assertEqual(saved['status'], 'PASS')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError): rehearsal.publish_pass(root, report(), True)
            self.assertEqual(list(root.iterdir()), [path])

    def test_private_fields_wrong_images_and_unknown_controls_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            cases = []
            for field in ('environment', 'canary_path', 'logs', 'password'):
                value = report(); value[field] = 'private'; cases.append(value)
            value = report(); value['old_images'] = dict(value['new_images']); cases.append(value)
            value = report(); value['new_images']['hub'] = 'hub:latest'; cases.append(value)
            value = report(); value['checks'].append('unverified-extra'); cases.append(value)
            value = report(); value['checks'].append(value['checks'][0]); cases.append(value)
            for value in cases:
                with self.assertRaises(ValueError): rehearsal.publish_pass(root, value, True)
                self.assertEqual(list(root.iterdir()), [])

    def test_only_expected_upgrade_refusals_satisfy_negative_controls(self):
        rehearsal.refused(Mock(side_effect=rehearsal.upgrade.UpgradeError('stale_upgrade_plan')))
        with self.assertRaises(AssertionError): rehearsal.refused(Mock())
        with self.assertRaisesRegex(RuntimeError, 'unexpected transport failure'):
            rehearsal.refused(Mock(side_effect=RuntimeError('unexpected transport failure')))

    def test_atomic_publication_failure_leaves_no_success_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch('verify_upgrade.os.link', side_effect=OSError('synthetic failure')):
                with self.assertRaises(OSError): rehearsal.publish_pass(root, report(), True)
            self.assertEqual(list(root.iterdir()), [])

    def test_partial_start_or_cleanup_failure_never_publishes(self):
        for cleanup_failure in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve(); canary = root/'canary.json'; canary.write_text('{}')
                source = {'revision':'a'*40, 'dirty':False}
                fixture = Mock(instance='rehearsal-fixture')
                fixture.start.side_effect = RuntimeError('partial fixture start')
                argv = ['verify_upgrade.py','--canary',str(canary),'--output',str(root/'run')]
                value = {'provenance':{'image_ids':report()['new_images'], 'waypoint':source}}
                cleanup = Mock(side_effect=RuntimeError('cleanup failed')) if cleanup_failure else Mock(return_value=True)
                with patch.object(rehearsal.sys,'argv',argv), patch.object(rehearsal.sys,'platform','linux'), patch.object(rehearsal.os,'geteuid',return_value=0), patch.object(rehearsal.upgrade,'read_json',return_value=(value,b'')), patch.object(rehearsal.upgrade,'canary',return_value='b'*64), patch.object(rehearsal,'_source',return_value=source), patch.object(rehearsal,'Notebook',return_value=fixture), patch.object(rehearsal,'cleanup_rehearsal',cleanup), patch.object(rehearsal,'publish_pass') as publish:
                    with self.assertRaises(RuntimeError): rehearsal.main()
                cleanup.assert_called_once()
                publish.assert_not_called()
                self.assertFalse((root/'run/UPGRADE-PASS.json').exists())

    def test_optimized_python_refuses_before_output_or_canary_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for script, arguments in [('verify_course.py',['--cairn-binary','/unused','--cairn-source','/unused','--lti-binary','/unused']),
                                      ('verify_upgrade.py',['--canary','/missing-canary'])]:
                output = root/script
                command = [sys.executable,'-O',str(Path(rehearsal.__file__).parent/script),*arguments,'--output',str(output)]
                result = subprocess.run(command, capture_output=True, text=True, env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'), timeout=30)
                self.assertNotEqual(result.returncode,0)
                self.assertRegex(result.stderr, r'optimized Python|Python assertions')
                self.assertFalse(output.exists())


if __name__ == '__main__': unittest.main()
