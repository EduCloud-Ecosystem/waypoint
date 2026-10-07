#!/usr/bin/env python3
"""Provision and verify hard XFS project quotas on a dedicated mounted filesystem."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

from backup import docker, identifier
from recovery import private_json


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def storage(root):
    if os.geteuid() != 0:
        raise ValueError('quota provisioning/inspection requires the worker administrator')
    if not re.fullmatch(r'/[a-zA-Z0-9_./-]+', str(root)) or root.resolve() != root or '..' in root.parts:
        raise ValueError('use an absolute canonical mount path without spaces or links')
    info = json.loads(run('findmnt', '--json', '--target', str(root), '--output', 'TARGET,FSTYPE,OPTIONS'))['filesystems'][0]
    if info['target'] != str(root) or info['fstype'] != 'xfs' or not {'prjquota', 'pquota'}.intersection(info['options'].split(',')):
        raise ValueError('home root must be a dedicated XFS mount with project quotas enabled')
    state = run('xfs_quota', '-x', '-c', 'state -p', str(root))
    if 'Accounting: ON' not in state or 'Enforcement: ON' not in state:
        raise ValueError('XFS project accounting and enforcement must both be ON')


def limits(root, kind):
    result = {}
    output = run('xfs_quota', '-x', '-c', 'report -p -' + kind + ' -n -N', str(root))
    for line in output.splitlines():
        match = re.match(r'\s*#?(\d+)\s+(\d+)\s+(\d+)\s+(\d+)', line)
        if match:
            project, used, soft, hard = map(int, match.groups())
            result[project] = {'used': used, 'hard': hard}
    return result


def registry(root):
    path = root / '.educloud-projects.json'
    if path.exists():
        data = json.loads(path.read_text())
        if data.get('version') != 1:
            raise ValueError('unsupported home registry')
        return data
    if any(p.name != 'lost+found' for p in root.iterdir()):
        raise ValueError('first provisioning requires an empty dedicated filesystem')
    return {'version': 1, 'next_id': 10000, 'homes': {}}


def expected_volume(root, name, entry):
    return {'labels': {'educloud.workspace.instance': entry['instance'],
                       'educloud.workspace.kind': 'home',
                       'educloud.workspace.quota-bytes': str(entry['quota_mb'] * 1024**2),
                       'educloud.workspace.project-id': str(entry['project_id'])},
            'options': {'type': 'none', 'o': 'bind', 'device': str(root / name)}}


def provision(root, instance, subjects, quota_mb, inode_limit=100000):
    storage(root)
    identifier(instance)
    if type(quota_mb) is not int or quota_mb < 16 or inode_limit < 128:
        raise ValueError('home quota must be at least 16 MiB and 128 inodes')
    if not subjects or any(not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,127}', s) for s in subjects):
        raise ValueError('supply stable lowercase subject IDs, one per line')
    # Validate initial emptiness before creating the registry lock.
    data = registry(root)
    if not (root / '.educloud-projects.json').exists():
        private_json(root / '.educloud-projects.json', data)
    with (root / '.educloud.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = registry(root)
        existing = set(docker('volume', 'ls', '--format', '{{.Name}}').splitlines())
        current_ids = set(limits(root, 'b'))
        for subject in sorted(set(subjects)):
            name = instance + '-home-' + hashlib.sha256(subject.encode()).hexdigest()[:24]
            path = root / name
            entry = data['homes'].get(name)
            if entry and (entry['quota_mb'] != quota_mb or entry['inode_limit'] != inode_limit):
                raise ValueError('changing an existing home quota requires a separate maintenance operation')
            if not entry:
                if path.exists() or path.is_symlink() or name in existing:
                    raise ValueError('refusing to adopt an existing unregistered home or volume')
                reserved = sum(v['quota_mb'] for v in data['homes'].values()) + quota_mb
                capacity = os.statvfs(root)
                if reserved * 1024**2 > capacity.f_blocks * capacity.f_frsize - 512 * 1024**2:
                    raise ValueError('home reservations would exhaust the filesystem; keep 512 MiB overhead')
                while data['next_id'] in current_ids:
                    data['next_id'] += 1
                entry = {'instance': instance, 'project_id': data['next_id'],
                         'quota_mb': quota_mb, 'inode_limit': inode_limit}
                data['next_id'] += 1
                data['homes'][name] = entry
                private_json(root / '.educloud-projects.json', data)
            if path.is_symlink():
                raise ValueError('home directories cannot be symlinks')
            wanted = expected_volume(root, name, entry)
            if name in existing:
                volume = json.loads(docker('volume', 'inspect', name))[0]
                if volume.get('Labels') != wanted['labels'] or volume.get('Options') != wanted['options']:
                    raise ValueError('existing volume does not match the registered quota home')
            path.mkdir(mode=0o700, exist_ok=True)
            # A stopped/new workspace is required for a consistent project setup.
            if docker('ps', '-q', '--filter', 'volume=' + name):
                raise ValueError('stop the learner before provisioning its home')
            project = entry['project_id']
            run('xfs_quota', '-x', '-c', f'project -s -p {path} {project}', str(root))
            run('xfs_quota', '-x', '-c', f'limit -p bsoft={quota_mb}m bhard={quota_mb}m isoft={inode_limit} ihard={inode_limit} {project}', str(root))
            os.chown(path, 1000, 1000)
            command = ['volume', 'create', '--driver', 'local']
            for key, value in wanted['labels'].items():
                command += ['--label', key + '=' + value]
            for key, value in wanted['options'].items():
                command += ['--opt', key + '=' + value]
            docker(*command, name)
    inspect(root, instance)


def inspect(root, instance):
    identifier(instance)
    status_dir = root / '.educloud-status'
    try:
        storage(root)
        data = registry(root)
        block = limits(root, 'b')
        inode = limits(root, 'i')
        homes = {name: value for name, value in data['homes'].items() if value['instance'] == instance}
        if not homes:
            raise ValueError('no registered homes for this instance')
        report = {'instance': instance, 'checked_at': time.time(), 'healthy': True, 'volumes': {}}
        for name, entry in homes.items():
            project = entry['project_id']
            if block.get(project, {}).get('hard') != entry['quota_mb'] * 1024 or inode.get(project, {}).get('hard') != entry['inode_limit']:
                raise ValueError('effective filesystem quota differs from the registered limit')
            if block[project]['used'] > block[project]['hard'] or inode[project]['used'] > inode[project]['hard']:
                raise ValueError('restored home exceeds its effective quota; operator maintenance required')
            path = root / name
            if path.is_symlink() or not path.is_dir():
                raise ValueError('registered home path missing or replaced')
            stat = run('xfs_io', '-c', 'stat', str(path))
            if not re.search(r'projid\s*=\s*' + str(project) + r'\b', stat):
                raise ValueError('home project ID differs')
            volume = json.loads(docker('volume', 'inspect', name))[0]
            wanted = expected_volume(root, name, entry)
            if volume.get('Labels') != wanted['labels'] or volume.get('Options') != wanted['options']:
                raise ValueError('home volume ownership or mount differs')
            report['volumes'][name] = {'quota_bytes': entry['quota_mb'] * 1024**2, 'project_id': project,
                                        'used_bytes': block[project]['used'] * 1024,
                                        'used_inodes': inode[project]['used'], 'inode_limit': entry['inode_limit']}
        status_dir.mkdir(mode=0o700, exist_ok=True)
        private_json(status_dir / (instance + '.json'), report)
        print(f'Verified {len(homes)} homes: hard byte/inode limits, project IDs and Docker mounts.')
        return report
    except Exception:
        if status_dir.is_dir():
            private_json(status_dir / (instance + '.json'),
                         {'instance': instance, 'checked_at': time.time(), 'healthy': False, 'volumes': {}})
        raise


def timer_files(root, instance):
    identifier(instance)
    if not re.fullmatch(r'/[a-zA-Z0-9_/-]+', str(root)):
        raise ValueError('quota timer requires an absolute path without spaces')
    name = 'educloud-home-quota-' + instance
    return {name + '.service': f'''[Unit]
Description=Verify EduCloud home quota enforcement ({instance})
After=docker.service
ConditionPathIsMountPoint={root}

[Service]
Type=oneshot
UMask=0077
ExecStart=/usr/bin/python3 /opt/educloud/waypoint/course-workspace/homes.py inspect --root {root} --instance {instance}
''', name + '.timer': f'''[Unit]
Description=Refresh EduCloud home quota status ({instance})

[Timer]
OnBootSec=10s
OnUnitActiveSec=60s
AccuracySec=1s
Unit={name}.service

[Install]
WantedBy=timers.target
'''}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['provision', 'inspect', 'render-timer'])
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--subjects-file', type=Path)
    parser.add_argument('--quota-mb', type=int, default=5120)
    parser.add_argument('--inode-limit', type=int, default=100000)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        if args.operation == 'provision':
            if not args.subjects_file:
                raise ValueError('--subjects-file is required')
            subjects = [line.strip() for line in args.subjects_file.read_text().splitlines() if line.strip()]
            provision(args.root, args.instance, subjects, args.quota_mb, args.inode_limit)
        elif args.operation == 'inspect':
            inspect(args.root, args.instance)
        else:
            if not args.output:
                raise ValueError('--output is required')
            files = timer_files(args.root, args.instance)
            args.output.mkdir(parents=True, exist_ok=False)
            for name, content in files.items():
                (args.output / name).write_text(content)
            print('Quota health timer rendered; install and enable on the prepared worker.')
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Home quota operation refused or failed: {exc}\n')


if __name__ == '__main__':
    main()
