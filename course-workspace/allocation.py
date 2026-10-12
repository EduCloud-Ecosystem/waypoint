#!/usr/bin/env python3
"""Explicit worker RAM/CPU reservations and stopped-course release.

Reservations survive stopped or uncertain startup. Released claims retain identity
and storage tombstones; this module never deletes learner work or transfers it.
"""
import argparse
from contextlib import contextmanager
from functools import wraps
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import subprocess
import tempfile
from urllib.parse import urlsplit
import uuid

VERSION = 'educloud-worker-allocation-v1'
MARKER = '.worker-allocation.json'
REQUIRED = '.worker-allocation-required.json'
AUTHORITY = Path('/var/lib/educloud/worker-allocation')
ROOT_UID = 0
MAX_JSON = 4 * 1024**2
HASH = re.compile(r'[a-f0-9]{64}\Z')
POLICY_FIELDS = {'version','worker_id','memory_budget_mb','cpu_budget','host_reserve_mb','host_reserve_cpu','state_directory','owner'}
MARKER_FIELDS = {'version','status','config_file','policy_sha256','worker_id','daemon_sha256','claim_sha256','directory_sha256'}
CLAIM_FIELDS = {'directory','instance','port','origin','dns_subtree','home_root','home_namespace','home_filesystem_uuid','issuer','client_id','active_limit','learner_memory_mb','learner_cpu','memory_mb','cpu'}
BINDING_FIELDS = {'version','config_file','policy_sha256','worker_id','daemon_sha256','state_directory'}


class AllocationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def guarded(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except AllocationError:
            raise
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, UnicodeError,
                OverflowError, RecursionError, subprocess.SubprocessError):
            raise AllocationError('allocation_refused_or_incomplete') from None
    return run


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def path_digest(path):
    return hashlib.sha256(os.fsencode(path)).hexdigest()


def exists(path):
    return path.exists() or path.is_symlink()


