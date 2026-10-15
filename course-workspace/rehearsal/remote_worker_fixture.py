"""Private two-process worker fixture; no Docker, notebooks or host providers."""
from contextlib import contextmanager
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests'), str(Path(__file__).resolve().parent)]
from verify_ownership_contract import FixtureProcesses, private_json, read_private, retained_digest
from ownership_contract import digest, worker_evidence, course_evidence, HEX32, HEX64
from evidence import sha256_file


SQL_SERVICE = r'''import os,sys,resource,fcntl,hashlib,json,sqlite3
resource.setrlimit(resource.RLIMIT_CPU,(5,5))
if sys.platform=="linux":resource.setrlimit(resource.RLIMIT_AS,(128*1024**2,128*1024**2))
resource.setrlimit(resource.RLIMIT_FSIZE,(1024*1024,1024*1024))
resource.setrlimit(resource.RLIMIT_NOFILE,(32,32))
home,database,lock_path,nonce,worker,course,epoch,authority,held_fd=sys.argv[1:]
with (os.fdopen(int(held_fd),"a+b") if held_fd!="-1" else open(lock_path,"a+b")) as gate:
    if held_fd=="-1":fcntl.flock(gate,fcntl.LOCK_EX)
    try:
        connection=sqlite3.connect("file:"+database+"?mode=ro",uri=True,timeout=1)
        connection.execute("BEGIN")
        identity=connection.execute("SELECT value FROM metadata WHERE key='authority_id'").fetchone()[0]
        value=json.loads(connection.execute("SELECT record FROM barriers WHERE course=?",(course,)).fetchone()[0])
        deny=identity!=authority or value['worker_id']!=worker or int(epoch)<=value['denied_through_epoch']
        connection.close()
    except Exception:deny=True
    if deny:print("REVIVAL_REFUSED",flush=True);sys.exit(23)
    print("READY "+nonce,flush=True)
for line in sys.stdin:
    if line.strip()=="PING":
        with open(os.path.join(home,"saved.txt"),"rb") as work: value=hashlib.sha256(work.read(1024*1024+1)).hexdigest()
        print("ALIVE "+nonce+" "+value,flush=True)
    elif line.strip()=="STOP":break
    else:sys.exit(24)
'''


