#!/usr/bin/env python3
"""Read-only same-worker health checks; public output contains no paths or records."""
import argparse
from datetime import datetime, timezone
import http.client
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import struct
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

VERSION = 1
MAX_JSON = 1024 * 1024
CHECK_IDS = ('deployment_disk', 'staging_disk', 'home_disk', 'docker_disk',
             'hub_readiness', 'backup_freshness', 'backup_last_attempt', 'quota_status', 'tls_expiry')
DEFAULT_THRESHOLDS = {'disk_min_free_bytes': 6 * 1024**3, 'disk_min_free_percent': 15,
    'inode_min_free': 10000, 'inode_min_free_percent': 10, 'backup_max_age_seconds': 36 * 3600,
    'quota_max_age_seconds': 120, 'quota_max_used_percent': 90, 'tls_min_days': 14}


def private_json(path):
    """No symlink or oversized/private-file fallback; never print parser errors."""
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid not in (0, os.geteuid()) or info.st_size > MAX_JSON:
            raise ValueError('invalid private JSON file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise ValueError('JSON input exceeds bound')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('expected JSON object')
        return value
    finally:
        os.close(fd)


def absolute_path(raw):
    if not isinstance(raw, str) or not raw or '\x00' in raw or not Path(raw).is_absolute():
        raise ValueError('an explicit absolute path is required')
    return raw


def number(value, minimum, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError('invalid numeric setting')
    return value


def load_config(path):
    cfg = private_json(path)
    fields = {'version', 'deployment_dir', 'staging_dir', 'home_root', 'docker_socket', 'docker_root',
              'hub_url', 'backup_status', 'quota_status', 'instance', 'tls_url', 'thresholds', 'timeout_seconds'}
    if set(cfg) - fields - {'tls_ca_file'} or not fields <= set(cfg) or (type(cfg['version']) is not int or cfg['version'] != VERSION):
        raise ValueError('unsupported health configuration')
    for key in ('deployment_dir', 'staging_dir', 'home_root', 'docker_socket', 'docker_root', 'backup_status', 'quota_status'):
        absolute_path(cfg[key])
    if not isinstance(cfg['instance'], str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', cfg['instance']):
        raise ValueError('invalid deployment identifier')
    hub = urlsplit(cfg['hub_url'])
    if hub.scheme != 'http' or hub.hostname != '127.0.0.1' or hub.username or hub.password or hub.query or hub.fragment or hub.path != '/hub/health':
        raise ValueError('Hub readiness must use the explicit loopback /hub/health endpoint')
    number(hub.port or 80, 1, 65535)
    tls = urlsplit(cfg['tls_url'])
    if tls.scheme != 'https' or not tls.hostname or tls.username or tls.password or tls.query or tls.fragment or tls.path not in ('', '/'):
        raise ValueError('TLS URL must be an HTTPS origin without credentials')
    number(tls.port or 443, 1, 65535)
    if 'tls_ca_file' in cfg:
        absolute_path(cfg['tls_ca_file'])
    number(cfg['timeout_seconds'], .1, 15)
    limits = cfg['thresholds']
    if not isinstance(limits, dict) or set(limits) != set(DEFAULT_THRESHOLDS):
        raise ValueError('all health thresholds must be explicit')
    for key in limits:
        maximum = 100 if 'percent' in key else 365 if key == 'tls_min_days' else 365 * 86400 if key.endswith('_seconds') else 2**60
        number(limits[key], .001, maximum)
    return cfg


def check(identifier, status, code, **metrics):
    return {'id': identifier, 'status': status, 'code': code, 'metrics': metrics}


def unavailable(identifier, code='unavailable'):
    return check(identifier, 'unavailable', code)


def disk_check(identifier, path, limits, require_mount=False):
    path = Path(path)
    if not path.is_dir() or path.is_symlink() or (require_mount and not os.path.ismount(path)):
        return unavailable(identifier, 'path_or_mount_unavailable')
    usage = os.statvfs(path)
    if usage.f_blocks <= 0 or usage.f_frsize <= 0 or usage.f_files <= 0 or usage.f_bavail < 0 or usage.f_favail < 0:
        return unavailable(identifier, 'filesystem_capacity_unavailable')
    free_bytes, free_inodes = usage.f_bavail * usage.f_frsize, usage.f_favail
    byte_percent, inode_percent = 100 * usage.f_bavail / usage.f_blocks, 100 * free_inodes / usage.f_files
    healthy = (free_bytes >= limits['disk_min_free_bytes'] and byte_percent >= limits['disk_min_free_percent']
               and free_inodes >= limits['inode_min_free'] and inode_percent >= limits['inode_min_free_percent'])
    return check(identifier, 'ok' if healthy else 'critical', 'capacity_ok' if healthy else 'capacity_low',
                 free_bytes=free_bytes, free_percent=round(byte_percent, 3), free_inodes=free_inodes,
                 free_inode_percent=round(inode_percent, 3))


class LocalDockerConnection(http.client.HTTPConnection):
    """Unix peer credentials prevent treating a remote/Desktop daemon as local disk."""
    def __init__(self, path, timeout):
        super().__init__('localhost', timeout=timeout)
        self.path = path

    def connect(self):
        if sys.platform != 'linux' or not hasattr(socket, 'SO_PEERCRED'):
            raise ValueError('native Linux daemon verification unavailable')
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        try:
            self.sock.connect(self.path)
            pid, _, _ = struct.unpack('3i', self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            command = Path(f'/proc/{pid}/comm').read_text().strip()
            if command != 'dockerd' and not (pid == 1 and command == 'systemd'):
                raise ValueError('socket peer is not native Docker or systemd socket activation')
            root, peer_root = os.stat('/'), os.stat(f'/proc/{pid}/root')
            if (root.st_dev, root.st_ino) != (peer_root.st_dev, peer_root.st_ino):
                raise ValueError('Docker peer uses another filesystem namespace')
            if os.readlink('/proc/self/ns/mnt') != os.readlink(f'/proc/{pid}/ns/mnt'):
                raise ValueError('Docker peer uses another mount namespace')
        except Exception:
            self.sock.close()
            self.sock = None
            raise


def docker_disk_probe(cfg):
    connection = LocalDockerConnection(cfg['docker_socket'], cfg['timeout_seconds'])
    try:
        connection.request('GET', '/info')
        response = connection.getresponse()
        raw = response.read(MAX_JSON + 1)
        if response.status != 200 or len(raw) > MAX_JSON:
            return unavailable('docker_disk', 'daemon_info_unavailable')
        info = json.loads(raw)
        root = info.get('DockerRootDir')
        if not isinstance(root, str) or Path(root) != Path(cfg['docker_root']):
            return unavailable('docker_disk', 'daemon_root_mismatch')
        return disk_check('docker_disk', root, cfg['thresholds'])
    finally:
        connection.close()


def hub_probe(cfg):
    parsed = urlsplit(cfg['hub_url'])
    connection = http.client.HTTPConnection('127.0.0.1', parsed.port or 80, timeout=cfg['timeout_seconds'])
    try:
        connection.request('GET', '/hub/health')
        response = connection.getresponse()
        # Do not follow login redirects or read any response body/session data.
        return check('hub_readiness', 'ok' if response.status == 200 else 'critical',
                     'ready' if response.status == 200 else 'not_ready', http_status=response.status)
    finally:
        connection.close()


def bounded_probe(kind, cfg):
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '_probe'],
                            input=json.dumps({'kind': kind, 'config': cfg}), text=True,
                            capture_output=True, timeout=cfg['timeout_seconds'] + 1)
    if result.returncode or len(result.stdout) > 4096:
        raise ValueError('bounded health probe unavailable')
    value = json.loads(result.stdout)
    expected = 'docker_disk' if kind == 'docker' else 'hub_readiness'
    if not isinstance(value, dict) or set(value) != {'id', 'status', 'code', 'metrics'} or value['id'] != expected:
        raise ValueError('invalid health probe result')
    return value


def probe_child():
    try:
        raw = sys.stdin.read(65537)
        if len(raw) > 65536:
            raise ValueError('probe input exceeds bound')
        request = json.loads(raw)
        function = {'docker': docker_disk_probe, 'hub': hub_probe}[request['kind']]
        print(json.dumps(function(request['config']), allow_nan=False))
        return 0
    except Exception:
        print('{"error":"probe_unavailable"}')
        return 1


def docker_disk_check(cfg):
    return bounded_probe('docker', cfg)


def hub_check(cfg):
    return bounded_probe('hub', cfg)


def iso_age(value, now):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('invalid timestamp')
    moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if moment.tzinfo is None:
        raise ValueError('timestamp requires timezone')
    age = now - moment.timestamp()
    if not math.isfinite(age) or age < 0:
        raise ValueError('future timestamp')
    return age


def backup_checks(cfg, now):
    data = private_json(cfg['backup_status'])
    if data.get('last_operation') != 'backup' or type(data.get('success')) is not bool:
        raise ValueError('invalid backup status')
    attempt_age = iso_age(data.get('last_attempt'), now)
    last_attempt = check('backup_last_attempt', 'ok' if data['success'] else 'critical',
                         'backup_succeeded' if data['success'] else 'backup_failed', age_seconds=round(attempt_age, 3))
    if not data.get('last_success'):
        return [unavailable('backup_freshness', 'no_successful_backup'), last_attempt]
    success_age = iso_age(data['last_success'], now)
    if success_age < attempt_age or (data['success'] and success_age != attempt_age) or not re.fullmatch(r'[0-9a-f]{64}', str(data.get('snapshot_id', ''))):
        raise ValueError('invalid backup success evidence')
    healthy = success_age <= cfg['thresholds']['backup_max_age_seconds']
    return [check('backup_freshness', 'ok' if healthy else 'critical', 'fresh' if healthy else 'stale',
                  age_seconds=round(success_age, 3)), last_attempt]


def quota_check(cfg, now):
    data = private_json(cfg['quota_status'])
    if data.get('instance') != cfg['instance'] or type(data.get('healthy')) is not bool:
        raise ValueError('quota status does not match deployment')
    checked = number(data.get('checked_at'), 1, now)
    age = now - checked
    if age > cfg['thresholds']['quota_max_age_seconds']:
        return check('quota_status', 'critical', 'stale', age_seconds=round(age, 3))
    if data['healthy'] is not True:
        return check('quota_status', 'critical', 'quota_check_failed', age_seconds=round(age, 3))
    volumes = data.get('volumes')
    if not isinstance(volumes, dict) or not 1 <= len(volumes) <= 10000:
        raise ValueError('invalid quota volume evidence')
    byte_peak = inode_peak = 0.0
    for value in volumes.values():
        if not isinstance(value, dict):
            raise ValueError('invalid quota observation')
        capacity = number(value.get('quota_bytes'), 1, 2**60)
        inodes = number(value.get('inode_limit'), 1, 2**60)
        used = number(value.get('used_bytes'), 0, 2**60)
        used_inodes = number(value.get('used_inodes'), 0, 2**60)
        byte_peak = max(byte_peak, 100 * used / capacity)
        inode_peak = max(inode_peak, 100 * used_inodes / inodes)
    healthy = max(byte_peak, inode_peak) < cfg['thresholds']['quota_max_used_percent']
    return check('quota_status', 'ok' if healthy else 'critical', 'quota_ok' if healthy else 'quota_near_limit',
                 age_seconds=round(age, 3), homes=len(volumes), highest_used_percent=round(byte_peak, 3),
                 highest_inode_percent=round(inode_peak, 3))


def tls_expiry(cfg):
    """Outer subprocess deadline also bounds DNS resolution and TLS handshake."""
    data = {'url': cfg['tls_url'], 'timeout': cfg['timeout_seconds'], 'ca_file': cfg.get('tls_ca_file')}
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '_tls'], input=json.dumps(data),
                            text=True, capture_output=True, timeout=cfg['timeout_seconds'] + 1)
    if result.returncode or len(result.stdout) > 2048:
        raise ValueError('verified TLS unavailable')
    expires = json.loads(result.stdout)['expires_at']
    return number(expires, 1, 2**40)


