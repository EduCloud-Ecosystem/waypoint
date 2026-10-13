import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ControllerCLI(unittest.TestCase):
    def test_missing_private_configuration_exits_without_path_or_traceback_or_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'controller_service.py'),
                '--config', str(root / 'synthetic-private-config'), '--service-config', str(root / 'synthetic-private-service')],
                capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '')
            self.assertEqual(result.stderr.strip(), 'authority_service_failed')
            self.assertEqual(list(root.iterdir()), [])


if __name__ == '__main__': unittest.main()
