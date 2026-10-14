"""Descriptor-confined disposal of the stopped synthetic Docker fixture."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
import authenticated_fixture as fixture

NAME = 'wa0123456789'


class Docker:
    def __init__(self, names=()):
        self.containers = self
        self.names = list(names)
        self.calls = []
    def list(self, all, filters):
        self.calls.append(filters)
        values = []
        for name, labels in self.names:
            if 'label' in filters:
                key, value = filters['label'].split('=', 1)
                match = labels.get(key) == value
            else: match = filters['name'] == '^/' + name + '$'
            if match:
                values.append(type('Container', (), {'attrs': {'Name': '/' + name, 'Config': {'Labels': labels}}, 'reload': lambda self: None})())
        return values


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name).resolve()
        self.root = self.parent / 'owned'; self.root.mkdir(mode=0o700)
        fixture.capture_cleanup_owner(self.root, NAME)
    def tree(self):
        directory = self.root / 'course' / 'nested'; directory.mkdir(mode=0o700, parents=True)
        file = directory / 'secret'; file.write_bytes(b'synthetic credential bytes'); file.chmod(0o600)
        return directory, file

    def test_private_modes_literal_symlink_and_exact_mount_exclusions(self):
        directory, file = self.tree()
        outside = self.parent / 'sentinel'; outside.write_bytes(b'outside untouched'); outside.chmod(0o640)
        prior = outside.stat()
        (directory / 'literal').symlink_to(outside)
        for name in ('control', 'backend'):
            mount = self.root / name; mount.mkdir(mode=0o700)
            marker = mount / 'volume-only'; marker.write_bytes(b'not host bind'); marker.chmod(0o640)
        controller = (NAME + '-authority', {'educloud.fixture': NAME})
        foreign = ('another-authority', {'educloud.fixture': 'another'})
        self.assertEqual(fixture.cleanup_permissions(self.root, Docker((controller, foreign))), {'ready': True})
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual((directory / 'literal').readlink(), outside)
        self.assertEqual(outside.read_bytes(), b'outside untouched')
        after = outside.stat()
        self.assertEqual((after.st_uid, after.st_gid, after.st_mode), (prior.st_uid, prior.st_gid, prior.st_mode))
        self.assertEqual(stat.S_IMODE((self.root / 'control/volume-only').stat().st_mode), 0o640)

    def test_active_owned_participants_refuse_before_handoff_and_foreign_ignored(self):
        _, file = self.tree()
        for name, labels in ((NAME + '-source-hub', {'educloud.workspace.instance': NAME + '-source'}),
                             (NAME + '-worker-one-guardian', {'educloud.runtime.kind': 'guardian'}),
                             (NAME + '-stray', {'educloud.fixture': NAME})):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'fixture_cleanup_participant_running'):
                fixture.cleanup_permissions(self.root, Docker(((name, labels),)))
        self.assertEqual(file.read_bytes(), b'synthetic credential bytes')
        self.assertEqual(fixture.cleanup_permissions(self.root, Docker((('foreign', {'educloud.fixture': 'foreign'}),))), {'ready': True})

    def test_root_binding_missing_manifest_creator_promotion_and_fresh_capture_refuse(self):
        record = self.root / 'fixture-owner.json'; initial = record.read_bytes()
        with self.assertRaises(FileExistsError): fixture.capture_cleanup_owner(self.root, NAME)
        self.assertEqual(record.read_bytes(), initial)
        value = json.loads(initial); value['creator_uid'] = (value['creator_uid'] + 1) % 65536
        record.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'fixture_cleanup_binding_changed'): fixture.cleanup_permissions(self.root, Docker())
        record.write_bytes(initial)
        old = self.parent / 'original'; self.root.rename(old); self.root.mkdir(mode=0o700)
        shutil.copyfile(old / 'fixture-owner.json', record); record.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'fixture_cleanup_binding_changed'): fixture.cleanup_permissions(self.root, Docker())
        record.unlink()
        with self.assertRaises(FileNotFoundError): fixture.cleanup_permissions(self.root, Docker())

    def test_directory_swap_to_outside_symlink_never_follows(self):
        course = self.root / 'course'; course.mkdir(mode=0o700)
        outside = self.parent / 'outside'; outside.mkdir(mode=0o755)
        sentinel = outside / 'sentinel'; sentinel.write_bytes(b'never touch'); sentinel.chmod(0o640)
        prior = sentinel.stat(); real = os.open; swapped = []
        def opened(name, flags, *args, **kwargs):
            if name == 'course' and kwargs.get('dir_fd') is not None and not swapped:
                course.rename(self.root / 'retained'); course.symlink_to(outside, target_is_directory=True); swapped.append(True)
            return real(name, flags, *args, **kwargs)
        with patch.object(fixture.os, 'open', side_effect=opened):
            with self.assertRaises(OSError): fixture.cleanup_permissions(self.root, Docker())
        self.assertTrue(swapped)
        self.assertEqual(sentinel.read_bytes(), b'never touch')
        after = sentinel.stat()
        self.assertEqual((after.st_uid, after.st_gid, after.st_mode), (prior.st_uid, prior.st_gid, prior.st_mode))

    def test_hardlink_and_unexpected_mount_refuse_without_foreign_change(self):
        outside = self.parent / 'sentinel'; outside.write_bytes(b'outside'); outside.chmod(0o640)
        os.link(outside, self.root / 'alias')
        with self.assertRaisesRegex(ValueError, 'fixture_cleanup_hardlink_invalid'): fixture.cleanup_permissions(self.root, Docker())
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o640)
        (self.root / 'alias').unlink()
        child = self.root / 'unexpected'; child.mkdir(mode=0o700); real = os.stat
        def inspected(name, *args, **kwargs):
            info = real(name, *args, **kwargs)
            if name == 'unexpected' and kwargs.get('dir_fd') is not None:
                values = list(info); values[2] += 1; return os.stat_result(values)
            return info
        with patch.object(fixture.os, 'stat', side_effect=inspected):
            with self.assertRaisesRegex(ValueError, 'fixture_cleanup_mount_escape'): fixture.cleanup_permissions(self.root, Docker())

    @unittest.skipUnless(sys.platform.startswith('linux'), 'actual Linux /proc identity required')
    def test_live_and_replaced_service_identity_refuse(self):
        record = self.root / 'service-pid.json'
        fixture.write(record, {'pid': os.getpid(), 'start_identity': fixture.process_identity(os.getpid())})
        with self.assertRaisesRegex(ValueError, 'fixture_cleanup_service_running'): fixture.cleanup_permissions(self.root, Docker())
        fixture.write(record, {'pid': os.getpid(), 'start_identity': 'f' * 64})
        with self.assertRaisesRegex(ValueError, 'fixture_cleanup_service_replaced'): fixture.cleanup_permissions(self.root, Docker())


    @unittest.skipUnless(sys.platform.startswith('linux'), 'actual Linux /proc identity required')
    def test_stop_service_waits_for_real_exit_and_refuses_reused_or_uncooperative_process(self):
        for ignore in (False, True):
            code = "import signal,time; " + ("signal.signal(signal.SIGTERM,signal.SIG_IGN); " if ignore else "") + "print('ready',flush=True); time.sleep(30)"
            child = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), 'ready')
                fixture.write(self.root / 'service-pid.json', {'pid': child.pid, 'start_identity': 'f' * 64})
                with patch.object(fixture.signal, 'pidfd_send_signal') as kill:
                    with self.assertRaisesRegex(ValueError, 'fixture_service_replaced'): fixture.stop_service(self.root)
                    kill.assert_not_called()
                fixture.write(self.root / 'service-pid.json', {'pid': child.pid, 'start_identity': fixture.process_identity(child.pid)})
                with patch.object(fixture, 'service_state', side_effect=['R', ValueError('fixture_service_replaced')]), patch.object(fixture.signal, 'pidfd_send_signal') as send:
                    with self.assertRaisesRegex(ValueError, 'fixture_service_replaced'): fixture.stop_service(self.root)
                    send.assert_not_called()
                self.assertIsNone(child.poll())
                if ignore:
                    with self.assertRaisesRegex(ValueError, 'fixture_service_stop_timeout'): fixture.stop_service(self.root, wait_seconds=.1)
                    self.assertIsNone(child.poll())
                else:
                    with patch.object(fixture.signal, 'pidfd_send_signal', wraps=fixture.signal.pidfd_send_signal) as send, patch.object(fixture.os, 'kill') as numeric:
                        self.assertEqual(fixture.stop_service(self.root), {'stopped': True})
                        self.assertEqual(send.call_count, 1); numeric.assert_not_called()
                    self.assertIsNotNone(child.poll())
                    self.assertEqual(fixture.cleanup_permissions(self.root, Docker()), {'ready': True})
            finally:
                if child.poll() is None: child.kill()
                child.wait(timeout=3); child.stdout.close()

    @unittest.skipUnless(sys.platform.startswith('linux'), 'actual Linux /proc identity required')
    def test_missing_pid_record_with_service_command_refuses_cleanup_and_stop(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)',
            'authenticated_fixture.py', 'serve', str(self.root)])
        try:
            # Exec'd process command must be visible before attempting cleanup.
            deadline = time.monotonic() + 2
            while not (Path('/proc') / str(child.pid) / 'cmdline').read_bytes() and time.monotonic() < deadline: time.sleep(.01)
            with self.assertRaisesRegex(ValueError, 'fixture_cleanup_service_running'): fixture.cleanup_permissions(self.root, Docker())
            with self.assertRaisesRegex(ValueError, 'fixture_cleanup_service_running'): fixture.stop_service(self.root)
        finally: child.kill(); child.wait(timeout=3)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'actual Linux holder identity required')
    def test_early_failure_stops_exact_holder_before_private_tree_handoff(self):
        control = self.root / 'control'; control.mkdir(mode=0o700)
        marker = self.root / 'late-writer'
        code = ('import pathlib,sys,time; print("ready",flush=True); time.sleep(1); '
                'pathlib.Path(sys.argv[4]).write_text("unexpected late write")')
        child = subprocess.Popen([sys.executable, '-c', code, 'authenticated_fixture.py',
            'enrollment-contention-hold', str(self.root), str(marker)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            fixture.write(control / 'enrollment-holder-course-main.json',
                {'pid': child.pid, 'start_identity': fixture.process_identity(child.pid)})
            with self.assertRaisesRegex(ValueError, 'fixture_cleanup_service_running'):
                fixture.cleanup_permissions(self.root, Docker())
            self.assertEqual(fixture.stop_service(self.root), {'stopped': True})
            child.wait(timeout=3)
            self.assertFalse(marker.exists())
            self.assertEqual(fixture.cleanup_permissions(self.root, Docker()), {'ready': True})
        finally:
            if child.poll() is None: child.kill()
            child.wait(timeout=3); child.stdout.close()

    @unittest.skipUnless(sys.platform.startswith('linux'), 'actual Linux holder identity required')
    def test_missing_or_replaced_holder_identity_refuses_handoff_without_numeric_signal(self):
        control = self.root / 'control'; control.mkdir(mode=0o700)
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)',
            'authenticated_fixture.py', 'enrollment-contention-hold', str(self.root)])
        try:
            deadline = time.monotonic() + 2
            while not (Path('/proc') / str(child.pid) / 'cmdline').read_bytes() and time.monotonic() < deadline: time.sleep(.01)
            with self.assertRaisesRegex(ValueError, 'fixture_cleanup_service_running'):
                fixture.cleanup_permissions(self.root, Docker())
            with self.assertRaisesRegex(ValueError, 'fixture_cleanup_service_running'):
                fixture.stop_service(self.root)
            fixture.write(control / 'enrollment-holder-course-peer.json',
                {'pid': child.pid, 'start_identity': 'f' * 64})
            with patch.object(fixture.signal, 'pidfd_send_signal') as send, patch.object(fixture.os, 'kill') as numeric:
                with self.assertRaisesRegex(ValueError, 'fixture_service_replaced'): fixture.stop_service(self.root)
                send.assert_not_called(); numeric.assert_not_called()
            self.assertIsNone(child.poll())
        finally: child.kill(); child.wait(timeout=3)
    @unittest.skipUnless(sys.platform.startswith('linux') and os.geteuid() == 0, 'root Linux fixture run required')
    def test_real_root_generated_tree_becomes_removable_by_unprivileged_creator(self):
        # Dedicated tmpfs during Docker verification; no host homes or Docker socket.
        self.root.joinpath('fixture-owner.json').unlink()
        uid, gid = 12345, 12345
        os.chown(self.parent, uid, gid); os.chmod(self.parent, 0o700); os.chown(self.root, uid, gid)
        fixture.capture_cleanup_owner(self.root, NAME)
        directory, file = self.tree()  # Root-generated0700/0600 bytes inside creator root.
        outside = self.parent / 'outside'; outside.write_bytes(b'outside retained'); outside.chmod(0o600)
        prior = outside.stat(); (directory / 'literal').symlink_to(outside)
        script = 'import shutil,sys; shutil.rmtree(sys.argv[1])'
        def unprivileged(): os.setgroups([]); os.setgid(gid); os.setuid(uid)
        failed = subprocess.run([sys.executable, '-c', script, str(self.root / 'course')], preexec_fn=unprivileged, capture_output=True, timeout=5)
        self.assertNotEqual(failed.returncode, 0)
        fixture.cleanup_permissions(self.root, Docker())
        self.assertEqual((directory.stat().st_uid, file.stat().st_uid), (uid, uid))
        removed = subprocess.run([sys.executable, '-c', script, str(self.root)], preexec_fn=unprivileged, capture_output=True, timeout=5)
        self.assertEqual(removed.returncode, 0)
        self.assertFalse(self.root.exists())
        after = outside.stat()
        self.assertEqual((after.st_uid, after.st_gid, after.st_mode), (prior.st_uid, prior.st_gid, prior.st_mode))
        self.assertEqual(outside.read_bytes(), b'outside retained')


if __name__ == '__main__': unittest.main()