def canonical(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or len(str(path)) > 4096:
        raise AllocationError('canonical_path_required')
    return path


def private_directory(path):
    canonical(path)
    value = path.lstat()
    if not stat.S_ISDIR(value.st_mode) or value.st_uid != ROOT_UID or value.st_mode & 0o077:
        raise AllocationError('private_root_directory_required')


def root_required():
    if os.geteuid() != ROOT_UID:
        raise AllocationError('root_operator_required')


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AllocationError('duplicate_record_key')
        result[key] = value
    return result


def read(path, limit=MAX_JSON):
    canonical(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        value = os.fstat(source.fileno())
        if not stat.S_ISREG(value.st_mode) or value.st_uid != ROOT_UID or value.st_mode & 0o077 or value.st_size > limit:
            raise AllocationError('private_root_record_required')
        raw = source.read(limit + 1)
        if len(raw) > limit:
            raise AllocationError('record_limit_exceeded')
    result = json.loads(raw, object_pairs_hook=unique,
                        parse_constant=lambda _: (_ for _ in ()).throw(AllocationError('nonfinite_record')))
    if not isinstance(result, dict):
        raise AllocationError('invalid_record')
    return result


def write(path, value):
    private_directory(path.parent)
    if exists(path):
        read(path)
    raw = (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
    if len(raw) > MAX_JSON:
        raise AllocationError('record_limit_exceeded')
    fd, name = tempfile.mkstemp(prefix='.allocation-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        sync(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def number(value, maximum):
    return type(value) in (int, float) and math.isfinite(value) and 0 < value <= maximum


def policy(config_path):
    path = canonical(config_path)
    value = read(path, 64 * 1024)
    if (set(value) != POLICY_FIELDS or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['worker_id'], str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', value['worker_id'])
            or not isinstance(value['owner'], str) or not 1 <= len(value['owner']) <= 256
            or any(ord(c) < 32 for c in value['owner'])
            or any(type(value[k]) is not int or not 1 <= value[k] <= 10**9 for k in ('memory_budget_mb','host_reserve_mb'))
            or not all(number(value[k], 100000) for k in ('cpu_budget','host_reserve_cpu'))
            or not isinstance(value['state_directory'], str)):
        raise AllocationError('invalid_worker_policy')
    state = canonical(value['state_directory'])
    if state == AUTHORITY or AUTHORITY in state.parents or state in AUTHORITY.parents:
        raise AllocationError('state_overlaps_authority')
    if path == state or state in path.parents:
        raise AllocationError('policy_overlaps_state')
    return value, digest(value)


def daemon():
    from deployment import require_native_worker, docker_json
    require_native_worker()
    info = docker_json('info', '--format', '{{json .}}')
    if (not isinstance(info, dict) or info.get('OSType') != 'linux'
            or not isinstance(info.get('ID'), str) or not 1 <= len(info['ID']) <= 256
            or any(ord(c) < 33 or ord(c) > 126 for c in info['ID'])
            or type(info.get('NCPU')) is not int or info['NCPU'] < 2
            or type(info.get('MemTotal')) is not int or info['MemTotal'] <= 0):
        raise AllocationError('worker_identity_or_capacity_invalid')
    return info, digest({'endpoint':'unix:///var/run/docker.sock', 'id':info['ID']})


def check_capacity(p, info, claims):
    if (p['memory_budget_mb'] + p['host_reserve_mb']) * 1024**2 > info['MemTotal'] or p['cpu_budget'] + p['host_reserve_cpu'] > info['NCPU']:
        raise AllocationError('policy_exceeds_worker_capacity')
    active = [entry['claim'] for entry in claims.values() if entry['status'] == 'ACTIVE']
    if sum(c['memory_mb'] for c in active) > p['memory_budget_mb'] or math.fsum(c['cpu'] for c in active) > p['cpu_budget']:
        raise AllocationError('worker_reservations_exceed_budget')


def binding(config_path, p, ph, dh):
    return {'version':VERSION,'config_file':str(config_path),'policy_sha256':ph,'worker_id':p['worker_id'],
            'daemon_sha256':dh,'state_directory':p['state_directory']}


@contextmanager
def worker_lock():
    root_required()
    parent = AUTHORITY.parent
    canonical(parent)
    if not exists(parent):
        parent.mkdir(mode=0o700)
        sync(parent.parent)
    private_directory(parent)
    path = parent / '.worker-allocation.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, 'a') as lock:
        value = os.fstat(lock.fileno())
        if not stat.S_ISREG(value.st_mode) or value.st_uid != ROOT_UID or value.st_mode & 0o077 or value.st_nlink != 1:
            raise AllocationError('worker_lock_invalid')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AllocationError('worker_operation_in_progress') from None
        yield


def ledger(expected, initialize=False):
    state = Path(expected['state_directory'])
    authority_record = AUTHORITY / 'authority.json'
    if exists(AUTHORITY):
        private_directory(AUTHORITY)
        if read(authority_record) != expected:
            raise AllocationError('worker_authority_binding_changed')
    elif initialize:
        if not exists(state):
            state.mkdir(mode=0o700)
            sync(state.parent)
        private_directory(state)
        if exists(state / 'ledger.json'):
            value = read(state / 'ledger.json')
            if value != dict(expected, claims={}):
                raise AllocationError('unbound_state_not_empty')
        else:
            write(state / 'ledger.json', dict(expected, claims={}))
        temporary = Path(tempfile.mkdtemp(prefix='.allocation-authority-', dir=AUTHORITY.parent))
        try:
            write(temporary / 'authority.json', expected)
            os.rename(temporary, AUTHORITY)
            sync(AUTHORITY.parent)
        finally:
            if temporary.exists():
                (temporary / 'authority.json').unlink(missing_ok=True)
                temporary.rmdir()
    else:
        raise AllocationError('worker_authority_missing')
    private_directory(state)
    value = read(state / 'ledger.json')
    if set(value) != BINDING_FIELDS | {'claims'} or any(value[k] != expected[k] for k in BINDING_FIELDS):
        raise AllocationError('worker_ledger_binding_changed')
    if not isinstance(value['claims'], dict) or len(value['claims']) > 1024:
        raise AllocationError('worker_ledger_invalid')
    for key, entry in value['claims'].items():
        if (not HASH.fullmatch(key) or not isinstance(entry, dict) or set(entry) != {'status','claim','claim_sha256'}
                or entry['status'] not in ('ACTIVE','RELEASED') or not valid_claim(entry['claim'])
                or key != path_digest(entry['claim']['directory']) or entry['claim_sha256'] != digest(entry['claim'])):
            raise AllocationError('worker_ledger_invalid')
    entries = list(value['claims'].items())
    for index, (_, left) in enumerate(entries):
        for _, right in entries[index + 1:]:
            if collides(left['claim'], right['claim']):
                raise AllocationError('worker_ledger_collision')
    return value


def overlap(left, right):
    a, b = Path(left), Path(right)
    return a == b or a in b.parents or b in a.parents


def dns_overlap(a, b):
    return a == b or a.endswith('.' + b) or b.endswith('.' + a)


def valid_claim(c):
    if not isinstance(c, dict) or set(c) != CLAIM_FIELDS:
        return False
    try:
        for k in ('directory','home_root'):
            canonical(c[k])
        if (not isinstance(c['home_filesystem_uuid'],str)
                or str(uuid.UUID(c['home_filesystem_uuid'])) != c['home_filesystem_uuid']
                or uuid.UUID(c['home_filesystem_uuid']).int == 0):
            return False
        for k in ('instance','home_namespace'):
            if not isinstance(c[k], str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', c[k]):
                return False
        parsed = urlsplit(c['origin'])
        if (parsed.scheme != 'https' or parsed.hostname != c['dns_subtree'] or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path or canonical_url(c['origin']) != c['origin']):
            return False
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', c['dns_subtree']) or '..' in c['dns_subtree']:
            return False
        if not re.fullmatch(r'https://[^/]+/realms/[A-Za-z0-9_-]+', c['issuer']) or canonical_url(c['issuer']) != c['issuer']:
            return False
        if not isinstance(c['client_id'], str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', c['client_id']):
            return False
        if any(type(c[k]) is not int or c[k] <= 0 for k in ('port','active_limit','learner_memory_mb','memory_mb')) or not 1024 <= c['port'] <= 65535:
            return False
        if not number(c['learner_cpu'], 100000) or not number(c['cpu'], 100000):
            return False
        return (c['instance'] == c['home_namespace'] and c['memory_mb'] == 1024 + c['active_limit'] * c['learner_memory_mb']
                and c['cpu'] == 1 + c['active_limit'] * c['learner_cpu'] and not overlap(c['directory'],c['home_root']))
    except (ValueError, TypeError, KeyError, AllocationError):
        return False


def canonical_url(value):
    parsed = urlsplit(value)
    host = parsed.hostname
    if not host or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AllocationError('course_origin_invalid')
    port = '' if parsed.port in (None, 443) else ':' + str(parsed.port)
    return 'https://' + host.lower() + port + parsed.path.rstrip('/')


def home_filesystem_uuid(root):
    """Bind quota isolation to stable native XFS identity, not reusable paths."""
    result = subprocess.run(['findmnt','--json','--target',str(root),'--output','TARGET,FSTYPE,UUID'],
                            capture_output=True,text=True,timeout=20,check=True)
    if len(result.stdout) > 64 * 1024:
        raise AllocationError('home_filesystem_inventory_limit')
    value = json.loads(result.stdout,object_pairs_hook=unique)
    rows = value.get('filesystems') if isinstance(value,dict) else None
    if (not isinstance(rows,list) or len(rows) != 1 or not isinstance(rows[0],dict)
            or rows[0].get('target') != str(canonical(root))
            or rows[0].get('fstype') != 'xfs' or not isinstance(rows[0].get('uuid'),str)):
        raise AllocationError('dedicated_xfs_identity_required')
    try:
        identity = str(uuid.UUID(rows[0]['uuid']))
    except ValueError:
        raise AllocationError('dedicated_xfs_identity_required') from None
    if identity == str(uuid.UUID(int=0)):
        raise AllocationError('dedicated_xfs_identity_required')
    return identity


def claim(directory, env):
    from deployment import validate_institutional
    cfg = validate_institutional(env)
    canonical(directory)
    home = canonical(cfg['home_root'])
    origin = canonical_url(cfg['public_url'])
    value = {'directory':str(directory),'instance':cfg['instance'],'port':int(env['WORKSPACE_PORT']),
             'origin':origin,'dns_subtree':urlsplit(origin).hostname,'home_root':str(home),'home_namespace':cfg['instance'],'home_filesystem_uuid':home_filesystem_uuid(home),
             'issuer':canonical_url(cfg['issuer']),'client_id':cfg['client_id'],'active_limit':cfg['active_limit'],
             'learner_memory_mb':cfg['memory_mb'],'learner_cpu':cfg['cpu'],
             'memory_mb':1024 + cfg['active_limit'] * cfg['memory_mb'],'cpu':1 + cfg['active_limit'] * cfg['cpu']}
    if not valid_claim(value):
        raise AllocationError('invalid_course_claim')
    return value


def collides(a, b):
    return (overlap(a['directory'], b['directory']) or a['instance'] == b['instance'] or a['port'] == b['port']
            or dns_overlap(a['dns_subtree'], b['dns_subtree']) or overlap(a['home_root'], b['home_root'])
            or a['home_namespace'] == b['home_namespace'] or a['home_filesystem_uuid'] == b['home_filesystem_uuid'] or (a['issuer'], a['client_id']) == (b['issuer'], b['client_id'])
            or overlap(a['directory'], b['home_root']) or overlap(a['home_root'], b['directory']))


def read_marker(directory):
    value = read(directory / MARKER, 64 * 1024)
    if (set(value) != MARKER_FIELDS or value['version'] != VERSION or value['status'] not in ('PENDING','ENROLLED')
            or not isinstance(value['config_file'], str) or not isinstance(value['worker_id'], str)
            or any(not isinstance(value[k], str) or not HASH.fullmatch(value[k]) for k in ('policy_sha256','daemon_sha256','claim_sha256','directory_sha256'))
            or value['directory_sha256'] != path_digest(directory)):
        raise AllocationError('invalid_course_enrollment')
    canonical(value['config_file'])
    return value


def expected_marker(directory, config_path, p, ph, dh, c, status='ENROLLED'):
    return {'version':VERSION,'status':status,'config_file':str(config_path),'policy_sha256':ph,'worker_id':p['worker_id'],
            'daemon_sha256':dh,'claim_sha256':digest(c),'directory_sha256':path_digest(directory)}


def required_gate(directory):
    if exists(directory / REQUIRED):
        if read(directory / REQUIRED, 64 * 1024) != {'version':'educloud-worker-allocation-required-v1','source':'snapshot_restore'}:
            raise AllocationError('invalid_restore_allocation_gate')
        return True
    return False


def current(directory, env, require_active=True):
    from deployment import require_active_course
    canonical(directory)
    if require_active:
        require_active_course(directory)
    if env.get('WORKSPACE_AUTH_MODE') == 'local-test' and (exists(AUTHORITY) or exists(directory / MARKER) or exists(directory / REQUIRED)):
        raise AllocationError('institutional_enrollment_required')
    if not exists(directory / MARKER):
        if exists(AUTHORITY) or exists(directory / REQUIRED):
            raise AllocationError('explicit_worker_enrollment_required')
        return None
    private_directory(directory)
    marker = read_marker(directory)
    if marker['status'] != 'ENROLLED':
        raise AllocationError('unfinished_worker_enrollment')
    if exists(directory / REQUIRED):
        raise AllocationError('unfinished_restore_enrollment')
    p, ph = policy(marker['config_file'])
    info, dh = daemon()
    c = claim(directory, env)
    if marker != expected_marker(directory, marker['config_file'], p, ph, dh, c):
        raise AllocationError('course_enrollment_changed')
    value = ledger(binding(marker['config_file'], p, ph, dh))
    entry = value['claims'].get(path_digest(directory))
    if entry != {'status':'ACTIVE','claim':c,'claim_sha256':digest(c)}:
        raise AllocationError('active_course_reservation_required')
    check_capacity(p, info, value['claims'])
    return p, value, c


def report(p, value):
    active = [e['claim'] for e in value['claims'].values() if e['status'] == 'ACTIVE']
    return {'version':1,'status':'PASS','scope':'worker-allocation-inventory',
            'worker_sha256':digest(p['worker_id']),'policy_sha256':value['policy_sha256'],'daemon_sha256':value['daemon_sha256'],
            'active_courses':len(active),'released_courses':len(value['claims'])-len(active),
            'memory_reserved_mb':sum(c['memory_mb'] for c in active),'cpu_reserved':math.fsum(c['cpu'] for c in active),
            **{k:p[k] for k in ('memory_budget_mb','cpu_budget','host_reserve_mb','host_reserve_cpu')}}


def running_containers():
    """Inspect every running container, including unlabelled home volume writers."""
    from deployment import docker_json
    # docker ps JSON is line-delimited; inspect one bounded ID list from Docker.
    result = subprocess.run(['docker','ps','-q','--no-trunc'], capture_output=True, text=True, timeout=20, check=True)
    if len(result.stdout) > 1024 * 1024:
        raise AllocationError('container_inventory_limit')
    ids = result.stdout.splitlines()
    if any(not re.fullmatch(r'[a-f0-9]{64}', key) for key in ids) or len(ids) > 1024:
        raise AllocationError('container_inventory_invalid')
    if not ids:
        return []
    containers = docker_json('inspect', *ids)
    if not isinstance(containers, list) or len(containers) != len(ids):
        raise AllocationError('container_inventory_invalid')
    return containers


def volume_paths(c):
    """Resolve Docker storage aliases, including local bind-volume device paths.

    A bind mount of a named volume's Docker Mountpoint has no Mounts.Name.
    Conversely a foreign named local volume exposes its device only through
    volume inspection. Preserve both aliases rather than trusting labels.
    """
    from deployment import docker_json
    result = subprocess.run(['docker','volume','ls','-q'],capture_output=True,text=True,timeout=20,check=True)
    if len(result.stdout) > 1024 * 1024:
        raise AllocationError('volume_inventory_limit')
    names = result.stdout.splitlines()
    if (len(names) > 4096 or len(names) != len(set(names))
            or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,255}',name) for name in names)):
        raise AllocationError('volume_inventory_invalid')
    records = {}
    for offset in range(0,len(names),32):
        requested = names[offset:offset+32]
        rows = docker_json('volume','inspect',*requested)
        if not isinstance(rows,list) or len(rows) != len(requested):
            raise AllocationError('volume_inventory_invalid')
        for row in rows:
            if (not isinstance(row,dict) or row.get('Name') not in requested or row['Name'] in records
                    or not isinstance(row.get('Mountpoint'),str) or not row['Mountpoint'].startswith('/')
                    or not isinstance(row.get('Driver'),str) or not isinstance(row.get('Options') or {},dict)):
                raise AllocationError('volume_inventory_invalid')
            options = row.get('Options') or {}
            device = options.get('device','') if row['Driver'] == 'local' else ''
            if not isinstance(device,str):
                raise AllocationError('volume_inventory_invalid')
            records[row['Name']] = {'path':str(Path(row['Mountpoint']).resolve()),
                                    'device':str(Path(device).resolve()) if device.startswith('/') else device}
    home_names = {name for name in records if name.startswith(c['home_namespace']+'-home-')}
    hub_names = {c['instance']+'-hub-data'} & records.keys()
    home_sources = {c['home_root']} | {records[name]['path'] for name in home_names}
    hub_sources = {records[name]['path'] for name in hub_names}
    # Resolve finite alias chains. Every successful pass adds a volume, and the
    # bounded inventory limits passes and work even for cyclic/hostile metadata.
    remaining = set(records) - home_names - hub_names
    while remaining:
        added = set()
        for name in remaining:
            row = records[name]
            if not row['device'].startswith('/'):
                continue
            if any(overlap(row['device'],path) for path in home_sources):
                home_names.add(name);home_sources.add(row['path']);added.add(name)
            elif any(overlap(row['device'],path) for path in hub_sources):
                hub_names.add(name);hub_sources.add(row['path']);added.add(name)
        if not added:
            break
        remaining -= added
    return home_sources,hub_sources,home_names,hub_names


def stopped(c, claims):
    containers = running_containers()
    home_sources,hub_sources,home_names,hub_names = volume_paths(c) if containers else ({c['home_root']},set(),set(),set())
    known = {e['claim']['instance'] for e in claims.values() if e['status'] == 'ACTIVE'}
    for item in containers:
        if not isinstance(item, dict) or not isinstance(item.get('Config'), dict) or not isinstance(item.get('Mounts'), list):
            raise AllocationError('container_inventory_invalid')
        labels = item['Config'].get('Labels') or {}
        if not isinstance(labels, dict) or not isinstance(item.get('Name'), str):
            raise AllocationError('container_inventory_invalid')
        instance = labels.get('educloud.workspace.instance')
        if instance == c['instance'] or item['Name'] == '/' + c['instance'] + '-hub':
            raise AllocationError('stop_course_before_allocation_change')
        if instance and instance not in known:
            raise AllocationError('unmanaged_running_course')
        for mount in item['Mounts']:
            if not isinstance(mount, dict):
                raise AllocationError('container_inventory_invalid')
            name, source = mount.get('Name',''), mount.get('Source','')
            if not isinstance(name,str) or not isinstance(source,str):
                raise AllocationError('container_inventory_invalid')
            if source.startswith('/'):
                source = str(Path(source).resolve())
            if (name == c['instance'] + '-hub-data' or name in hub_names
                    or source.startswith('/') and any(overlap(source,path) for path in hub_sources)):
                raise AllocationError('stop_all_hub_volume_writers')
            if (name.startswith(c['home_namespace']+'-home-') or name in home_names
                    or source.startswith('/') and any(overlap(source,path) for path in home_sources)):
                raise AllocationError('stop_all_home_volume_writers')


def legacy_exclusion(env):
    for item in running_containers():
        if not isinstance(item,dict) or not isinstance(item.get('Config'),dict) or not isinstance(item.get('Name'),str):
            raise AllocationError('container_inventory_invalid')
        labels=item['Config'].get('Labels') or {}
        if not isinstance(labels,dict):
            raise AllocationError('container_inventory_invalid')
        instance=labels.get('educloud.workspace.instance')
        if instance and instance != env.get('WORKSPACE_INSTANCE'):
            raise AllocationError('unmanaged_multiple_courses_refused')


@guarded
def enroll(directory, config_path):
    from deployment import settings, require_active_course, _inspect_course
    from pilot import locked
    directory, config_path = canonical(directory), canonical(config_path)
    root_required()
    private_directory(directory)
    with locked(directory), worker_lock():
        require_active_course(directory)
        env = settings(directory)
        p, ph = policy(config_path)
        if any(overlap(p['state_directory'],str(path)) or overlap(str(config_path),str(path)) for path in (directory, Path(env['WORKSPACE_HOME_ROOT']))):
            raise AllocationError('worker_policy_overlaps_course_state')
        info, dh = daemon()
        _inspect_course(directory)
        c = claim(directory,env)
        check_capacity(p,info,{})
        expected = expected_marker(directory,config_path,p,ph,dh,c)
        value = ledger(binding(config_path,p,ph,dh), initialize=True)
        required_gate(directory)
        if exists(directory / MARKER):
            marker = read_marker(directory)
            if dict(marker,status='ENROLLED') != expected:
                raise AllocationError('course_enrollment_changed')
        stopped(c,value['claims'])
        key = path_digest(directory)
        old = value['claims'].get(key)
        if old and old['claim'] != c:
            raise AllocationError('frozen_course_claim_changed')
        if any(other != key and collides(c,e['claim']) for other,e in value['claims'].items()):
            raise AllocationError('course_identity_or_storage_collision')
        if key not in value['claims'] and len(value['claims']) >= 1024:
            raise AllocationError('worker_course_limit_exceeded')
        value['claims'][key] = {'status':'ACTIVE','claim':c,'claim_sha256':digest(c)}
        check_capacity(p,info,value['claims'])
        write(directory / MARKER,dict(expected,status='PENDING'))
        write(Path(p['state_directory'])/'ledger.json',value)
        write(directory / MARKER,expected)
        # Validate the durable lease before clearing a recovery interruption gate.
        saved = ledger(binding(config_path,p,ph,dh))
        if saved['claims'].get(key) != value['claims'][key] or read_marker(directory) != expected:
            raise AllocationError('durable_lease_verification_failed')
        if exists(directory / REQUIRED):
            (directory / REQUIRED).unlink()
            sync(directory)
        return report(p,saved)


@guarded
def check(directory, env):
    if not exists(AUTHORITY) and not exists(Path(directory)/MARKER) and not exists(Path(directory)/REQUIRED):
        from deployment import require_active_course
        require_active_course(Path(directory))
        legacy_exclusion(env)
        return None
    with worker_lock():
        selected = current(Path(directory),env)
        return report(selected[0],selected[1]) if selected else None


@contextmanager
def admission(directory,env):
    """Hold the daemon-wide lock across Compose and readiness; caller holds course lock."""
    try:
        if sys.platform != 'linux' and env.get('WORKSPACE_AUTH_MODE') == 'local-test' and not exists(AUTHORITY) and not exists(Path(directory)/MARKER) and not exists(Path(directory)/REQUIRED):
            from deployment import require_active_course
            require_active_course(Path(directory))
            legacy_exclusion(env)
            yield
        else:
            with worker_lock():
                selected = current(Path(directory),env)
                if selected is None:
                    legacy_exclusion(env)
                yield
    except AllocationError:
        raise
    except (OSError,ValueError,TypeError,KeyError,RuntimeError,subprocess.SubprocessError):
        raise AllocationError('allocation_refused_or_incomplete') from None


@guarded
def release(directory):
    from deployment import settings
    from pilot import locked
    directory = canonical(directory)
    root_required()
    private_directory(directory)
    with locked(directory), worker_lock():
        env = settings(directory)
        marker = read_marker(directory)
        if marker['status'] != 'ENROLLED' or exists(directory / REQUIRED):
            raise AllocationError('unfinished_worker_enrollment')
        p,ph = policy(marker['config_file'])
        info,dh = daemon()
        c = claim(directory,env)
        if marker != expected_marker(directory,marker['config_file'],p,ph,dh,c):
            raise AllocationError('course_enrollment_changed')
        value = ledger(binding(marker['config_file'],p,ph,dh))
        key = path_digest(directory)
        old = value['claims'].get(key)
        if not old or old['claim'] != c:
            raise AllocationError('course_reservation_missing')
        check_capacity(p,info,value['claims'])
        stopped(c,value['claims'])
        value['claims'][key] = dict(old,status='RELEASED')
        write(Path(p['state_directory'])/'ledger.json',value)
        return report(p,value)


@guarded
def inventory(config_path):
    root_required()
    config_path = canonical(config_path)
    with worker_lock():
        p,ph = policy(config_path)
        info,dh = daemon()
        value = ledger(binding(config_path,p,ph,dh))
        check_capacity(p,info,value['claims'])
        return report(p,value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['enroll','release','inventory'])
    parser.add_argument('--directory',type=Path)
    parser.add_argument('--config',type=Path)
    args = parser.parse_args()
    if args.operation in ('enroll','release') and args.directory is None or args.operation in ('enroll','inventory') and args.config is None:
        parser.error('operation requires --directory and/or --config')
    try:
        if args.operation == 'enroll':
            value = enroll(args.directory,args.config)
        elif args.operation == 'release':
            value = release(args.directory)
        else:
            value = inventory(args.config)
        print(json.dumps(value,sort_keys=True,indent=2))
    except AllocationError as exc:
        parser.exit(1,f'Allocation refused or incomplete: {exc.code}\n')


if __name__ == '__main__':
    main()