class WorkerProcesses(FixtureProcesses):
    """Reuse exact handles/FD exclusion; children read the atomic SQLite floor."""
    def __init__(self, root, authority_id, database, *, initialize):
        if initialize:
            super().__init__(root, authority_id)
        else:
            self.backend = Path(root)
            self.lock_path = self.backend / 'activation.lock'
            if self.backend.is_symlink() or not self.lock_path.is_file(): raise ValueError('fixture_process_state_missing')
            self.children = []; self.services = {}; self.endpoint_children = {}; self.cleanup_verified = False
            self.authority_id = authority_id; self.local = threading.local()
        self.database = Path(database)

    def launch(self, worker, course, home, *, epoch=1, expect_refusal=False):
        key = self.identity(worker, course); nonce = uuid.uuid4().hex
        if self.running_count(worker, course): raise ValueError('fixture_owned_service_exists')
        held = getattr(self.local, 'fds', {}); fd = held.get(key)
        if held and fd is None: raise ValueError('fixture_wrong_activation_lock')
        child = subprocess.Popen([sys.executable, '-I', '-u', '-c', SQL_SERVICE, str(home), str(self.database),
            str(self.activation_lock_path(worker, course)), nonce, worker, course, str(epoch), self.authority_id,
            str(fd if fd is not None else -1)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True, bufsize=0, pass_fds=(fd,) if fd is not None else ())
        self.children.append(child); self.endpoint_children[child] = key
        reply = self.receive(child)
        if expect_refusal:
            if reply != 'REVIVAL_REFUSED' or child.wait(timeout=3) != 23: raise ValueError('fixture_revival_not_refused')
            return None
        if reply != 'READY ' + nonce or child.poll() is not None: raise ValueError('fixture_service_not_ready')
        self.services[key] = (child, nonce, Path(home))
        return child


def owned_effect(method):
    """Fixture shutdown cannot certify cleanup while an effect is in flight."""
    @wraps(method)
    def invoke(self, *args, **kwargs):
        if self.closing or not self.effects.acquire(timeout=5): raise ValueError('fixture_worker_closing')
        try:
            if self.closing: raise ValueError('fixture_worker_closing')
            return method(self, *args, **kwargs)
        finally: self.effects.release()
    return invoke


class LocalWorkerBackend:
    fixture_only = True
    scope = 'course'

    def __init__(self, directory, *, initialize=False):
        self.effects = threading.RLock(); self.closing = False
        self.root = Path(directory); self.config = read_private(self.root / 'config.json')
        self.authority_id = self.config['authority_id']; self.worker_id = self.config['worker_id']
        self.courses = sorted(self.config['courses'][self.worker_id])
        self.database = self.root / 'barriers.sqlite'; self.sentinel = self.root / 'barriers.json'
        if initialize:
            fd = os.open(self.database, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600); os.close(fd)
            with self.connection(initial=True) as conn:
                conn.executescript('BEGIN IMMEDIATE; CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);'
                    'CREATE TABLE barriers(course TEXT PRIMARY KEY,record TEXT NOT NULL);'
                    'CREATE TABLE history(sequence INTEGER PRIMARY KEY,record TEXT NOT NULL,digest TEXT NOT NULL);')
                conn.executemany('INSERT INTO metadata VALUES(?,?)', [('authority_id', self.authority_id), ('binding', digest(self.config))])
                for course in self.courses:
                    self._store(conn, course, {'authority_id': self.authority_id, 'worker_id': self.worker_id,
                        'course_id': course, 'barrier_id': uuid.uuid4().hex, 'denied_through_epoch': 0, 'request_sha256': '0' * 64})
                conn.commit()
            info = self.database.stat()
            private_json(self.sentinel, {'authority_id': self.authority_id, 'binding': digest(self.config), 'device': info.st_dev, 'inode': info.st_ino})
        with self.connection() as conn:
            conn.execute('BEGIN'); self._validate(conn); conn.commit()
        self.processes = WorkerProcesses(self.root / 'process-control', self.authority_id, self.database, initialize=initialize)

    @contextmanager
    def connection(self, initial=False):
        info = self.database.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
                or info.st_nlink != 1 or info.st_size > 8 * 1024**2): raise ValueError('fixture_barrier_state_invalid')
        if not initial and read_private(self.sentinel) != {'authority_id': self.authority_id,
                'binding': digest(self.config), 'device': info.st_dev, 'inode': info.st_ino}:
            raise ValueError('fixture_barrier_binding_changed')
        conn = sqlite3.connect(self.database.as_uri() + '?mode=rw', uri=True, isolation_level=None, timeout=1)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA journal_mode=DELETE'); conn.execute('PRAGMA synchronous=EXTRA'); conn.execute('PRAGMA busy_timeout=1000')
            if (conn.execute('PRAGMA journal_mode').fetchone()[0] != 'delete'
                    or conn.execute('PRAGMA synchronous').fetchone()[0] != 3
                    or conn.execute('PRAGMA busy_timeout').fetchone()[0] != 1000):
                raise ValueError('fixture_barrier_durability_invalid')
            yield conn
        finally: conn.close()

    def _store(self, conn, course, value):
        row = conn.execute('SELECT sequence,digest FROM history ORDER BY sequence DESC LIMIT 1').fetchone()
        record = {'sequence': 1 if row is None else row['sequence'] + 1, 'previous': '0' * 64 if row is None else row['digest'], 'barrier': value}
        if record['sequence'] > 256: raise ValueError('fixture_barrier_history_bound')
        conn.execute('INSERT OR REPLACE INTO barriers VALUES(?,?)', (course, json.dumps(value, sort_keys=True)))
        conn.execute('INSERT INTO history VALUES(?,?,?)', (record['sequence'], json.dumps(record, sort_keys=True), digest(record)))

    def _validate(self, conn):
        if dict(conn.execute('SELECT key,value FROM metadata')) != {'authority_id': self.authority_id, 'binding': digest(self.config)}:
            raise ValueError('fixture_barrier_identity_changed')
        previous = '0' * 64; latest = {}; floors = {}; rows = conn.execute('SELECT * FROM history ORDER BY sequence LIMIT 257').fetchall()
        if not 1 <= len(rows) <= 256: raise ValueError('fixture_barrier_history_invalid')
        for index, row in enumerate(rows, 1):
            record = json.loads(row['record']); value = record['barrier']; course = value['course_id']
            if (set(record) != {'sequence', 'previous', 'barrier'} or record['sequence'] != index or row['sequence'] != index
                    or record['previous'] != previous or digest(record) != row['digest'] or course not in self.courses
                    or set(value) != {'authority_id', 'worker_id', 'course_id', 'barrier_id', 'denied_through_epoch', 'request_sha256'}
                    or value['authority_id'] != self.authority_id or value['worker_id'] != self.worker_id
                    or not isinstance(value['barrier_id'], str) or not HEX32.fullmatch(value['barrier_id'])
                    or not isinstance(value['request_sha256'], str) or not HEX64.fullmatch(value['request_sha256'])
                    or type(value['denied_through_epoch']) is not int or not floors.get(course, 0) <= value['denied_through_epoch'] <= 10**9
                    or course in latest and value['barrier_id'] != latest[course]['barrier_id']): raise ValueError('fixture_barrier_history_invalid')
            previous = row['digest']; latest[course] = value; floors[course] = value['denied_through_epoch']
        current = {r['course']: json.loads(r['record']) for r in conn.execute('SELECT * FROM barriers LIMIT 17')}
        if current != latest or set(current) != set(self.courses): raise ValueError('fixture_barrier_history_invalid')

    def inspect_worker(self, worker):
        if worker != self.worker_id: raise ValueError('fixture_worker_scope_invalid')
        return dict(worker_evidence(read_private(self.root / 'identity.json'), worker))

    def home(self, course): return self.root / 'courses' / course / 'retained'

    def inspect_course(self, course, worker):
        self.inspect_worker(worker)
        if course not in self.courses: raise ValueError('fixture_course_scope_invalid')
        directory = self.root / 'courses' / course
        return dict(course_evidence({'logical_identity': read_private(directory / 'logical.json'),
            'profile': read_private(directory / 'profile.json'), 'runtime_sha256': sha256_file(directory / 'runtime.py'),
            'snapshot_sha256': sha256_file(directory / 'snapshot.fixture'), 'retained_tree_sha256': retained_digest(self.home(course)),
            'admission_floor': self.barrier(worker, course)['denied_through_epoch'] + 1,
            'running_count': self.processes.running_count(worker, course)}))

    def barrier(self, worker, course):
        if worker != self.worker_id or course not in self.courses: raise ValueError('fixture_endpoint_invalid')
        with self.connection() as conn:
            conn.execute('BEGIN'); self._validate(conn)
            result = json.loads(conn.execute('SELECT record FROM barriers WHERE course=?', (course,)).fetchone()['record']); conn.commit()
        return result

    def validate_request(self, request, workers):
        if workers != [self.worker_id] or request['authority_id'] != self.authority_id or request['course_id'] not in self.courses:
            raise ValueError('fixture_request_scope_invalid')
        observed = self.inspect_worker(self.worker_id)
        prefix = '' if 'worker_id' in request else ('source_' if request['source_worker_id'] == self.worker_id else 'receiver_')
        if request[prefix + 'provider_id'] != observed['provider_id'] or request[prefix + 'boot_id'] != observed['boot_id']:
            raise ValueError('fixture_worker_incarnation_changed')

    @contextmanager
    def locks(self, endpoints):
        if any(w != self.worker_id or c not in self.courses for w, c in endpoints): raise ValueError('fixture_lock_scope_invalid')
        with self.processes.activation_locks(endpoints): yield

    @contextmanager
    def activation_context(self, request):
        with self.locks([(self.worker_id, request['course_id'])]):
            self.validate_request(request, [self.worker_id])
            if request['epoch'] <= self.barrier(self.worker_id, request['course_id'])['denied_through_epoch']:
                raise ValueError('fixture_epoch_fenced')
            yield

    @owned_effect
    def fence(self, request, workers):
        self.validate_request(request, workers); course = request['course_id']
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE'); self._validate(conn)
            old = json.loads(conn.execute('SELECT record FROM barriers WHERE course=?', (course,)).fetchone()['record'])
            value = dict(old, denied_through_epoch=max(old['denied_through_epoch'], request['old_epoch']), request_sha256=digest(request))
            if value != old: self._store(conn, course, value)
            self._validate(conn); conn.commit()

    @owned_effect
    def stop_endpoint(self, worker, course):
        if worker != self.worker_id or course not in self.courses: raise ValueError('fixture_stop_scope_invalid')
        self.processes.stop(worker, course)

    @owned_effect
    def runtime(self, request, operation, payload, authorize):
        course = request['course_id']; worker = request['worker_id']
        self.validate_request(request, [worker])
        if payload != {} or not callable(authorize) or self.processes.identity(worker, course) not in getattr(self.processes.local, 'fds', {}):
            raise ValueError('fixture_runtime_requires_held_admission')
        authorize()
        if operation in ('hub.authorize', 'guardian.observe'):
            return {'authorized': True, 'epoch': request['epoch'], 'worker_id': worker, 'course_id': course}
        if operation == 'hub.spawn':
            if not self.processes.running_count(worker, course):
                authorize(); self.processes.launch(worker, course, self.home(course), epoch=request['epoch'])
                try: authorize()
                except Exception:
                    self.stop_endpoint(worker, course); raise
        elif operation == 'hub.stop':
            authorize(); self.stop_endpoint(worker, course); return {'stopped': True}
        elif operation != 'hub.poll': raise ValueError('fixture_runtime_operation_invalid')
        running = self.processes.running_count(worker, course) == 1
        proof = None
        if running:
            pid = self.processes.live(worker, course); _, nonce, _ = self.processes.services[self.processes.identity(worker, course)]
            proof = digest({'pid': pid, 'nonce': nonce})
        return {'running': running, 'process_sha256': proof, 'work_sha256': sha256_file(self.home(course) / 'saved.txt')}

    def close(self):
        self.closing = True
        if not self.effects.acquire(timeout=5): raise ValueError('fixture_effect_cleanup_unverified')
        try: return self.processes.close()
        finally: self.effects.release()


