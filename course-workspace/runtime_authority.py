"""Disposable local Docker runtime adapter for the reviewed ownership protocol.

One daemon, explicitly logical workers. This is not a remote provider adapter.
Only the privileged authority/guardian use Docker; Hub receives bounded views.
"""
from contextlib import contextmanager, ExitStack
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
import uuid

from ownership_contract import (Authority, OwnershipError, digest, encoded, profile,
                                logical_identity, worker_evidence)

VERSION = 'educloud-local-runtime-v1'
ENV_KEYS = frozenset({'JUPYTERHUB_API_TOKEN', 'JUPYTERHUB_CLIENT_ID', 'JUPYTERHUB_HOST',
    'JUPYTERHUB_USER', 'JUPYTERHUB_OAUTH_CALLBACK_URL', 'JUPYTERHUB_API_URL',
    'JUPYTERHUB_BASE_URL', 'JUPYTERHUB_SERVICE_PREFIX', 'JUPYTERHUB_SERVICE_URL',
    'JUPYTERHUB_ACTIVITY_URL', 'JUPYTERHUB_OAUTH_ACCESS_SCOPES',
    'JUPYTERHUB_OAUTH_CLIENT_ALLOWED_SCOPES', 'JUPYTERHUB_SERVER_NAME',
    'JUPYTERHUB_COOKIE_OPTIONS', 'JUPYTERHUB_OAUTH_SCOPES', 'JUPYTERHUB_COOKIE_HOST_PREFIX_ENABLED',
    'JUPYTERHUB_PUBLIC_URL', 'JUPYTERHUB_PUBLIC_HUB_URL'})
HASH = re.compile(r'[0-9a-f]{64}\Z')
HEX = re.compile(r'[0-9a-f]{32}\Z')
SLUG = re.compile(r'[a-z][a-z0-9-]{0,63}\Z')
IMAGE = re.compile(r'sha256:[0-9a-f]{64}\Z')
MAX_RECORD = 256 * 1024
ADMISSION_UNUSED_SNAPSHOT = digest({'purpose': 'unused-runtime-admission-snapshot-v1'})
ADMISSION_UNUSED_RETAINED = digest({'purpose': 'unused-runtime-admission-retained-v1'})


class RuntimeRefusal(OwnershipError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def refuse(code):
    raise RuntimeRefusal(code)


def private_file(path, directory=False):
    path = Path(path)
    value = path.lstat()
    if (value.st_uid != os.geteuid() or stat.S_IMODE(value.st_mode) != (0o700 if directory else 0o600)
            or not directory and value.st_nlink != 1
            or not (stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode))):
        refuse('runtime_private_state_invalid')
    return value


