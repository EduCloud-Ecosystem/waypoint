"""Keep the source-pinned, non-enforcing capacity witnesses reproducible in CI."""
import json
import hashlib
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest


class CapacityQualificationTests(unittest.TestCase):
    def test_production_transition_witnesses(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            archive = root / "qualification/capacity/pinned-v1.tar.gz"
            self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(),
                             "4da52b3c1889968d15ac12670a9bc9fe89379f15c9188ee7ef6d43ceed6f6333")
            frozen = Path(directory) / "pinned"
            with tarfile.open(archive) as source:
                source.extractall(frozen, filter="data")
            contract = json.loads((frozen / "research-workspace/qualification/capacity/contract-v1.json").read_text())
            manifest = json.loads((frozen / "research-workspace/evidence/2026-10-17/capacity-contract/manifest.json").read_text())
            for name, expected in {**contract["sources"]["waypoint"]["sha256"], **manifest["sha256"]}.items():
                self.assertEqual(hashlib.sha256((frozen / name).read_bytes()).hexdigest(), expected, name)
            for name in ("contract-v1.json", "qualify.py", "qualify_broker.py"):
                relative = "qualification/capacity/" + name
                self.assertEqual((root / relative).read_bytes(), (frozen / "research-workspace" / relative).read_bytes())
            output = Path(directory) / "qualification.json"
            result = subprocess.run([sys.executable, "-W", "error::ResourceWarning",
                                     str(frozen / "research-workspace/qualification/capacity/qualify.py"), "--output", str(output)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report["tests_run"], 8)
            self.assertTrue(report["tests_passed"])
            self.assertFalse(report["quota_enforced"])
            self.assertEqual(report["go_no_go"]["admission_wiring"], "NO-GO")
