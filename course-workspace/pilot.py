#!/usr/bin/env python3
"""Private, synthetic, one-learner trial. No cloud account or public ports."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from http.client import HTTPException
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
from urllib.error import URLError
from urllib.request import urlopen
import uuid

from backup import docker, identifier

ROOT = Path(__file__).resolve().parent
DEFAULT = ROOT / 'output' / 'minimal-pilot'


def initialize(directory, port=18000):
    if not 1024 <= port <= 65535:
        raise ValueError('port must be between 1024 and 65535')
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    envfile = directory / 'pilot.env'
    instance = 'trial-' + uuid.uuid4().hex[:12]
    env = dict(WORKSPACE_INSTANCE=instance,
               WORKSPACE_IMAGE='educloud-python-r:2026-10-06',
               WORKSPACE_PORT=str(port),
               WORKSPACE_ENV_FILE=str(envfile),
               WORKSPACE_PUBLIC_URL=f'http://127.0.0.1:{port}',
               WORKSPACE_AUTH_MODE='local-test',
               WORKSPACE_TEST_PASSWORD=secrets.token_urlsafe(32),
               WORKSPACE_MEMORY_MB='1024', WORKSPACE_CPU_LIMIT='1',
               WORKSPACE_ACTIVE_LIMIT='1', WORKSPACE_IDLE_SECONDS='900',
               WORKSPACE_MAX_AGE_SECONDS='7200')
    with open(envfile, 'x', opener=lambda path, flags: os.open(path, flags, 0o600)) as f:
        f.write(''.join(f'{k}={v}\n' for k, v in env.items()))
    print(f'Created private trial configuration: {envfile}')
    return env


def settings(directory):
    envfile = directory / 'pilot.env'
    if envfile.is_symlink() or envfile.stat().st_mode & 0o077:
        raise ValueError('pilot.env must be a private, non-symlink file (chmod 600)')
    env = dict(line.split('=', 1) for line in envfile.read_text().splitlines() if line)
    identifier(env['WORKSPACE_INSTANCE'])
    if not env['WORKSPACE_INSTANCE'].startswith('trial-'):
        raise ValueError('only generated trial instances are supported')
    port = int(env['WORKSPACE_PORT'])
    expected = {'WORKSPACE_AUTH_MODE': 'local-test', 'WORKSPACE_ACTIVE_LIMIT': '1',
                'WORKSPACE_MEMORY_MB': '1024', 'WORKSPACE_CPU_LIMIT': '1',
                'WORKSPACE_ENV_FILE': str(envfile),
                'WORKSPACE_PUBLIC_URL': f'http://127.0.0.1:{port}'}
    if any(env.get(k) != v for k, v in expected.items()) or not 1024 <= port <= 65535:
        raise ValueError('trial settings changed; use the institutional deployment for other limits/auth')
    if len(env.get('WORKSPACE_TEST_PASSWORD', '')) < 24:
        raise ValueError('trial password is missing or too short')
    return env


@contextmanager
def locked(directory):
    # Start/stop/checkpoint invocations cooperate on one persistent lock inode.
    with (directory / 'operation.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('another trial operation is in progress') from None
        yield


def compose(directory, *args):
    # Parent-shell overrides must not redirect this private trial's deployment.
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith(('WORKSPACE_', 'COMPOSE_'))}
    subprocess.run(['docker', 'compose', '--env-file', str(directory / 'pilot.env'),
                    '-f', str(ROOT / 'compose.yaml'), *args],
                   cwd=ROOT, env=clean, check=True)


def wait_ready(env):
    url = env['WORKSPACE_PUBLIC_URL'] + '/hub/login'
    for _ in range(60):
        try:
            with urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (URLError, OSError, HTTPException):
            pass
        time.sleep(1)
    raise RuntimeError('trial Hub did not become ready; inspect docker compose logs')


def start(directory, env, build=True):
    info = json.loads(docker('info', '--format', '{{json .}}'))
    if info['MemTotal'] < 3 * 1024**3 or info['NCPU'] < 2:
        raise ValueError('trial requires at least 3 GiB Docker RAM and 2 CPUs; use a 4 GiB host')
    if build:
        compose(directory, '--profile', 'build', 'build')
    compose(directory, 'up', '-d', '--no-build', 'hub')
    wait_ready(env)
    print(f"Ready: {env['WORKSPACE_PUBLIC_URL']} (synthetic users alice or bob; one active at a time)")
    print(f'Password is WORKSPACE_TEST_PASSWORD in {directory / "pilot.env"}; never publish this file.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['init', 'start', 'stop', 'status', 'checkpoint'])
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    parser.add_argument('--port', type=int, default=18000, help='init only; use the same SSH tunnel local port')
    parser.add_argument('--no-build', action='store_true', help='start using existing local images')
    parser.add_argument('--destination', type=Path, help='checkpoint only; a new directory under an existing parent')
    args = parser.parse_args()
    directory = args.directory.resolve()
    try:
        if args.operation == 'init':
            initialize(directory, args.port)
            return
        env = settings(directory)
        with locked(directory):
            if args.operation == 'start':
                start(directory, env, not args.no_build)
            elif args.operation == 'stop':
                compose(directory, 'stop', 'hub')
                print('Stopped. Saved files and configuration retained.')
            elif args.operation == 'checkpoint':
                from checkpoint import checkpoint
                destination = args.destination or directory / ('checkpoint-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
                checkpoint(directory, env, destination.absolute())
            else:
                compose(directory, 'ps', '-a')
                print(f"URL: {env['WORKSPACE_PUBLIC_URL']}; one learner, 1 GiB, 1 CPU; synthetic data only")
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Trial operation refused or failed: {exc}\n')


if __name__ == '__main__':
    main()
