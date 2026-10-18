import json
import io
from pathlib import Path
import tempfile
import unittest
import threading
from unittest.mock import Mock, patch
import pilot_oidc as pilot
import verify_oidc as verify


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


class ObserverCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.rid = 'a' * 32
        state = self.root / 'state'; (state / 'broker').mkdir(parents=True)
        (state / 'broker/ledger.json').write_text(json.dumps({'owner':'owned'}))
        run = state / 'runs' / self.rid; run.mkdir(parents=True)
        (run / 'request.private.json').write_text(json.dumps({'project_id':'project'}))
        self.subject = object.__new__(verify.Pilot)
        self.subject.root = self.root; self.subject.state_name = 'state'
        self.subject.operator = {'projects':['project'], 'users':{'editor':{'sub':'editor'}}}
        self.subject.login = Mock(); self.subject.check = Mock()
        record = {'Actor':{'ID':'exact-owned-container','Attributes':{
            'org.educloud.outfitter.owner':'owned', 'org.educloud.outfitter.request':self.rid}}}
        self.event = Mock(); self.event.stdout = io.StringIO(json.dumps(record)+'\n')
        self.inspection = json.dumps([{'Config':{'Labels':{'org.educloud.outfitter.owner':'owned'}},
                                      'State':{'Running':True,'Paused':True}}])
    def tearDown(self): self.tmp.cleanup()

    def test_teardown_error_cannot_skip_unpause(self):
        paused = threading.Event(); calls = []
        def run(args, **kwargs):
            calls.append(args)
            if args[1] == 'pause': paused.set()
            return Mock(returncode=0, stdout=self.inspection)
        def submit(*args):
            self.assertTrue(paused.wait(2))
            return {'body':{'id':self.rid}}
        self.subject.api = submit
        self.subject.private = Mock(side_effect=RuntimeError('injected provider control failure'))
        self.event.terminate.side_effect = RuntimeError('injected observer teardown failure')
        with patch.object(verify.subprocess, 'Popen', return_value=self.event), patch.object(verify.subprocess, 'run', side_effect=run):
            with self.assertRaisesRegex(RuntimeError, 'observer teardown'):
                self.subject.active_compute_revocation()
        self.assertIn(['docker','unpause','exact-owned-container'], calls)
        self.assertFalse((self.root/'state/hold-cancel').exists())

    def test_inflight_inspection_cannot_pause_after_cleanup_starts(self):
        inspecting = threading.Event(); release = threading.Event(); calls = []
        def run(args, **kwargs):
            calls.append(args)
            inspecting.set(); self.assertTrue(release.wait(2))
            return Mock(returncode=0, stdout=self.inspection)
        def submit(*args):
            self.assertTrue(inspecting.wait(2))
            raise RuntimeError('injected submit failure')
        self.subject.api = submit
        self.event.terminate.side_effect = release.set
        with patch.object(verify.subprocess, 'Popen', return_value=self.event), patch.object(verify.subprocess, 'run', side_effect=run):
            with self.assertRaisesRegex(RuntimeError, 'submit failure'):
                self.subject.active_compute_revocation()
        self.assertFalse(any(args[1] == 'pause' for args in calls))
        self.assertFalse((self.root/'state/hold-cancel').exists())
