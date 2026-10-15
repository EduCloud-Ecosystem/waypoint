"""Private worker quarantine journal. No automatic recreation or quarantine expiry."""
import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

VERSION = 'educloud-worker-journal-v1'
HEX = re.compile(r'[0-9a-f]{32}\Z')
HASH = re.compile(r'[0-9a-f]{64}\Z')
SLUG = re.compile(r'[a-z][a-z0-9-]{0,63}\Z')
MAX_EVENTS = 4096
MAX_DATABASE = 8 * 1024**2
SCHEMA = {'events': 'CREATE TABLE events(sequence INTEGER PRIMARY KEY, record TEXT NOT NULL, digest TEXT NOT NULL)',
          'state': 'CREATE TABLE state(course TEXT PRIMARY KEY, session TEXT)',
          'head': 'CREATE TABLE head(singleton INTEGER PRIMARY KEY, sequence INTEGER NOT NULL, digest TEXT NOT NULL)'}


class JournalError(ValueError):
    def __init__(self): super().__init__('worker_journal_refused')


def encoded(value): return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
def digest(value): return hashlib.sha256(encoded(value)).hexdigest()
def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value: raise JournalError()
        value[key] = item
    return value
def parsed(raw): return json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(JournalError()))


def private(path, directory=False):
    info = path.lstat()
    if (path.resolve() != path or info.st_uid != os.geteuid() or info.st_mode & 0o077
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or not directory and info.st_nlink != 1): raise JournalError()
    return info


