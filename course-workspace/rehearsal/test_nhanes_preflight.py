"""Invalid identity images must retain failure evidence before resources start."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import nhanes_desktop as journey


class IdentityImagePreflight(unittest.TestCase):
    def test_invalid_image_retains_failure_without_starting_resources(self):
        image = 'sha256:' + '1' * 64

        def command(argv, cwd=None):
            if argv[:2] == ['git', 'status']:
                return ''
            if argv[:2] == ['git', 'rev-parse']:
                return journey.PINS.get(Path(cwd).name, 'synthetic-waypoint')
            if argv[:3] == ['docker', 'image', 'inspect']:
                return 'invalid-image-id' if argv[3] == journey.FORGEJO_IMAGE else image
            raise AssertionError('unexpected work after preflight')

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'result'
            argv = ['nhanes_desktop', '--cairn', '/cairn', '--belay', '/belay',
                    '--kernel-python', '/unused/python', '--kernel-lock', '/unused/lock',
                    '--output', str(root), '--grader-image', image,
                    '--checkout-image', image, '--execute-synthetic']
            previous_umask = os.umask(0o077)
            try:
                with patch('sys.argv', argv), patch.object(journey, 'command', side_effect=command), \
                        patch.object(journey, 'Course') as course, \
                        patch.object(journey.subprocess, 'Popen') as process, \
                        patch.object(journey.signal, 'signal'), patch.object(journey.signal, 'alarm'), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(journey.main(), 1)
                course.assert_not_called()
                process.assert_not_called()
            finally:
                os.umask(previous_umask)
            result = json.loads((root / 'result.json').read_text())
            self.assertEqual(result['failed'], [
                'ValueError: pinned identity fixture has no valid local image ID'])
            self.assertTrue(result['cleanup'])
            self.assertEqual(result['model_transports'], 0)
            self.assertIn('ValueError', (root / 'failure.txt').read_text())


if __name__ == '__main__':
    unittest.main()
