"""Private reviewed credentials for configured local workers, never provisioning.

Fresh binding observations precede the short registry CAS; they are not atomic
with another database or provider. Inode/sentinel/history/clock checks detect
ordinary state loss or replacement, not privileged restoration of every plane.
"""
from contextlib import contextmanager
from copy import deepcopy
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time
import uuid

VERSION = 'educloud-worker-enrollment-v1'
HEX32 = re.compile(r'[0-9a-f]{32}\Z'); HEX64 = re.compile(r'[0-9a-f]{64}\Z'); SLUG = re.compile(r'[a-z][a-z0-9-]{0,63}\Z')
MAX_JSON = 512 * 1024; MAX_ROWS = 2048; MAX_CERTIFICATES = 1024
ACTIONS = {'enroll', 'stage', 'activate', 'retire', 'revoke', 'revoke_worker'}
PLAN_FIELDS = {'version', 'authority_id', 'store_id', 'binding_sha256', 'plan_id', 'revision', 'head_sha256', 'state_sha256',
               'issued_at', 'action', 'worker_id', 'credentials', 'slot', 'target_fingerprint', 'overlap_seconds', 'plan_sha256'}
CERT_FIELDS = {'fingerprint', 'spki_sha256', 'not_before', 'not_after'}
RECORD_FIELDS = CERT_FIELDS | {'worker_id', 'slot', 'state', 'activated_at', 'overlap_until', 'overlap_seconds'}


class EnrollmentError(ValueError):
    def __init__(self, code): self.code = code; super().__init__(code)


def refuse(code='worker_enrollment_refused'): raise EnrollmentError(code)


def guarded(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        try: return fn(*args, **kwargs)
        except EnrollmentError: raise
        except Exception: refuse('worker_enrollment_state_unavailable')
    return run


def encoded(value): return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
def digest(value): return hashlib.sha256(encoded(value)).hexdigest()
def hex_value(value, pattern=HEX64): return isinstance(value, str) and pattern.fullmatch(value)
def slug(value): return isinstance(value, str) and SLUG.fullmatch(value)
def timestamp(value): return type(value) is int and 0 <= value <= 2**40


def parsed(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: refuse()
            result[key] = value
        return result
    if len(raw) > MAX_JSON: refuse('worker_enrollment_quota_exceeded')
    return json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: refuse())


def private(path, directory=False):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path: refuse('worker_enrollment_state_unavailable')
    info = path.lstat()
    if (info.st_uid != os.geteuid() or info.st_mode & 0o077 or not directory and info.st_nlink != 1
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))): refuse('worker_enrollment_state_unavailable')
    return info


def sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)


def read_private(path, protected):
    info = private(path)
    if info.st_size > MAX_JSON or (info.st_dev, info.st_ino) == protected: refuse('worker_enrollment_state_unavailable')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        actual = os.fstat(source.fileno())
        if (info.st_dev, info.st_ino) != (actual.st_dev, actual.st_ino): refuse('worker_enrollment_state_unavailable')
        return parsed(source.read(MAX_JSON + 1))


