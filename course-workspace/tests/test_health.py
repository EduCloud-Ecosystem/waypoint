import copy
from datetime import datetime, timezone
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import health


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1791712800
        self.stamp = datetime.fromtimestamp(self.now - 10, timezone.utc).isoformat()
        self.cfg = {'version': 1, 'deployment_dir': str(self.root), 'staging_dir': str(self.root),
            'home_root': str(self.root), 'docker_socket': '/var/run/docker.sock', 'docker_root': str(self.root),
            'hub_url': 'http://127.0.0.1:8888/hub/health', 'backup_status': str(self.root / 'backup.json'),
            'quota_status': str(self.root / 'quota.json'), 'instance': 'synthetic-course',
            'tls_url': 'https://workspace.invalid', 'thresholds': dict(health.DEFAULT_THRESHOLDS), 'timeout_seconds': 1}
        self.config = self.root / 'health.json'
        self.write(self.config, self.cfg)
        self.backup = {'last_operation': 'backup', 'last_attempt': self.stamp, 'success': True,
                       'last_success': self.stamp, 'snapshot_id': 'a' * 64}
        self.quota = {'instance': self.cfg['instance'], 'healthy': True, 'checked_at': self.now - 10,
                     'volumes': {'PRIVATE-LEARNER': {'quota_bytes': 10000, 'used_bytes': 100,
                                                   'inode_limit': 1000, 'used_inodes': 10}}}
        self.write(self.cfg['backup_status'], self.backup)
        self.write(self.cfg['quota_status'], self.quota)

    def write(self, path, value):
        path = Path(path)
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def test_config_requires_private_explicit_settings_and_no_remote_hub_credentials(self):
        self.assertEqual(health.load_config(self.config), self.cfg)
        self.assertEqual(self.cfg['thresholds']['quota_max_age_seconds'], 120)
        for change in ({'version': True}, {'hub_url': 'http://remote.invalid/hub/health'},
                       {'hub_url': 'http://secret@127.0.0.1/hub/health'},
                       {'tls_url': 'https://user:password@workspace.invalid'},
                       {'timeout_seconds': float('nan')}, {'docker_root': 'relative'},
                       {'thresholds': {}}, {'unknown': 'private-secret'}):
            self.write(self.config, dict(self.cfg, **change))
            with self.assertRaises(ValueError):
                health.load_config(self.config)
        self.write(self.config, self.cfg)
        self.config.chmod(0o644)
        with self.assertRaises(ValueError):
            health.load_config(self.config)
        self.config.chmod(0o600)
        alias = self.root / 'alias.json'
        alias.symlink_to(self.config)
        with self.assertRaises(OSError):
            health.load_config(alias)

    def test_foreign_owner_private_file_is_refused(self):
        actual = os.stat(self.config)
        fake = SimpleNamespace(st_mode=actual.st_mode, st_size=actual.st_size, st_uid=os.geteuid()+1000)
        with patch('health.os.fstat', return_value=fake):
            with self.assertRaises(ValueError):
                health.private_json(self.config)

    def test_backup_failed_attempt_is_bad_even_with_recent_success(self):
        self.backup.update(success=False, last_attempt=datetime.fromtimestamp(self.now - 1, timezone.utc).isoformat())
        self.write(self.cfg['backup_status'], self.backup)
        values = health.backup_checks(self.cfg, self.now)
        self.assertEqual([v['status'] for v in values], ['ok', 'critical'])
        self.backup['last_success'] = datetime.fromtimestamp(self.now - 200000, timezone.utc).isoformat()
        self.write(self.cfg['backup_status'], self.backup)
        self.assertEqual(health.backup_checks(self.cfg, self.now)[0]['code'], 'stale')

    def test_bad_backup_timestamps_and_inconsistent_success_never_pass(self):
        for value in ('not-a-time', '2026-10-11T10:00:00', None, 123,
                      datetime.fromtimestamp(self.now + 1, timezone.utc).isoformat()):
            for field in ('last_success', 'last_attempt'):
                altered = dict(self.backup, **{field: value})
                self.write(self.cfg['backup_status'], altered)
                if field == 'last_success' and value is None:
                    self.assertEqual(health.backup_checks(self.cfg, self.now)[0]['status'], 'unavailable')
                else:
                    with self.assertRaises(ValueError):
                        health.backup_checks(self.cfg, self.now)
        altered = dict(self.backup, last_attempt=datetime.fromtimestamp(self.now - 1, timezone.utc).isoformat())
        self.write(self.cfg['backup_status'], altered)
        with self.assertRaises(ValueError):
            health.backup_checks(self.cfg, self.now)

    def test_quota_freshness_failure_and_thresholds(self):
        self.assertEqual(health.quota_check(self.cfg, self.now)['status'], 'ok')
        for field, value, expected in [('checked_at', self.now-121, 'stale'),
                                       ('healthy', False, 'quota_check_failed')]:
            self.write(self.cfg['quota_status'], dict(self.quota, **{field: value}))
            self.assertEqual(health.quota_check(self.cfg, self.now)['code'], expected)
        full = copy.deepcopy(self.quota)
        full['volumes']['PRIVATE-LEARNER']['used_inodes'] = 900
        self.write(self.cfg['quota_status'], full)
        self.assertEqual(health.quota_check(self.cfg, self.now)['code'], 'quota_near_limit')
        for value in (float('nan'), True, self.now+1, '2026-10-11', None):
            self.write(self.cfg['quota_status'], dict(self.quota, checked_at=value))
            with self.assertRaises(ValueError):
                health.quota_check(self.cfg, self.now)
        for change in ({'instance':'other'}, {'healthy':'true'}, {'volumes':{}},
                       {'volumes': {'private': {'quota_bytes': 0}}}):
            self.write(self.cfg['quota_status'], dict(self.quota, **change))
            with self.assertRaises(ValueError):
                health.quota_check(self.cfg, self.now)

    def test_disk_checks_use_available_space_and_refuse_missing_home_mount(self):
        usage = SimpleNamespace(f_blocks=1000, f_frsize=1024**3, f_bavail=300, f_files=100000, f_favail=20000)
        with patch('health.os.statvfs', return_value=usage), patch('health.os.path.ismount', return_value=True):
            self.assertEqual(health.disk_check('home_disk', self.root, self.cfg['thresholds'], True)['status'], 'ok')
            usage.f_bavail = 1
            self.assertEqual(health.disk_check('home_disk', self.root, self.cfg['thresholds'], True)['code'], 'capacity_low')
            usage.f_files = 0
            self.assertEqual(health.disk_check('home_disk', self.root, self.cfg['thresholds'], True)['status'], 'unavailable')
        with patch('health.os.path.ismount', return_value=False):
            self.assertEqual(health.disk_check('home_disk', self.root, self.cfg['thresholds'], True)['status'], 'unavailable')

    @patch('health.socket.SO_PEERCRED', 17, create=True)
    def test_desktop_remote_proxy_peer_and_other_root_cannot_claim_local_docker_disk(self):
        fake = Mock()
        fake.getsockopt.return_value = struct.pack('3i', 456, 0, 0)
        for command in ('socat', 'docker-proxy', 'com.docker.backend'):
            connection = health.LocalDockerConnection('/private/docker.sock', 1)
            with patch('health.sys.platform', 'linux'), patch('health.socket.socket', return_value=fake), patch.object(Path, 'read_text', return_value=command):
                with self.assertRaises(ValueError):
                    connection.connect()
            fake.close.assert_called()
        connection = health.LocalDockerConnection('/private/docker.sock', 1)
        with patch('health.sys.platform', 'linux'), patch('health.socket.socket', return_value=fake), patch.object(Path, 'read_text', return_value='dockerd'), patch('health.os.stat', side_effect=[SimpleNamespace(st_dev=1, st_ino=1), SimpleNamespace(st_dev=2, st_ino=2)]):
            with self.assertRaises(ValueError):
                connection.connect()
        with patch('health.sys.platform', 'darwin'):
            with self.assertRaises(ValueError):
                health.LocalDockerConnection('/private/docker.sock', 1).connect()

    @patch('health.socket.SO_PEERCRED', 17, create=True)
    def test_native_docker_requires_same_mount_namespace_even_when_root_matches(self):
        fake = Mock()
        fake.getsockopt.return_value = struct.pack('3i', 456, 0, 0)
        root = SimpleNamespace(st_dev=1, st_ino=1)
        for namespaces, healthy in ((['mnt:[1]', 'mnt:[2]'], False), (['mnt:[1]', 'mnt:[1]'], True)):
            connection = health.LocalDockerConnection('/private/docker.sock', 1)
            with patch('health.sys.platform', 'linux'), patch('health.socket.socket', return_value=fake), patch.object(Path, 'read_text', return_value='dockerd'), patch('health.os.stat', return_value=root), patch('health.os.readlink', side_effect=namespaces):
                if healthy:
                    connection.connect()
                    self.assertIs(connection.sock, fake)
                else:
                    with self.assertRaises(ValueError):
                        connection.connect()
                    self.assertIsNone(connection.sock)

    def test_native_docker_root_mismatch_never_stats_operator_filesystem(self):
        connection = Mock()
        response = Mock(status=200)
        response.read.return_value = json.dumps({'DockerRootDir': '/remote/private'}).encode()
        connection.getresponse.return_value = response
        with patch('health.LocalDockerConnection', return_value=connection), patch('health.disk_check') as disk:
            self.assertEqual(health.docker_disk_probe(self.cfg)['code'], 'daemon_root_mismatch')
        disk.assert_not_called()
        connection.close.assert_called_once()

    def test_unavailable_checks_are_nonhealthy_and_export_contains_no_private_details(self):
        ok_disk = lambda name, *_args, **_kwargs: health.check(name, 'ok', 'capacity_ok', free_bytes=100)
        with patch('health.disk_check', side_effect=ok_disk), patch('health.docker_disk_check', side_effect=RuntimeError('PRIVATE-DOCKER-TOKEN')), patch('health.hub_check', side_effect=TimeoutError('https://private:user@machine')), patch('health.tls_expiry', side_effect=ssl.SSLError('PRIVATE-CERT')):
            report = health.evaluate(self.cfg, self.now)
        text = json.dumps(report)
        self.assertFalse(report['healthy'])
        self.assertEqual(len(report['checks']), 9)
        for private in ('PRIVATE', self.cfg['instance'], str(self.root), 'snapshot_id', 'machine'):
            self.assertNotIn(private, text)
        destination = self.root / 'monitor.json'
        health.export_status(destination, report)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(destination.read_text()), report)
        destination.unlink()
        destination.symlink_to(self.config)
        with self.assertRaises(ValueError):
            health.export_status(destination, report)
        self.assertEqual(health.load_config(self.config), self.cfg)

    def test_probe_deadlines_and_invalid_cli_report_have_sanitized_deterministic_exit(self):
        with patch('health.subprocess.run', side_effect=subprocess.TimeoutExpired('PRIVATE-SOCKET', 1)):
            with self.assertRaises(subprocess.TimeoutExpired):
                health.docker_disk_check(self.cfg)
            with self.assertRaises(subprocess.TimeoutExpired):
                health.tls_expiry(self.cfg)
        with patch('health.evaluate', return_value={'version':1,'healthy':False,'checks':[]}), patch('sys.stdout',new_callable=io.StringIO) as out:
            self.assertEqual(health.main(['--config', str(self.config)]), 1)
        self.assertFalse(json.loads(out.getvalue())['healthy'])
        self.config.write_text('PRIVATE-PARSE-ERROR')
        with patch('sys.stdout',new_callable=io.StringIO) as out:
            self.assertEqual(health.main(['--config', str(self.config)]), 2)
        self.assertNotIn('PRIVATE', out.getvalue())


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_GET(self):
        self.server.paths.append(self.path)
        self.send_response(self.server.result_status)
        self.end_headers()


