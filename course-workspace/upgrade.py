#!/usr/bin/env python3
"""Review and apply stopped institutional image upgrades after a full canary.

Never starts, pulls, builds, or rolls back a runtime. Recovery uses the preserved
exact encrypted snapshot into a fresh destination with old images, saved files,
and fresh Hub sessions. Archived Hub data remains encrypted historical context.
"""
import argparse
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from functools import wraps

from course_manifest import _read_regular, _unique_object, _image
from deployment import preflight, docker_json, validate_institutional, require_active_course
from maintenance import (safe_directory, environment as maintenance_environment, home_lock, snapshot, stopped,
                         write, sync_directory, ROOT_PENDING, PENDING as MAINTENANCE_PENDING, digest)
from pilot import locked
from recovery import load_config, backup_repository_locked, restic, TAG
from rehearsal import evidence

VERSION = 'educloud-upgrade-v1'
PENDING = '.upgrade-pending.json'
RECEIPT = '.upgrade-receipt.json'
MAX_JSON = 64 * 1024
MAX_ENV_BYTES = 256 * 1024
MAX_JOURNAL_BYTES = 2 * MAX_ENV_BYTES + 2 * MAX_JSON
MAX_ENTRIES = 200000
HASH = re.compile(r'[0-9a-f]{64}\Z')
IMAGE = re.compile(r'sha256:[0-9a-f]{64}\Z')
PLAN_FIELDS = {'version', 'tool_sha256', 'bindings', 'counts', 'old_images', 'new_images', 'snapshot_id', 'digest'}
BINDINGS = {'directory_sha256', 'env_sha256', 'policy_sha256', 'files_sha256', 'canary_sha256',
            'canary_path_sha256', 'recovery_config_sha256', 'recovery_config_path_sha256',
            'backup_password_sha256', 'backup_password_path_sha256', 'source_manifest_sha256'}