def create_worker_files(root, authority_id, worker, courses, *, source=None):
    root = Path(root); root.mkdir(mode=0o700)
    identity = {'worker_id': worker, 'provider_id': 'provider-' + worker, 'boot_id': uuid.uuid4().hex,
        'policy_sha256': digest({'worker': worker, 'fixture_only': True}), 'memory_budget_mb': 3072,
        'cpu_budget': 2.5, 'capacity_memory_mb': 3072, 'capacity_cpu': 2.5}
    private_json(root / 'identity.json', identity); (root / 'courses').mkdir(mode=0o700)
    config = {'authority_id': authority_id, 'worker_id': worker, 'workers': {worker: identity}, 'courses': {worker: {c: {} for c in courses}}}
    private_json(root / 'config.json', config)
    for course in courses:
        destination = root / 'courses' / course
        if source is not None:
            shutil.copytree(Path(source) / 'courses' / course, destination, copy_function=shutil.copy2)
            continue
        destination.mkdir(mode=0o700); (destination / 'retained').mkdir(mode=0o700)
        private_json(destination / 'logical.json', {'origin': 'https://' + course + '.remote-fixture.test',
            'dns_subtree': course + '.remote-fixture.test', 'issuer': 'https://id.remote-fixture.test/realms/' + course, 'client_id': course})
        private_json(destination / 'profile.json', {'active_limit': 1, 'learner_memory_mb': 512,
            'learner_cpu': .25, 'memory_mb': 1536, 'cpu': 1.25})
        for name, body in [('runtime.py', SQL_SERVICE), ('retained/saved.txt', 'synthetic retained ' + course + ' coursework\n')]:
            path = destination / name; path.write_text(body); path.chmod(0o600)
        refresh_snapshot(root, course)
    return config


