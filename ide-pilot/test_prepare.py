# SPDX-License-Identifier: Apache-2.0
"""Preparation checks use a new synthetic directory for each run."""
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


class PreparationTests(unittest.TestCase):
    def test_private_credentials_and_non_destructive_preparation(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "workspace"
            command = [sys.executable, str(Path(__file__).with_name("prepare.py")), str(output)]
            subprocess.run(command, check=True, capture_output=True)
            credentials = output / "server.env"
            self.assertEqual(stat.S_IMODE(credentials.stat().st_mode), 0o600)
            before = credentials.read_bytes()
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual(credentials.read_bytes(), before)
            self.assertEqual(
                subprocess.check_output(["git", "status", "--porcelain"], cwd=output / "project"), b""
            )
            result = subprocess.run(
                [sys.executable, "-m", "unittest", "-v"], cwd=output / "project", capture_output=True
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn(b"10.0 != 15", result.stderr)


if __name__ == "__main__":
    unittest.main()
