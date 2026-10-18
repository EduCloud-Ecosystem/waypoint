"""Run pinned offline writer witnesses without providers or accepted state."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class SQLiteGuardQualificationTests(unittest.TestCase):
    def test_source_pinned_protocol_witnesses(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            result = subprocess.run([sys.executable, "-W", "error::ResourceWarning",
                                     str(root / "qualification/sqlite-guard/qualify.py"),
                                     "--output", str(output)], capture_output=True, text=True,
                                    cwd=root, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(output.read_bytes())
            self.assertEqual(report["tests_run"], 9)
            self.assertTrue(report["tests_passed"])
            self.assertEqual(len(report["observations"]), 9)
            self.assertFalse(report["quota_enforced"])
            self.assertEqual(report["available_capacity"], "UNKNOWN")
            self.assertEqual(report["quota_admission"], "NO-GO")
            self.assertEqual(report["fifth_headroom_failure"], "FAIL preserved")
