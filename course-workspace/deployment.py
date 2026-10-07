#!/usr/bin/env python3
"""Validated institutional lifecycle; private trials retain their existing entry point."""
import argparse
import os
from pathlib import Path
import re
import subprocess

from hub.settings import load
from pilot import ROOT, locked, settings as trial_settings, wait_ready as trial_ready


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
    result = load(env)
    if result['mode'] != 'oidc':
        raise ValueError('institutional recovery requires oidc mode')
    if not re.fullmatch(r'https://[^/]+/realms/[a-zA-Z0-9_-]+', result['issuer']):
        raise ValueError('institutional recovery requires a Keycloak realm issuer')
    if any(not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,127}', s) for s in result['subjects']):
        raise ValueError('roster must use stable lowercase subject IDs')
    if not 1024 <= int(env['WORKSPACE_PORT']) <= 65535:
        raise ValueError('invalid loopback port')
    return result


def settings(directory):
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
    env = settings(directory)
    clean = {k: v for k, v in os.environ.items() if not k.startswith(('WORKSPACE_', 'COMPOSE_'))}
    files = ['-f', str(ROOT / 'compose.yaml')]
    if env.get('WORKSPACE_HOME_ROOT'):
        files += ['-f', str(ROOT / 'quota-compose.yaml')]
    subprocess.run(['docker','compose','--env-file',env['WORKSPACE_ENV_FILE'],*files,*args],
                   cwd=ROOT, env=clean, check=True)


def wait_ready(env):
    # Hub readiness is local; reverse proxy/IdP acceptance remains separate.
    trial_ready(dict(env, WORKSPACE_PUBLIC_URL='http://127.0.0.1:' + env['WORKSPACE_PORT']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['start','stop','status'])
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    directory = args.directory.resolve()
    try:
        env = settings(directory)
        with locked(directory):
            if args.operation == 'start':
                from homes import inspect
                inspect(Path(env['WORKSPACE_HOME_ROOT']), env['WORKSPACE_INSTANCE'])
                compose(directory, 'up','-d','--no-build','--pull','never','hub')
                wait_ready(env)
                print('Hub ready on loopback. Verify identity/TLS before routing learners to this worker.')
            elif args.operation == 'stop':
                compose(directory, 'stop','hub')
            else:
                compose(directory, 'ps','-a')
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Deployment refused or failed: {exc}\n')


if __name__ == '__main__':
    main()
