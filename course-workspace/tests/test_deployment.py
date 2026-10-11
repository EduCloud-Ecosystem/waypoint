import hashlib
import os
import struct
from types import SimpleNamespace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deployment import compose, preflight, settings, require_native_worker, validate_institutional, docker_json
from public_course import render


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        native = patch('deployment.require_native_worker')
        self.native = native.start()
        self.addCleanup(native.stop)

    def fixture(self, root):
        _, _, env = render('course.example.org', 'https://id.example.org/realms/course',
                           'course', 'course-one', ['alice-id'], '/srv/homes')
        env.update(WORKSPACE_ENV_FILE=str(root / '.env'), WORKSPACE_OIDC_CLIENT_SECRET='fixture-only')
        (root / '.env').write_text(''.join(f'{k}={v}\n' for k, v in env.items()))
        (root / '.env').chmod(0o600)
        home = 'course-one-home-' + hashlib.sha256(b'alice-id').hexdigest()[:24]
        return {'volumes': {home: {'quota_bytes': 5120 * 1024**2, 'inode_limit': 100000}}}

    def inspect(self, *args):
        if args[0] == 'info':
            return {'OSType': 'linux', 'NCPU': 4, 'MemTotal': 12 * 1024**3, 'Architecture': 'x86_64'}
        image_id = args[2] if len(args) > 2 and args[2].startswith('sha256:') else 'sha256:' + 'a' * 64
        return [{'Os': 'linux', 'Architecture': 'amd64', 'Id': image_id}]

    def test_explicit_hub_pin_is_inspected_and_wrong_image_id_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            homes = self.fixture(root)
            hub_id = 'sha256:' + 'b' * 64
            with (root / '.env').open('a') as out:
                out.write('WORKSPACE_HUB_IMAGE=' + hub_id + '\n')
            with patch('deployment.docker_json', side_effect=self.inspect) as docker, patch('homes.inspect', return_value=homes):
                actual = preflight(root)
            self.assertEqual(actual['images']['hub'], hub_id)
            self.assertIn(('image', 'inspect', hub_id), [call.args for call in docker.call_args_list])
            with patch('deployment.docker_json', side_effect=[self.inspect('info'), self.inspect('image', 'inspect', 'legacy-tag')]), patch('homes.inspect') as inspector:
                with self.assertRaisesRegex(ValueError, 'differs from the configured immutable'):
                    preflight(root)
                inspector.assert_not_called()

    def test_preflight_records_images_but_never_identity_or_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = self.fixture(root)
            with patch('deployment.docker_json', side_effect=self.inspect), patch('homes.inspect', return_value=report):
                actual = preflight(root)
            self.assertEqual(actual['memory_required_mb'], 10240)
            self.assertEqual(actual['approved_home_count'], 1)
            self.assertNotIn('alice-id', str(actual))
            self.assertNotIn('fixture-only', str(actual))
            self.assertEqual(actual['images']['course'], 'sha256:' + 'a' * 64)

    def test_missing_learner_home_or_wrong_quota_refuses_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = self.fixture(root)
            for broken in [{'volumes': {}}, {'volumes': {k: dict(v, inode_limit=999) for k, v in report['volumes'].items()}}]:
                with patch('deployment.docker_json', side_effect=self.inspect), patch('homes.inspect', return_value=broken):
                    with self.assertRaises(ValueError):
                        preflight(root)

    def test_insufficient_memory_or_wrong_architecture_refuses_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            for responses in [
                [dict(self.inspect('info'), MemTotal=4 * 1024**3)],
                [self.inspect('info'), [{'Os': 'linux', 'Architecture': 'arm64', 'Id': 'sha256:'+'a'*64}]],
            ]:
                with patch('deployment.docker_json', side_effect=responses), patch('homes.inspect') as homes:
                    with self.assertRaises(ValueError):
                        preflight(root)
                    homes.assert_not_called()

    def test_pending_maintenance_blocks_even_if_marker_is_dangling_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            marker = root / '.maintenance-pending.json'
            marker.symlink_to(root / 'missing')
            with self.assertRaisesRegex(ValueError, 'maintenance is incomplete'):
                settings(root)

    def test_native_failure_happens_before_daemon_capacity_or_homes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            self.native.side_effect = ValueError('native worker unavailable')
            with patch('deployment.docker_json') as docker, patch('homes.inspect') as homes:
                with self.assertRaisesRegex(ValueError, 'native worker'):
                    preflight(root)
                docker.assert_not_called()
                homes.assert_not_called()

    def test_malformed_daemon_capacity_is_controlled_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            for info in [None, [], {}, dict(self.inspect('info'), NCPU='4'), dict(self.inspect('info'), MemTotal=True)]:
                with patch('deployment.docker_json', return_value=info), patch('homes.inspect') as homes:
                    with self.assertRaises(ValueError):
                        preflight(root)
                    homes.assert_not_called()

    def test_native_endpoint_rejects_remote_context_even_with_local_host_override(self):
        with patch('deployment.sys.platform', 'linux'), patch('deployment.socket.SO_PEERCRED', 17, create=True), patch.dict(os.environ, {'DOCKER_HOST': 'unix:///var/run/docker.sock', 'DOCKER_CONTEXT': 'remote'}), patch('deployment.docker_json', return_value=[{'Endpoints': {'docker': {'Host': 'ssh://remote'}}}]), patch('deployment.socket.socket') as sock:
            with self.assertRaisesRegex(ValueError, 'native /var/run/docker.sock'):
                require_native_worker()
            sock.assert_not_called()

    def test_native_endpoint_rejects_desktop_or_different_filesystem_peer(self):
        for command, peer_inode in [('com.docker.backend', 1), ('dockerd', 2)]:
            sock = MagicMock()
            sock.__enter__.return_value = sock
            sock.getsockopt.return_value = struct.pack('3i', 42, 0, 0)
            with patch('deployment.sys.platform', 'linux'), patch('deployment.socket.SO_PEERCRED', 17, create=True), patch.dict(os.environ, {'DOCKER_HOST': 'unix:///var/run/docker.sock', 'DOCKER_CONTEXT': ''}), patch('deployment.socket.socket', return_value=sock), patch('deployment.Path.read_text', return_value=command), patch('deployment.os.readlink', return_value='mnt:[1]'), patch('deployment.os.stat', side_effect=[SimpleNamespace(st_dev=1, st_ino=1), SimpleNamespace(st_dev=1, st_ino=peer_inode)]):
                with self.assertRaisesRegex(ValueError, 'filesystem identity'):
                    require_native_worker()

    def test_native_same_filesystem_dockerd_and_systemd_are_accepted(self):
        for pid, command in [(42, 'dockerd'), (1, 'systemd')]:
            sock = MagicMock()
            sock.__enter__.return_value = sock
            sock.getsockopt.return_value = struct.pack('3i', pid, 0, 0)
            with patch('deployment.sys.platform', 'linux'), patch('deployment.socket.SO_PEERCRED', 17, create=True), patch.dict(os.environ, {'DOCKER_HOST': '', 'DOCKER_CONTEXT': ''}), patch('deployment.docker_json', return_value=[{'Endpoints': {'docker': {'Host': 'unix:///var/run/docker.sock'}}}]), patch('deployment.socket.socket', return_value=sock), patch('deployment.Path.read_text', return_value=command), patch('deployment.os.readlink', return_value='mnt:[1]'), patch('deployment.os.stat', return_value=SimpleNamespace(st_dev=1, st_ino=1)):
                require_native_worker()
            sock.connect.assert_called_once_with('/var/run/docker.sock')

    def test_invalid_numeric_environment_value_is_not_echoed(self):
        _, _, env = render('course.example.org', 'https://id.example.org/realms/course', 'course', 'course-one', ['alice-id'], '/srv/homes')
        env.update(WORKSPACE_OIDC_CLIENT_SECRET='fixture-only', WORKSPACE_MEMORY_MB='accidentally-pasted-secret')
        with self.assertRaises(ValueError) as error:
            validate_institutional(env)
        self.assertNotIn('accidentally-pasted-secret', str(error.exception))

    def test_docker_diagnostics_never_escape_inspection(self):
        import subprocess
        with patch('deployment.subprocess.run', side_effect=subprocess.CalledProcessError(1, ['docker'], stderr='private-token')):
            with self.assertRaises(ValueError) as error:
                docker_json('info')
        self.assertNotIn('private-token', str(error.exception))

    def test_recovered_image_id_and_retained_removed_learner_home_are_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            homes = self.fixture(root)
            envfile = root / '.env'
            original = envfile.read_text()
            image_line = next(line for line in original.splitlines() if line.startswith('WORKSPACE_IMAGE='))
            envfile.write_text(original.replace(image_line, 'WORKSPACE_IMAGE=sha256:' + 'b' * 64))
            existing = next(iter(homes['volumes'].values()))
            homes['volumes']['course-one-home-' + 'f' * 24] = dict(existing)
            with patch('deployment.docker_json', side_effect=self.inspect) as docker, patch('homes.inspect', return_value=homes):
                report = preflight(root)
            self.assertEqual(report['approved_home_count'], 1)
            self.assertEqual(report['retained_home_count'], 1)
            self.assertIn((('image', 'inspect', 'sha256:' + 'b' * 64),), [(call.args,) for call in docker.call_args_list])

    def test_pending_maintenance_does_not_invoke_docker_or_homes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            (root / '.maintenance-pending.json').write_text('{}')
            with patch('deployment.docker_json') as docker, patch('homes.inspect') as homes:
                with self.assertRaisesRegex(ValueError, 'maintenance is incomplete'):
                    preflight(root)
                docker.assert_not_called()
                homes.assert_not_called()
                self.native.assert_not_called()

    def test_native_peer_same_root_but_different_mount_namespace_is_refused(self):
        sock = MagicMock()
        sock.__enter__.return_value = sock
        sock.getsockopt.return_value = struct.pack('3i', 42, 0, 0)
        with patch('deployment.sys.platform', 'linux'), patch('deployment.socket.SO_PEERCRED', 17, create=True), patch.dict(os.environ, {'DOCKER_HOST': 'unix:///var/run/docker.sock', 'DOCKER_CONTEXT': ''}), patch('deployment.socket.socket', return_value=sock), patch('deployment.Path.read_text', return_value='dockerd'), patch('deployment.os.readlink', side_effect=['mnt:[1]', 'mnt:[2]']), patch('deployment.os.stat', return_value=SimpleNamespace(st_dev=1, st_ino=1)):
            with self.assertRaisesRegex(ValueError, 'filesystem identity'):
                require_native_worker()


