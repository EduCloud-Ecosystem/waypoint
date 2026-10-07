#!/usr/bin/env python3
"""Render a dedicated Ubuntu worker and inspect local operating capacity."""
import argparse
import ipaddress
import json
from pathlib import Path
import re
import shutil
import subprocess

from backup import docker

SOURCE = '/opt/educloud/waypoint/course-workspace'
PILOT = '/srv/educloud/pilot'
CONFIG = '/etc/educloud/recovery.json'


def units():
    return {
        'educloud-backup.service': f'''[Unit]
Description=EduCloud encrypted workspace backup
After=docker.service network-online.target
Wants=network-online.target
ConditionPathExists={CONFIG}

[Service]
Type=oneshot
UMask=0077
EnvironmentFile=-/etc/educloud/backend.env
ExecStart=/usr/bin/python3 {SOURCE}/recovery.py backup --directory {PILOT} --config {CONFIG} --apply-retention
TimeoutStartSec=2h
Nice=10
''',
        'educloud-backup.timer': '''[Unit]
Description=Daily EduCloud maintenance window (02:00 UTC)

[Timer]
OnCalendar=*-*-* 02:00:00 UTC
Persistent=true
Unit=educloud-backup.service

[Install]
WantedBy=timers.target
''',
        'educloud-backup-health.service': f'''[Unit]
Description=Check EduCloud backup age and last outcome
ConditionPathExists={CONFIG}

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {SOURCE}/recovery.py status --directory {PILOT}
''',
        'educloud-backup-health.timer': '''[Unit]
Description=Hourly EduCloud backup health check

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
''',
    }


def cloud_config(public_key, cidr, release):
    if not re.fullmatch(r'[0-9a-f]{40}', release):
        raise ValueError('release must be a full immutable Git commit SHA')
    network = str(ipaddress.ip_network(cidr, strict=False))
    if not re.fullmatch(r'(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) [A-Za-z0-9+/=]+(?: [^\r\n]+)?', public_key):
        raise ValueError('provide one SSH public key, never a private key')
    files = [{'path': '/etc/systemd/system/' + name, 'permissions': '0644', 'content': content}
             for name, content in units().items()]
    return {'package_update': True,
            'packages': ['docker.io', 'docker-compose-v2', 'python3', 'restic', 'git', 'ufw', 'xfsprogs'],
            'ssh_pwauth': False, 'disable_root': True,
            'users': ['default', {'name': 'educloud', 'groups': 'sudo', 'shell': '/bin/bash',
                                 'sudo': ['ALL=(ALL) NOPASSWD:ALL'], 'lock_passwd': True,
                                 'ssh_authorized_keys': [public_key]}],
            'write_files': files,
            'runcmd': [['systemctl', 'enable', '--now', 'docker'],
                       ['ufw', 'default', 'deny', 'incoming'], ['ufw', 'default', 'allow', 'outgoing'],
                       ['ufw', 'allow', 'from', network, 'to', 'any', 'port', '22', 'proto', 'tcp'],
                       ['ufw', '--force', 'enable'],
                       ['mkdir', '-p', '/opt/educloud/waypoint', '/srv/educloud', '/etc/educloud'],
                       ['chmod', '700', '/srv/educloud', '/etc/educloud'],
                       ['git', '-C', '/opt/educloud/waypoint', 'init'],
                       ['git', '-C', '/opt/educloud/waypoint', 'remote', 'add', 'origin',
                        'https://github.com/EduCloud-Ecosystem/waypoint.git'],
                       ['git', '-C', '/opt/educloud/waypoint', 'fetch', '--depth', '1', 'origin', release],
                       ['git', '-C', '/opt/educloud/waypoint', 'checkout', '--detach', release],
                       ['python3', SOURCE + '/pilot.py', 'init', '--directory', PILOT],
                       ['systemctl', 'daemon-reload']],
            'final_message': 'EduCloud worker prepared. Build or restore images, configure backup credentials, then run acceptance before enabling timers.'}


def doctor(directory):
    info = json.loads(docker('info', '--format', '{{json .}}'))
    space = shutil.disk_usage(directory)
    report = {'docker_architecture': info['Architecture'], 'docker_ram_gib': round(info['MemTotal'] / 1024**3, 2),
              'docker_cpus': info['NCPU'], 'operator_disk_free_gib': round(space.free / 1024**3, 2),
              'operator_disk_free_percent': round(100 * space.free / space.total, 1)}
    print(json.dumps(report, indent=2))
    if info['MemTotal'] < 3 * 1024**3 or info['NCPU'] < 2 or space.free < 6 * 1024**3:
        raise ValueError('insufficient baseline capacity: require 3 GiB Docker RAM, 2 CPUs and 6 GiB staging disk free')
    print('Baseline capacity passed; this is not a class-load or filesystem-quota acceptance result.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['render', 'doctor'])
    parser.add_argument('--public-key', type=Path)
    parser.add_argument('--ssh-cidr')
    parser.add_argument('--release')
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.operation == 'doctor':
            doctor(args.directory)
        else:
            if not all([args.public_key, args.ssh_cidr, args.release]):
                raise ValueError('--public-key, --ssh-cidr and --release are required')
            key = args.public_key.read_text().strip()
            config = cloud_config(key, args.ssh_cidr, args.release)
            subprocess.run(['ssh-keygen', '-l', '-f', str(args.public_key)], check=True, stdout=subprocess.DEVNULL)
            args.directory.mkdir(parents=True, exist_ok=False, mode=0o700)
            (args.directory / 'cloud-init.yaml').write_text('#cloud-config\n' + json.dumps(config, indent=2) + '\n')
            for name, content in units().items():
                (args.directory / name).write_text(content)
            print('Rendered cloud-init and systemd units. No cloud resource was created or timer enabled.')
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Worker setup refused or failed: {exc}\n')


if __name__ == '__main__':
    main()