def refresh_snapshot(root, course):
    path = Path(root) / 'courses' / course / 'snapshot.fixture'
    raw = ('OPAQUE SYNTHETIC LOCAL SNAPSHOT\n' + retained_digest(Path(root) / 'courses' / course / 'retained') + '\n').encode()
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        os.fchmod(stream.fileno(), 0o600); stream.write(raw); stream.flush(); os.fsync(stream.fileno())


def worker_main(root, initialize):
    from worker_transport import WorkerServer
    from worker_channel_state import WorkerJournal
    backend = LocalWorkerBackend(root, initialize=initialize); settings = read_private(Path(root) / 'transport.json')
    journal_args = (Path(root) / 'channel-state', backend.authority_id, backend.worker_id, backend.courses, digest(backend.config))
    journal = (WorkerJournal.create if initialize else WorkerJournal.open)(*journal_args)
    server = None; thread = None; port = settings['port']
    def start():
        nonlocal server, thread, port
        server = WorkerServer(('127.0.0.1', port), backend, backend.worker_id, settings['certfile'], settings['keyfile'],
            settings['ca_file'], settings['acl_file'], journal=journal, runtime_dispatch=backend.runtime)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True); thread.start()
    def stop():
        nonlocal server, thread
        if server is not None:
            server.shutdown(); server.server_close(); thread.join(4)
            if thread.is_alive(): raise ValueError('fixture_worker_listener_cleanup_failed')
            server = None
    try:
        start(); print(json.dumps({'ready': True, 'port': port}), flush=True)
        for line in sys.stdin:
            if len(line) > 1024: raise ValueError('fixture_control_bounds')
            command = json.loads(line)
            if command == {'operation': 'pause'}: stop(); result = {'paused': True}
            elif command == {'operation': 'resume'}:
                if server is not None: raise ValueError('fixture_listener_already_running')
                journal = WorkerJournal.open(*journal_args); start(); result = {'ready': True, 'port': port}
            elif command == {'operation': 'stop'}:
                stop(); backend.close(); result = {'stopped': True, 'cleanup_verified': backend.processes.cleanup_verified}
                print(json.dumps(result), flush=True); return
            else: raise ValueError('fixture_control_operation_refused')
            print(json.dumps(result), flush=True)
    finally:
        stop(); backend.close()


