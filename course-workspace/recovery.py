#!/usr/bin/env python3
"""Encrypted, self-contained trial recovery using an operator-owned Restic repository."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit

from backup import digest, docker, restore, validate_manifest
from checkpoint import checkpoint
from pilot import ROOT, initialize, locked, settings

TAG = 'educloud-workspace-v1'
BASE_CODE = ('pilot.py', 'checkpoint.py', 'backup.py', 'volume_archive.py',
        'recovery.py', 'compose.yaml')
CODE = BASE_CODE + ('quota-compose.yaml', 'homes.py')


def private_json(path, data):
    temporary = path.with_name(path.name + '.new')
    with open(temporary, 'x', opener=lambda p, f: os.open(p, f, 0o600)) as out:
        json.dump(data, out, indent=2)
        out.write('\n')
    os.replace(temporary, path)


def private_file(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError('configuration and password files must be regular owner-only files')


def load_config(path):
    private_file(path)
    cfg = json.loads(path.read_text())
    if cfg.get('version') != 1:
        raise ValueError('unsupported recovery configuration')
    password = Path(cfg['password_file'])
    if not password.is_absolute():
        raise ValueError('password_file must be absolute')
    private_file(password)
    if len(password.read_text().strip()) < 24:
        raise ValueError('use a generated repository password of at least 24 characters')
    repository = cfg['repository']
    if not isinstance(repository, str) or any(c.isspace() for c in repository):
        raise ValueError('invalid repository location')
    if repository.startswith('sftp:'):
        if not re.fullmatch(r'sftp:(?:[a-zA-Z0-9_.-]+@)?[a-zA-Z0-9_.-]+:/[a-zA-Z0-9_./-]+', repository):
            raise ValueError('use sftp:SSH_ALIAS:/absolute/repository/path')
    elif repository.startswith('s3:https://'):
        url = urlsplit(repository[3:])
        if not url.hostname or url.username or url.password or url.query or url.fragment or not url.path.strip('/'):
            raise ValueError('use an HTTPS S3 endpoint/bucket without embedded credentials')
    elif not (cfg.get('local_test') is True and Path(repository).is_absolute()):
        raise ValueError('off-host repository must use SFTP or HTTPS S3; local paths require local_test=true')
    for key, default in [('keep_last', 7), ('keep_daily', 7), ('keep_weekly', 4)]:
        value = cfg.get(key, default)
        if type(value) is not int or value < 1:
            raise ValueError('retention counts must be positive integers')
        cfg[key] = value
    return cfg


def restic(cfg, *args, cwd=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith('RESTIC_')}
    env.update(RESTIC_REPOSITORY=cfg['repository'], RESTIC_PASSWORD_FILE=cfg['password_file'])
    command = ['restic', '--no-cache']
    if cfg['repository'].startswith('sftp:'):
        command += ['-o', 'sftp.args=-oBatchMode=yes -oStrictHostKeyChecking=yes']
    result = subprocess.run(command + list(args), cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        # Backend errors can contain URLs/credentials. Keep them out of CI/chat logs.
        raise RuntimeError(f'Restic {args[0]} failed (exit {result.returncode}); run Restic privately for backend diagnostics')
    return result.stdout


def image_info(reference):
    return json.loads(docker('image', 'inspect', reference))[0]


def write_status(directory, operation, success, snapshot=None):
    path = directory / 'recovery-status.json'
    previous = json.loads(path.read_text()) if path.exists() else {}
    now = datetime.now(timezone.utc).isoformat()
    record = dict(previous, last_operation=operation, last_attempt=now, success=success)
    if success and snapshot:
        record.update(last_success=now, snapshot_id=snapshot)
    private_json(path, record)


def package(directory, env, bundle):
    bundle.mkdir(mode=0o700)
    hub = json.loads(docker('inspect', env['WORKSPACE_INSTANCE'] + '-hub'))[0]
    images = {'hub': image_info(hub['Image']), 'course': image_info(env['WORKSPACE_IMAGE'])}
    needed = sum(i['Size'] for i in images.values()) + 512 * 1024**2
    if shutil.disk_usage(bundle).free < needed:
        raise ValueError('insufficient staging disk for runtime images plus workspace archives')
    # Large immutable image export happens before the short offline checkpoint.
    subprocess.run(['docker', 'image', 'save', '--output', str(bundle / 'images.tar'),
                    *dict.fromkeys(i['Id'] for i in images.values())], check=True)
    (bundle / 'images.tar').chmod(0o600)
    checkpoint(directory, env, bundle / 'volumes')
    runtime = json.loads((bundle / 'volumes' / 'runtime.json').read_text())
    if runtime['hub_image_id'] != images['hub']['Id'] or runtime['course_image_id'] != images['course']['Id']:
        raise ValueError('runtime images changed during checkpoint; retry with deployments paused')
    private_json(bundle / 'environment.json', env)
    (bundle / 'runtime').mkdir(mode=0o700)
    for name in CODE:
        shutil.copyfile(ROOT / name, bundle / 'runtime' / name)
        (bundle / 'runtime' / name).chmod(0o600)
    files = {str(p.relative_to(bundle)): digest(p) for p in bundle.rglob('*') if p.is_file()}
    metadata = {'version': 1, 'instance': env['WORKSPACE_INSTANCE'], 'files': files,
                'images': {k: {'id': v['Id'], 'architecture': v['Architecture'], 'os': v['Os']}
                           for k, v in images.items()}}
    private_json(bundle / 'bundle.json', metadata)
    return metadata


def validate_bundle(bundle):
    if bundle.is_symlink() or not bundle.is_dir():
        raise ValueError('missing recovery bundle')
    paths = list(bundle.rglob('*'))
    if any(p.is_symlink() or (not p.is_file() and not p.is_dir()) for p in paths):
        raise ValueError('recovery bundle contains links or special files')
    meta = json.loads((bundle / 'bundle.json').read_text())
    if meta.get('version') != 1:
        raise ValueError('unsupported recovery bundle')
    actual = {str(p.relative_to(bundle)) for p in paths if p.is_file()} - {'bundle.json'}
    if actual != set(meta['files']):
        raise ValueError('bundle file inventory differs')
    required = {'images.tar', 'environment.json', 'volumes/manifest.json', 'volumes/runtime.json'}
    required.update('runtime/' + name for name in BASE_CODE)
    if not required.issubset(actual):
        raise ValueError('incomplete recovery bundle')
    for name, checksum in meta['files'].items():
        if digest(bundle / name) != checksum:
            raise ValueError('bundle checksum differs')
    env = json.loads((bundle / 'environment.json').read_text())
    if env.get('WORKSPACE_HOME_ROOT') and not {'runtime/homes.py', 'runtime/quota-compose.yaml'}.issubset(actual):
        raise ValueError('quota recovery runtime is incomplete')
    if env.get('WORKSPACE_AUTH_MODE') != 'local-test' or env.get('WORKSPACE_INSTANCE') != meta['instance']:
        raise ValueError('this recovery version supports the synthetic trial only')
    if validate_manifest(json.loads((bundle / 'volumes/manifest.json').read_text()), bundle / 'volumes') != meta['instance']:
        raise ValueError('volume source differs from bundle')
    info = json.loads(docker('info', '--format', '{{json .}}'))
    arch = {'aarch64': 'arm64', 'x86_64': 'amd64'}.get(info['Architecture'], info['Architecture'])
    for image in meta['images'].values():
        if image['os'] != 'linux' or image['architecture'] != arch or not re.fullmatch(r'sha256:[0-9a-f]{64}', image['id']):
            raise ValueError('runtime image is incompatible with this worker architecture')
    return meta, env


def backup_repository(directory, cfg, apply_retention=False):
    env = settings(directory)
    with locked(directory):
        try:
            restic(cfg, 'cat', 'config')  # Credentials/repository must work before downtime.
            with tempfile.TemporaryDirectory(prefix='.recovery-', dir=directory) as scratch:
                package(directory, env, Path(scratch) / 'bundle')
                output = restic(cfg, 'backup', '--json', '--host', env['WORKSPACE_INSTANCE'],
                                '--tag', TAG, '--tag', 'instance=' + env['WORKSPACE_INSTANCE'],
                                'bundle', cwd=scratch)
                summaries = [json.loads(line) for line in output.splitlines() if line.strip()]
                snapshot = next(row['snapshot_id'] for row in summaries if row.get('message_type') == 'summary')
                if not re.fullmatch(r'[0-9a-f]{64}', snapshot):
                    raise ValueError('Restic did not return a complete snapshot ID')
                restic(cfg, 'check')
                write_status(directory, 'backup', True, snapshot)
            print(f'Encrypted snapshot: {snapshot}. Plaintext staging removed.')
            if apply_retention:
                retention(cfg, env['WORKSPACE_INSTANCE'], apply=True)
            return snapshot
        except BaseException:
            write_status(directory, 'backup', False)
            raise


def restore_repository(cfg, snapshot, destination, port, home_root=None):
    if not re.fullmatch(r'[0-9a-f]{64}', snapshot):
        raise ValueError('restore requires an exact 64-character snapshot ID, never latest')
    if destination.exists() or destination.is_symlink() or not destination.parent.is_dir():
        raise ValueError('restore requires a new destination under an existing parent')
    snapshots = json.loads(restic(cfg, 'snapshots', '--json', snapshot))
    if len(snapshots) != 1 or snapshots[0]['id'] != snapshot or TAG not in snapshots[0].get('tags', []):
        raise ValueError('snapshot is not an EduCloud recovery package')
    with tempfile.TemporaryDirectory(prefix='.restore-', dir=destination.parent) as scratch:
        restic(cfg, 'restore', snapshot, '--target', scratch, '--verify')
        bundle = Path(scratch) / 'bundle'
        meta, saved = validate_bundle(bundle)
        if bool(saved.get('WORKSPACE_HOME_ROOT')) != bool(home_root):
            raise ValueError('quota-backed recovery requires an explicit replacement --home-root; ordinary trials omit it')
        # Restore into a new namespace; never overwrite an existing home/config.
        env = initialize(destination, port)
        instance = env['WORKSPACE_INSTANCE']
        existing = set(docker('volume', 'ls', '--format', '{{.Name}}').splitlines())
        if any(name.startswith(instance + '-') for name in existing):
            raise ValueError('target namespace already has volumes')
        subprocess.run(['docker', 'image', 'load', '--input', str(bundle / 'images.tar')], check=True,
                       stdout=subprocess.DEVNULL)
        for value in meta['images'].values():
            if image_info(value['id'])['Id'] != value['id']:
                raise ValueError('restored image ID differs')
        env.update(saved)
        env.update(WORKSPACE_INSTANCE=instance, WORKSPACE_IMAGE=meta['images']['course']['id'],
                   WORKSPACE_PORT=str(port), WORKSPACE_PUBLIC_URL=f'http://127.0.0.1:{port}',
                   WORKSPACE_ENV_FILE=str(destination / 'pilot.env'))
        if home_root:
            env['WORKSPACE_HOME_ROOT'] = str(home_root)
        if any(not isinstance(v, str) or '\n' in v or '\r' in v for v in env.values()):
            raise ValueError('invalid restored environment')
        (destination / 'pilot.env').write_text(''.join(f'{k}={v}\n' for k, v in env.items()))
        settings(destination)  # Reapply the small trial's invariant settings.
        docker('tag', meta['images']['hub']['id'], instance + '-hub:pilot')
        home_options = {}
        if home_root:
            from homes import provision, registry, expected_volume
            provision(home_root, instance, ['alice', 'bob'], int(env['WORKSPACE_HOME_QUOTA_MB']),
                      int(env.get('WORKSPACE_HOME_INODE_LIMIT', '100000')))
            records = registry(home_root)['homes']
            volume_manifest = json.loads((bundle / 'volumes/manifest.json').read_text())
            for item in volume_manifest['volumes']:
                if item['kind'] == 'home':
                    name = instance + '-' + item['suffix']
                    home_options[name] = expected_volume(home_root, name, records[name])
        restore(instance, bundle / 'volumes', meta['images']['course']['id'], home_options)
        if home_root:
            from homes import inspect
            inspect(home_root, instance)
        runtime = destination / 'runtime'
        shutil.copytree(bundle / 'runtime', runtime)
        private_json(destination / 'recovery-source.json', {'snapshot_id': snapshot, 'source_instance': meta['instance']})
    print(f'Restored to {destination}; no server started. Use its runtime/pilot.py start --no-build --directory {destination}')
    return env


def retention(cfg, instance, apply=False):
    from backup import identifier
    identifier(instance)
    args = ['forget', '--json', '--host', instance, '--tag', TAG + ',instance=' + instance,
            '--group-by', 'host,tags', '--keep-last', str(cfg['keep_last']),
            '--keep-daily', str(cfg['keep_daily']), '--keep-weekly', str(cfg['keep_weekly'])]
    if apply:
        restic(cfg, 'check')
        args += ['--prune']
    else:
        args += ['--dry-run']
    return restic(cfg, *args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['keygen', 'init', 'backup', 'snapshots', 'restore', 'check', 'retention', 'status'])
    parser.add_argument('--config', type=Path)
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--snapshot')
    parser.add_argument('--port', type=int, default=18000)
    parser.add_argument('--instance')
    parser.add_argument('--home-root', type=Path, help='quota-backed restore only: a dedicated replacement XFS mount')
    parser.add_argument('--apply', action='store_true', help='retention only; otherwise preview')
    parser.add_argument('--apply-retention', action='store_true', help='backup only; prune this instance after a successful backup')
    args = parser.parse_args()
    try:
        if args.operation == 'keygen':
            if not args.destination:
                raise ValueError('--destination is required')
            with open(args.destination, 'x', opener=lambda p, f: os.open(p, f, 0o600)) as out:
                out.write(secrets.token_urlsafe(48) + '\n')
            print('Repository password created. Preserve a separate secure copy before relying on backups.')
            return
        if args.operation == 'status':
            if not args.directory:
                raise ValueError('--directory is required')
            record = json.loads((args.directory / 'recovery-status.json').read_text())
            print(json.dumps(record, indent=2))
            last = datetime.fromisoformat(record.get('last_success', '1970-01-01T00:00:00+00:00'))
            if not record['success'] or (datetime.now(timezone.utc) - last).total_seconds() > 26 * 3600:
                raise ValueError('backup failed or last success is older than 26 hours')
            return
        if not args.config:
            raise ValueError('--config is required')
        cfg = load_config(args.config.resolve())
        if args.operation == 'backup':
            if not args.directory:
                raise ValueError('--directory is required')
            backup_repository(args.directory.resolve(), cfg, args.apply_retention)
        elif args.operation == 'restore':
            if not args.destination or not args.snapshot:
                raise ValueError('--destination and --snapshot are required')
            restore_repository(cfg, args.snapshot, args.destination.absolute(), args.port, args.home_root)
        elif args.operation == 'retention':
            if not args.instance:
                raise ValueError('--instance is required')
            print(retention(cfg, args.instance, args.apply))
        elif args.operation == 'init':
            restic(cfg, 'init')
            print('Encrypted repository initialized.')
        elif args.operation == 'check':
            restic(cfg, 'check', '--read-data')
            print('Repository metadata and stored data verified.')
        else:
            print(restic(cfg, 'snapshots', '--json', '--tag', TAG))
    except (ValueError, KeyError, OSError, RuntimeError, StopIteration, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Recovery refused or failed: {exc}\n')


if __name__ == '__main__':
    main()