class WorkerJournal:
    def __init__(self, directory, authority_id, worker_id, courses, binding_sha256):
        self.directory = Path(directory)
        if (not self.directory.is_absolute() or not isinstance(authority_id, str) or not HEX.fullmatch(authority_id)
                or not isinstance(worker_id, str) or not SLUG.fullmatch(worker_id)
                or not isinstance(courses, list) or not 1 <= len(courses) <= 256
                or any(not isinstance(c, str) or not SLUG.fullmatch(c) for c in courses)
                or len(set(courses)) != len(courses) or not isinstance(binding_sha256, str) or not HASH.fullmatch(binding_sha256)):
            raise JournalError()
        self.authority_id, self.worker_id = authority_id, worker_id
        self.courses, self.binding_sha256 = sorted(courses), binding_sha256
        self.database, self.sentinel = self.directory / 'journal.sqlite', self.directory / 'journal.json'

    @classmethod
    def create(cls, directory, authority_id, worker_id, courses, binding_sha256):
        self = cls(directory, authority_id, worker_id, courses, binding_sha256)
        self.directory.mkdir(mode=0o700, exist_ok=False)
        fd = os.open(self.database, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600); os.close(fd)
        with self._connection(initial=True) as conn:
            conn.executescript('BEGIN IMMEDIATE; CREATE TABLE events(sequence INTEGER PRIMARY KEY, record TEXT NOT NULL, digest TEXT NOT NULL); CREATE TABLE state(course TEXT PRIMARY KEY, session TEXT); CREATE TABLE head(singleton INTEGER PRIMARY KEY, sequence INTEGER NOT NULL, digest TEXT NOT NULL);')
            conn.execute('INSERT INTO head VALUES(1,0,?)', ('0' * 64,))
            conn.executemany('INSERT INTO state VALUES(?,NULL)', [(c,) for c in self.courses]); conn.commit()
        info = private(self.database)
        record = self._identity(info)
        fd = os.open(self.sentinel, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as target: target.write(encoded(record)); target.flush(); os.fsync(target.fileno())
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
        return cls.open(directory, authority_id, worker_id, courses, binding_sha256)

    @classmethod
    def open(cls, directory, authority_id, worker_id, courses, binding_sha256):
        self = cls(directory, authority_id, worker_id, courses, binding_sha256)
        with self._connection() as conn:
            conn.execute('BEGIN'); self._validate(conn); conn.commit()
        return self

    def _identity(self, info):
        return {'version': VERSION, 'authority_id': self.authority_id, 'worker_id': self.worker_id,
                'courses': self.courses, 'binding_sha256': self.binding_sha256,
                'database_device': info.st_dev, 'database_inode': info.st_ino}

    @contextmanager
    def _connection(self, initial=False):
        try:
            private(self.directory, True); info = private(self.database)
            if info.st_size > MAX_DATABASE: raise JournalError()
            if not initial:
                marker = private(self.sentinel)
                if marker.st_size > 65536: raise JournalError()
                fd = os.open(self.sentinel, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, 'rb') as source:
                    if os.fstat(source.fileno()) != marker: raise JournalError()
                    value = parsed(source.read(65537))
                if value != self._identity(info): raise JournalError()
            conn = sqlite3.connect(self.database.as_uri() + '?mode=rw', uri=True, timeout=1)
            try:
                conn.execute('PRAGMA journal_mode=DELETE'); conn.execute('PRAGMA synchronous=EXTRA'); conn.execute('PRAGMA busy_timeout=1000')
                conn.execute('PRAGMA trusted_schema=OFF')
                if (conn.execute('PRAGMA journal_mode').fetchone()[0] != 'delete'
                        or conn.execute('PRAGMA synchronous').fetchone()[0] != 3
                        or conn.execute('PRAGMA busy_timeout').fetchone()[0] != 1000
                        or conn.execute('PRAGMA trusted_schema').fetchone()[0] != 0): raise JournalError()
                if (private(self.database).st_dev, private(self.database).st_ino) != (info.st_dev, info.st_ino): raise JournalError()
                yield conn
            finally: conn.close()
        except JournalError: raise
        except Exception: raise JournalError() from None

    def _validate(self, conn):
        schema = conn.execute("SELECT type,substr(name,1,257),substr(sql,1,1025) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 4").fetchall()
        if len(schema) != len(SCHEMA) or {(kind, name): sql for kind, name, sql in schema} != {('table', name): sql for name, sql in SCHEMA.items()}: raise JournalError()
        state = {course: None for course in self.courses}; previous = '0' * 64; sessions = set()
        rows = conn.execute('SELECT sequence,substr(record,1,65537),digest,length(record) FROM events ORDER BY sequence LIMIT ?', (MAX_EVENTS + 1,)).fetchall()
        if len(rows) > MAX_EVENTS: raise JournalError()
        for expected, (sequence, raw, stamp, length) in enumerate(rows, 1):
            if not isinstance(raw, str) or type(length) is not int or length > 65536: raise JournalError()
            try: event = parsed(raw)
            except Exception: raise JournalError() from None
            if (not isinstance(event, dict) or set(event) != {'sequence', 'course', 'session', 'action', 'previous'}
                    or type(sequence) is not int or type(event['sequence']) is not int
                    or event['sequence'] != sequence or sequence != expected
                    or event['course'] not in state or not isinstance(event['session'], str) or not HEX.fullmatch(event['session'])
                    or event['previous'] != previous or digest(event) != stamp): raise JournalError()
            course, session = event['course'], event['session']
            if event['action'] == 'begin':
                if session in sessions: raise JournalError()
                sessions.add(session); state[course] = session
            elif event['action'] == 'finish' and state[course] == session: state[course] = None
            else: raise JournalError()
            previous = stamp
        current = conn.execute('SELECT course,session FROM state LIMIT 257').fetchall()
        if len(current) != len(state) or dict(current) != state: raise JournalError()
        if conn.execute('SELECT singleton,sequence,digest FROM head LIMIT 2').fetchall() != [(1, len(rows), previous)]: raise JournalError()
        return state, previous, len(rows)

    def state(self, course):
        try:
            with self._connection() as conn:
                conn.execute('BEGIN'); state, _, _ = self._validate(conn); conn.commit()
            if course not in state: raise JournalError()
            return {'quarantined': state[course] is not None}
        except JournalError: raise
        except Exception: raise JournalError() from None

    def _change(self, action, course, session):
        if course not in self.courses or not isinstance(session, str) or not HEX.fullmatch(session): raise JournalError()
        try:
            with self._connection() as conn:
                conn.execute('BEGIN IMMEDIATE'); state, previous, count = self._validate(conn)
                if action == 'begin' and state[course] == session: conn.commit(); return
                if action == 'finish' and state[course] != session or count >= MAX_EVENTS: raise JournalError()
                event = {'sequence': count + 1, 'course': course, 'session': session, 'action': action, 'previous': previous}
                conn.execute('INSERT INTO events VALUES(?,?,?)', (count + 1, encoded(event).decode(), digest(event)))
                if conn.execute('UPDATE state SET session=? WHERE course=?', (session if action == 'begin' else None, course)).rowcount != 1: raise JournalError()
                if conn.execute('UPDATE head SET sequence=?,digest=? WHERE singleton=1 AND sequence=? AND digest=?',
                                (count + 1, digest(event), count, previous)).rowcount != 1: raise JournalError()
                self._validate(conn); conn.commit()
        except JournalError: raise
        except Exception: raise JournalError() from None

    def begin(self, course, session): self._change('begin', course, session)
    def finish(self, course, session): self._change('finish', course, session)
