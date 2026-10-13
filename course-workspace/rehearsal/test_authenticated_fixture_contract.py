"""Regression for the observed missing runtime import in the native fixture."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime_authority


class FixtureContract(unittest.TestCase):
    def test_cli_failure_keeps_private_exception_details_out_of_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canary = 'fixture-private-error-canary'
            (root / 'sitecustomize.py').write_text(
                "import subprocess\ndef private_failure(*args, **kwargs):\n"
                "    raise RuntimeError('fixture-private-error-canary')\n"
                "subprocess.run = private_failure\n")
            report = root / 'report.json'
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('verify_authenticated_runtime.py')),
                '--output', str(report)], env=dict(os.environ, PYTHONPATH=str(root)),
                capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(canary, result.stdout + result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            evidence = json.loads(report.read_text())
            self.assertEqual(evidence['status'], 'FAIL')
            self.assertEqual(evidence['failure'], 'RuntimeError')
            self.assertTrue(evidence['cleanup_verified'])

    def test_every_lazy_runtime_import_in_fixture_is_available(self):
        tree = ast.parse(Path(__file__).with_name('authenticated_fixture.py').read_text())
        imported = [alias.name for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == 'runtime_authority' for alias in node.names]
        self.assertTrue(imported)
        for name in imported:
            with self.subTest(name=name): self.assertTrue(hasattr(runtime_authority, name))


if __name__ == '__main__': unittest.main()