class UpgradeError(ValueError):
    """Only fixed public codes may cross this boundary."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def guarded(function):
    @wraps(function)
    def run(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except UpgradeError:
            raise
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, OverflowError,
                UnicodeError, RecursionError, subprocess.SubprocessError):
            raise UpgradeError('upgrade_refused_or_incomplete') from None
    return run


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def path_hash(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise UpgradeError('canonical_path_required')
    return sha(os.fsencode(path))


def read_json(path, private=True, limit=MAX_JSON):
    raw = _read_regular(Path(path), limit, private=private)
    value = json.loads(raw, object_pairs_hook=_unique_object,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if not isinstance(value, dict):
        raise UpgradeError('invalid_record')
    return value, raw


def tool_hash():
    here = Path(__file__).resolve().parent
    return digest({name: sha((here / name).read_bytes()) for name in
                   ('upgrade.py', 'maintenance.py', 'deployment.py', 'recovery.py', 'course_manifest.py', 'rehearsal/evidence.py',
                    'hub/settings.py', 'homes.py', 'backup.py', 'checkpoint.py', 'volume_archive.py', 'pilot.py', 'health.py', 'public_course.py', 'allocation.py')})


def environment(directory):
    """Resume-only reader: upgrade journal blocks public lifecycle, not this path."""
    safe_directory(directory)
    require_active_course(directory)
    pending = directory / '.retirement-pending.json'
    if pending.exists() or pending.is_symlink():
        raise UpgradeError('unfinished_retirement')
    if (directory / 'pilot.env').exists() or (directory / 'pilot.env').is_symlink():
        raise UpgradeError('institutional_deployment_required')
    raw = _read_regular(directory / '.env', MAX_ENV_BYTES, private=True).decode('utf-8')
    env = {}
    for line in raw.splitlines():
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if (not separator or not re.fullmatch(r'WORKSPACE_[A-Z_]+', key) or key in env
                or any(char.isspace() for char in value) or any(char in value for char in ('$', '"', "'", '#', '\\', '\x00'))):
            raise UpgradeError('private_environment_invalid')
        env[key] = value
    validate_institutional(env)
    if env.get('WORKSPACE_ENV_FILE') != str(directory / '.env'):
        raise UpgradeError('environment_deployment_mismatch')
    return env


def plan_destination(plan_path, protected=()):
    path_hash(plan_path)
    reserved = {'manifest.json', 'source-manifest.json', 'validation.json', 'recovery.json', 'health.json',
                'keycloak-client.json', 'cairn-binding.json', 'operation.lock', 'pilot.env', 'nginx.conf',
                'recovery-status.json'}
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,100}\.json', plan_path.name)
            or plan_path.name in reserved or plan_path.exists() or plan_path.is_symlink()
            or not plan_path.parent.is_dir()):
        raise UpgradeError('new_private_plan_required')
    metadata = plan_path.parent.stat()
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise UpgradeError('new_private_plan_required')
    if any(plan_path == Path(root) or Path(root) in plan_path.parents for root in protected):
        raise UpgradeError('plan_overlaps_preserved_state')


def no_pending(directory, root):
    if any(path.exists() or path.is_symlink() for path in
           (directory / PENDING, directory / MAINTENANCE_PENDING, root / ROOT_PENDING)):
        raise UpgradeError('unfinished_operation')


def candidate_image(reference, architecture):
    if not _image(reference):
        raise UpgradeError('candidate_image_must_be_immutable')
    rows = docker_json('image', 'inspect', reference)
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise UpgradeError('candidate_image_missing')
    image = rows[0]
    if (image.get('Os') != 'linux' or image.get('Architecture') != architecture
            or not isinstance(image.get('Id'), str) or not IMAGE.fullmatch(image['Id'])):
        raise UpgradeError('candidate_image_incompatible')
    if reference.startswith('sha256:') and image['Id'] != reference:
        raise UpgradeError('candidate_image_identity_changed')
    return image['Id']


def worker_architecture():
    info = docker_json('info', '--format', '{{json .}}')
    if not isinstance(info, dict) or info.get('OSType') != 'linux' or not isinstance(info.get('Architecture'), str):
        raise UpgradeError('native_worker_required')
    return {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(info['Architecture'], info['Architecture'])


def canary(path, images):
    value, raw = read_json(path, private=False)
    required = {'version', 'status', 'scope', 'cleanup_verified', 'provenance', 'phases', 'started_at',
                'finished_at', 'duration_seconds', 'checks', 'metrics', 'course_manifest_sha256'}
    if (set(value) != required or type(value['version']) is not int or value['version'] != 4
            or value['status'] != 'PASS' or value['scope'] != 'full-linux' or value['cleanup_verified'] is not True):
        raise UpgradeError('full_canary_required')
    p = value['provenance']
    evidence._validate_provenance(p)
    here = Path(__file__).resolve().parent / 'rehearsal'
    expected_fixtures = {name: evidence.sha256_file(here / name) for name in evidence.FIXTURE_FILES}
    if p['fixture_files_sha256'] != expected_fixtures:
        raise UpgradeError('canary_fixture_version_changed')
    if (p['fixture_version'] != 'course-pilot-v4' or p['hardware']['os'] != 'Linux'
            or p['cairn']['dirty'] is not False or p['waypoint']['dirty'] is not False
            or set(p['image_ids']) != evidence.IMAGE_ROLES):
        raise UpgradeError('clean_canary_required')
    if any(p['image_ids'][role] != images[role] for role in ('hub', 'course')):
        raise UpgradeError('candidate_canary_mismatch')
    if (not isinstance(value['phases'], dict) or set(value['phases']) != evidence.REQUIRED_PHASES['full-linux']
            or len(value['phases']) != 15 or any(status is not True for status in value['phases'].values())):
        raise UpgradeError('complete_canary_required')
    if not isinstance(value['course_manifest_sha256'], str) or not HASH.fullmatch(value['course_manifest_sha256']):
        raise UpgradeError('canary_manifest_missing')
    start, finish = evidence._timestamp(value['started_at']), evidence._timestamp(value['finished_at'])
    duration = value['duration_seconds']
    if type(duration) not in (int, float) or not 0 <= duration <= 7 * 86400 or not math.isfinite(duration) or finish < start:
        raise UpgradeError('invalid_canary_timing')
    checks, metrics = value['checks'], value['metrics']
    if (not isinstance(checks, list) or not checks or len(checks) > 200
            or any(not isinstance(check, str) or not evidence.SLUG.fullmatch(check) for check in checks)
            or not isinstance(metrics, dict) or len(metrics) > 200
            or any(not isinstance(key, str) or not evidence.SLUG.fullmatch(key)
                   or type(number) not in (int, float) or not 0 <= number <= 10**18 or not math.isfinite(number)
                   for key, number in metrics.items())):
        raise UpgradeError('invalid_canary_metrics')
    if metrics.get('model_calls') != 0 or type(metrics.get('model_calls')) is not int:
        raise UpgradeError('synthetic_no_model_canary_required')
    if any(number != 0 for key, number in metrics.items() if key.endswith('_errors') or key.endswith('_failures')):
        raise UpgradeError('canary_errors_present')
    return sha(raw)


def recovery_inputs(path):
    cfg, raw = read_json(path)
    # load_config also checks the backend and retention contract. Precheck the
    # referenced password with no-follow/O_NONBLOCK before its legacy reader.
    if not isinstance(cfg.get('password_file'), str):
        raise UpgradeError('backup_configuration_invalid')
    password = _read_regular(Path(cfg['password_file']), 4096, private=True)
    captured = dict(cfg)
    for key, default in [('keep_last', 7), ('keep_daily', 7), ('keep_weekly', 4)]:
        captured.setdefault(key, default)
    cfg = load_config(Path(path))
    if (cfg != captured or _read_regular(Path(path), MAX_JSON, private=True) != raw
            or _read_regular(Path(captured['password_file']), 4096, private=True) != password):
        raise UpgradeError('backup_configuration_changed_during_read')
    return cfg, {'recovery_config_sha256': sha(raw), 'recovery_config_path_sha256': path_hash(path),
                 'backup_password_sha256': sha(password), 'backup_password_path_sha256': path_hash(cfg['password_file'])}


def hub_data(env, old_images):
    instance = env['WORKSPACE_INSTANCE']
    rows = docker_json('container', 'inspect', instance + '-hub')
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise UpgradeError('owned_stopped_hub_required')
    container = rows[0]
    labels = container.get('Config', {}).get('Labels', {}) or {}
    if (container.get('Name') != '/' + instance + '-hub' or container.get('State', {}).get('Running') is not False
            or labels.get('educloud.workspace.instance') != instance or labels.get('educloud.workspace.kind') != 'hub'
            or container.get('Image') != old_images['hub']):
        raise UpgradeError('owned_stopped_hub_required')
    name = instance + '-hub-data'
    if not any(mount.get('Type') == 'volume' and mount.get('Name') == name and mount.get('Destination') == '/data'
               for mount in container.get('Mounts', []) if isinstance(mount, dict)):
        raise UpgradeError('hub_data_binding_changed')
    # All live users of Hub data matter, including containers with foreign labels.
    from backup import docker
    if docker('ps', '-q', '--filter', 'volume=' + name):
        raise UpgradeError('live_hub_data_writer')
    rows = docker_json('volume', 'inspect', name)
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise UpgradeError('hub_volume_missing')
    volume = rows[0]
    labels = volume.get('Labels') or {}
    mount = volume.get('Mountpoint')
    if (volume.get('Driver') != 'local' or volume.get('Options') not in (None, {})
            or labels.get('educloud.workspace.instance') != instance or labels.get('educloud.workspace.kind') != 'hub'
            or not isinstance(mount, str)):
        raise UpgradeError('hub_volume_ownership_changed')
    path = Path(mount)
    if not path.is_absolute() or path.resolve() != path or path.is_symlink() or not path.is_dir():
        raise UpgradeError('hub_volume_path_changed')
    return path


def tree_stamp(roots):
    """No-follow metadata stamp detects changed saved files without publishing names.

    Includes inode, size, mode, nanosecond mtime and ctime, and link targets. A
    same-size edit with restored mtime still changes ctime. No atime is included:
    the read-only encrypted checkpoint itself may update access time.
    """
    records = []
    def walk(directory, relative, descriptor, depth=0):
        if depth > 128:
            raise UpgradeError('saved_tree_depth_exceeded')
        names = []
        with os.scandir(descriptor) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(records) + len(names) > MAX_ENTRIES:
                    raise UpgradeError('saved_tree_budget_exceeded')
        for name in sorted(names):
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            key = relative + '/' + name
            if len(os.fsencode(key)) > 4096:
                raise UpgradeError('saved_tree_path_budget_exceeded')
            record = [key, info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
            if stat.S_ISLNK(info.st_mode):
                record.append(os.readlink(name, dir_fd=descriptor))
            elif not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise UpgradeError('saved_tree_special_file')
            records.append(record)
            if len(records) > MAX_ENTRIES:
                raise UpgradeError('saved_tree_budget_exceeded')
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                try:
                    walk(directory, key, child, depth + 1)
                finally:
                    os.close(child)
    for index, root in enumerate(roots):
        root = Path(root)
        if not root.is_absolute() or root.resolve() != root or root.is_symlink():
            raise UpgradeError('saved_tree_path_changed')
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            records.append([str(index), info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns])
            walk(root, str(index), fd)
        finally:
            os.close(fd)
    return {'sha256': digest(records), 'entry_count': len(records)}


def state(directory, env, old_images):
    current = snapshot(directory, env)
    source_manifest = directory / 'manifest.json'
    if not source_manifest.exists() and not source_manifest.is_symlink():
        source_manifest = directory / 'source-manifest.json'
    manifest_hash = sha(b'no-source-manifest')
    if source_manifest.exists() or source_manifest.is_symlink():
        manifest_hash = sha(_read_regular(source_manifest, 256 * 1024, private=True))
    stopped(env['WORKSPACE_INSTANCE'], current['homes'], False)
    hub = hub_data(env, old_images)
    root = Path(env['WORKSPACE_HOME_ROOT'])
    files = tree_stamp([root / name for name in sorted(current['homes'])] + [hub])
    return {'env_sha256': digest(env), 'policy_sha256': digest(current), 'files_sha256': files['sha256'], 'source_manifest_sha256': manifest_hash}, {
        'retained_homes': len(current['homes']), 'file_entries': files['entry_count']}


def backup_exists(cfg, snapshot_id, instance):
    raw = restic(cfg, 'snapshots', '--json', snapshot_id)
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_JSON:
        raise UpgradeError('preserved_backup_metadata_invalid')
    rows = json.loads(raw)
    if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict) or rows[0].get('id') != snapshot_id
            or rows[0].get('hostname') != instance or TAG not in rows[0].get('tags', [])
            or 'instance=' + instance not in rows[0].get('tags', [])):
        raise UpgradeError('preserved_backup_mismatch')


def load_plan(plan_path):
    plan, _ = read_json(plan_path)
    if (set(plan) != PLAN_FIELDS or plan.get('version') != VERSION
            or not isinstance(plan.get('digest'), str) or plan['digest'] != digest({key: value for key, value in plan.items() if key != 'digest'})
            or not isinstance(plan['bindings'], dict) or set(plan['bindings']) != BINDINGS
            or any(not isinstance(value, str) or not HASH.fullmatch(value) for value in plan['bindings'].values())
            or not isinstance(plan['tool_sha256'], str) or not HASH.fullmatch(plan['tool_sha256'])
            or not isinstance(plan['snapshot_id'], str) or not HASH.fullmatch(plan['snapshot_id'])
            or not isinstance(plan['counts'], dict) or set(plan['counts']) != {'retained_homes', 'file_entries'}
            or any(type(value) is not int or not 1 <= value <= MAX_ENTRIES for value in plan['counts'].values())):
        raise UpgradeError('plan_digest_or_schema_mismatch')
    for key in ('old_images', 'new_images'):
        if (not isinstance(plan[key], dict) or set(plan[key]) != {'hub', 'course'}
                or any(not isinstance(value, str) or not IMAGE.fullmatch(value) for value in plan[key].values())):
            raise UpgradeError('plan_image_schema_mismatch')
    return plan


@guarded
def preview(directory, hub_image, course_image, canary_path, recovery_config_path, plan_path):
    directory, plan_path = Path(directory), Path(plan_path)
    safe_directory(directory)
    plan_destination(plan_path)
    with locked(directory):
        env = maintenance_environment(directory)
        root = Path(env['WORKSPACE_HOME_ROOT'])
        with home_lock(root):
            no_pending(directory, root)
            old = preflight(directory)['images']
            architecture = worker_architecture()
            new = {'hub': candidate_image(hub_image, architecture), 'course': candidate_image(course_image, architecture)}
            if new == old:
                raise UpgradeError('candidate_images_unchanged')
            canary_hash = canary(canary_path, new)
            cfg, recovery_bindings = recovery_inputs(recovery_config_path)
            protected = [root, hub_data(env, old)]
            if cfg['repository'].startswith('/'):
                protected.append(Path(cfg['repository']))
            plan_destination(plan_path, protected)
            before, counts = state(directory, env, old)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                snapshot_id = backup_repository_locked(directory, cfg, quiet=True)
            if not isinstance(snapshot_id, str) or not HASH.fullmatch(snapshot_id):
                raise UpgradeError('preserved_backup_missing')
            after, after_counts = state(directory, environment(directory), old)
            _, frozen_recovery = recovery_inputs(recovery_config_path)
            if (before != after or counts != after_counts or frozen_recovery != recovery_bindings
                    or canary(canary_path, new) != canary_hash):
                raise UpgradeError('state_changed_during_backup')
            backup_exists(cfg, snapshot_id, env['WORKSPACE_INSTANCE'])
            bindings = {'directory_sha256': path_hash(directory), 'canary_sha256': canary_hash,
                        'canary_path_sha256': path_hash(canary_path), **recovery_bindings, **after}
            plan = {'version': VERSION, 'tool_sha256': tool_hash(), 'bindings': bindings, 'counts': counts,
                    'old_images': old, 'new_images': new, 'snapshot_id': snapshot_id}
            plan['digest'] = digest(plan)
            write(plan_path, plan, exclusive=True)
            return plan


@guarded
def apply(directory, plan_path, canary_path, recovery_config_path):
    directory, plan_path = Path(directory), Path(plan_path)
    safe_directory(directory)
    plan = load_plan(plan_path)
    if plan['tool_sha256'] != tool_hash() or plan['bindings']['directory_sha256'] != path_hash(directory):
        raise UpgradeError('plan_tool_or_deployment_mismatch')
    with locked(directory):
        env = environment(directory)
        root = Path(env['WORKSPACE_HOME_ROOT'])
        pending = directory / PENDING
        with home_lock(root):
            if any(path.exists() or path.is_symlink() for path in (directory / MAINTENANCE_PENDING, root / ROOT_PENDING)):
                raise UpgradeError('unfinished_maintenance')
            cfg, recovery_bindings = recovery_inputs(recovery_config_path)
            if any(plan['bindings'][key] != value for key, value in recovery_bindings.items()):
                raise UpgradeError('backup_configuration_changed')
            if (plan['bindings']['canary_path_sha256'] != path_hash(canary_path)
                    or plan['bindings']['canary_sha256'] != canary(canary_path, plan['new_images'])):
                raise UpgradeError('canary_changed')
            architecture = worker_architecture()
            if any(candidate_image(value, architecture) != value for value in plan['new_images'].values()):
                raise UpgradeError('candidate_images_changed')
            backup_exists(cfg, plan['snapshot_id'], env['WORKSPACE_INSTANCE'])
            if pending.exists() or pending.is_symlink():
                journal, _ = read_json(pending, limit=MAX_JOURNAL_BYTES)
                if (set(journal) != {'version', 'plan', 'old_env', 'new_env'} or journal.get('version') != VERSION
                        or journal.get('plan') != plan or not isinstance(journal.get('old_env'), dict)
                        or not isinstance(journal.get('new_env'), dict)):
                    raise UpgradeError('resume_requires_exact_plan')
                old_env, new_env = journal['old_env'], journal['new_env']
                expected = dict(old_env, WORKSPACE_HUB_IMAGE=plan['new_images']['hub'], WORKSPACE_IMAGE=plan['new_images']['course'])
                if new_env != expected or digest(old_env) != plan['bindings']['env_sha256'] or env not in (old_env, new_env):
                    raise UpgradeError('pending_environment_changed')
                # snapshot policy includes env: compare policy with the exact old
                # environment after verifying the actual current environment above.
                observed, counts = state(directory, old_env, plan['old_images'])
                if observed != {key: plan['bindings'][key] for key in ('env_sha256', 'policy_sha256', 'files_sha256', 'source_manifest_sha256')} or counts != plan['counts']:
                    raise UpgradeError('pending_saved_state_changed')
            else:
                old_env = env
                current = preflight(directory)['images']
                observed, counts = state(directory, env, plan['old_images'])
                if (current != plan['old_images'] or observed != {key: plan['bindings'][key] for key in ('env_sha256', 'policy_sha256', 'files_sha256', 'source_manifest_sha256')}
                        or counts != plan['counts']):
                    raise UpgradeError('stale_upgrade_plan')
                new_env = dict(env, WORKSPACE_HUB_IMAGE=plan['new_images']['hub'], WORKSPACE_IMAGE=plan['new_images']['course'])
                journal = {'version': VERSION, 'plan': plan, 'old_env': old_env, 'new_env': new_env}
                if len((json.dumps(journal, indent=2, sort_keys=True) + '\n').encode()) > MAX_JOURNAL_BYTES:
                    raise UpgradeError('pending_journal_budget_exceeded')
                write(pending, journal, exclusive=True)
            write(directory / '.env', ''.join(f'{key}={value}\n' for key, value in new_env.items()).encode())
            result = preflight(directory, allow_pending=True)
            if result['images'] != plan['new_images']:
                raise UpgradeError('candidate_preflight_mismatch')
            observed, counts = state(directory, old_env, plan['old_images'])
            if observed != {key: plan['bindings'][key] for key in ('env_sha256', 'policy_sha256', 'files_sha256', 'source_manifest_sha256')} or counts != plan['counts']:
                raise UpgradeError('saved_state_changed_during_apply')
            receipt = {'version': VERSION, 'status': 'APPLIED', 'plan_digest': plan['digest'],
                       'snapshot_id': plan['snapshot_id'], 'images': plan['new_images'], 'counts': counts,
                       'scope': 'stopped-image-configuration', 'deployment_started': False}
            write(directory / RECEIPT, receipt)
            pending.unlink()
            sync_directory(directory)
            return receipt


resume = apply


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('preview', 'apply', 'resume'))
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--canary', type=Path, required=True)
    parser.add_argument('--recovery-config', type=Path, required=True)
    parser.add_argument('--hub-image')
    parser.add_argument('--course-image')
    args = parser.parse_args(argv)
    try:
        if args.operation == 'preview':
            plan = preview(args.directory, args.hub_image, args.course_image, args.canary, args.recovery_config, args.plan)
            result = {'version': VERSION, 'status': 'PREVIEWED', 'plan_digest': plan['digest'],
                      'old_images': plan['old_images'], 'new_images': plan['new_images'],
                      'counts': plan['counts'], 'snapshot_id': plan['snapshot_id'],
                      'scope': 'stopped-image-configuration', 'deployment_started': False}
        elif args.hub_image is not None or args.course_image is not None:
            raise UpgradeError('apply_uses_reviewed_images_only')
        else:
            result = apply(args.directory, args.plan, args.canary, args.recovery_config)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except UpgradeError as exc:
        print(json.dumps({'version': VERSION, 'status': 'REFUSED_OR_INCOMPLETE', 'code': exc.code,
                          'deployment_started': False, 'instruction': 'Keep the deployment stopped. Resume the exact pending plan, or recover the preserved snapshot into a fresh destination.'}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
