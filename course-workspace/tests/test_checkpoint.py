import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, call

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from checkpoint import checkpoint


class CheckpointTests(unittest.TestCase):
    env = {'WORKSPACE_INSTANCE': 'trial-test', 'WORKSPACE_IMAGE': 'course:pinned'}

    def inspect(self, running=True, label='trial-test'):
        return [json.dumps([{'Config': {'Labels': {'educloud.workspace.instance': label}},
                             'State': {'Running': running}, 'Image': 'sha256:hub'}]),
                json.dumps([{'Id': 'sha256:course', 'Architecture': 'amd64'}])]

    def test_failed_backup_resumes_previously_running_hub(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            with patch('checkpoint.docker', side_effect=self.inspect()), \
                    patch('checkpoint.compose') as compose, \
                    patch('checkpoint.wait_ready') as ready, \
                    patch('checkpoint.backup', side_effect=ValueError('live writer')):
                with self.assertRaisesRegex(ValueError, 'live writer'):
                    checkpoint(path, self.env, path / 'archive')
            self.assertEqual(compose.call_args_list,
                             [call(path, 'stop', 'hub'), call(path, 'start', 'hub')])
            ready.assert_called_once_with(self.env)

    def test_stopped_hub_stays_stopped_on_failure(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            with patch('checkpoint.docker', side_effect=self.inspect(running=False)), \
                    patch('checkpoint.compose') as compose, \
                    patch('checkpoint.backup', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    checkpoint(path, self.env, path / 'archive')
            compose.assert_not_called()

    def test_existing_archive_refused_before_downtime(self):
        with tempfile.TemporaryDirectory() as root:
            with patch('checkpoint.compose') as compose, patch('checkpoint.docker') as docker:
                with self.assertRaises(ValueError):
                    checkpoint(Path(root), self.env, Path(root))
            compose.assert_not_called()
            docker.assert_not_called()

    def test_unrelated_hub_is_never_stopped(self):
        with tempfile.TemporaryDirectory() as root:
            with patch('checkpoint.docker', side_effect=self.inspect(label='other')), \
                    patch('checkpoint.compose') as compose:
                with self.assertRaisesRegex(ValueError, 'not owned'):
                    checkpoint(Path(root), self.env, Path(root) / 'archive')
            compose.assert_not_called()


if __name__ == '__main__':
    unittest.main()
