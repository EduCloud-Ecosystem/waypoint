#!/usr/bin/env python3
"""Stopped institutional roster/quota maintenance with reviewed plans and resumable journals."""
from contextlib import contextmanager
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import subprocess

import homes
from backup import docker
from deployment import read_environment, validate_institutional, require_native_worker, require_active_course
from pilot import locked

PENDING = '.maintenance-pending.json'
ROOT_PENDING = '.educloud-maintenance.json'
VERSION = 'educloud-maintenance-v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def write(path, value, exclusive=False):
    raw = (json.dumps(value, indent=2, sort_keys=True) + '\n').encode() if not isinstance(value, bytes) else value
    fd, name = tempfile.mkstemp(prefix='.maintenance-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        if exclusive:
            os.link(name, path)
        else:
            if path.is_symlink():
                raise ValueError('refusing a symlink output')
            os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077 or path.stat().st_size > 8 * 1024**2:
        raise ValueError('maintenance records must be bounded regular owner-only files')
    return json.loads(path.read_text())


def safe_directory(directory):
    if not directory.is_absolute() or directory.resolve() != directory or not directory.is_dir() or directory.stat().st_mode & 0o077:
        raise ValueError('use an existing canonical owner-only deployment directory')
    if (directory / 'operation.lock').is_symlink():
        raise ValueError('operation lock cannot be a symlink')
    if (directory / 'operation.lock').exists() and not (directory / 'operation.lock').is_file():
        raise ValueError('operation lock must be a regular file')


def environment(directory):
    safe_directory(directory)
    require_active_course(directory)
    pending = directory / '.retirement-pending.json'
    if pending.exists() or pending.is_symlink():
        raise ValueError('course retirement is incomplete; resume it before roster or quota maintenance')
    pending = directory / '.upgrade-pending.json'
    if pending.exists() or pending.is_symlink():
        raise ValueError('image upgrade is incomplete; resume it before roster or quota maintenance')
    if not directory.is_absolute() or directory.resolve() != directory or not directory.is_dir():
        raise ValueError('use an existing canonical absolute deployment directory')
    if (directory / 'pilot.env').exists():
        raise ValueError('maintenance requires an institutional deployment')
    env = read_environment(directory / '.env')
    validate_institutional(env)
    if env['WORKSPACE_ENV_FILE'] != str(directory / '.env'):
        raise ValueError('environment belongs to a different deployment')
    return env


@contextmanager
def home_lock(root):
    require_native_worker()
    homes.storage(root)
    path = root / '.educloud.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('another home operation is in progress') from None
        yield


def stopped(instance, names, resizing):
    # Exact expected name and labels both matter: do not trust only one inventory.
    running = set(docker('ps', '-q', '--filter', 'label=educloud.workspace.instance=' + instance).splitlines())
    running.update(docker('ps', '-q', '--filter', 'name=^/' + instance + '-hub$').splitlines())
    if running:
        raise ValueError('stop the Hub and all learner servers before maintenance')
    for name in names:
        args = ['ps', '-q'] if not resizing else ['ps', '-a', '-q']
        if docker(*args, '--filter', 'volume=' + name):
            raise ValueError('home has container references; stop users and remove stopped notebook containers before resizing')


def volume(root, name, entry):
    value = json.loads(docker('volume', 'inspect', name))[0]
    expected = homes.expected_volume(root, name, entry)
    if value.get('Driver') != 'local' or value.get('Labels') != expected['labels'] or value.get('Options') != expected['options']:
        raise ValueError('home volume ownership or bind mount differs')
    return expected


def identity_path(directory):
    return directory / '.maintenance-identity.json'


def identity(env):
    return {key: env[key] for key in ('WORKSPACE_INSTANCE', 'WORKSPACE_OIDC_ISSUER', 'WORKSPACE_OIDC_CLIENT_ID', 'WORKSPACE_PUBLIC_URL', 'WORKSPACE_HOME_ROOT')}


def check_identity(directory, env):
    path = identity_path(directory)
    if path.exists() or path.is_symlink():
        if read(path) != identity(env):
            raise ValueError('immutable issuer/client/instance/origin/storage binding changed; migration is a separate operation')


def selected_homes(root, env):
    data = read(root / '.educloud-projects.json')
    if data.get('version') != 1 or not isinstance(data.get('homes'), dict):
        raise ValueError('invalid home registry')
    projects = [entry['project_id'] for entry in data['homes'].values()]
    if any(type(v) is not int or v < 1 for v in projects) or len(set(projects)) != len(projects):
        raise ValueError('registry project identities must be unique positive integers')
    instance = env['WORKSPACE_INSTANCE']
    entries = {name: value for name, value in data['homes'].items() if value['instance'] == instance}
    if not entries:
        raise ValueError('no registered homes for this deployment')
    for name, entry in entries.items():
        if not re.fullmatch(re.escape(instance) + r'-home-[a-f0-9]{24}', name):
            raise ValueError('invalid registered home name')
        if type(entry['project_id']) is not int or entry['project_id'] < 1:
            raise ValueError('invalid project identity')
        path = root / name
        if path.is_symlink() or not path.is_dir():
            raise ValueError('registered home missing or replaced')
        stat = homes.run('xfs_io', '-c', 'stat', str(path))
        if not re.search(r'projid\s*=\s*' + str(entry['project_id']) + r'\b', stat):
            raise ValueError('home project identity changed')
    return data, entries


def snapshot(directory, env):
    root = Path(env['WORKSPACE_HOME_ROOT'])
    data, entries = selected_homes(root, env)
    block, inode = homes.limits(root, 'b'), homes.limits(root, 'i')
    observed = {}
    for name, entry in entries.items():
        if entry['quota_mb'] != int(env['WORKSPACE_HOME_QUOTA_MB']) or entry['inode_limit'] != int(env['WORKSPACE_HOME_INODE_LIMIT']):
            raise ValueError('registered quota differs from deployment')
        b, i = block[entry['project_id']], inode[entry['project_id']]
        if b['hard'] != entry['quota_mb'] * 1024 or i['hard'] != entry['inode_limit']:
            raise ValueError('effective quota differs from registry')
        observed[name] = {'blocks': b, 'inodes': i, 'volume': volume(root, name, entry)}
    return {'env_sha256': digest(env), 'registry': data, 'homes': observed,
            'identity': identity(env), 'filesystem': filesystem(root)}


def filesystem(root):
    # Bind plan to the mounted filesystem, not just a reusable pathname.
    return json.loads(homes.run('findmnt', '--json', '--target', str(root), '--output', 'TARGET,SOURCE,UUID,FSTYPE'))['filesystems'][0]


def requested(env, subjects, quota_mb, inode_limit, data):
    if not subjects or len(subjects) > 10000 or len(subjects) != len(set(subjects)) or any(not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,127}', s) for s in subjects):
        raise ValueError('use a nonempty unique roster of immutable lowercase subject IDs')
    if type(quota_mb) is not int or quota_mb < 16 or type(inode_limit) is not int or inode_limit < 128:
        raise ValueError('quota needs at least 16 MiB and 128 inodes')
    for subject in subjects:
        name = env['WORKSPACE_INSTANCE'] + '-home-' + hashlib.sha256(subject.encode()).hexdigest()[:24]
        if name not in data['homes'] or data['homes'][name]['instance'] != env['WORKSPACE_INSTANCE']:
            raise ValueError('new roster homes must be provisioned at current quotas before preview; no existing files will be adopted')
    result = dict(env, WORKSPACE_ALLOWED_SUBJECTS=','.join(sorted(subjects)), WORKSPACE_HOME_QUOTA_MB=str(quota_mb), WORKSPACE_HOME_INODE_LIMIT=str(inode_limit))
    validate_institutional(result)
    return result


def capacity(root, data, instance, quota_mb, inode_limit, block, inode):
    reservation = 0
    for entry in data['homes'].values():
        own = entry['instance'] == instance
        reservation += quota_mb if own else entry['quota_mb']
        if own and (block[entry['project_id']]['used'] > quota_mb * 1024 or inode[entry['project_id']]['used'] > inode_limit):
            raise ValueError('requested quota is below actual byte or inode usage')
    stat = os.statvfs(root)
    if reservation * 1024**2 > stat.f_blocks * stat.f_frsize - 512 * 1024**2:
        raise ValueError('reservations exceed filesystem capacity with 512 MiB overhead')


def preview(directory, subjects, quota_mb, inode_limit, plan_path):
    safe_directory(directory)
    with locked(directory):
        env = environment(directory)
        root = Path(env['WORKSPACE_HOME_ROOT'])
        with home_lock(root):
            if any(path.exists() or path.is_symlink() for path in (directory / PENDING, root / ROOT_PENDING)):
                raise ValueError('unfinished maintenance must be resumed first')
            check_identity(directory, env)
            before = snapshot(directory, env)
            new = requested(env, subjects, quota_mb, inode_limit, before['registry'])
            resizing = any(new[k] != env[k] for k in ('WORKSPACE_HOME_QUOTA_MB', 'WORKSPACE_HOME_INODE_LIMIT'))
            stopped(env['WORKSPACE_INSTANCE'], before['homes'], resizing)
            capacity(root, before['registry'], env['WORKSPACE_INSTANCE'], quota_mb, inode_limit, homes.limits(root, 'b'), homes.limits(root, 'i'))
            plan = {'version': VERSION, 'directory': str(directory), 'before': before,
                    'subjects': sorted(subjects), 'quota_mb': quota_mb, 'inode_limit': inode_limit,
                    'added_subjects': sorted(set(subjects) - set(env['WORKSPACE_ALLOWED_SUBJECTS'].split(','))),
                    'removed_subjects': sorted(set(env['WORKSPACE_ALLOWED_SUBJECTS'].split(',')) - set(subjects)),
                    'retained_homes': sorted(before['homes'])}
            plan['digest'] = digest(plan)
            write(plan_path, plan, exclusive=True)
            return plan


def apply(directory, plan_path):
    safe_directory(directory)
    plan = read(plan_path)
    unsigned = {k: v for k, v in plan.items() if k != 'digest'}
    if plan.get('version') != VERSION or plan.get('digest') != digest(unsigned) or plan.get('directory') != str(directory):
        raise ValueError('plan digest/version/deployment mismatch')
    with locked(directory):
        env = environment(directory)
        root = Path(env['WORKSPACE_HOME_ROOT'])
        pending, marker = directory / PENDING, root / ROOT_PENDING
        with home_lock(root):
            check_identity(directory, env)
            if pending.exists() or pending.is_symlink():
                journal = read(pending)
                if journal['plan_digest'] != plan['digest'] or journal['plan'] != plan:
                    raise ValueError('resume requires the exact pending plan')
            else:
                if marker.exists() or marker.is_symlink():
                    raise ValueError('home filesystem has unfinished maintenance')
                if snapshot(directory, env) != plan['before']:
                    raise ValueError('plan is stale; preview again before mutation')
                new = requested(env, plan['subjects'], plan['quota_mb'], plan['inode_limit'], plan['before']['registry'])
                resizing = any(new[k] != env[k] for k in ('WORKSPACE_HOME_QUOTA_MB', 'WORKSPACE_HOME_INODE_LIMIT'))
                stopped(env['WORKSPACE_INSTANCE'], plan['before']['homes'], resizing)
                capacity(root, plan['before']['registry'], env['WORKSPACE_INSTANCE'], plan['quota_mb'], plan['inode_limit'], homes.limits(root, 'b'), homes.limits(root, 'i'))
                journal = {'plan_digest': plan['digest'], 'plan': plan, 'old_env': env, 'new_env': new}
                write(pending, journal, exclusive=True)
            if identity(env) != plan['before']['identity'] or filesystem(root) != plan['before']['filesystem']:
                raise ValueError('identity or mounted filesystem changed')
            if env not in (journal['old_env'], journal['new_env']):
                raise ValueError('configuration changed during pending maintenance')
            marker_value = {'directory': str(directory), 'plan_digest': plan['digest']}
            if marker.exists() or marker.is_symlink():
                if read(marker) != marker_value:
                    raise ValueError('another maintenance owns this filesystem')
            else:
                write(marker, marker_value, exclusive=True)
            old, new = journal['old_env'], journal['new_env']
            resizing = any(old[k] != new[k] for k in ('WORKSPACE_HOME_QUOTA_MB', 'WORKSPACE_HOME_INODE_LIMIT'))
            data, entries = selected_homes(root, env)
            wanted_data = json.loads(json.dumps(plan['before']['registry']))
            for entry in wanted_data['homes'].values():
                if entry['instance'] == env['WORKSPACE_INSTANCE']:
                    entry.update(quota_mb=plan['quota_mb'], inode_limit=plan['inode_limit'])
            if data not in (plan['before']['registry'], wanted_data):
                raise ValueError('registry changed during maintenance')
            stopped(env['WORKSPACE_INSTANCE'], entries, resizing)
            blocks, inodes = homes.limits(root, 'b'), homes.limits(root, 'i')
            capacity(root, data, env['WORKSPACE_INSTANCE'], plan['quota_mb'], plan['inode_limit'], blocks, inodes)
            existing = set(docker('volume', 'ls', '--format', '{{.Name}}').splitlines())
            # Prevalidate every home before any quota/volume mutation on a resume.
            for name, entry in entries.items():
                original, wanted = plan['before']['registry']['homes'][name], wanted_data['homes'][name]
                if blocks[entry['project_id']]['hard'] not in (original['quota_mb'] * 1024, wanted['quota_mb'] * 1024) or inodes[entry['project_id']]['hard'] not in (original['inode_limit'], wanted['inode_limit']):
                    raise ValueError('unexpected quota drift during maintenance')
                if name in existing:
                    value = json.loads(docker('volume', 'inspect', name))[0]
                    choices = [homes.expected_volume(root, name, e) for e in (original, wanted)]
                    if value.get('Driver') != 'local' or not any(value.get('Labels') == v['labels'] and value.get('Options') == v['options'] for v in choices):
                        raise ValueError('foreign or changed volume during maintenance')
                elif not resizing:
                    raise ValueError('roster-only maintenance cannot repair a missing home volume')
            for name, entry in entries.items():
                wanted = wanted_data['homes'][name]
                project = entry['project_id']
                if resizing:
                    homes.run('xfs_quota', '-x', '-c', f"limit -p bsoft={wanted['quota_mb']}m bhard={wanted['quota_mb']}m isoft={wanted['inode_limit']} ihard={wanted['inode_limit']} {project}", str(root))
                    expected = homes.expected_volume(root, name, wanted)
                    if name in existing:
                        value = json.loads(docker('volume', 'inspect', name))[0]
                        if value.get('Labels') != expected['labels']:
                            # This deletes only verified Docker bind-volume metadata, never home files.
                            docker('volume', 'rm', name)
                            existing.remove(name)
                    if name not in existing:
                        args = ['volume', 'create', '--driver', 'local']
                        for key, value in expected['labels'].items():
                            args += ['--label', key + '=' + value]
                        for key, value in expected['options'].items():
                            args += ['--opt', key + '=' + value]
                        docker(*args, name)
            write(root / '.educloud-projects.json', wanted_data)
            write(directory / '.env', ''.join(f'{k}={v}\n' for k, v in new.items()).encode())
            write(identity_path(directory), identity(new))
            homes.inspect(root, env['WORKSPACE_INSTANCE'], _maintenance=True)
            write(directory / '.maintenance-receipt.json', {'version': VERSION, 'plan_digest': plan['digest'], 'identity': identity(new), 'env_sha256': digest(new), 'retained_homes': sorted(entries)})
            marker.unlink()
            sync_directory(root)
            pending.unlink()
            sync_directory(directory)
            return {'plan_digest': plan['digest'], 'retained_homes': sorted(entries)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['preview', 'apply'])
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--subjects-file', type=Path)
    parser.add_argument('--quota-mb', type=int)
    parser.add_argument('--inode-limit', type=int)
    args = parser.parse_args()
    try:
        if args.operation == 'preview':
            if args.subjects_file is None:
                raise ValueError('preview requires --subjects-file')
            env = environment(args.directory)
            subjects = [s.strip() for s in args.subjects_file.read_text().splitlines() if s.strip()]
            preview(args.directory, subjects, args.quota_mb if args.quota_mb is not None else int(env['WORKSPACE_HOME_QUOTA_MB']), args.inode_limit if args.inode_limit is not None else int(env['WORKSPACE_HOME_INODE_LIMIT']), args.plan)
            print('Private maintenance plan saved; review before apply. No homes or deployment settings changed.')
        else:
            if args.subjects_file is not None or args.quota_mb is not None or args.inode_limit is not None:
                raise ValueError('apply accepts only the reviewed plan and deployment directory')
            apply(args.directory, args.plan)
            print('Maintenance verified; retained all registered homes. Deployment remains stopped; take a new backup before reopening.')
    except subprocess.CalledProcessError as exc:
        parser.exit(1, f'Maintenance backend failed (exit {exc.returncode}); keep deployment stopped and resume the same plan.\n')
    except (ValueError, KeyError, OSError, RuntimeError) as exc:
        parser.exit(1, f'Maintenance refused or incomplete; keep deployment stopped and resume the same plan: {exc}\n')


if __name__ == '__main__':
    main()
