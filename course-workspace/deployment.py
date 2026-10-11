#!/usr/bin/env python3
"""Validated institutional lifecycle; private trials retain their existing entry point."""
import argparse
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import socket
import struct
import sys

from hub.settings import load
from pilot import ROOT, locked, settings as trial_settings, wait_ready as trial_ready


def require_no_maintenance(directory):
    pending = directory / '.maintenance-pending.json'
    if pending.exists() or pending.is_symlink():
        raise ValueError('maintenance is incomplete; resume the recorded maintenance before starting or backing up')
    pending = directory / '.upgrade-pending.json'
    if pending.exists() or pending.is_symlink():
        raise ValueError('image upgrade is incomplete; resume the recorded upgrade before starting or backing up')
    pending = directory / '.retirement-pending.json'
    if pending.exists() or pending.is_symlink():
        raise ValueError('course retirement is incomplete; resume the recorded retirement before starting or backing up')


def require_active_course(directory):
    marker = directory / '.course-retired.json'
    if marker.exists() or marker.is_symlink():
        raise ValueError('course is retired; startup and policy changes are blocked while work is retained')


def read_environment(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError('environment must be a regular owner-only file (chmod 600)')
    env = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or not re.fullmatch(r'WORKSPACE_[A-Z_]+', key) or key in env:
            raise ValueError('environment requires unique WORKSPACE_ keys and literal values')
        # Compose interpolates dollar expressions and interprets quoting/comments.
        # Restrict this portable recovery format so validation and Compose agree.
        if any(c.isspace() for c in value) or any(c in value for c in ('$','"',"'",'#','\\','\x00')):
            raise ValueError('environment values must be literal without whitespace or interpolation')
        env[key] = value
    return env


def validate_institutional(env):
    try:
        result = load(env)
    except (ValueError, TypeError, OverflowError):
        raise ValueError("invalid institutional configuration; inspect the private environment") from None
    if result['mode'] != 'oidc':
        raise ValueError('institutional recovery requires oidc mode')
    if not re.fullmatch(r'https://[^/]+/realms/[a-zA-Z0-9_-]+', result['issuer']):
        raise ValueError('institutional recovery requires a Keycloak realm issuer')
    if any(not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,127}', s) for s in result['subjects']):
        raise ValueError('roster must use stable lowercase subject IDs')
    port = env.get('WORKSPACE_PORT', '')
    if not re.fullmatch(r'[0-9]{4,5}', port) or not 1024 <= int(port) <= 65535:
        raise ValueError('invalid loopback port')
    return result


def settings(directory, allow_pending=False):
    if not allow_pending:
        require_no_maintenance(directory)
    if (directory / 'pilot.env').exists():
        if (directory / '.env').exists():
            raise ValueError('ambiguous deployment: both pilot.env and .env exist')
        return trial_settings(directory)
    env = read_environment(directory / '.env')
    validate_institutional(env)
    if env.get('WORKSPACE_ENV_FILE') != str(directory / '.env'):
        raise ValueError('WORKSPACE_ENV_FILE must identify this deployment .env')
    return env


def compose(directory, *args):
    # Operators must still be able to stop/inspect a worker after an interrupted
    # operation. Starting, exporting or backing up partial policy stays blocked.
    if any(arg in ('up', 'start', 'restart', 'run', 'create') for arg in args):
        require_active_course(directory)
    env = settings(directory, allow_pending=args in (('stop', 'hub'), ('ps', '-a')))
    if env.get('WORKSPACE_AUTH_MODE') == 'oidc':
        require_native_worker()
    clean = {k: v for k, v in os.environ.items() if not k.startswith(('WORKSPACE_', 'COMPOSE_'))}
    files = ['-f', str(ROOT / 'compose.yaml')]
    if env.get('WORKSPACE_HOME_ROOT'):
        files += ['-f', str(ROOT / 'quota-compose.yaml')]
    subprocess.run(['docker','compose','--env-file',env['WORKSPACE_ENV_FILE'],*files,*args],
                   cwd=ROOT, env=clean, check=True)


def wait_ready(env):
    # Hub readiness is local; reverse proxy/IdP acceptance remains separate.
    trial_ready(dict(env, WORKSPACE_PUBLIC_URL='http://127.0.0.1:' + env['WORKSPACE_PORT']))


def docker_json(*args):
    """Bounded host inspection; raw daemon diagnostics never enter a report."""
    try:
        result = subprocess.run(['docker', *args], capture_output=True, text=True,
                                check=True, timeout=20)
        if len(result.stdout) > 1024 * 1024:
            raise ValueError('Docker inspection exceeded the report limit')
        return json.loads(result.stdout)
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        raise ValueError('Docker inspection failed; inspect the worker privately') from None


def require_native_worker():
    """Bind local XFS inspection to the daemon used by Compose and homes.py.

    Keep this stdlib-only: historical recovery runtimes contain deployment.py
    but do not contain the newer health monitor module.
    """
    if sys.platform != 'linux' or not hasattr(socket, 'SO_PEERCRED'):
        raise ValueError('institutional startup requires a native Linux Docker worker')
    endpoint = os.environ.get('DOCKER_HOST')
    if os.environ.get('DOCKER_CONTEXT') or not endpoint:
        contexts = docker_json('context', 'inspect')
        if not isinstance(contexts, list) or len(contexts) != 1 or not isinstance(contexts[0], dict):
            raise ValueError('Docker context could not be verified')
        endpoints = contexts[0].get('Endpoints')
        if not isinstance(endpoints, dict) or not isinstance(endpoints.get('docker'), dict):
            raise ValueError('Docker context endpoint could not be verified')
        endpoint = endpoints['docker'].get('Host')
    if endpoint != 'unix:///var/run/docker.sock':
        raise ValueError('Docker must use the native /var/run/docker.sock endpoint used by the Hub')
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(5)
            peer.connect('/var/run/docker.sock')
            pid, _, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            command = Path(f'/proc/{pid}/comm').read_text().strip()
            if command != 'dockerd' and not (pid == 1 and command == 'systemd'):
                raise ValueError('unverified native Docker peer')
            if os.readlink('/proc/self/ns/mnt') != os.readlink(f'/proc/{pid}/ns/mnt'):
                raise ValueError('Docker uses a different mount namespace')
            local, remote = os.stat('/'), os.stat(f'/proc/{pid}/root')
            if (local.st_dev, local.st_ino) != (remote.st_dev, remote.st_ino):
                raise ValueError('Docker uses a different filesystem namespace')
    except (OSError, ValueError, struct.error):
        raise ValueError('native Docker filesystem identity could not be verified') from None


def preflight(directory, *, allow_pending=False):
    """Inspect the configured course without pulling images or starting services."""
    # Only the stopped-course upgrade transaction uses this internal override
    # to verify its newly written pins before removing the interruption marker.
    require_active_course(directory)
    if allow_pending:
        pending = directory / '.maintenance-pending.json'
        if pending.exists() or pending.is_symlink():
            raise ValueError('maintenance is incomplete; resume the recorded maintenance before starting or backing up')
        pending = directory / '.retirement-pending.json'
        if pending.exists() or pending.is_symlink():
            raise ValueError('course retirement is incomplete; resume the recorded retirement before starting or backing up')
    env = settings(directory, allow_pending=allow_pending)
    cfg = validate_institutional(env)
    require_native_worker()
    info = docker_json('info', '--format', '{{json .}}')
    if (not isinstance(info, dict) or type(info.get('NCPU')) is not int
            or type(info.get('MemTotal')) is not int
            or not isinstance(info.get('Architecture'), str)):
        raise ValueError('Docker capacity/architecture information is invalid')
    # One GiB for the Hub and one GiB reserved for host services. This is a
    # conservative admission floor, not a measured class-capacity guarantee.
    required_mb = cfg['active_limit'] * cfg['memory_mb'] + 2048
    if info.get('OSType') != 'linux' or info.get('NCPU', 0) < 2:
        raise ValueError('require a Linux Docker worker with at least two CPUs')
    if info.get('MemTotal', 0) < required_mb * 1024**2:
        raise ValueError('configured active learner limit exceeds the worker memory budget')
    architecture = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(info['Architecture'], info['Architecture'])
    images = {}
    for role, reference in [('hub', cfg['hub_image']), ('course', cfg['image'])]:
        rows = docker_json('image', 'inspect', reference)
        if not isinstance(rows, list) or len(rows) != 1:
            raise ValueError('required image is missing or ambiguous')
        image = rows[0]
        if not isinstance(image, dict):
            raise ValueError('required image inspection is invalid')
        if image.get('Os') != 'linux' or image.get('Architecture') != architecture:
            raise ValueError('required image is incompatible with the worker')
        if not isinstance(image.get('Id'), str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image['Id']):
            raise ValueError('required image has no immutable image ID')
        if reference.startswith('sha256:') and image['Id'] != reference:
            raise ValueError('required image differs from the configured immutable image ID')
        images[role] = image['Id']
    from homes import inspect
    with redirect_stdout(io.StringIO()):
        report = inspect(Path(cfg['home_root']), cfg['instance'])
    expected = {cfg['instance'] + '-home-' + hashlib.sha256(s.encode()).hexdigest()[:24]
                for s in cfg['subjects']}
    if not expected.issubset(report['volumes']):
        raise ValueError('approved roster contains unprovisioned homes')
    # Retained removed-learner homes share the course policy and remain backed up.
    for home in report['volumes'].values():
        if home['quota_bytes'] != cfg['home_quota_mb'] * 1024**2 or home['inode_limit'] != cfg['home_inode_limit']:
            raise ValueError('registered home limits differ from deployment policy')
    return {'version': 1, 'status': 'ready', 'scope': 'local-startup-preflight',
            'instance': cfg['instance'], 'approved_home_count': len(expected),
            'retained_home_count': len(report['volumes']) - len(expected),
            'memory_required_mb': required_mb, 'docker_memory_mb': info['MemTotal'] // 1024**2,
            'images': images,
            'limits': ['Not institutional identity, public TLS, backup, or class-load acceptance.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['preflight','start','stop','status'])
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    directory = args.directory.resolve()
    try:
        env = settings(directory, allow_pending=args.operation in ('stop', 'status'))
        with locked(directory):
            if args.operation == 'preflight':
                print(json.dumps(preflight(directory), indent=2))
            elif args.operation == 'start':
                preflight(directory)
                compose(directory, 'up','-d','--no-build','--pull','never','hub')
                wait_ready(env)
                print('Hub ready on loopback. Verify identity/TLS before routing learners to this worker.')
            elif args.operation == 'stop':
                compose(directory, 'stop','hub')
            else:
                compose(directory, 'ps','-a')
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f'Deployment refused or failed: {exc}\n')


if __name__ == '__main__':
    main()
