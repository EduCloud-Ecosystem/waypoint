"""Keep the source-pinned, non-enforcing capacity witnesses reproducible in CI."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class CapacityQualificationTests(unittest.TestCase):
    def test_production_transition_witnesses(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "qualification.json"
            result = subprocess.run([sys.executable, "-W", "error::ResourceWarning",
                                     str(root / "qualification/capacity/qualify.py"), "--output", str(output)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report["tests_run"], 8)
            self.assertTrue(report["tests_passed"])
            self.assertFalse(report["quota_enforced"])
            self.assertEqual(report["go_no_go"]["admission_wiring"], "NO-GO")
