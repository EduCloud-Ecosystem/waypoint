import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import pilot_oidc as pilot


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()

    def test_stopped_or_started_pilot_cannot_restart_without_new_epoch(self):
        for marker in ('started.json', 'stopped.json'):
            with self.subTest(marker=marker):
                path = self.root / marker; path.write_text('{}')
                with patch.object(pilot, 'compose') as compose:
                    with self.assertRaises(ValueError): pilot.start(self.root, {'deadline': 999999999999})
                    compose.assert_not_called()
                path.unlink()

    def test_start_crossing_deadline_stops_owned_stack(self):
        watcher = Mock(); watcher.poll.return_value = None
        with patch.object(pilot.time, 'time', side_effect=[100, 100, 201, 201]), patch.object(pilot.subprocess, 'Popen', return_value=watcher), patch.object(pilot, 'compose') as compose:
            with self.assertRaises(ValueError): pilot.start(self.root, {'deadline': 200})
        self.assertEqual([c.args[1:] for c in compose.call_args_list], [('up', '-d', '--build'), ('down', '--volumes')])
        self.assertTrue((self.root / 'stopped.json').exists())

    def test_lost_watchdog_stops_owned_stack(self):
        watcher = Mock(); watcher.poll.return_value = 1
        with patch.object(pilot.time, 'time', return_value=100), patch.object(pilot.subprocess, 'Popen', return_value=watcher), patch.object(pilot, 'compose') as compose:
            with self.assertRaises(ValueError): pilot.start(self.root, {'deadline': 200})
        self.assertEqual(compose.call_args.args[1:], ('down', '--volumes'))

    def test_repeated_stop_preserves_original_observation(self):
        with patch.object(pilot, 'compose') as compose:
            pilot.stop(self.root)
            before=(self.root/'stopped.json').read_bytes()
            pilot.stop(self.root)
        self.assertEqual((self.root/'stopped.json').read_bytes(),before)
        self.assertEqual(compose.call_count,2)
