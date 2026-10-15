from copy import deepcopy
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pilot_host


class PilotHostTests(unittest.TestCase):
    def setUp(self):
        self.real_no_volumes = pilot_host.no_volumes
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.info = {'OSType': 'linux', 'Architecture': 'x86_64', 'NCPU': 4,
                     'MemTotal': 8 * 1024**3, 'DockerRootDir': '/var/lib/docker', 'Containers': 0}
        self.release = 'a' * 40
        for name, value in [
                ('require_native_worker', None), ('is_administrator', True), ('docker_json', self.info),
                ('no_volumes', True), ('shutil.disk_usage', SimpleNamespace(free=30 * 1024**3)),
                ('platform.freedesktop_os_release', {'ID': 'ubuntu', 'VERSION_ID': '24.04'})]:
            mock = patch.object(pilot_host, name, return_value=value) if '.' not in name else patch(
                'pilot_host.' + name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        mock = patch.object(pilot_host, 'git_output', side_effect=lambda *args:
                            self.release if args[0] == 'rev-parse' else '')
        mock.start()
        self.addCleanup(mock.stop)
        # The same administrative identity must own the staging directory.
        owner = self.directory.stat().st_uid
        identity = patch('pilot_host.os.geteuid', return_value=owner)
        identity.start()
        self.addCleanup(identity.stop)

    def report(self):
        return pilot_host.inspect_host(self.directory, self.release)

    def test_ready_is_only_prerequisites_and_has_no_private_observations(self):
        report = self.report()
        self.assertEqual(report['status'], 'READY_FOR_SMOKE')
        for key in ('hosted_acceptance', 'independent_host_recovery', 'production_ready'):
            self.assertFalse(report[key])
        self.assertNotIn('/var/lib/docker', str(report))
        self.assertNotIn(self.temp.name, str(report))

    def test_remote_daemon_refuses_without_inventory(self):
        with patch.object(pilot_host, 'require_native_worker', side_effect=ValueError('private endpoint')), \
                patch.object(pilot_host, 'docker_json') as info, \
                patch.object(pilot_host, 'no_volumes') as volumes:
            report = self.report()
        self.assertEqual(report['status'], 'REFUSED')
        info.assert_not_called()
        volumes.assert_not_called()
        self.assertNotIn('private endpoint', str(report))

    def test_small_or_malformed_host_refuses(self):
        for key, value in [('NCPU', 2), ('NCPU', True), ('MemTotal', 4 * 1024**3),
                           ('MemTotal', '8589934592'), ('Architecture', 'aarch64'),
                           ('Containers', 1), ('Containers', False), ('DockerRootDir', 'relative')]:
            with self.subTest(key=key, value=value):
                info = deepcopy(self.info)
                info[key] = value
                with patch.object(pilot_host, 'docker_json', return_value=info):
                    self.assertEqual(self.report()['status'], 'REFUSED')

    def test_existing_volumes_require_operator_review_not_cleanup(self):
        with patch.object(pilot_host, 'no_volumes', return_value=False):
            self.assertFalse(self.report()['checks']['empty_volume_inventory'])

    def test_unknown_inventory_refuses_without_raw_diagnostics(self):
        with patch.object(pilot_host, 'docker_json', side_effect=ValueError('SECRET')):
            report = self.report()
        self.assertEqual(report['status'], 'REFUSED')
        self.assertNotIn('SECRET', str(report))

    def test_both_filesystems_must_have_room(self):
        for values in ((30, 10), (10, 30)):
            with patch('pilot_host.shutil.disk_usage', side_effect=[
                    SimpleNamespace(free=value * 1024**3) for value in values]):
                self.assertEqual(self.report()['status'], 'REFUSED')

    def test_dirty_or_wrong_source_refuses(self):
        for results in [(self.release, ' M private.json'), ('b' * 40,)]:
            with patch.object(pilot_host, 'git_output', side_effect=results):
                self.assertFalse(self.report()['checks']['clean_pinned_source'])
        self.release = 'main'
        self.assertFalse(self.report()['checks']['clean_pinned_source'])

    def test_unsupported_os_refuses(self):
        with patch('pilot_host.platform.freedesktop_os_release', return_value={'ID': 'other'}):
            self.assertEqual(self.report()['status'], 'REFUSED')

    def test_no_volumes_checks_exit_status_and_timeout(self):
        with patch.object(pilot_host.subprocess, 'run', return_value=SimpleNamespace(stdout='')) as run:
            self.assertTrue(self.real_no_volumes())
            self.assertTrue(run.call_args.kwargs['check'])
            self.assertEqual(run.call_args.kwargs['timeout'], 20)
        with patch.object(pilot_host.subprocess, 'run', return_value=SimpleNamespace(stdout='retained\n')):
            self.assertFalse(self.real_no_volumes())

    def test_shared_staging_directory_refuses(self):
        self.directory.chmod(0o755)
        self.assertFalse(self.report()['checks']['private_staging_directory'])

    def test_symlink_and_wrong_owner_staging_refuse(self):
        link = self.directory / 'linked'
        target = self.directory / 'target'
        target.mkdir(mode=0o700)
        link.symlink_to(target)
        self.assertFalse(pilot_host.inspect_host(link, self.release)['checks']['private_staging_directory'])
        with patch('pilot_host.os.geteuid', return_value=self.directory.stat().st_uid + 1):
            self.assertFalse(self.report()['checks']['private_staging_directory'])

    def test_unprivileged_inspection_is_not_ready(self):
        with patch.object(pilot_host, 'is_administrator', return_value=False):
            self.assertEqual(self.report()['status'], 'REFUSED')


if __name__ == '__main__':
    unittest.main()