def publish(path, value):
    private(path.parent, True); fd, name = tempfile.mkstemp(prefix='.enrollment-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as target: target.write(encoded(value)); target.flush(); os.fsync(target.fileno())
        os.link(name, path); sync(path.parent)
    finally:
        Path(name).unlink(missing_ok=True); sync(path.parent)


def validate_binding(binding):
    if (not isinstance(binding, dict) or set(binding) != {'runtime_config_sha256', 'ca_sha256', 'home_root_bindings_sha256', 'workers'}
            or any(not hex_value(binding[key]) for key in ('runtime_config_sha256', 'ca_sha256', 'home_root_bindings_sha256'))
            or not isinstance(binding['workers'], dict) or not 1 <= len(binding['workers']) <= 32): refuse('worker_enrollment_binding_changed')
    providers = set()
    for worker, value in binding['workers'].items():
        if (not slug(worker) or not isinstance(value, dict) or set(value) != {'provider_id', 'boot_id', 'evidence_sha256', 'courses'}
                or not slug(value['provider_id']) or value['provider_id'] in providers or not hex_value(value['boot_id'], HEX32)
                or not hex_value(value['evidence_sha256']) or not isinstance(value['courses'], list) or not 1 <= len(value['courses']) <= 32
                or any(not slug(course) for course in value['courses']) or value['courses'] != sorted(set(value['courses']))): refuse('worker_enrollment_binding_changed')
        providers.add(value['provider_id'])
    if len(encoded(binding)) > MAX_JSON: refuse('worker_enrollment_quota_exceeded')
    return deepcopy(binding)


def certificate(value, now=None):
    if (not isinstance(value, dict) or set(value) != CERT_FIELDS or not hex_value(value['fingerprint']) or not hex_value(value['spki_sha256'])
            or not timestamp(value['not_before']) or not timestamp(value['not_after']) or value['not_before'] >= value['not_after']): refuse()
    if now is not None and not value['not_before'] <= now < value['not_after']: refuse('worker_enrollment_certificate_not_current')
    return value


class EnrollmentStore:
    def __init__(self, directory, authority_id, binding, clock=time.time):
        if not hex_value(authority_id, HEX32) or not callable(clock): refuse()
        self.directory = Path(directory); self.authority_id = authority_id; self.binding = validate_binding(binding); self.clock = clock
        self.database = self.directory / 'enrollment.sqlite'; self.sentinel = self.directory / 'enrollment.json'

    @classmethod
    @guarded
    def create(cls, directory, authority_id, binding, clock=time.time):
        self = cls(directory, authority_id, binding, clock); private(self.directory.parent, True)
        self.directory.mkdir(mode=0o700, exist_ok=False)
        fd = os.open(self.database, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600); os.close(fd)
        store_id = uuid.uuid4().hex; now = self._now(); state = self._initial()
        conn = self._raw()
        try:
            conn.executescript('BEGIN IMMEDIATE; CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);'
                'CREATE TABLE state(singleton INTEGER PRIMARY KEY CHECK(singleton=1),record TEXT NOT NULL);'
                'CREATE TABLE plans(plan_id TEXT PRIMARY KEY,record TEXT NOT NULL,consumed INTEGER NOT NULL CHECK(consumed IN(0,1)));'
                'CREATE TABLE events(sequence INTEGER PRIMARY KEY,record TEXT NOT NULL);')
            meta = {'version': VERSION, 'authority_id': authority_id, 'store_id': store_id, 'binding': encoded(self.binding).decode(),
                    'revision': '0', 'head': '0' * 64, 'clock_floor': str(now), 'plan_count': '0'}
            conn.executemany('INSERT INTO metadata VALUES(?,?)', meta.items())
            conn.execute('INSERT INTO state VALUES(1,?)', (encoded(state).decode(),)); conn.execute('COMMIT')
        finally: conn.close()
        info = private(self.database)
        publish(self.sentinel, {'version': VERSION, 'authority_id': authority_id, 'store_id': store_id,
                               'device': info.st_dev, 'inode': info.st_ino})
        return cls.open(directory, authority_id, binding, clock)

    @classmethod
    @guarded
    def open(cls, directory, authority_id, binding, clock=time.time):
        self = cls(directory, authority_id, binding, clock)
        with self._connection() as conn:
            conn.execute('BEGIN IMMEDIATE'); meta, _ = self._validate(conn, full=True); self._advance_clock(conn, meta); conn.execute('COMMIT')
        return self

    def _initial(self):
        return {'workers': {worker: {'enrolled': False, 'revoked': False} for worker in self.binding['workers']}, 'certificates': {}}

    def _now(self):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 2**40: refuse('worker_enrollment_clock_rollback')
        return int(value)

    def _raw(self):
        conn = sqlite3.connect(self.database.as_uri() + '?mode=rw', uri=True, timeout=1, isolation_level=None); conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA journal_mode=DELETE'); conn.execute('PRAGMA synchronous=EXTRA'); conn.execute('PRAGMA busy_timeout=1000')
            if (conn.execute('PRAGMA journal_mode').fetchone()[0] != 'delete' or conn.execute('PRAGMA synchronous').fetchone()[0] != 3
                    or conn.execute('PRAGMA busy_timeout').fetchone()[0] != 1000): refuse('worker_enrollment_state_unavailable')
            return conn
        except Exception: conn.close(); raise

    @contextmanager
    def _connection(self):
        private(self.directory, True); info = private(self.database)
        if info.st_size > 64 * 1024**2: refuse('worker_enrollment_quota_exceeded')
        sentinel = read_private(self.sentinel, (info.st_dev, info.st_ino))
        if (not isinstance(sentinel, dict) or set(sentinel) != {'version', 'authority_id', 'store_id', 'device', 'inode'}
                or sentinel['version'] != VERSION or sentinel['authority_id'] != self.authority_id or not hex_value(sentinel['store_id'], HEX32)
                or type(sentinel['device']) is not int or type(sentinel['inode']) is not int
                or (sentinel['device'], sentinel['inode']) != (info.st_dev, info.st_ino)): refuse('worker_enrollment_state_unavailable')
        conn = self._raw()
        try: yield conn
        finally: conn.close()

    def _slots(self, worker): return {'guardian'} | {'hub:' + course for course in self.binding['workers'][worker]['courses']}

    def _plan(self, value, store_id):
        if (not isinstance(value, dict) or set(value) != PLAN_FIELDS or value['version'] != VERSION or value['authority_id'] != self.authority_id
                or value['store_id'] != store_id or value['binding_sha256'] != digest(self.binding) or not hex_value(value['plan_id'], HEX32)
                or type(value['revision']) is not int or not 0 <= value['revision'] <= MAX_ROWS or not timestamp(value['issued_at'])
                or not hex_value(value['head_sha256']) or not hex_value(value['state_sha256']) or value['worker_id'] not in self.binding['workers']
                or value['action'] not in ACTIONS or value['plan_sha256'] != digest({k: v for k, v in value.items() if k != 'plan_sha256'})): refuse('worker_enrollment_state_unavailable')
        action = value['action']; credentials = value['credentials']; slot = value['slot']; target = value['target_fingerprint']; overlap = value['overlap_seconds']
        if action == 'enroll':
            if not isinstance(credentials, dict) or set(credentials) != self._slots(value['worker_id']) or slot is not None or target is not None or overlap is not None: refuse()
            for cert in credentials.values(): certificate(cert)
        elif action == 'stage':
            if slot not in self._slots(value['worker_id']) or target is not None or overlap is not None: refuse()
            certificate(credentials)
        elif action == 'activate':
            if slot not in self._slots(value['worker_id']) or credentials is not None or target is not None or type(overlap) is not int or not 1 <= overlap <= 300: refuse()
        elif action in ('retire', 'revoke'):
            if not hex_value(target) or credentials is not None or slot is not None or overlap is not None: refuse()
        elif any(x is not None for x in (credentials, slot, target, overlap)): refuse()
        return value

    def _schema(self, state):
        if not isinstance(state, dict) or set(state) != {'workers', 'certificates'} or set(state['workers']) != set(self.binding['workers']): refuse('worker_enrollment_state_unavailable')
        for worker in state['workers'].values():
            if not isinstance(worker, dict) or set(worker) != {'enrolled', 'revoked'} or any(type(v) is not bool for v in worker.values()) or worker['revoked'] and not worker['enrolled']: refuse('worker_enrollment_state_unavailable')
        if not isinstance(state['certificates'], dict) or len(state['certificates']) > MAX_CERTIFICATES: refuse('worker_enrollment_quota_exceeded')
        keys = set(); active = set(); staged = set()
        for fingerprint, record in state['certificates'].items():
            if not isinstance(record, dict) or set(record) != RECORD_FIELDS or record['fingerprint'] != fingerprint: refuse('worker_enrollment_state_unavailable')
            certificate({k: record[k] for k in CERT_FIELDS})
            worker = record['worker_id']; slot = record['slot']; status = record['state']
            if (worker not in state['workers'] or slot not in self._slots(worker) or not state['workers'][worker]['enrolled'] or record['spki_sha256'] in keys
                    or status not in ('ACTIVE', 'STAGED', 'OVERLAP', 'RETIRED', 'REVOKED') or state['workers'][worker]['revoked'] and status != 'REVOKED'): refuse('worker_enrollment_state_unavailable')
            keys.add(record['spki_sha256'])
            if status in ('ACTIVE', 'STAGED'):
                selected = active if status == 'ACTIVE' else staged
                if (worker, slot) in selected: refuse('worker_enrollment_state_unavailable')
                selected.add((worker, slot))
            if record['activated_at'] is not None and not timestamp(record['activated_at']): refuse('worker_enrollment_state_unavailable')
            if status == 'STAGED' and any(record[k] is not None for k in ('activated_at', 'overlap_until', 'overlap_seconds')): refuse('worker_enrollment_state_unavailable')
            if status == 'ACTIVE' and (record['activated_at'] is None or record['overlap_until'] is not None or record['overlap_seconds'] is not None): refuse('worker_enrollment_state_unavailable')
            if record['overlap_until'] is not None:
                if (not timestamp(record['overlap_until']) or record['activated_at'] is None or type(record['overlap_seconds']) is not int
                        or not 1 <= record['overlap_seconds'] <= 300 or record['overlap_until'] != min(record['not_after'], record['activated_at'] + record['overlap_seconds'])): refuse('worker_enrollment_state_unavailable')
            elif record['overlap_seconds'] is not None or status == 'OVERLAP': refuse('worker_enrollment_state_unavailable')
        return state

    def _transition(self, state, plan, now):
        state = deepcopy(state); worker = plan['worker_id']; action = plan['action']; account = state['workers'][worker]
        certs = state['certificates']; slot = plan['slot']
        if action == 'enroll':
            if account['enrolled']: refuse('worker_enrollment_already_enrolled')
            account['enrolled'] = True
        elif not account['enrolled'] or account['revoked']: refuse('worker_enrollment_worker_unavailable')
        def add(cert, target_slot, status):
            certificate(cert, now)
            if cert['fingerprint'] in certs or any(c['spki_sha256'] == cert['spki_sha256'] for c in certs.values()): refuse('worker_enrollment_certificate_reuse')
            certs[cert['fingerprint']] = dict(cert, worker_id=worker, slot=target_slot, state=status,
                activated_at=now if status == 'ACTIVE' else None, overlap_until=None, overlap_seconds=None)
        if action == 'enroll':
            for target_slot, cert in sorted(plan['credentials'].items()): add(cert, target_slot, 'ACTIVE')
        elif action == 'stage':
            if any(c['worker_id'] == worker and c['slot'] == slot and (c['state'] == 'STAGED' or c['state'] == 'OVERLAP' and self._live(c, now)) for c in certs.values()): refuse('worker_enrollment_rotation_pending')
            add(plan['credentials'], slot, 'STAGED')
        elif action == 'activate':
            staged = [c for c in certs.values() if c['worker_id'] == worker and c['slot'] == slot and c['state'] == 'STAGED']
            if len(staged) != 1: refuse('worker_enrollment_rotation_pending')
            certificate({k: staged[0][k] for k in CERT_FIELDS}, now)
            for cert in certs.values():
                if cert['worker_id'] != worker or cert['slot'] != slot: continue
                if cert['state'] == 'OVERLAP':
                    if self._live(cert, now): refuse('worker_enrollment_rotation_pending')
                    cert['state'] = 'RETIRED'
                if cert['state'] == 'ACTIVE':
                    cert.update(state='OVERLAP', activated_at=now, overlap_seconds=plan['overlap_seconds'], overlap_until=min(cert['not_after'], now + plan['overlap_seconds']))
            staged[0].update(state='ACTIVE', activated_at=now)
        elif action in ('retire', 'revoke'):
            target = certs.get(plan['target_fingerprint'])
            if target is None or target['worker_id'] != worker or target['state'] in ('RETIRED', 'REVOKED') or action == 'retire' and target['state'] != 'OVERLAP': refuse('worker_enrollment_target_unavailable')
            target['state'] = 'RETIRED' if action == 'retire' else 'REVOKED'
        elif action == 'revoke_worker':
            account['revoked'] = True
            for cert in certs.values():
                if cert['worker_id'] == worker: cert['state'] = 'REVOKED'
        return self._schema(state)

    @staticmethod
    def _live(cert, now): return cert['not_before'] <= now < cert['not_after'] and (cert['state'] == 'ACTIVE' or cert['state'] == 'OVERLAP' and now < cert['overlap_until'])

    def _validate(self, conn, full=False):
        meta = dict(conn.execute('SELECT key,value FROM metadata LIMIT 9'))
        sentinel = read_private(self.sentinel, (self.database.lstat().st_dev, self.database.lstat().st_ino))
        if (set(meta) != {'version', 'authority_id', 'store_id', 'binding', 'revision', 'head', 'clock_floor', 'plan_count'}
                or any(meta[k] != sentinel[k] for k in ('version', 'authority_id', 'store_id')) or parsed(meta['binding'].encode()) != self.binding
                or not hex_value(meta['head']) or any(not re.fullmatch(r'[0-9]{1,13}', meta[k]) for k in ('revision', 'clock_floor', 'plan_count'))
                or not 0 <= int(meta['revision']) <= MAX_ROWS or not 0 <= int(meta['plan_count']) <= MAX_ROWS or not timestamp(int(meta['clock_floor']))): refuse('worker_enrollment_state_unavailable')
        rows = conn.execute('SELECT singleton,record FROM state LIMIT 2').fetchall()
        if len(rows) != 1 or type(rows[0]['singleton']) is not int or rows[0]['singleton'] != 1: refuse('worker_enrollment_state_unavailable')
        state = self._schema(parsed(rows[0]['record'].encode()))
        if conn.execute('SELECT count(*) FROM plans').fetchone()[0] != int(meta['plan_count']) or conn.execute('SELECT count(*) FROM events').fetchone()[0] != int(meta['revision']): refuse('worker_enrollment_state_unavailable')
        if conn.execute('SELECT count(*) FROM plans WHERE consumed=1').fetchone()[0] != int(meta['revision']): refuse('worker_enrollment_state_unavailable')
        plans = {}
        if full:
            for row in conn.execute('SELECT * FROM plans LIMIT ?', (MAX_ROWS + 1,)):
                value = self._plan(parsed(row['record'].encode()), meta['store_id'])
                if value['plan_id'] != row['plan_id'] or type(row['consumed']) is not int or row['consumed'] not in (0, 1): refuse('worker_enrollment_state_unavailable')
                plans[row['plan_id']] = (value, row['consumed'])
        events = conn.execute('SELECT * FROM events ORDER BY sequence ' + ('' if full else 'DESC ') + 'LIMIT ?', (MAX_ROWS + 1 if full else 1,)).fetchall()
        previous = '0' * 64; replay = self._initial(); referenced = set(); previous_time = 0
        for row in events:
            event = parsed(row['record'].encode())
            if (not isinstance(event, dict) or set(event) != {'sequence', 'plan_id', 'plan_sha256', 'previous_sha256', 'state_sha256', 'applied_at', 'digest'}
                    or type(event['sequence']) is not int or event['sequence'] != row['sequence'] or not timestamp(event['applied_at']) or event['applied_at'] > int(meta['clock_floor'])
                    or event['digest'] != digest({k: v for k, v in event.items() if k != 'digest'}) or not hex_value(event['previous_sha256']) or not hex_value(event['state_sha256'])): refuse('worker_enrollment_state_unavailable')
            if full:
                plan, consumed = plans.get(event['plan_id'], (None, None))
                if (plan is None or consumed != 1 or event['plan_id'] in referenced or event['sequence'] != len(referenced) + 1
                        or event['previous_sha256'] != previous or plan['head_sha256'] != previous or plan['revision'] != len(referenced)
                        or plan['state_sha256'] != digest(replay) or event['plan_sha256'] != plan['plan_sha256']
                        or event['applied_at'] < max(previous_time, plan['issued_at'])): refuse('worker_enrollment_state_unavailable')
                replay = self._transition(replay, plan, event['applied_at'])
                if digest(replay) != event['state_sha256']: refuse('worker_enrollment_state_unavailable')
                referenced.add(event['plan_id']); previous = event['digest']; previous_time = event['applied_at']
        if (int(meta['revision']) == 0 and (meta['head'] != '0' * 64 or state != self._initial())
                or events and (events[-1]['sequence'] != int(meta['revision']) or parsed(events[-1]['record'].encode())['digest'] != meta['head']
                              or parsed(events[-1]['record'].encode())['state_sha256'] != digest(state)) or full and replay != state): refuse('worker_enrollment_state_unavailable')
        return meta, state

    def _advance_clock(self, conn, meta):
        now = self._now()
        if now < int(meta['clock_floor']): refuse('worker_enrollment_clock_rollback')
        if now > int(meta['clock_floor']):
            conn.execute("UPDATE metadata SET value=? WHERE key='clock_floor'", (str(now),))
        return now

    @staticmethod
    def _summary(plan, status, revision):
        return {'status': status, 'plan_id': plan['plan_id'], 'plan_sha256': plan['plan_sha256'], 'action': plan['action'], 'worker_id': plan['worker_id'], 'revision': revision}

    @guarded
    def preview(self, action, worker_id, *, credentials=None, slot=None, target_fingerprint=None, overlap_seconds=None):
        with self._connection() as conn:
            conn.execute('BEGIN IMMEDIATE'); meta, state = self._validate(conn, full=True); now = self._advance_clock(conn, meta)
            if int(meta['plan_count']) >= MAX_ROWS: refuse('worker_enrollment_quota_exceeded')
            plan = {'version': VERSION, 'authority_id': self.authority_id, 'store_id': meta['store_id'], 'binding_sha256': digest(self.binding),
                'plan_id': uuid.uuid4().hex, 'revision': int(meta['revision']), 'head_sha256': meta['head'], 'state_sha256': digest(state), 'issued_at': now,
                'action': action, 'worker_id': worker_id, 'credentials': deepcopy(credentials), 'slot': slot, 'target_fingerprint': target_fingerprint, 'overlap_seconds': overlap_seconds}
            plan['plan_sha256'] = digest(plan); self._plan(plan, meta['store_id']); self._transition(state, plan, now)
            conn.execute('INSERT INTO plans VALUES(?,?,0)', (plan['plan_id'], encoded(plan).decode()))
            conn.execute("UPDATE metadata SET value=? WHERE key='plan_count'", (str(int(meta['plan_count']) + 1),)); conn.execute('COMMIT')
            return self._summary(plan, 'PLAN_READY', int(meta['revision']))

    def _read_plan(self, conn, meta, plan_id, plan_sha256):
        if not hex_value(plan_id, HEX32) or not hex_value(plan_sha256): refuse()
        row = conn.execute('SELECT * FROM plans WHERE plan_id=?', (plan_id,)).fetchone()
        if row is None: refuse('worker_enrollment_plan_unavailable')
        plan = self._plan(parsed(row['record'].encode()), meta['store_id'])
        if plan['plan_id'] != plan_id or plan['plan_sha256'] != plan_sha256: refuse('worker_enrollment_stale_plan')
        if row['consumed']: refuse('worker_enrollment_plan_consumed')
        return plan

    @guarded
    def plan(self, plan_id, plan_sha256):
        with self._connection() as conn:
            conn.execute('BEGIN'); meta, _ = self._validate(conn, full=True); result = self._read_plan(conn, meta, plan_id, plan_sha256); conn.execute('COMMIT'); return deepcopy(result)

    @guarded
    def certificates_for_plan(self, plan_id, plan_sha256):
        """Fresh public-certificate metadata for the trusted local CLI verifier."""
        with self._connection() as conn:
            conn.execute('BEGIN'); meta, state = self._validate(conn, full=True); plan = self._read_plan(conn, meta, plan_id, plan_sha256)
            if plan['revision'] != int(meta['revision']) or plan['head_sha256'] != meta['head'] or plan['state_sha256'] != digest(state): refuse('worker_enrollment_stale_plan')
            now = self._now()
            if now < int(meta['clock_floor']): refuse('worker_enrollment_clock_rollback')
            self._transition(state, plan, now)
            if plan['action'] == 'enroll': result = plan['credentials']
            elif plan['action'] == 'stage': result = {plan['slot']: plan['credentials']}
            elif plan['action'] == 'activate':
                result = {plan['slot']: {key: record[key] for key in CERT_FIELDS} for record in state['certificates'].values()
                          if record['worker_id'] == plan['worker_id'] and record['slot'] == plan['slot'] and record['state'] == 'STAGED'}
            else: result = {}
            conn.execute('COMMIT'); return deepcopy(result)

    @guarded
    def apply(self, plan_id, plan_sha256, verify_binding_callback):
        if not callable(verify_binding_callback): refuse('worker_enrollment_binding_changed')
        with self._connection() as conn:
            conn.execute('BEGIN'); meta, state = self._validate(conn, full=True); plan = self._read_plan(conn, meta, plan_id, plan_sha256)
            if plan['revision'] != int(meta['revision']) or plan['head_sha256'] != meta['head'] or plan['state_sha256'] != digest(state): refuse('worker_enrollment_stale_plan')
            observed_now = self._now()
            if observed_now < int(meta['clock_floor']): refuse('worker_enrollment_clock_rollback')
            self._transition(state, plan, observed_now); conn.execute('COMMIT')
        # No registry transaction while observing another DB/Docker/CA. The
        # callback is precommit evidence, not a multi-database atomic assertion.
        if validate_binding(verify_binding_callback()) != self.binding: refuse('worker_enrollment_binding_changed')
        with self._connection() as conn:
            conn.execute('BEGIN IMMEDIATE'); meta, state = self._validate(conn, full=True); now = self._advance_clock(conn, meta)
            current = self._read_plan(conn, meta, plan_id, plan_sha256)
            if current != plan or plan['revision'] != int(meta['revision']) or plan['head_sha256'] != meta['head'] or plan['state_sha256'] != digest(state): refuse('worker_enrollment_stale_plan')
            if int(meta['revision']) >= MAX_ROWS: refuse('worker_enrollment_quota_exceeded')
            state = self._transition(state, plan, now); revision = int(meta['revision']) + 1
            event = {'sequence': revision, 'plan_id': plan_id, 'plan_sha256': plan_sha256, 'previous_sha256': meta['head'], 'state_sha256': digest(state), 'applied_at': now}
            event['digest'] = digest(event)
            if conn.execute('UPDATE state SET record=? WHERE singleton=1', (encoded(state).decode(),)).rowcount != 1: refuse('worker_enrollment_state_unavailable')
            if conn.execute('UPDATE plans SET consumed=1 WHERE plan_id=? AND consumed=0', (plan_id,)).rowcount != 1: refuse('worker_enrollment_stale_plan')
            conn.execute('INSERT INTO events VALUES(?,?)', (revision, encoded(event).decode()))
            conn.execute("UPDATE metadata SET value=? WHERE key='revision'", (str(revision),)); conn.execute("UPDATE metadata SET value=? WHERE key='head'", (event['digest'],)); conn.execute('COMMIT')
            return self._summary(plan, 'COMPLETE', revision)

    @guarded
    def entries(self):
        with self._connection() as conn:
            # Every call reads and fully validates fresh state. Most checks
            # occur in an already-persisted clock second and need no writer.
            conn.execute('BEGIN'); meta, state = self._validate(conn, full=True); now = self._now()
            if now < int(meta['clock_floor']): refuse('worker_enrollment_clock_rollback')
            if now > int(meta['clock_floor']):
                observed = now
                # Never upgrade a SQLite read transaction: release it, take a
                # writer, then re-read all history/state and check both clocks.
                conn.execute('ROLLBACK'); conn.execute('BEGIN IMMEDIATE')
                meta, state = self._validate(conn, full=True); now = self._advance_clock(conn, meta)
                if now < observed: refuse('worker_enrollment_clock_rollback')
            conn.execute('COMMIT')
        result = {}
        for fingerprint, cert in state['certificates'].items():
            if not self._live(cert, now): continue
            worker = cert['worker_id']; slot = cert['slot']
            result[fingerprint] = {'role': 'guardian' if slot == 'guardian' else 'hub', 'workers': [worker],
                                  'courses': self.binding['workers'][worker]['courses'][:] if slot == 'guardian' else [slot[4:]]}
        return result
