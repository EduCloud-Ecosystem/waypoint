import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from research_workspace.__main__ import read_bundle
from research_workspace.recovery import read_file


class OperatorFileTests(unittest.TestCase):
    def test_bundle_commands_refuse_fifo_without_waiting_for_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "evidence.zip"
            os.mkfifo(fifo)
            for command in (["verify-bundle", str(fifo)], ["plan-reproduction", str(fifo), "--trusted-sha256", "0" * 64]):
                result = subprocess.run([sys.executable, "-m", "research_workspace", *command], capture_output=True, timeout=3)
                self.assertEqual(result.returncode, 1)
                self.assertIn(b"regular evidence file", result.stderr)

    def test_private_reader_refuses_special_files_and_hardlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "manifest.json"
            os.mkfifo(fifo)
            with self.assertRaises(ValueError):
                read_file(fifo)
            normal = Path(directory) / "normal"
            normal.write_bytes(b"123")
            os.link(normal, Path(directory) / "alias")
            with self.assertRaises(ValueError):
                read_file(normal)
            with self.assertRaises(ValueError):
                read_bundle(Path(directory))


if __name__ == "__main__":
    unittest.main()