class LocalTransportTests(unittest.TestCase):
    def server(self, tls=None):
        server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
        server.result_status = 200
        server.paths = []
        if tls:
            server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def cleanup():
            server.shutdown(); server.server_close(); thread.join(timeout=3)
        self.addCleanup(cleanup)
        return server

    def test_real_local_hub_readiness_does_not_follow_redirects(self):
        server = self.server()
        cfg = {'hub_url': f'http://127.0.0.1:{server.server_port}/hub/health', 'timeout_seconds': 2}
        self.assertEqual(health.hub_check(cfg)['status'], 'ok')
        for code in (302, 401, 503):
            server.result_status = code
            self.assertEqual(health.hub_check(cfg)['status'], 'critical')
        self.assertEqual(server.paths, ['/hub/health'] * 4)

    @unittest.skipUnless(shutil.which('openssl'), 'OpenSSL needed for local verified TLS integration')
    def test_verified_tls_expiry_trust_failure_and_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def openssl(*args):
                subprocess.run(['openssl',*args],cwd=root,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
            openssl('req','-x509','-newkey','rsa:2048','-nodes','-days','2','-subj','/CN=Synthetic Health CA','-keyout','ca.key','-out','ca.pem')
            openssl('req','-newkey','rsa:2048','-nodes','-subj','/CN=127.0.0.1','-keyout','server.key','-out','server.csr')
            (root/'extensions').write_text('subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\n')
            openssl('x509','-req','-in','server.csr','-CA','ca.pem','-CAkey','ca.key','-CAcreateserial','-days','1','-extfile','extensions','-out','server.pem')
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(root/'server.pem',root/'server.key')
            server = self.server(context)
            cfg = {'tls_url': f'https://127.0.0.1:{server.server_port}', 'timeout_seconds':2,'tls_ca_file':str(root/'ca.pem')}
            remaining = health.tls_expiry(cfg) - time.time()
            self.assertGreater(remaining, 23*3600)
            self.assertLessEqual(remaining, 86400)
            with self.assertRaises(ValueError):
                health.tls_expiry({k:v for k,v in cfg.items() if k != 'tls_ca_file'})
            idle = socket.socket()
            idle.bind(('127.0.0.1',0));idle.listen()
            try:
                started=time.monotonic()
                with self.assertRaises((ValueError,subprocess.TimeoutExpired)):
                    health.tls_expiry(dict(cfg,tls_url=f'https://127.0.0.1:{idle.getsockname()[1]}',timeout_seconds=.2))
                self.assertLess(time.monotonic()-started,2)
            finally: idle.close()