class InterruptedOperationTests(unittest.TestCase):
    fixture = DeploymentTests.fixture
    def test_pending_upgrade_blocks_start_but_permits_emergency_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            (root / '.upgrade-pending.json').symlink_to(root / 'absent-journal')
            with patch('deployment.require_native_worker'), patch('deployment.subprocess.run') as run:
                compose(root, 'stop', 'hub')
                run.assert_called_once()
                run.reset_mock()
                with self.assertRaisesRegex(ValueError, 'upgrade is incomplete'):
                    compose(root, 'up', '-d', 'hub')
                run.assert_not_called()
            with patch('deployment.docker_json') as docker, patch('homes.inspect') as homes:
                with self.assertRaisesRegex(ValueError, 'upgrade is incomplete'):
                    preflight(root)
                docker.assert_not_called()
                homes.assert_not_called()

    def test_upgrade_internal_preflight_cannot_bypass_quota_maintenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            (root / '.maintenance-pending.json').write_text('{}')
            with patch('deployment.docker_json') as docker:
                with self.assertRaisesRegex(ValueError, 'maintenance is incomplete'):
                    preflight(root, allow_pending=True)
                docker.assert_not_called()

    def test_pending_maintenance_allows_stop_but_never_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            (root / '.maintenance-pending.json').write_text('{}')
            with patch('deployment.require_native_worker'), patch('deployment.subprocess.run') as run:
                compose(root, 'stop', 'hub')
                self.assertEqual(run.call_args.args[0][-2:], ['stop', 'hub'])
                run.reset_mock()
                with self.assertRaisesRegex(ValueError, 'maintenance is incomplete'):
                    compose(root, 'up', '-d', 'hub')
                run.assert_not_called()