def tls_child():
    try:
        data = json.loads(sys.stdin.read(16385))
        target = urlsplit(data['url'])
        context = ssl.create_default_context(cafile=data.get('ca_file'))
        with socket.create_connection((target.hostname, target.port or 443), timeout=data['timeout']) as raw:
            with context.wrap_socket(raw, server_hostname=target.hostname) as secured:
                expires = ssl.cert_time_to_seconds(secured.getpeercert()['notAfter'])
        print(json.dumps({'expires_at': expires}))
        return 0
    except Exception:
        print('{"error":"verified_tls_unavailable"}')
        return 1


def evaluate(cfg, now=None):
    now = time.time() if now is None else now
    number(now, 1, 2**40)
    rows = []
    def attempt(ids, action):
        try:
            value = action()
            rows.extend(value if isinstance(value, list) else [value])
        except Exception:
            rows.extend(unavailable(identifier) for identifier in ids)
    for identifier, setting in (('deployment_disk', 'deployment_dir'), ('staging_disk', 'staging_dir'), ('home_disk', 'home_root')):
        attempt((identifier,), lambda identifier=identifier, setting=setting:
                disk_check(identifier, cfg[setting], cfg['thresholds'], require_mount=identifier == 'home_disk'))
    attempt(('docker_disk',), lambda: docker_disk_check(cfg))
    attempt(('hub_readiness',), lambda: hub_check(cfg))
    attempt(('backup_freshness', 'backup_last_attempt'), lambda: backup_checks(cfg, now))
    attempt(('quota_status',), lambda: quota_check(cfg, now))
    def tls():
        days = (tls_expiry(cfg) - now) / 86400
        healthy = days >= cfg['thresholds']['tls_min_days']
        return check('tls_expiry', 'ok' if healthy else 'critical', 'valid' if healthy else 'expiring', days_remaining=round(days, 3))
    attempt(('tls_expiry',), tls)
    assert {row['id'] for row in rows} == set(CHECK_IDS)
    return {'version': VERSION, 'checked_at': datetime.fromtimestamp(now, timezone.utc).isoformat(),
            'healthy': all(row['status'] == 'ok' for row in rows), 'checks': rows}


def export_status(path, report):
    """Atomic 0600 replace under an existing private directory, without following links."""
    path = Path(path)
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir() or parent.stat().st_mode & 0o077 or parent.stat().st_uid not in (0, os.geteuid()) or path.is_symlink():
        raise ValueError('status output requires a private directory and non-symlink target')
    raw = (json.dumps(report, indent=2, allow_nan=False) + '\n').encode()
    if len(raw) > 65536:
        raise ValueError('health output exceeds bound')
    fd, temporary = tempfile.mkstemp(prefix='.health-', dir=parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            os.fchmod(out.fileno(), 0o600)
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        report = evaluate(load_config(args.config))
        if args.output:
            export_status(args.output, report)
        print(json.dumps(report, sort_keys=True, allow_nan=False))
        return 0 if report['healthy'] else 1
    except Exception:
        print(json.dumps({'version': VERSION, 'healthy': False, 'error': 'invalid_configuration_or_output'}))
        return 2


if __name__ == '__main__':
    sys.exit(tls_child() if sys.argv[1:] == ['_tls'] else probe_child() if sys.argv[1:] == ['_probe'] else main())