class WorkerProcess:
    def __init__(self, directory, *, initialize):
        self.directory = Path(directory)
        self.child = subprocess.Popen([sys.executable, '-I', '-u', str(Path(__file__).resolve()),
            '--worker', str(self.directory), 'create' if initialize else 'open'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, bufsize=0)
        self.cleanup_verified = False
        try:
            self.ready = self.receive()
            if set(self.ready) != {'ready', 'port'} or self.ready['ready'] is not True or type(self.ready['port']) is not int:
                raise ValueError('fixture_worker_start_refused')
        except Exception:
            FixtureProcesses.reap(self.child)
            raise ValueError('fixture_worker_start_refused') from None

    def receive(self):
        return json.loads(FixtureProcesses.receive(self.child))

    def command(self, operation):
        self.child.stdin.write((json.dumps({'operation': operation}) + '\n').encode()); self.child.stdin.flush()
        return self.receive()

    def close(self):
        if self.child.poll() is None:
            try:
                result = self.command('stop')
                if result != {'stopped': True, 'cleanup_verified': True}: raise ValueError('fixture_worker_cleanup_failed')
                self.child.wait(timeout=5)
            except Exception:
                FixtureProcesses.reap(self.child)
                raise ValueError('fixture_worker_cleanup_unverified') from None
        if self.child.returncode != 0: raise ValueError('fixture_worker_exit_failed')
        for stream in (self.child.stdin, self.child.stdout):
            if stream is not None: stream.close()
        self.cleanup_verified = True
        return True


class RemoteFixture:
    """Two disjoint subprocess servers, one controller and disposable TLS keys."""
    def __init__(self, root):
        from authority_test_pki import TestPKI
        from ownership_contract import Authority
        from remote_backend import RemoteBackend
        self.root = Path(root).resolve(); self.workers = {}; self.cleanup_verified = False
        self.root.mkdir(mode=0o700, exist_ok=False)
        class Empty:
            fixture_only = True
            scope = 'course'
        initial = Authority.create(self.root / 'authority', Empty())
        self.authority_id = initial.authority_id
        self.pki = TestPKI(self.root / 'pki'); self.credentials = self.pki.issue('controller')
        self.directories = {}; self.clients = {}; identities = {}; self.server_certificates = {}
        try:
            for worker in ('worker-one', 'worker-two'):
                courses = ['main-course', 'peer-course'] if worker == 'worker-one' else ['main-course']
                directory = self.root / worker
                create_worker_files(directory, self.authority_id, worker, courses,
                    source=self.directories.get('worker-one') if worker == 'worker-two' else None)
                self.directories[worker] = directory
                cert, key = self.pki.issue(worker, 'server', hostname='localhost')
                for name, source in [('server.pem', cert), ('server.key', key), ('ca.pem', self.pki.ca_cert)]:
                    shutil.copyfile(source, directory / name); (directory / name).chmod(0o600)
                self.server_certificates[worker] = cert
                private_json(directory / 'acl.json', {'version': 'educloud-worker-channel-v1',
                    'authority_id': self.authority_id, 'worker_id': worker,
                    'controllers': {self.pki.fingerprint(self.credentials[0]): courses}})
                private_json(directory / 'transport.json', {'port': 0,
                    'certfile': str(directory / 'server.pem'), 'keyfile': str(directory / 'server.key'),
                    'ca_file': str(directory / 'ca.pem'), 'acl_file': str(directory / 'acl.json')})
                self.workers[worker] = WorkerProcess(directory, initialize=True)
                identities[worker] = read_private(directory / 'identity.json'); self.set_client(worker)
            self.backend = RemoteBackend(self.authority_id, identities, self.clients,
                {'worker-one': ['main-course', 'peer-course'], 'worker-two': ['main-course']})
            self.authority = Authority.open(self.root / 'authority', self.authority_id, self.backend)
            for worker in self.workers: self.authority.register_worker(worker)
            for course in ('main-course', 'peer-course'): self.authority.register_course(course, 'worker-one')
        except Exception:
            self.close(); raise

    def set_client(self, worker):
        from worker_transport import WorkerClient
        self.clients[worker] = WorkerClient('localhost', self.workers[worker].ready['port'], self.authority_id,
            worker, self.pki.fingerprint(self.server_certificates[worker]), self.pki.ca_cert, *self.credentials)
        if hasattr(self, 'backend'): self.backend.clients[worker] = self.clients[worker]

    def runtime(self, worker, course, operation='hub.poll'):
        with self.authority.admission(course, worker):
            return self.backend.runtime(worker, course, operation, {})

    def state(self, worker, course='main-course'):
        with self.clients[worker].session(course, 'inspect') as channel: return channel.call('state')

    def restart(self, worker):
        self.workers[worker].close()
        self.workers[worker] = WorkerProcess(self.directories[worker], initialize=False)
        self.set_client(worker)

    def close(self):
        failures = []
        for process in reversed(list(self.workers.values())):
            try: process.close()
            except Exception: failures.append(True)
        if failures or any(not process.cleanup_verified for process in self.workers.values()):
            raise ValueError('fixture_owned_cleanup_unverified')
        self.cleanup_verified = True
        return True


if __name__ == '__main__':
    try:
        if len(sys.argv) != 4 or sys.argv[1] != '--worker' or sys.argv[3] not in ('create', 'open'): raise ValueError()
        worker_main(Path(sys.argv[2]), sys.argv[3] == 'create')
    except Exception:
        raise SystemExit('remote_worker_fixture_failed') from None