def read_record(path):
    private_file(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        raw = stream.read(MAX_RECORD + 1)
    if len(raw) > MAX_RECORD:
        refuse('runtime_record_bounds')
    def unique(pairs):
        value = {}
        for key, data in pairs:
            if key in value:
                refuse('runtime_record_duplicate')
            value[key] = data
        return value
    return json.loads(raw, object_pairs_hook=unique,
        parse_constant=lambda _: refuse('runtime_record_nonfinite'))


def metadata(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def open_directory(path):
    """No-follow EVERY path component, including trusted root ancestors."""
    path = Path(path)
    if not path.is_absolute() or any(part in ('.', '..') for part in path.parts):
        refuse('runtime_evidence_path_invalid')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        return fd
    except BaseException:
        os.close(fd); raise


def bounded_names(fd, remaining):
    names = []
    with os.scandir(fd) as listing:
        for child in listing:
            if len(names) >= remaining: refuse('runtime_tree_bounds')
            names.append(child.name)
    return sorted(names)


def file_digest_at(parent, name, info, remaining):
    if not stat.S_ISREG(info.st_mode) or info.st_size > remaining:
        refuse('runtime_evidence_invalid_or_bounds')
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        if metadata(os.fstat(fd)) != metadata(info): refuse('runtime_evidence_changed')
        value = hashlib.sha256(); total = 0
        while chunk := os.read(fd, 65536):
            total += len(chunk)
            if total > remaining: refuse('runtime_evidence_bounds')
            value.update(chunk)
        if (total != info.st_size or metadata(os.fstat(fd)) != metadata(info)
                or metadata(os.stat(name, dir_fd=parent, follow_symlinks=False)) != metadata(info)):
            refuse('runtime_evidence_changed')
        return value.hexdigest(), total
    finally: os.close(fd)


def file_hash(path):
    path = Path(path)
    parent = open_directory(path.parent)
    try:
        before = os.fstat(parent)
        info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        value, _ = file_digest_at(parent, path.name, info, 64 * 1024**2)
        after = open_directory(path.parent)
        try:
            if (os.fstat(after).st_dev, os.fstat(after).st_ino) != (before.st_dev, before.st_ino):
                refuse('runtime_evidence_changed')
        finally: os.close(after)
        return value
    finally: os.close(parent)


def tree_hash(root=None, *, descriptor=None, _budget=None):
    """Bounded descriptor-relative reads, same device and stable metadata."""
    fd = open_directory(root) if descriptor is None else os.dup(descriptor)
    budget = _budget if _budget is not None else {'bytes': 0, 'entries': 0}
    entries = []; total = budget['bytes']; count = budget['entries']
    initial = os.fstat(fd); device = initial.st_dev
    def visit(directory, relative, depth):
        nonlocal count, total
        before = os.fstat(directory)
        if before.st_dev != device or depth > 64: refuse('runtime_tree_mount_or_depth')
        count += 1
        if count > 10000: refuse('runtime_tree_bounds')
        entries.append({'path': relative, 'kind': 'directory', 'mode': stat.S_IMODE(before.st_mode),
                        'uid': before.st_uid, 'gid': before.st_gid})
        for name in bounded_names(directory, 10000 - count):
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if info.st_dev != device: refuse('runtime_tree_mount_or_depth')
            path = relative + '/' + name
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    if metadata(os.fstat(child)) != metadata(info): refuse('runtime_evidence_changed')
                    visit(child, path, depth + 1)
                finally: os.close(child)
            else:
                count += 1
                if count > 10000: refuse('runtime_tree_bounds')
                item = {'path': path, 'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid}
                if stat.S_ISREG(info.st_mode):
                    value, size = file_digest_at(directory, name, info, 64 * 1024**2 - total)
                    total += size; item.update(kind='file', sha256=value)
                elif stat.S_ISLNK(info.st_mode):
                    target = os.readlink(name, dir_fd=directory)
                    if len(os.fsencode(target)) > 4096: refuse('runtime_tree_bounds')
                    item.update(kind='link', target=target)
                else: refuse('runtime_tree_special_entry')
                entries.append(item)
            if metadata(os.stat(name, dir_fd=directory, follow_symlinks=False)) != metadata(info):
                refuse('runtime_evidence_changed')
        if metadata(os.fstat(directory)) != metadata(before): refuse('runtime_evidence_changed')
    try:
        visit(fd, '.', 0)
        if root is not None:
            after = open_directory(root)
            try:
                if metadata(os.fstat(after)) != metadata(initial): refuse('runtime_evidence_changed')
            finally: os.close(after)
        budget.update(bytes=total, entries=count)
        return digest(sorted(entries, key=lambda item: item['path']))
    finally: os.close(fd)


def validate_config(config):
    if (not isinstance(config, dict) or config.get('version') != VERSION
            or not HEX.fullmatch(config.get('authority_id', ''))
            or not isinstance(config.get('daemon_id'), str) or not config['daemon_id']
            or not Path(config.get('backend_directory', '')).is_absolute()
            or not isinstance(config.get('workers'), dict) or not isinstance(config.get('courses'), dict)
            or not isinstance(config.get('guardians'), dict) or set(config['guardians']) != set(config['workers'])
            or not 1 <= len(config['workers']) <= 16 or set(config['courses']) != set(config['workers'])):
        refuse('runtime_configuration_invalid')
    if (type(config.get('host_reserve_mb', 2048)) is not int or not 1024 <= config.get('host_reserve_mb', 2048) <= 1048576
            or type(config.get('host_reserve_cpu', 1)) not in (int, float)
            or not math.isfinite(config.get('host_reserve_cpu', 1)) or not 0.5 <= config.get('host_reserve_cpu', 1) <= 1024):
        refuse('runtime_shared_daemon_reserve_invalid')
    instances = set()
    for worker, worker_config in config['workers'].items():
        worker_evidence(worker_config, worker)
        guardian = config['guardians'][worker]
        if (set(guardian) != {'container_name', 'image_id', 'heartbeat_seconds'}
                or not SLUG.fullmatch(guardian['container_name']) or not IMAGE.fullmatch(guardian['image_id'])
                or type(guardian['heartbeat_seconds']) not in (int, float)
                or not math.isfinite(guardian['heartbeat_seconds']) or not 1 <= guardian['heartbeat_seconds'] <= 10):
            refuse('runtime_guardian_configuration_invalid')
        if not isinstance(config['courses'][worker], dict):
            refuse('runtime_configuration_invalid')
        for course, item in config['courses'][worker].items():
            if not SLUG.fullmatch(course) or not isinstance(item, dict):
                refuse('runtime_configuration_invalid')
            logical_identity(item['logical_identity']); profile(item['profile'])
            if (not SLUG.fullmatch(item.get('instance', '')) or item['instance'] in instances
                    or item.get('hub_container') != item['instance'] + '-hub'
                    or not IMAGE.fullmatch(item.get('image_id', ''))
                    or not isinstance(item.get('subjects'), list) or not 1 <= len(item['subjects']) <= 1000
                    or len(set(item['subjects'])) != len(item['subjects'])
                    or any(not isinstance(subject, str) or not 1 <= len(subject) <= 256
                           or any(ord(char) < 32 for char in subject) for subject in item['subjects'])
                    or any(not Path(item.get(key, '')).is_absolute() for key in ('home_root', 'snapshot_path', 'runtime_path'))):
                refuse('runtime_configuration_invalid')
            instances.add(item['instance'])
            extras = item.get('server_environment', {})
            if (not isinstance(extras, dict) or set(extras) - {'WORKSPACE_CAIRN_ORIGIN', 'WORKSPACE_CAIRN_LOCAL_TEST'}
                    or any(not isinstance(value, str) or len(value) > 512 for value in extras.values())):
                refuse('runtime_configuration_invalid')
        if (sum(item['profile']['memory_mb'] for item in config['courses'][worker].values()) > worker_config['memory_budget_mb']
                or sum(item['profile']['cpu'] for item in config['courses'][worker].values()) > worker_config['cpu_budget']):
            refuse('runtime_fixed_namespace_budget_exceeded')
    return config


class DockerBackend:
    fixture_only = True  # Disposable LOCAL integration; core is not promoted.
    scope = 'course'

    def __init__(self, config, client=None, *, initialize=False, clock=time.time, inspect_roots=True):
        self.config = validate_config(config)
        self.authority_id = config['authority_id']
        self.directory = Path(config['backend_directory'])
        self.clock = clock
        self.admission_scope = threading.local()
        if client is None:
            import docker
            client = docker.DockerClient(base_url='unix:///var/run/docker.sock', version='auto', timeout=5)
        self.client = client
        self.database = self.directory / 'runtime-barriers.sqlite'
        self.sentinel = self.directory / 'runtime-state.json'
        self.home_bindings = {}
        if initialize and not inspect_roots: refuse('runtime_bootstrap_requires_root_inspection')
        if inspect_roots:
            for worker, courses in config['courses'].items():
                for course, item in courses.items():
                    fd = open_directory(item['home_root'])
                    try:
                        info = os.fstat(fd)
                        self.home_bindings[worker + '/' + course] = [info.st_dev, info.st_ino]
                    finally: os.close(fd)
        else:
            # Stopper-only Guardian has NO homes mount and never attests trees.
            bindings = read_record(self.sentinel).get('home_root_bindings')
            expected = {worker + '/' + course for worker, courses in config['courses'].items() for course in courses}
            if (not isinstance(bindings, dict) or set(bindings) != expected
                    or any(not isinstance(value, list) or len(value) != 2 or type(value[0]) is not int
                           or value[0] < 0 or type(value[1]) is not int or value[1] <= 0 for value in bindings.values())):
                refuse('runtime_home_root_binding_invalid')
            self.home_bindings = bindings
        self._initializing = initialize
        if initialize:
            self._initialize()
        self._initializing = False
        private_file(self.directory, True)
        marker = read_record(self.sentinel)
        info = private_file(self.database)
        if marker != {'version': VERSION, 'authority_id': self.authority_id,
                      'database_device': info.st_dev, 'database_inode': info.st_ino,
                      'config_sha256': digest(config), 'home_root_bindings': self.home_bindings}:
            refuse('runtime_database_binding_changed')
        self.daemon()
        for courses in config['courses'].values():
            for item in courses.values(): self.image_labels(item['image_id'])
        for item in config['guardians'].values(): self.image_labels(item['image_id'])
        with self.connection() as conn:
            conn.execute('BEGIN'); self._validate(conn); conn.execute('COMMIT')

    def _initialize(self):
        self.directory.mkdir(mode=0o700, exist_ok=False)
        fd = os.open(self.database, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with self.connection() as conn:
            conn.executescript('''BEGIN IMMEDIATE;
                CREATE TABLE barriers(endpoint TEXT PRIMARY KEY,record TEXT NOT NULL);
                CREATE TABLE history(sequence INTEGER PRIMARY KEY,endpoint TEXT NOT NULL,record TEXT NOT NULL,previous TEXT NOT NULL,digest TEXT NOT NULL);
                CREATE TABLE guardians(endpoint TEXT PRIMARY KEY,record TEXT NOT NULL);
            ''')
            for worker, courses in self.config['courses'].items():
                for course in courses:
                    record = {'authority_id': self.authority_id, 'worker_id': worker,
                        'course_id': course, 'barrier_id': uuid.uuid4().hex,
                        'denied_through_epoch': 0, 'request_sha256': '0' * 64}
                    self._store(conn, self.endpoint(worker, course), record)
            conn.execute('COMMIT')
        info = self.database.stat()
        raw = encoded({'version': VERSION, 'authority_id': self.authority_id,
                      'database_device': info.st_dev, 'database_inode': info.st_ino,
                      'config_sha256': digest(self.config), 'home_root_bindings': self.home_bindings})
        fd = os.open(self.sentinel, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)

    @contextmanager
    def connection(self):
        private_file(self.directory, True); info = private_file(self.database)
        if not self._initializing:
            marker = read_record(self.sentinel)
            if marker != {'version': VERSION, 'authority_id': self.authority_id,
                          'database_device': info.st_dev, 'database_inode': info.st_ino,
                          'config_sha256': digest(self.config), 'home_root_bindings': self.home_bindings}:
                refuse('runtime_database_binding_changed')
        conn = sqlite3.connect(self.database.as_uri() + '?mode=rw', uri=True, timeout=2, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            if conn.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
                refuse('runtime_database_journal_changed')
            conn.execute('PRAGMA synchronous=EXTRA')
            conn.execute('PRAGMA busy_timeout=2000')
            conn.execute('PRAGMA trusted_schema=OFF')
            if (conn.execute('PRAGMA synchronous').fetchone()[0] != 3
                    or conn.execute('PRAGMA busy_timeout').fetchone()[0] != 2000
                    or conn.execute('PRAGMA trusted_schema').fetchone()[0] != 0):
                refuse('runtime_database_durability_invalid')
            yield conn
        finally: conn.close()

    def endpoint(self, worker, course):
        self.course(worker, course)
        return worker + '/' + course

    def course(self, worker, course):
        try: return self.config['courses'][worker][course]
        except (KeyError, TypeError): refuse('runtime_endpoint_unknown')

    def daemon(self):
        info = self.client.info()
        if info.get('ID') != self.config['daemon_id']:
            refuse('runtime_daemon_changed')
        if (sum(item['memory_budget_mb'] for item in self.config['workers'].values()) + self.config.get('host_reserve_mb', 2048)
                > info.get('MemTotal', 0) // 1024**2
                or sum(item['cpu_budget'] for item in self.config['workers'].values()) + self.config.get('host_reserve_cpu', 1)
                > info.get('NCPU', 0)):
            refuse('runtime_shared_daemon_budget_exceeded')

    def image_labels(self, image_id):
        """Only exact inherited labels of the pinned image are harmless provenance."""
        self.daemon()
        image = self.client.images.get(image_id)
        labels = image.attrs.get('Config', {}).get('Labels') or {}
        if (image.attrs.get('Id') != image_id or not isinstance(labels, dict) or len(labels) > 64
                or any(not isinstance(key, str) or not isinstance(value, str) or len(key) > 256 or len(value) > 4096
                       or key.startswith('educloud.') for key, value in labels.items())):
            refuse('runtime_image_labels_unsupported')
        return dict(labels)

    def _validate(self, conn):
        expected = {self.endpoint(worker, course) for worker, courses in self.config['courses'].items() for course in courses}
        rows = conn.execute('SELECT * FROM barriers').fetchall()
        if len(rows) != len(expected) or {row['endpoint'] for row in rows} != expected:
            refuse('runtime_barrier_history_missing')
        previous = '0' * 64
        latest = {}
        history = conn.execute('SELECT * FROM history ORDER BY sequence LIMIT 4097').fetchall()
        if not history or len(history) > 4096:
            refuse('runtime_barrier_history_invalid')
        for sequence, row in enumerate(history, 1):
            record = json.loads(row['record'])
            value = {'sequence': sequence, 'endpoint': row['endpoint'], 'record': record, 'previous': previous}
            if row['sequence'] != sequence or row['previous'] != previous or row['digest'] != digest(value):
                refuse('runtime_barrier_history_invalid')
            if (set(record) != {'authority_id', 'worker_id', 'course_id', 'barrier_id', 'denied_through_epoch', 'request_sha256'}
                    or record['authority_id'] != self.authority_id
                    or self.endpoint(record['worker_id'], record['course_id']) != row['endpoint']
                    or not HEX.fullmatch(record['barrier_id']) or not HASH.fullmatch(record['request_sha256'])
                    or type(record['denied_through_epoch']) is not int or not 0 <= record['denied_through_epoch'] <= 10**9):
                refuse('runtime_barrier_history_invalid')
            if row['endpoint'] in latest:
                old = latest[row['endpoint']]
                if record['barrier_id'] != old['barrier_id'] or record['denied_through_epoch'] < old['denied_through_epoch']:
                    refuse('runtime_barrier_history_invalid')
            latest[row['endpoint']] = record
            previous = row['digest']
        if {row['endpoint']: json.loads(row['record']) for row in rows} != latest:
            refuse('runtime_barrier_history_missing')

    def _store(self, conn, endpoint, record):
        last = conn.execute('SELECT sequence,digest FROM history ORDER BY sequence DESC LIMIT 1').fetchone()
        sequence = last['sequence'] + 1 if last else 1
        previous = last['digest'] if last else '0' * 64
        value = {'sequence': sequence, 'endpoint': endpoint, 'record': record, 'previous': previous}
        conn.execute('INSERT INTO history VALUES(?,?,?,?,?)', (sequence, endpoint, encoded(record).decode(), previous, digest(value)))
        conn.execute('INSERT OR REPLACE INTO barriers VALUES(?,?)', (endpoint, encoded(record).decode()))

    def barrier(self, worker, course):
        with self.connection() as conn:
            conn.execute('BEGIN'); self._validate(conn)
            row = conn.execute('SELECT record FROM barriers WHERE endpoint=?', (self.endpoint(worker, course),)).fetchone()
            conn.execute('COMMIT')
            return json.loads(row['record'])

    def fence(self, request, endpoints):
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE'); self._validate(conn)
            for worker in endpoints:
                endpoint = self.endpoint(worker, request['course_id'])
                old = json.loads(conn.execute('SELECT record FROM barriers WHERE endpoint=?', (endpoint,)).fetchone()['record'])
                record = dict(old, denied_through_epoch=max(old['denied_through_epoch'], request['old_epoch']),
                              request_sha256=digest(request))
                self._store(conn, endpoint, record)
                conn.execute('DELETE FROM guardians WHERE endpoint=?', (endpoint,))
            self._validate(conn); conn.execute('COMMIT')

    @contextmanager
    def locks(self, endpoints, *, wait_seconds=3):
        if type(wait_seconds) not in (int, float) or not 0 < wait_seconds <= 3:
            refuse('runtime_lock_wait_invalid')
        deadline = time.monotonic() + wait_seconds
        with ExitStack() as stack:
            for worker, course in sorted(set(endpoints)):
                name = hashlib.sha256(self.endpoint(worker, course).encode()).hexdigest() + '.lock'
                fd = os.open(self.directory / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                if os.fstat(fd).st_uid != os.geteuid() or stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
                    os.close(fd); refuse('runtime_lock_invalid')
                stack.callback(os.close, fd)
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline: refuse('runtime_endpoint_lock_busy')
                        time.sleep(0.02)
            yield

    def inspect_worker(self, worker):
        self.daemon()
        try: return dict(self.config['workers'][worker])
        except KeyError: refuse('runtime_worker_unknown')

    def inspect_course(self, course, worker):
        item = self.course(worker, course)
        scope = getattr(self.admission_scope, 'endpoint', None)
        if scope is not None and scope[:2] == (worker, course):
            # Authority.admission consumes none of the snapshot/tree fields.
            # This trusted thread-local branch exists ONLY inside its held lock.
            root = open_directory(item['home_root'])
            try:
                info = os.fstat(root)
                if [info.st_dev, info.st_ino] != self.home_bindings[self.endpoint(worker, course)]:
                    refuse('runtime_home_root_changed')
            finally: os.close(root)
            return {'logical_identity': item['logical_identity'], 'profile': item['profile'],
                'snapshot_sha256': ADMISSION_UNUSED_SNAPSHOT, 'runtime_sha256': file_hash(item['runtime_path']),
                'retained_tree_sha256': ADMISSION_UNUSED_RETAINED,
                'admission_floor': self.barrier(worker, course)['denied_through_epoch'] + 1,
                'running_count': sum(self.running(container) for container in self.containers(worker, course))}
        retained = {}
        budget = {'bytes': 0, 'entries': 0}
        fd = open_directory(item['home_root'])
        try:
            before = os.fstat(fd)
            if [before.st_dev, before.st_ino] != self.home_bindings[self.endpoint(worker, course)]:
                refuse('runtime_home_root_changed')
            for name in bounded_names(fd, 1000):
                prefix = item['instance'] + '-home-'
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if (not name.startswith(prefix) or not re.fullmatch(r'[0-9a-f]{24}', name[len(prefix):])
                        or not stat.S_ISDIR(info.st_mode) or info.st_dev != before.st_dev):
                    refuse('runtime_home_inventory_invalid')
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if metadata(os.fstat(child)) != metadata(info): refuse('runtime_evidence_changed')
                    retained[name[len(prefix):]] = tree_hash(descriptor=child, _budget=budget)
                finally: os.close(child)
                if metadata(os.stat(name, dir_fd=fd, follow_symlinks=False)) != metadata(info):
                    refuse('runtime_evidence_changed')
            after = open_directory(item['home_root'])
            try:
                if metadata(os.fstat(after)) != metadata(before) or metadata(os.fstat(fd)) != metadata(before):
                    refuse('runtime_evidence_changed')
            finally: os.close(after)
        finally: os.close(fd)
        return {'logical_identity': item['logical_identity'], 'profile': item['profile'],
            'snapshot_sha256': file_hash(item['snapshot_path']), 'runtime_sha256': file_hash(item['runtime_path']),
            'retained_tree_sha256': digest(retained),
            'admission_floor': self.barrier(worker, course)['denied_through_epoch'] + 1,
            'running_count': sum(self.running(container) for container in self.containers(worker, course))}

    def validate_request(self, request, workers):
        if request['authority_id'] != self.authority_id:
            refuse('runtime_authority_binding_changed')
        for worker in workers:
            evidence = self.inspect_worker(worker)
            prefix = '' if 'worker_id' in request else ('source_' if worker == request['source_worker_id'] else 'receiver_')
            if request[prefix + 'provider_id'] != evidence['provider_id'] or request[prefix + 'boot_id'] != evidence['boot_id']:
                refuse('runtime_worker_incarnation_changed')

    @contextmanager
    def activation_context(self, request):
        worker, course = request['worker_id'], request['course_id']
        with self.locks([(worker, course)]):
            self.validate_request(request, [worker])
            if self.barrier(worker, course)['denied_through_epoch'] >= request['epoch']:
                refuse('runtime_epoch_fenced')
            if getattr(self.admission_scope, 'endpoint', None) is not None:
                refuse('runtime_admission_scope_nested')
            self.admission_scope.endpoint = (worker, course, request['epoch'])
            try: yield
            finally: self.admission_scope.endpoint = None

    @contextmanager
    def fence_context(self, request, *, authorize):
        workers = [request['source_worker_id'], request['receiver_worker_id']]
        course = request['course_id']
        with self.locks([(worker, course) for worker in workers]):
            authorize()  # Mandatory fresh authority validation BEFORE ANY effects.
            self.validate_request(request, workers)
            if any(self.running(c) for c in self.containers(workers[1], course)):
                refuse('runtime_receiver_must_be_stopped')
            self.fence(request, [workers[0]])
            self.stop_endpoint(workers[0], course)
            view = FenceView(self, request, workers, reconciliation=False)
            try: yield view
            finally: view.active = False

    @contextmanager
    def reconciliation_context(self, request, *, authorize):
        workers = [request['source_worker_id'], request['receiver_worker_id']]
        course = request['course_id']
        with self.locks([(worker, course) for worker in workers]):
            authorize()
            self.validate_request(request, workers)
            self.fence(request, workers)  # BOTH floors/history in ONE durable SQL transaction.
            for worker in workers: self.stop_endpoint(worker, course)
            view = FenceView(self, request, workers, reconciliation=True)
            try: yield view
            finally: view.active = False

    def subject(self, worker, course, subject):
        item = self.course(worker, course)
        if subject not in item['subjects']:
            refuse('runtime_subject_not_in_roster')
        return hashlib.sha256(subject.encode()).hexdigest()[:24]

    def labels(self, worker, course, subject, epoch):
        item = self.course(worker, course)
        return {'educloud.workspace.instance': item['instance'], 'educloud.workspace.kind': 'learner',
            'educloud.runtime.authority': self.authority_id, 'educloud.runtime.worker': worker,
            'educloud.runtime.course': course, 'educloud.runtime.subject': self.subject(worker, course, subject),
            'educloud.runtime.epoch': str(epoch)}

    @staticmethod
    def running(container):
        container.reload()
        return container.attrs['State']['Running'] is True

    def verified_container(self, container, worker, course, *, check_policy=False):
        self.daemon(); container.reload()
        item = self.course(worker, course)
        labels = container.attrs['Config'].get('Labels') or {}
        key = labels.get('educloud.runtime.subject')
        subjects = [subject for subject in item['subjects'] if self.subject(worker, course, subject) == key]
        try: epoch = int(labels.get('educloud.runtime.epoch', ''))
        except ValueError: refuse('runtime_container_scope_invalid')
        expected = dict(self.image_labels(item['image_id']), **self.labels(worker, course, subjects[0], epoch)) if len(subjects) == 1 and epoch > 0 else None
        if (labels != expected or container.attrs.get('Name', '').lstrip('/') != item['instance'] + '-learner-' + str(key)
                or container.attrs.get('Image') != item['image_id']):
            refuse('runtime_container_scope_invalid')
        if not check_policy:
            return subjects[0], epoch
        host = container.attrs['HostConfig']
        network = item['instance'] + '-user-' + str(key)
        mounts = container.attrs.get('Mounts', [])
        env = dict(value.split('=', 1) for value in container.attrs['Config'].get('Env', []) if '=' in value)
        if (host.get('Privileged') is not False or host.get('ReadonlyRootfs') is not True
                or host.get('CapDrop') != ['ALL'] or host.get('SecurityOpt') != ['no-new-privileges:true']
                or host.get('PidsLimit') != 128 or host.get('Memory') != item['profile']['learner_memory_mb'] * 1024**2
                or host.get('MemorySwap') != item['profile']['learner_memory_mb'] * 1024**2
                or host.get('CpuPeriod') != 100000 or host.get('CpuQuota') != int(item['profile']['learner_cpu'] * 100000)
                or host.get('RestartPolicy', {}).get('Name') not in ('no', '')
                or set(container.attrs['NetworkSettings']['Networks']) != {network}
                or len(mounts) != 1 or mounts[0].get('Type') != 'volume'
                or mounts[0].get('Name') != item['instance'] + '-home-' + str(key)
                or mounts[0].get('Destination') != '/home/learner' or mounts[0].get('RW') is not True
                or container.attrs['Config'].get('User') != '1000:1000'
                or env.get('JUPYTERHUB_USER') != subjects[0]):
            refuse('runtime_container_policy_changed')
        return subjects[0], epoch

    def containers(self, worker, course):
        self.daemon()
        item = self.course(worker, course)
        values = self.client.containers.list(all=True, filters={'label': [
            'educloud.workspace.instance=' + item['instance'], 'educloud.workspace.kind=learner']})
        if len(values) > len(item['subjects']): refuse('runtime_container_bounds')
        for value in values: self.verified_container(value, worker, course)
        return values

    def remove(self, container, worker, course, *, authorize=None):
        _, epoch = self.verified_container(container, worker, course)
        if authorize is not None:
            if not callable(authorize) or getattr(self.admission_scope, 'endpoint', None) != (worker, course, epoch):
                refuse('runtime_admission_scope_invalid')
            authorize()
        container.update(restart_policy={'Name': 'no'})
        if authorize is not None: authorize()
        container.stop(timeout=5)
        if authorize is not None: authorize()
        container.remove(v=False, force=False)  # NEVER volumes, NEVER unrelated resources.

    def stop_endpoint(self, worker, course):
        for container in self.containers(worker, course): self.remove(container, worker, course)
        if self.containers(worker, course): refuse('runtime_quiescence_unverified')

    def guardian_identity(self, worker):
        self.daemon()
        item = self.config['guardians'][worker]
        container = self.client.containers.get(item['container_name']); container.reload()
        labels = dict(self.image_labels(item['image_id']), **{'educloud.runtime.authority': self.authority_id,
                    'educloud.runtime.worker': worker, 'educloud.runtime.kind': 'guardian'})
        if (container.attrs.get('Name', '').lstrip('/') != item['container_name']
                or container.attrs.get('Image') != item['image_id'] or container.attrs['Config'].get('Labels') != labels
                or container.attrs['State'].get('Running') is not True
                or not isinstance(container.attrs['State'].get('StartedAt'), str)):
            refuse('guardian_container_unavailable')
        return {'guardian_container_id': container.id, 'start_identity': digest({'container_id': container.id,
            'started_at': container.attrs['State']['StartedAt'], 'authority_id': self.authority_id, 'worker_id': worker})}

    def guardian_heartbeat(self, worker, course, epoch, identity, cert_sha256):
        # Caller holds admission's endpoint lock; no nested locks or reciprocal RPC.
        if self.guardian_identity(worker) != identity or self.barrier(worker, course)['denied_through_epoch'] >= epoch:
            refuse('guardian_identity_or_epoch_invalid')
        record = {'authority_id': self.authority_id, 'worker_id': worker, 'course_id': course,
            'provider_id': self.config['workers'][worker]['provider_id'], 'boot_id': self.config['workers'][worker]['boot_id'],
            'epoch': epoch, **identity, 'cert_sha256': cert_sha256, 'checked_at': self.clock()}
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE'); self._validate(conn)
            conn.execute('INSERT OR REPLACE INTO guardians VALUES(?,?)', (self.endpoint(worker, course), encoded(record).decode()))
            conn.execute('COMMIT')

    def require_guardian(self, worker, course, epoch):
        with self.connection() as conn:
            conn.execute('BEGIN'); self._validate(conn)
            row = conn.execute('SELECT record FROM guardians WHERE endpoint=?', (self.endpoint(worker, course),)).fetchone()
            conn.execute('COMMIT')
        if row is None: refuse('guardian_unavailable')
        record = json.loads(row['record'])
        expected = {'authority_id': self.authority_id, 'worker_id': worker, 'course_id': course,
            'provider_id': self.config['workers'][worker]['provider_id'], 'boot_id': self.config['workers'][worker]['boot_id'], 'epoch': epoch}
        reason = None
        if any(record.get(key) != value for key, value in expected.items() if key != 'epoch'):
            reason = 'binding_changed'
        elif record.get('epoch') != epoch:
            reason = 'epoch_changed'
        elif type(record.get('checked_at')) not in (int, float) or not math.isfinite(record['checked_at']):
            reason = 'timestamp_invalid'
        else:
            age = self.clock() - record['checked_at']
            if age < 0: reason = 'heartbeat_future'
            elif age > self.config['guardians'][worker]['heartbeat_seconds']: reason = 'heartbeat_expired'
            elif self.guardian_identity(worker) != {key: record.get(key) for key in ('guardian_container_id', 'start_identity')}:
                reason = 'container_identity_changed'
        if reason is not None:
            logging.getLogger(__name__).warning('Guardian heartbeat refused: %s', reason)
            refuse('guardian_stale_or_changed')
        return record

    def guardian_snapshot(self, worker, course):
        # Give fresh observation a bounded acquisition window amid Hub/watchdog
        # traffic. Failed exact-ID stopping retains its shorter .1s bound.
        with self.locks([(worker, course)], wait_seconds=1):
            return [(container.id, self.verified_container(container, worker, course)[1])
                    for container in self.containers(worker, course)]

    def guardian_failclosed(self, worker, course, identity, max_epoch, observed):
        with self.locks([(worker, course)], wait_seconds=0.1):
            if self.guardian_identity(worker) != identity:
                refuse('guardian_replaced_before_stop')
            with self.connection() as conn:
                conn.execute('BEGIN IMMEDIATE'); self._validate(conn)
                current = conn.execute('SELECT record FROM guardians WHERE endpoint=?', (self.endpoint(worker, course),)).fetchone()
                if current and json.loads(current['record'])['epoch'] > max_epoch:
                    conn.execute('COMMIT'); return
                conn.execute('DELETE FROM guardians WHERE endpoint=?', (self.endpoint(worker, course),)); conn.execute('COMMIT')
            for container in self.containers(worker, course):
                if (container.id, self.verified_container(container, worker, course)[1]) in observed:
                    self.remove(container, worker, course)

    def prepare_home_network(self, worker, course, subject, *, authorize):
        endpoint = getattr(self.admission_scope, 'endpoint', None)
        if not callable(authorize) or endpoint is None or endpoint[:2] != (worker, course):
            refuse('runtime_admission_scope_invalid')
        item = self.course(worker, course); key = self.subject(worker, course, subject)
        volume_name = item['instance'] + '-home-' + key
        volume = self.client.volumes.get(volume_name)
        labels = item.get('home_labels', {}).get(subject, {'educloud.workspace.instance': item['instance'], 'educloud.workspace.kind': 'home'})
        options = item.get('home_options', {}).get(subject, {'type': 'none', 'o': 'bind', 'device': str(Path(item['home_root']) / volume_name)})
        if volume.attrs.get('Labels') != labels or volume.attrs.get('Options') != options:
            refuse('runtime_home_not_preprovisioned')
        if item.get('quota_status_path'):
            status = read_record(item['quota_status_path'])
            if (status.get('instance') != item['instance'] or status.get('healthy') is not True
                    or type(status.get('checked_at')) not in (int, float)
                    or not 0 <= self.clock() - status['checked_at'] <= 120
                    or volume_name not in status.get('volumes', {})):
                refuse('runtime_quota_unhealthy_or_stale')
        name = item['instance'] + '-user-' + key
        values = self.client.networks.list(names=[name])
        values = [value for value in values if value.name == name]
        if len(values) > 1: refuse('runtime_network_ambiguous')
        if values:
            network = values[0]
        else:
            authorize()
            network = self.client.networks.create(name, internal=True,
                        labels={'educloud.workspace.instance': item['instance']})
        network.reload()
        if network.attrs.get('Labels') != {'educloud.workspace.instance': item['instance']} or network.attrs.get('Internal') is not True:
            refuse('runtime_network_scope_invalid')
        hub = self.client.containers.get(item['hub_container']); hub.reload()
        hub_labels = dict(self.image_labels(hub.attrs['Image']), **{'educloud.workspace.instance': item['instance'], 'educloud.workspace.kind': 'hub'})
        if hub.attrs['Config'].get('Labels') != hub_labels:
            refuse('runtime_hub_scope_invalid')
        if name not in hub.attrs['NetworkSettings']['Networks']:
            authorize()
            network.connect(hub, aliases=[item['hub_container']])
        return volume_name, name


class FenceView:
    def __init__(self, backend, request, workers, reconciliation):
        self.backend, self.request, self.workers, self.reconciliation = backend, request, workers, reconciliation
        self.active = True

    def inspect(self):
        backend, request = self.backend, self.request
        if not self.active: refuse('runtime_fence_context_expired')
        for worker in self.workers:
            name = hashlib.sha256(backend.endpoint(worker, request['course_id']).encode()).hexdigest() + '.lock'
            fd = os.open(backend.directory / name, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError: pass
                else: refuse('runtime_endpoint_exclusion_unverified')
            finally: os.close(fd)
        backend.validate_request(request, self.workers)
        endpoints = {}
        for worker in self.workers:
            if any(backend.running(c) for c in backend.containers(worker, request['course_id'])):
                refuse('runtime_quiescence_unverified')
            record = backend.barrier(worker, request['course_id'])
            if self.reconciliation or worker == self.workers[0]:
                if record['request_sha256'] != digest(request) or record['denied_through_epoch'] < request['old_epoch']:
                    refuse('runtime_barrier_binding_changed')
            endpoints[worker] = {'barrier_id': record['barrier_id'], 'barrier_sha256': digest(record),
                'denied_through_epoch': record['denied_through_epoch'], 'observed_running_count': 0}
        if self.reconciliation:
            return {'request_sha256': digest(request), 'scope': 'course', 'endpoints': endpoints}
        return dict(endpoints[self.workers[0]], request_sha256=digest(request), scope='course')


class RuntimeDispatcher:
    def __init__(self, authority, backend, guardian_authorize, *, monotonic=time.monotonic):
        self.authority, self.backend = authority, backend
        self.guardian_authorize = guardian_authorize
        self.monotonic = monotonic
        self.busy_since = {}

    def dispatch(self, principal, op, course_id, payload):
        worker = principal.get('worker_id')
        if (principal.get('course_id') != course_id or not callable(principal.get('authorize'))
                or principal.get('role') != ('guardian' if op == 'guardian.observe' else 'hub')
                or op not in {'hub.authorize', 'hub.spawn', 'hub.poll', 'hub.stop', 'guardian.observe'}
                or not isinstance(payload, dict)):
            refuse('runtime_principal_scope_invalid')
        self.backend.course(worker, course_id)
        if op in {'hub.authorize', 'guardian.observe'}:
            if op == 'hub.authorize' and payload: refuse('runtime_payload_invalid')
            if op == 'guardian.observe' and (set(payload) != {'guardian_container_id', 'start_identity'}
                    or any(not isinstance(value, str) or not HASH.fullmatch(value) for value in payload.values())):
                refuse('runtime_payload_invalid')
            with self.authority.admission(course_id, worker) as view:
                principal['authorize']()
                if op == 'guardian.observe':
                    self.backend.guardian_heartbeat(worker, course_id, view['epoch'], payload, principal['cert_sha256'])
                return {'authorized': True, 'epoch': view['epoch'], 'worker_id': worker, 'course_id': course_id}
        subject = payload.get('subject')
        self.backend.subject(worker, course_id, subject)
        if op == 'hub.spawn':
            if set(payload) != {'subject', 'environment', 'expected_epoch'} or type(payload['expected_epoch']) is not int:
                refuse('runtime_payload_invalid')
            env = payload['environment']
            if (not isinstance(env, dict) or set(env) - ENV_KEYS or len(encoded(env)) > 32768
                    or any(not isinstance(value, str) or len(value) > 8192 or '\x00' in value for value in env.values())
                    or env.get('JUPYTERHUB_USER') != subject or env.get('JUPYTERHUB_SERVER_NAME', '')
                    or not env.get('JUPYTERHUB_API_TOKEN') or not env.get('JUPYTERHUB_API_URL')):
                refuse('runtime_environment_invalid')
            with self.authority.admission(course_id, worker) as view:
                epoch = view['epoch']
                if payload['expected_epoch'] != epoch: refuse('runtime_cached_epoch_invalid')
                def authorize_effect():
                    principal['authorize']()
                    heartbeat = self.backend.require_guardian(worker, course_id, epoch)
                    self.guardian_authorize(heartbeat['cert_sha256'], worker, course_id)
                    principal['authorize']()  # Guardian verification can itself perform slow inspection.
                heartbeat = self.backend.require_guardian(worker, course_id, epoch)
                self.guardian_authorize(heartbeat['cert_sha256'], worker, course_id)
                item = self.backend.course(worker, course_id)
                if env['JUPYTERHUB_API_URL'] != 'http://' + item['hub_container'] + ':8081/hub/api':
                    refuse('runtime_environment_scope_invalid')
                environment = dict(item.get('server_environment', {}), **env)
                environment.update(JUPYTERHUB_DISABLE_USER_CONFIG='1', JUPYTERHUB_ROOT_DIR='/home/learner/work',
                                   JUPYTERHUB_DEFAULT_URL='/lab')
                values = self.backend.containers(worker, course_id)
                key = self.backend.subject(worker, course_id, subject)
                name = item['instance'] + '-learner-' + key
                for container in values:
                    current_subject, current_epoch = self.backend.verified_container(container, worker, course_id, check_policy=True)
                    if current_subject == subject:
                        if current_epoch != epoch: refuse('runtime_cached_epoch_invalid')
                        current_environment = dict(value.split('=', 1) for value in container.attrs['Config'].get('Env', []) if '=' in value)
                        compatible = {key: value for key, value in current_environment.items()
                            if key in ENV_KEYS or key in environment} == environment
                        if self.backend.running(container) and compatible:
                            principal['authorize']()
                            heartbeat = self.backend.require_guardian(worker, course_id, epoch)
                            self.guardian_authorize(heartbeat['cert_sha256'], worker, course_id)
                            return self.server(container, epoch)
                        authorize_effect()
                        self.backend.remove(container, worker, course_id, authorize=authorize_effect)
                values = self.backend.containers(worker, course_id)
                if sum(self.backend.running(c) for c in values) >= item['profile']['active_limit']:
                    refuse('runtime_course_capacity_exceeded')
                volume, network = self.backend.prepare_home_network(worker, course_id, subject, authorize=authorize_effect)
                authorize_effect()  # Fresh cert ACL + Guardian under the SAME admission lock, right before CREATE.
                container = self.backend.client.containers.create(image=item['image_id'], name=name,
                    command=['jupyterhub-singleuser', '--ip=0.0.0.0', '--port=8888',
                        '--ServerApp.root_dir=/home/learner/work', '--ServerApp.default_url=/lab'],
                    user='1000:1000', environment=environment, labels=self.backend.labels(worker, course_id, subject, epoch),
                    volumes={volume: {'bind': '/home/learner', 'mode': 'rw'}}, network=network,
                    cap_drop=['ALL'], security_opt=['no-new-privileges:true'], read_only=True,
                    pids_limit=128, mem_limit=item['profile']['learner_memory_mb'] * 1024**2,
                    memswap_limit=item['profile']['learner_memory_mb'] * 1024**2,
                    cpu_period=100000, cpu_quota=int(item['profile']['learner_cpu'] * 100000),
                    tmpfs={'/tmp': 'rw,nosuid,nodev,size=128m,mode=1777'}, restart_policy={'Name': 'no'}, detach=True)
                try:
                    authorize_effect()
                    container.start()
                    self.backend.verified_container(container, worker, course_id, check_policy=True)
                    return self.server(container, epoch)
                except BaseException:
                    self.backend.remove(container, worker, course_id)
                    raise
        if set(payload) != {'subject', 'server_id', 'epoch'} or not HASH.fullmatch(payload.get('server_id', '')) or type(payload.get('epoch')) is not int:
            refuse('runtime_payload_invalid')
        # Poll/stop also require live ownership: an old Hub never controls a new epoch.
        with self.authority.admission(course_id, worker) as view:
            if payload['epoch'] != view['epoch']: refuse('runtime_cached_epoch_invalid')
            principal['authorize']()
            matches = [c for c in self.backend.containers(worker, course_id) if c.id == payload['server_id']]
            if not matches: return {'stopped': True} if op == 'hub.stop' else {'running': False, 'exit_code': 0}
            container = matches[0]
            if self.backend.verified_container(container, worker, course_id) != (subject, payload['epoch']):
                refuse('runtime_container_scope_invalid')
            if op == 'hub.stop':
                principal['authorize']()
                self.backend.remove(container, worker, course_id, authorize=principal['authorize'])
                return {'stopped': True}
            return {'running': self.backend.running(container), 'exit_code': None if self.backend.running(container) else container.attrs['State']['ExitCode']}

    def supervision_tick(self):
        """Independent authority-service watchdog; never waits for remote RPC."""
        overdue = False
        for worker, courses in self.backend.config['courses'].items():
            for course in courses:
                endpoint = (worker, course)
                first = self.busy_since.get(endpoint)
                age = self.monotonic() - first if first is not None else 0
                # Brief probes stay responsive initially. Repeated contention
                # gets a bounded gap-acquisition window without extending the
                # existing continuous-inability limit or renewing any grant.
                wait = min(1, 10 - age) if 0.5 <= age < 10 else 0.1
                try:
                    self._supervise_endpoint(worker, course, wait_seconds=wait)
                except RuntimeRefusal as error:
                    if error.code != 'runtime_endpoint_lock_busy': raise
                    first = self.busy_since.setdefault(endpoint, self.monotonic())
                    overdue = overdue or self.monotonic() - first >= 10
                    continue  # NO successful observation/heartbeat/admission.
                self.busy_since.pop(endpoint, None)
        if overdue: refuse('runtime_endpoint_enforcement_unavailable')

    def _supervise_endpoint(self, worker, course, *, wait_seconds=0.1):
        with self.backend.locks([(worker, course)], wait_seconds=wait_seconds):
            valid = False
            reason = 'runtime_supervision_refused'
            try:
                # Fresh trusted SQL view under the already-held endpoint lock.
                # No call to admission (which would reacquire the flock).
                observed_worker = self.backend.inspect_worker(worker)
                with self.authority._connection() as conn:
                    conn.execute('BEGIN'); self.authority._validate(conn)
                    current = self.authority._course(conn, course)
                    self.authority._worker(conn, worker, observed_worker)
                    conn.execute('COMMIT')
                if current['owner'] == worker and current['pending'] is None:
                    record = self.backend.require_guardian(worker, course, current['epoch'])
                    if self.backend.barrier(worker, course)['denied_through_epoch'] < current['epoch']:
                        self.guardian_authorize(record['cert_sha256'], worker, course)
                        for container in self.backend.containers(worker, course):
                            if self.backend.verified_container(container, worker, course, check_policy=True)[1] != current['epoch']:
                                refuse('runtime_cached_epoch_invalid')
                        valid = True
            except Exception as error:
                code = getattr(error, 'code', None)
                if isinstance(code, str) and code in {'guardian_unavailable', 'guardian_stale_or_changed', 'guardian_container_unavailable', 'runtime_cached_epoch_invalid'}:
                    reason = code
                elif code == 'runtime_container_policy_changed':
                    reason = 'runtime_container_policy_invalid'
            if not valid:
                if self.backend.containers(worker, course):
                    logging.getLogger(__name__).warning('Runtime supervision stopping owned learners: %s', reason)
                self.backend.stop_endpoint(worker, course)

    @staticmethod
    def server(container, epoch):
        container.reload()
        networks = container.attrs['NetworkSettings']['Networks']
        if len(networks) != 1: refuse('runtime_server_network_invalid')
        address = next(iter(networks.values()))['IPAddress']
        import ipaddress
        ipaddress.ip_address(address)
        return {'epoch': epoch, 'server_id': container.id, 'address': address, 'port': 8888}


def bootstrap(config, authority_directory, client=None):
    """Fresh explicit bootstrap only; existing authority/backend paths refuse."""
    class EmptyBackend:
        fixture_only = True
        scope = 'course'
    authority = Authority.create(authority_directory, EmptyBackend())
    config = dict(config, authority_id=authority.authority_id)
    backend = DockerBackend(config, client, initialize=True)
    authority = Authority.open(authority_directory, authority.authority_id, backend)
    return authority, backend, config
