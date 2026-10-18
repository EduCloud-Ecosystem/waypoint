"""Run pinned offline writer witnesses without providers or accepted state."""
import json
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import tarfile
import unittest


class SQLiteGuardQualificationTests(unittest.TestCase):
    def test_source_pinned_protocol_witnesses(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            archive = root / "qualification/sqlite-guard/pinned-v1.tar.gz"
            self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(),
                             "6bf625ca8c826e85d2cb80893e1e61fd673e52fff6b33a9bf1439c8e7977efdf")
            frozen = Path(directory) / "pinned"
            with tarfile.open(archive) as source:
                source.extractall(frozen, filter="data")
            contract = json.loads((frozen / "research-workspace/qualification/sqlite-guard/contract-v1.json").read_bytes())
            manifest = json.loads((frozen / "research-workspace/evidence/2026-10-18/sqlite-guard/manifest.json").read_bytes())
            for name, expected in {**contract["source_sha256"], **manifest["sha256"]}.items():
                self.assertEqual(hashlib.sha256((frozen / name).read_bytes()).hexdigest(), expected, name)
            for name in ("contract-v1.json", "qualify.py"):
                relative = "qualification/sqlite-guard/" + name
                self.assertEqual((root / relative).read_bytes(), (frozen / "research-workspace" / relative).read_bytes())
            output = Path(directory) / "report.json"
            result = subprocess.run([sys.executable, "-W", "error::ResourceWarning",
                                     str(frozen / "research-workspace/qualification/sqlite-guard/qualify.py"),
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
