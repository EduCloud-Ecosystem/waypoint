#!/usr/bin/env python3
"""Read-only prerequisites for a dedicated single-host worker-channel smoke test.

This is not acceptance, provisioning, firewall verification or a cleanup tool.
Run as administrator on the host, before starting the disposable rehearsal.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

from deployment import docker_json, require_native_worker

ROOT = Path(__file__).resolve().parent.parent
MIN_MEMORY = 6656 * 1024**2
MIN_DISK = 20 * 1024**3


def is_administrator():
    return os.geteuid() == 0


def git_output(*args):
    result = subprocess.run(['git', '-C', str(ROOT), *args], check=True,
                            capture_output=True, text=True, timeout=20)
    if len(result.stdout) > 1024 * 1024:
        raise ValueError('git inspection too large')
    return result.stdout.strip()


def no_volumes():
    result = subprocess.run(['docker', 'volume', 'ls', '--quiet'], check=True,
                            capture_output=True, text=True, timeout=20)
    return not result.stdout.strip()


def inspect_host(directory, release):
    """Return only bounded check outcomes, never daemon paths or diagnostics."""
    checks = {}

    def check(name, action):
        try:
            checks[name] = bool(action())
        except (ValueError, OSError, subprocess.SubprocessError, KeyError, TypeError):
            checks[name] = False

    def native():
        require_native_worker()
        return True

    check('native_linux_docker', native)
    check('administrator', is_administrator)
    check('ubuntu_24_04', lambda: platform.freedesktop_os_release().get('ID') == 'ubuntu'
          and platform.freedesktop_os_release().get('VERSION_ID') == '24.04')
    check('python_3_12_or_newer', lambda: sys.version_info >= (3, 12))
    check('clean_pinned_source', lambda: bool(re.fullmatch(r'[0-9a-f]{40}', release))
          and git_output('rev-parse', 'HEAD') == release
          and not git_output('status', '--porcelain', '--untracked-files=all'))
    check('private_staging_directory', lambda: Path(directory).is_dir()
          and Path(directory).absolute() == Path(directory).resolve()
          and Path(directory).stat().st_uid == os.geteuid()
          and not Path(directory).stat().st_mode & 0o077)
    check('staging_disk', lambda: Path(directory).is_dir()
          and shutil.disk_usage(directory).free >= MIN_DISK)

    # Never inspect a remote/shared daemon after the native endpoint check fails.
    info = None
    if checks['native_linux_docker']:
        try:
            info = docker_json('info', '--format', '{{json .}}')
        except (ValueError, OSError, subprocess.SubprocessError):
            pass
    check('docker_capacity', lambda: isinstance(info, dict) and info.get('OSType') == 'linux'
          and info.get('Architecture') in ('x86_64', 'amd64')
          and type(info.get('NCPU')) is int and 4 <= info['NCPU'] <= 4096
          and type(info.get('MemTotal')) is int and info['MemTotal'] >= MIN_MEMORY)
    check('docker_storage', lambda: isinstance(info, dict)
          and isinstance(info.get('DockerRootDir'), str)
          and Path(info['DockerRootDir']).is_absolute()
          and shutil.disk_usage(info['DockerRootDir']).free >= MIN_DISK)
    # Stopped containers and retained volumes also indicate possible coursework.
    checks['empty_container_inventory'] = (isinstance(info, dict)
        and type(info.get('Containers')) is int and info['Containers'] == 0)
    check('empty_volume_inventory', lambda: checks['native_linux_docker']
          and no_volumes())
    return {'version': 'waypoint-pilot-host-v1',
            'scope': 'dedicated-host-prerequisites-only',
            'status': 'READY_FOR_SMOKE' if all(checks.values()) else 'REFUSED',
            'checks': checks, 'hosted_acceptance': False,
            'independent_host_recovery': False, 'production_ready': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True,
                        help='existing private staging directory; nothing is created')
    parser.add_argument('--release', required=True, help='reviewed full Git commit SHA')
    args = parser.parse_args()
    report = inspect_host(args.directory, args.release)
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'READY_FOR_SMOKE' else 1


if __name__ == '__main__':
    sys.exit(main())
