#!/usr/bin/env python3
"""Standalone ownership contract for disposable supervised logical workers only.

No production allocation, deployment, recovery, Hub or provider integration.
SQLite is a single trusted authority, not HA or protection from restoring every
trusted plane. Independent persistent backend revival barriers are mandatory.
"""
from contextlib import contextmanager
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
from urllib.parse import urlsplit
import uuid

VERSION='educloud-ownership-contract-v1'
SCOPE='standalone-disposable-logical-worker-contract'
HEX32=re.compile(r'[0-9a-f]{32}\Z');HEX64=re.compile(r'[0-9a-f]{64}\Z');SLUG=re.compile(r'[a-z][a-z0-9-]{0,63}\Z')
MAX_JSON=256*1024;MAX_EVENTS=4096;MAX_COURSES=256;MAX_WORKERS=64
WORKER_FIELDS={'worker_id','provider_id','boot_id','policy_sha256','memory_budget_mb','cpu_budget','capacity_memory_mb','capacity_cpu'}
COURSE_FIELDS={'logical_identity','profile','snapshot_sha256','runtime_sha256','retained_tree_sha256','admission_floor','running_count'}
PLAN_FIELDS={'version','scope','authority_id','transaction_id','course_id','source_worker_id','receiver_worker_id','old_epoch',
             'workers','evidence','authority_state_sha256','digest'}


class OwnershipError(ValueError):
    def __init__(self,code):self.code=code;super().__init__(code)


def guarded(fn):
    @wraps(fn)
    def run(*args,**kwargs):
        try:return fn(*args,**kwargs)
        except OwnershipError:raise
        except Exception:
            raise OwnershipError('ownership_contract_refused_or_incomplete') from None
    return run


def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def digest(value):return hashlib.sha256(encoded(value)).hexdigest()
def fault_hook(stage,transaction_id):pass


def unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise OwnershipError('duplicate_record_key')
        result[key]=value
    return result


def parsed(raw):return json.loads(raw,object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(OwnershipError('nonfinite_record')))

def canonical(path):
    path=Path(path)
    if not path.is_absolute() or path.resolve()!=path:raise OwnershipError('canonical_contract_path_required')
    return path


def private(path,directory=False):
    info=path.lstat()
    if (info.st_uid!=os.geteuid() or info.st_mode&0o077 or info.st_nlink!=1 and not directory
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise OwnershipError('private_contract_state_required')
    return info


def sync(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)


def read_json(path,protected=None):
    canonical(path);info=private(path)
    if protected is not None and (info.st_dev,info.st_ino)==protected:raise OwnershipError('contract_record_aliases_database')
    if info.st_size>MAX_JSON:raise OwnershipError('contract_record_limit')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as source:
        current=os.fstat(source.fileno())
        if (current.st_dev,current.st_ino)!=(info.st_dev,info.st_ino):raise OwnershipError('contract_record_changed')
        raw=source.read(MAX_JSON+1)
    if len(raw)>MAX_JSON:raise OwnershipError('contract_record_limit')
    value=parsed(raw)
    if not isinstance(value,dict):raise OwnershipError('invalid_contract_record')
    return value


def publish(path,value):
    canonical(path);private(path.parent,True)
    raw=encoded(value)+b'\n'
    if len(raw)>MAX_JSON:raise OwnershipError('contract_record_limit')
    fd,name=tempfile.mkstemp(prefix='.ownership-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as output:output.write(raw);output.flush();os.fsync(output.fileno())
        try:os.link(name,path)
        except FileExistsError:raise OwnershipError('new_private_contract_record_required') from None
        sync(path.parent)
    finally:
        Path(name).unlink(missing_ok=True);sync(path.parent)


def slug(value):return isinstance(value,str) and SLUG.fullmatch(value)
def number(value,maximum):return type(value) in (int,float) and math.isfinite(value) and 0<value<=maximum


def worker_evidence(value,worker_id):
    if (not isinstance(value,dict) or set(value)!=WORKER_FIELDS or value['worker_id']!=worker_id or not slug(worker_id)
            or not slug(value['provider_id']) or not isinstance(value['boot_id'],str) or not HEX32.fullmatch(value['boot_id'])
            or not isinstance(value['policy_sha256'],str) or not HEX64.fullmatch(value['policy_sha256'])
            or any(type(value[key]) is not int or not 1<=value[key]<=10**9 for key in ('memory_budget_mb','capacity_memory_mb'))
            or any(not number(value[key],100000) for key in ('cpu_budget','capacity_cpu'))
            or value['memory_budget_mb']>value['capacity_memory_mb'] or value['cpu_budget']>value['capacity_cpu']):
        raise OwnershipError('invalid_worker_evidence')
    return value


def canonical_url(value,path_allowed=False):
    if not isinstance(value,str) or len(value)>512 or any(ord(c)<33 or ord(c)>126 for c in value):raise OwnershipError('invalid_logical_identity')
    parts=urlsplit(value)
    if parts.scheme!='https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or '\\' in value:
        raise OwnershipError('invalid_logical_identity')
    host=parts.hostname.lower()
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?',host) or '..' in host:raise OwnershipError('invalid_logical_identity')
    port='' if parts.port in (None,443) else ':'+str(parts.port)
    if not path_allowed and parts.path not in ('','/'):raise OwnershipError('invalid_logical_identity')
    return 'https://'+host+port+(parts.path.rstrip('/') if path_allowed else '')


def logical_identity(value):
    if not isinstance(value,dict) or set(value)!={'origin','dns_subtree','issuer','client_id'}:raise OwnershipError('invalid_logical_identity')
    if (canonical_url(value['origin'])!=value['origin'] or urlsplit(value['origin']).hostname!=value['dns_subtree']
            or canonical_url(value['issuer'],True)!=value['issuer'] or not re.fullmatch(r'https://[^/]+/realms/[A-Za-z0-9_-]+',value['issuer'])
            or not isinstance(value['client_id'],str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,128}',value['client_id'])):
        raise OwnershipError('invalid_logical_identity')
    return value


def profile(value):
    if (not isinstance(value,dict) or set(value)!={'active_limit','learner_memory_mb','learner_cpu','memory_mb','cpu'}
            or type(value['active_limit']) is not int or not 1<=value['active_limit']<=1000
            or type(value['learner_memory_mb']) is not int or not 128<=value['learner_memory_mb']<=1048576
            or not number(value['learner_cpu'],1024) or value['learner_cpu']<0.1
            or type(value['memory_mb']) is not int or value['memory_mb']!=1024+value['active_limit']*value['learner_memory_mb']
            or not number(value['cpu'],100000) or value['cpu']!=1+value['active_limit']*value['learner_cpu']):raise OwnershipError('invalid_contract_profile')
    return value


def course_evidence(value):
    if not isinstance(value,dict) or set(value)!=COURSE_FIELDS:raise OwnershipError('invalid_course_evidence')
    logical_identity(value['logical_identity']);profile(value['profile'])
    if (any(not isinstance(value[key],str) or not HEX64.fullmatch(value[key]) for key in ('snapshot_sha256','runtime_sha256','retained_tree_sha256'))
            or type(value['admission_floor']) is not int or not 1<=value['admission_floor']<=10**9
            or type(value['running_count']) is not int or not 0<=value['running_count']<=1000):raise OwnershipError('invalid_course_evidence')
    return value


def stable_evidence(value):return {key:data for key,data in value.items() if key not in ('admission_floor','running_count')}
def dns_overlap(a,b):return a==b or a.endswith('.'+b) or b.endswith('.'+a)
def collides(a,b):return dns_overlap(a['dns_subtree'],b['dns_subtree']) or (a['issuer'],a['client_id'])==(b['issuer'],b['client_id'])


class Authority:
    def __init__(self,directory,authority_id,backend):
        if getattr(backend,'fixture_only',False) is not True or getattr(backend,'scope',None)!='course':raise OwnershipError('fixture_course_backend_required')
        self.directory=canonical(directory);self.authority_id=authority_id;self.backend=backend
        self.database=self.directory/'authority.sqlite';self.sentinel=self.directory/'authority.json'

    @classmethod
    @guarded
    def create(cls,directory,backend):
        directory=canonical(directory);private(directory.parent,True)
        directory.mkdir(mode=0o700,exist_ok=False)
        identity=uuid.uuid4().hex;self=cls(directory,identity,backend)
        fd=os.open(self.database,os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW,0o600);os.close(fd)
        conn=self._raw_connection()
        try:
            conn.executescript('''BEGIN IMMEDIATE;
                CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE workers(worker_id TEXT PRIMARY KEY,provider_id TEXT UNIQUE NOT NULL,evidence TEXT NOT NULL);
                CREATE TABLE courses(course_id TEXT PRIMARY KEY,owner TEXT NOT NULL,epoch INTEGER NOT NULL,identity TEXT NOT NULL,profile TEXT NOT NULL,runtime_sha256 TEXT NOT NULL,pending TEXT);
                CREATE TABLE reservations(worker_id TEXT NOT NULL,course_id TEXT NOT NULL,status TEXT NOT NULL,profile TEXT NOT NULL,PRIMARY KEY(worker_id,course_id));
                CREATE TABLE transfers(transaction_id TEXT PRIMARY KEY,course_id TEXT NOT NULL,plan TEXT NOT NULL,status TEXT NOT NULL,proof TEXT,receipt TEXT);
                CREATE TABLE events(sequence INTEGER PRIMARY KEY,kind TEXT NOT NULL,transaction_id TEXT,state_sha256 TEXT NOT NULL,previous_sha256 TEXT NOT NULL,digest TEXT NOT NULL);
            ''')
            conn.execute('INSERT INTO metadata VALUES(?,?)',('version',VERSION));conn.execute('INSERT INTO metadata VALUES(?,?)',('authority_id',identity))
            self._event(conn,'created',None);conn.execute('COMMIT')
        finally:conn.close()
        info=private(self.database)
        publish(self.sentinel,{'version':VERSION,'authority_id':identity,'database_device':info.st_dev,'database_inode':info.st_ino})
        sync(directory)
        return cls.open(directory,identity,backend)

    @classmethod
    @guarded
    def open(cls,directory,pinned_authority_id,backend):
        if not isinstance(pinned_authority_id,str) or not HEX32.fullmatch(pinned_authority_id):raise OwnershipError('pinned_authority_required')
        self=cls(directory,pinned_authority_id,backend)
        with self._connection() as conn:
            conn.execute('BEGIN');self._validate(conn);conn.execute('COMMIT')
        return self

    def _raw_connection(self):
        conn=sqlite3.connect(self.database.as_uri()+'?mode=rw',uri=True,timeout=1,isolation_level=None)
        conn.row_factory=sqlite3.Row
        try:
            conn.execute('PRAGMA journal_mode=DELETE');conn.execute('PRAGMA synchronous=EXTRA');conn.execute('PRAGMA busy_timeout=1000')
            if (conn.execute('PRAGMA journal_mode').fetchone()[0]!='delete' or conn.execute('PRAGMA synchronous').fetchone()[0]!=3
                    or conn.execute('PRAGMA busy_timeout').fetchone()[0]!=1000):raise OwnershipError('durable_sqlite_settings_required')
            return conn
        except BaseException:
            conn.close();raise

    @contextmanager
    def _connection(self):
        private(self.directory,True);info=private(self.database)
        saved=read_json(self.sentinel,protected=(info.st_dev,info.st_ino))
        if saved!={'version':VERSION,'authority_id':self.authority_id,'database_device':info.st_dev,'database_inode':info.st_ino}:raise OwnershipError('authority_identity_or_database_changed')
        if info.st_size>32*1024**2:raise OwnershipError('authority_storage_limit')
        conn=self._raw_connection()
        try:yield conn
        finally:conn.close()

    def _snapshot(self,conn):
        result={}
        for table,key in [('workers','worker_id'),('courses','course_id'),('reservations','worker_id,course_id'),('transfers','transaction_id')]:
            limit=MAX_WORKERS if table=='workers' else MAX_COURSES if table=='courses' else MAX_EVENTS
            rows=conn.execute('SELECT * FROM '+table+' ORDER BY '+key+' LIMIT ?', (limit+1,)).fetchall()
            if len(rows)>limit:raise OwnershipError('authority_history_limit')
            result[table]=[dict(row) for row in rows]
        return result

    def _event(self,conn,kind,transaction_id):
        row=conn.execute('SELECT * FROM events ORDER BY sequence DESC LIMIT 1').fetchone()
        sequence=1 if row is None else row['sequence']+1
        if sequence>MAX_EVENTS:raise OwnershipError('authority_history_limit')
        previous='0'*64 if row is None else row['digest'];state=digest(self._snapshot(conn))
        value={'sequence':sequence,'kind':kind,'transaction_id':transaction_id,'state_sha256':state,'previous_sha256':previous}
        value['digest']=digest(value)
        conn.execute('INSERT INTO events VALUES(?,?,?,?,?,?)',tuple(value[key] for key in ('sequence','kind','transaction_id','state_sha256','previous_sha256','digest')))
        conn.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',('head_sha256',value['digest']))

    def _validate(self,conn):
        meta=dict(conn.execute('SELECT key,value FROM metadata'))
        if set(meta)!={'version','authority_id','head_sha256'} or meta['version']!=VERSION or meta['authority_id']!=self.authority_id:raise OwnershipError('authority_history_missing_or_changed')
        events=conn.execute('SELECT * FROM events ORDER BY sequence LIMIT ?', (MAX_EVENTS+1,)).fetchall()
        if not events or len(events)>MAX_EVENTS:raise OwnershipError('authority_history_missing_or_changed')
        previous='0'*64
        for index,row in enumerate(events,1):
            value=dict(row)
            if (row['sequence']!=index or row['previous_sha256']!=previous or row['digest']!=digest({k:v for k,v in value.items() if k!='digest'})):raise OwnershipError('authority_history_missing_or_changed')
            previous=row['digest']
        if previous!=meta['head_sha256'] or events[-1]['state_sha256']!=digest(self._snapshot(conn)):raise OwnershipError('authority_history_missing_or_changed')
        workers={row['worker_id']:worker_evidence(parsed(row['evidence']),row['worker_id']) for row in conn.execute('SELECT * FROM workers')}
        courses={row['course_id']:dict(row) for row in conn.execute('SELECT * FROM courses')}
        identities=[]
        for key,row in courses.items():
            if not slug(key) or row['owner'] not in workers or type(row['epoch']) is not int or not 1<=row['epoch']<=10**9:raise OwnershipError('authority_ownership_invalid')
            identity=logical_identity(parsed(row['identity']));profile(parsed(row['profile']))
            if not isinstance(row['runtime_sha256'],str) or not HEX64.fullmatch(row['runtime_sha256']) or row['pending'] is not None and not HEX32.fullmatch(row['pending']):raise OwnershipError('authority_ownership_invalid')
            if any(collides(identity,old) for old in identities):raise OwnershipError('global_identity_collision')
            identities.append(identity)
        for row in conn.execute('SELECT * FROM reservations'):
            if row['worker_id'] not in workers or row['course_id'] not in courses or row['status'] not in ('OWNER','RESERVED','FENCED'):raise OwnershipError('authority_reservations_invalid')
            profile(parsed(row['profile']))
        for key,row in courses.items():
            owner=conn.execute("SELECT * FROM reservations WHERE course_id=? AND status='OWNER'",(key,)).fetchall()
            if len(owner)!=1 or owner[0]['worker_id']!=row['owner'] or owner[0]['profile']!=row['profile']:raise OwnershipError('authority_reservations_invalid')
            if row['pending'] is not None:
                tx=conn.execute('SELECT * FROM transfers WHERE transaction_id=?',(row['pending'],)).fetchone()
                if tx is None or tx['course_id']!=key or tx['status'] not in ('PREPARED','COMMITTED'):raise OwnershipError('authority_transfer_history_invalid')
        histories={key:[] for key in courses}
        for row in conn.execute('SELECT * FROM transfers'):
            plan=parsed(row['plan']);self._validate_plan(plan)
            if row['transaction_id']!=plan['transaction_id'] or row['course_id']!=plan['course_id'] or row['course_id'] not in courses or row['status'] not in ('PREPARED','COMMITTED','COMPLETE'):raise OwnershipError('authority_transfer_history_invalid')
            if any(key not in workers or plan['workers'][key]!=workers[key] for key in plan['workers']):raise OwnershipError('authority_transfer_history_invalid')
            if row['status']=='PREPARED':
                if row['proof'] is not None or row['receipt'] is not None:raise OwnershipError('authority_transfer_history_invalid')
            else:
                proof=self._proof_record(parsed(row['proof']),self._request(plan))
                if row['status']=='COMPLETE':
                    if parsed(row['receipt'])!=self._receipt(plan,proof):raise OwnershipError('authority_transfer_history_invalid')
                elif row['receipt'] is not None:raise OwnershipError('authority_transfer_history_invalid')
            histories[row['course_id']].append((plan,row['status']))
        for key,history in histories.items():
            history.sort(key=lambda item:item[0]['old_epoch']);current=courses[key]
            epoch=1;owner=history[0][0]['source_worker_id'] if history else current['owner'];pending=None
            for index,(plan,status) in enumerate(history):
                if plan['old_epoch']!=epoch or plan['source_worker_id']!=owner or (status!='COMPLETE' and index!=len(history)-1):raise OwnershipError('authority_transfer_history_invalid')
                if status!='PREPARED':epoch+=1;owner=plan['receiver_worker_id']
                if status!='COMPLETE':pending=plan['transaction_id']
            if current['epoch']!=epoch or current['owner']!=owner or current['pending']!=pending:raise OwnershipError('authority_transfer_history_invalid')
            if pending is not None:
                plan,status=history[-1]
                for worker,expected in [(plan['source_worker_id'],'OWNER' if status=='PREPARED' else 'FENCED'),
                                        (plan['receiver_worker_id'],'RESERVED' if status=='PREPARED' else 'OWNER')]:
                    row=conn.execute('SELECT status,profile FROM reservations WHERE course_id=? AND worker_id=?',(key,worker)).fetchone()
                    if row is None or row['status']!=expected or parsed(row['profile'])!=plan['evidence'][worker]['profile']:raise OwnershipError('authority_reservations_invalid')
            for row in conn.execute('SELECT * FROM reservations WHERE course_id=?',(key,)):
                if row['status']=='RESERVED':
                    if pending is None or history[-1][1]!='PREPARED' or row['worker_id']!=history[-1][0]['receiver_worker_id']:raise OwnershipError('authority_reservations_invalid')
                elif row['status']=='FENCED' and not any(plan['source_worker_id']==row['worker_id'] and status!='PREPARED' for plan,status in history):raise OwnershipError('authority_reservations_invalid')
        self._capacity(conn,workers)

    def _counted_profiles(self,conn,worker):
        profiles=[]
        for row in conn.execute('SELECT r.*,c.pending FROM reservations r JOIN courses c ON c.course_id=r.course_id WHERE r.worker_id=?',(worker,)):
            counted=row['status'] in ('OWNER','RESERVED')
            if row['status']=='FENCED' and row['pending'] is not None:
                tx=conn.execute('SELECT plan FROM transfers WHERE transaction_id=?',(row['pending'],)).fetchone()
                counted=tx is not None and parsed(tx['plan'])['source_worker_id']==worker
            if counted:profiles.append(parsed(row['profile']))
        return profiles

    def _capacity(self,conn,workers=None):
        if workers is None:workers={row['worker_id']:parsed(row['evidence']) for row in conn.execute('SELECT * FROM workers')}
        for worker,value in workers.items():
            profiles=self._counted_profiles(conn,worker)
            if sum(p['memory_mb'] for p in profiles)>value['memory_budget_mb'] or math.fsum(p['cpu'] for p in profiles)>value['cpu_budget']:raise OwnershipError('receiver_capacity_unavailable')

    def _worker(self,conn,worker_id,current):
        row=conn.execute('SELECT * FROM workers WHERE worker_id=?',(worker_id,)).fetchone()
        if row is None:raise OwnershipError('registered_worker_required')
        saved=worker_evidence(parsed(row['evidence']),worker_id)
        current=worker_evidence(current,worker_id)
        if current!=saved:raise OwnershipError('worker_incarnation_policy_or_capacity_changed')
        return saved

    def _course(self,conn,course_id):
        row=conn.execute('SELECT * FROM courses WHERE course_id=?',(course_id,)).fetchone()
        if row is None:raise OwnershipError('registered_course_required')
        return dict(row)

    def _summary(self,status,course=None,plan=None):
        value={'version':VERSION,'scope':SCOPE,'status':status,'authority_id':self.authority_id,'fixture_only':True,'production_admission':False,'services_started':False,'runtime_enforcement':False,'provider_adapter':False,'authenticated_remote_transport':False}
        if course is not None:value['epoch']=course['epoch']
        if plan is not None:value.update(transaction_id=plan['transaction_id'],plan_sha256=plan['digest'])
        return value

    @guarded
    def register_worker(self,worker_id):
        if not slug(worker_id):raise OwnershipError('invalid_worker_id')
        observed=worker_evidence(self.backend.inspect_worker(worker_id),worker_id)
        with self._connection() as conn:
            conn.execute('BEGIN IMMEDIATE');self._validate(conn)
            old=conn.execute('SELECT * FROM workers WHERE worker_id=?',(worker_id,)).fetchone()
            if old is not None:
                if parsed(old['evidence'])!=observed:raise OwnershipError('worker_registration_changed')
                conn.execute('COMMIT');return self._summary('REGISTERED')
            if conn.execute('SELECT 1 FROM workers WHERE provider_id=?',(observed['provider_id'],)).fetchone():raise OwnershipError('duplicate_provider_identity')
            conn.execute('INSERT INTO workers VALUES(?,?,?)',(worker_id,observed['provider_id'],encoded(observed).decode()))
            self._event(conn,'worker_registered',None);conn.execute('COMMIT')
        return self._summary('REGISTERED')

    @guarded
    def register_course(self,course_id,worker_id):
        if not slug(course_id):raise OwnershipError('invalid_course_id')
        observed=course_evidence(self.backend.inspect_course(course_id,worker_id))
        observed_worker=worker_evidence(self.backend.inspect_worker(worker_id),worker_id)
        with self._connection() as conn:
            conn.execute('BEGIN IMMEDIATE');self._validate(conn);self._worker(conn,worker_id,observed_worker)
            if conn.execute('SELECT 1 FROM courses WHERE course_id=?',(course_id,)).fetchone():raise OwnershipError('course_already_registered')
            if observed['admission_floor']>1:raise OwnershipError('backend_admission_epoch_unavailable')
            for row in conn.execute('SELECT identity FROM courses'):
                if collides(observed['logical_identity'],parsed(row['identity'])):raise OwnershipError('global_identity_collision')
            p=encoded(observed['profile']).decode()
            conn.execute('INSERT INTO courses VALUES(?,?,?,?,?,?,NULL)',(course_id,worker_id,1,encoded(observed['logical_identity']).decode(),p,observed['runtime_sha256']))
            conn.execute('INSERT INTO reservations VALUES(?,?,?,?)',(worker_id,course_id,'OWNER',p))
            self._capacity(conn);self._event(conn,'course_registered',None);conn.execute('COMMIT')
        return self._summary('REGISTERED',{'epoch':1})

    def _validate_plan(self,plan):
        if (not isinstance(plan,dict) or set(plan)!=PLAN_FIELDS or plan['version']!=VERSION or plan['scope']!=SCOPE
                or plan['authority_id']!=self.authority_id or not isinstance(plan['transaction_id'],str) or not HEX32.fullmatch(plan['transaction_id'])
                or not slug(plan['course_id']) or not slug(plan['source_worker_id']) or not slug(plan['receiver_worker_id'])
                or plan['source_worker_id']==plan['receiver_worker_id'] or type(plan['old_epoch']) is not int or not 1<=plan['old_epoch']<10**9
                or not isinstance(plan['authority_state_sha256'],str) or not HEX64.fullmatch(plan['authority_state_sha256'])
                or not isinstance(plan['workers'],dict) or not isinstance(plan['evidence'],dict)
                or set(plan['workers'])!={plan['source_worker_id'],plan['receiver_worker_id']} or set(plan['evidence'])!=set(plan['workers'])
                or plan['digest']!=digest({k:v for k,v in plan.items() if k!='digest'})):
            raise OwnershipError('invalid_ownership_plan')
        for key in plan['workers']:worker_evidence(plan['workers'][key],key);course_evidence(plan['evidence'][key])
        if plan['workers'][plan['source_worker_id']]['provider_id']==plan['workers'][plan['receiver_worker_id']]['provider_id']:raise OwnershipError('distinct_provider_identity_required')
        if stable_evidence(plan['evidence'][plan['source_worker_id']])!=stable_evidence(plan['evidence'][plan['receiver_worker_id']]):raise OwnershipError('matching_retained_backup_runtime_and_profile_required')
        if plan['evidence'][plan['receiver_worker_id']]['running_count']!=0:raise OwnershipError('receiver_course_must_be_stopped')
        if plan['evidence'][plan['receiver_worker_id']]['admission_floor']>plan['old_epoch']+1:raise OwnershipError('backend_admission_epoch_unavailable')

    @guarded
    def preview(self,course_id,receiver_id,plan_path):
        plan_path=canonical(plan_path)
        if self.directory==plan_path or self.directory in plan_path.parents:raise OwnershipError('plan_overlaps_authority')
        # Never wait for backend exclusion with a SQLite transaction held.
        with self._connection() as conn:
            conn.execute('BEGIN');self._validate(conn);course=self._course(conn,course_id)
            state=digest(self._snapshot(conn));conn.execute('COMMIT')
        if course['pending'] is not None:raise OwnershipError('ownership_transfer_pending')
        source=course['owner']
        if source==receiver_id:raise OwnershipError('distinct_receiver_required')
        workers,evidence=self._observe(course_id,(source,receiver_id))
        with self._connection() as conn:
            conn.execute('BEGIN');self._validate(conn)
            if self._course(conn,course_id)!=course or digest(self._snapshot(conn))!=state:raise OwnershipError('preview_authority_changed')
            for key in workers:self._worker(conn,key,workers[key])
            if evidence[source]['logical_identity']!=parsed(course['identity']) or evidence[source]['profile']!=parsed(course['profile']) or evidence[source]['runtime_sha256']!=course['runtime_sha256']:raise OwnershipError('source_course_binding_changed')
            plan={'version':VERSION,'scope':SCOPE,'authority_id':self.authority_id,'transaction_id':uuid.uuid4().hex,'course_id':course_id,
                  'source_worker_id':source,'receiver_worker_id':receiver_id,'old_epoch':course['epoch'],'workers':workers,'evidence':evidence,
                  'authority_state_sha256':state}
            plan['digest']=digest(plan);self._validate_plan(plan)
            profiles=self._counted_profiles(conn,receiver_id)+[evidence[source]['profile']]
            if sum(p['memory_mb'] for p in profiles)>workers[receiver_id]['memory_budget_mb'] or math.fsum(p['cpu'] for p in profiles)>workers[receiver_id]['cpu_budget']:raise OwnershipError('receiver_capacity_unavailable')
            conn.execute('COMMIT')
        publish(plan_path,plan)
        return self._summary('PLAN_READY',course,plan)

    def _observe(self,course_id,workers):
        return ({key:worker_evidence(self.backend.inspect_worker(key),key) for key in workers},
                {key:course_evidence(self.backend.inspect_course(course_id,key)) for key in workers})

    def _fresh(self,conn,plan,observations,fenced=False):
        workers,evidence=observations
        for key in plan['workers']:
            if self._worker(conn,key,workers[key])!=plan['workers'][key]:raise OwnershipError('worker_binding_changed')
            observed=evidence[key]
            if stable_evidence(observed)!=stable_evidence(plan['evidence'][key]):raise OwnershipError('retained_backup_runtime_or_profile_changed')
            if key==plan['receiver_worker_id']:
                if observed['admission_floor']>plan['old_epoch']+1:raise OwnershipError('backend_admission_epoch_unavailable')
                if observed['running_count']!=0:raise OwnershipError('receiver_course_must_be_stopped')
            elif fenced and observed['running_count']!=0:raise OwnershipError('persistent_source_fence_unverified')

    def _request(self,plan):
        source=plan['workers'][plan['source_worker_id']];receiver=plan['workers'][plan['receiver_worker_id']]
        return {'authority_id':self.authority_id,'transaction_id':plan['transaction_id'],'course_id':plan['course_id'],
                'source_worker_id':plan['source_worker_id'],'receiver_worker_id':plan['receiver_worker_id'],
                'source_provider_id':source['provider_id'],'source_boot_id':source['boot_id'],
                'receiver_provider_id':receiver['provider_id'],'receiver_boot_id':receiver['boot_id'],'old_epoch':plan['old_epoch'],'plan_sha256':plan['digest']}

    def _proof_record(self,value,request):
        if (not isinstance(value,dict) or set(value)!={'request_sha256','barrier_id','barrier_sha256','scope','denied_through_epoch','observed_running_count'}
                or value['request_sha256']!=digest(request) or not isinstance(value['barrier_id'],str) or not HEX32.fullmatch(value['barrier_id'])
                or not isinstance(value['barrier_sha256'],str) or not HEX64.fullmatch(value['barrier_sha256']) or value['scope']!='course'
                or type(value['denied_through_epoch']) is not int or not request['old_epoch']<=value['denied_through_epoch']<=10**9
                or type(value['observed_running_count']) is not int or value['observed_running_count']!=0):raise OwnershipError('persistent_source_fence_unverified')
        return value

    def _proof(self,context,request):return self._proof_record(context.inspect(),request)

    def _receipt(self,plan,proof):
        value={'version':VERSION,'scope':SCOPE,'authority_id':self.authority_id,'transaction_id':plan['transaction_id'],
               'course_id':plan['course_id'],'plan_sha256':plan['digest'],'source_worker_id':plan['source_worker_id'],
               'owner_worker_id':plan['receiver_worker_id'],'epoch':plan['old_epoch']+1,'fence_sha256':digest(proof)}
        value['digest']=digest(value)
        return value

    @guarded
    def apply(self,plan_path):
        plan_path=canonical(plan_path)
        info=private(self.database);plan=read_json(plan_path,protected=(info.st_dev,info.st_ino));self._validate_plan(plan)
        tx=plan['transaction_id'];source=plan['source_worker_id'];receiver=plan['receiver_worker_id'];cid=plan['course_id']
        observations=self._observe(cid,plan['workers'])
        with self._connection() as conn:
            conn.execute('BEGIN IMMEDIATE');self._validate(conn);course=self._course(conn,cid)
            existing=conn.execute('SELECT * FROM transfers WHERE transaction_id=?',(tx,)).fetchone()
            if existing is not None and parsed(existing['plan'])!=plan:raise OwnershipError('transfer_plan_changed')
            complete=existing is not None and existing['status']=='COMPLETE'
            if complete:
                if course['owner']!=receiver or course['epoch']!=plan['old_epoch']+1 or course['pending'] is not None:raise OwnershipError('stale_completed_ownership_decision')
                self._fresh(conn,plan,observations,True)
            elif existing is None:
                if (course['owner']!=source or course['epoch']!=plan['old_epoch'] or course['pending'] is not None
                        or digest(self._snapshot(conn))!=plan['authority_state_sha256']):raise OwnershipError('stale_ownership_approval')
                self._fresh(conn,plan,observations)
                p=encoded(plan['evidence'][source]['profile']).decode()
                conn.execute('INSERT OR REPLACE INTO reservations VALUES(?,?,?,?)',(receiver,cid,'RESERVED',p))
                conn.execute('INSERT INTO transfers VALUES(?,?,?,?,NULL,NULL)',(tx,cid,encoded(plan).decode(),'PREPARED'))
                conn.execute('UPDATE courses SET pending=? WHERE course_id=?',(tx,cid))
                self._capacity(conn);self._event(conn,'transfer_prepared',tx)
            elif course['pending']!=tx:raise OwnershipError('transfer_pending_history_changed')
            conn.execute('COMMIT')
        if not complete:fault_hook('prepared',tx)
        request=self._request(plan)
        # BOTH endpoint activation, boot and barrier exclusions remain held until
        # durable ownership/receipt proof is complete. Never hold SQL while
        # entering a backend context or performing a blocking backend read.
        with self.backend.fence_context(request) as context:
            proof=self._proof(context,request)
            if not complete:fault_hook('fenced',tx)
            observations=self._observe(cid,plan['workers']);proof=self._proof(context,request)
            with self._connection() as conn:
                conn.execute('BEGIN IMMEDIATE');self._validate(conn);self._fresh(conn,plan,observations,True)
                course=self._course(conn,cid)
                row=conn.execute('SELECT * FROM transfers WHERE transaction_id=?',(tx,)).fetchone()
                if row is None or parsed(row['plan'])!=plan:raise OwnershipError('transfer_pending_history_changed')
                if complete:
                    if row['status']!='COMPLETE' or course['pending'] is not None or course['owner']!=receiver or course['epoch']!=plan['old_epoch']+1 or parsed(row['proof'])!=proof or parsed(row['receipt'])!=self._receipt(plan,proof):raise OwnershipError('completed_ownership_state_changed')
                    conn.execute('COMMIT');return self._summary('COMPLETE',course,plan)
                if course['pending']!=tx:raise OwnershipError('transfer_pending_history_changed')
                if row['status']=='PREPARED':
                    if course['owner']!=source or course['epoch']!=plan['old_epoch']:raise OwnershipError('ownership_compare_and_swap_failed')
                    changed=conn.execute('UPDATE courses SET owner=?,epoch=? WHERE course_id=? AND owner=? AND epoch=? AND pending=?',
                        (receiver,plan['old_epoch']+1,cid,source,plan['old_epoch'],tx)).rowcount
                    if changed!=1:raise OwnershipError('ownership_compare_and_swap_failed')
                    changed_source=conn.execute("UPDATE reservations SET status='FENCED' WHERE worker_id=? AND course_id=? AND status='OWNER'",(source,cid)).rowcount
                    changed_receiver=conn.execute("UPDATE reservations SET status='OWNER' WHERE worker_id=? AND course_id=? AND status='RESERVED'",(receiver,cid)).rowcount
                    if changed_source!=1 or changed_receiver!=1:raise OwnershipError('authority_reservations_invalid')
                    conn.execute("UPDATE transfers SET status='COMMITTED',proof=? WHERE transaction_id=?",(encoded(proof).decode(),tx))
                    self._capacity(conn);self._event(conn,'ownership_committed',tx)
                elif row['status']!='COMMITTED' or course['owner']!=receiver or course['epoch']!=plan['old_epoch']+1:raise OwnershipError('committed_ownership_state_changed')
                elif parsed(row['proof'])!=proof:raise OwnershipError('persistent_fence_identity_changed')
                conn.execute('COMMIT')
            fault_hook('committed',tx)
            observations=self._observe(cid,plan['workers']);current_proof=self._proof(context,request)
            with self._connection() as conn:
                conn.execute('BEGIN IMMEDIATE');self._validate(conn);self._fresh(conn,plan,observations,True)
                row=conn.execute('SELECT * FROM transfers WHERE transaction_id=?',(tx,)).fetchone();course=self._course(conn,cid)
                if row is None or row['status']!='COMMITTED' or parsed(row['proof'])!=current_proof or course['owner']!=receiver or course['epoch']!=plan['old_epoch']+1 or course['pending']!=tx:raise OwnershipError('committed_ownership_state_changed')
                receipt=self._receipt(plan,current_proof)
                conn.execute("UPDATE transfers SET status='COMPLETE',receipt=? WHERE transaction_id=?",(encoded(receipt).decode(),tx))
                conn.execute('UPDATE courses SET pending=NULL WHERE course_id=? AND pending=?',(cid,tx))
                self._event(conn,'transfer_complete',tx);self._validate(conn);conn.execute('COMMIT')
            fault_hook('complete',tx)
        return self._summary('COMPLETE',course,plan)

    resume=apply

    @contextmanager
    def admission(self,course_id,worker_id):
        try:
            observed_worker=worker_evidence(self.backend.inspect_worker(worker_id),worker_id)
            with self._connection() as conn:
                conn.execute('BEGIN');self._validate(conn);course=self._course(conn,course_id);worker=self._worker(conn,worker_id,observed_worker)
                if course['owner']!=worker_id:raise OwnershipError('current_owner_required')
                if course['pending'] is not None:raise OwnershipError('ownership_transfer_pending')
                request={'authority_id':self.authority_id,'course_id':course_id,'worker_id':worker_id,'provider_id':worker['provider_id'],'boot_id':worker['boot_id'],'epoch':course['epoch']}
                conn.execute('COMMIT')
            with self.backend.activation_context(request):
                observed_worker=worker_evidence(self.backend.inspect_worker(worker_id),worker_id)
                evidence=course_evidence(self.backend.inspect_course(course_id,worker_id))
                with self._connection() as conn:
                    conn.execute('BEGIN');self._validate(conn);current=self._course(conn,course_id);self._worker(conn,worker_id,observed_worker)
                    if (current!=course or evidence['logical_identity']!=parsed(course['identity']) or evidence['profile']!=parsed(course['profile'])
                            or evidence['runtime_sha256']!=course['runtime_sha256'] or evidence['admission_floor']>course['epoch']):raise OwnershipError('activation_ownership_or_binding_changed')
                    conn.execute('COMMIT')
                grant=self._summary('FIXTURE_GRANT',course)
                grant.update(course_id=course_id,worker_id=worker_id)
                yield grant
        except OwnershipError:raise
        except Exception:raise OwnershipError('ownership_contract_refused_or_incomplete') from None

    @guarded
    def inventory(self):
        with self._connection() as conn:
            conn.execute('BEGIN');self._validate(conn)
            result=self._summary('INVENTORY');result['registered_workers']=conn.execute('SELECT count(*) FROM workers').fetchone()[0]
            result['registered_courses']=conn.execute('SELECT count(*) FROM courses').fetchone()[0]
            result['pending_transfers']=conn.execute("SELECT count(*) FROM transfers WHERE status!='COMPLETE'").fetchone()[0]
            result['reservations']={}
            for row in conn.execute('SELECT worker_id FROM workers ORDER BY worker_id'):
                key=row['worker_id'];profiles=self._counted_profiles(conn,key)
                result['reservations'][key]={'count':len(profiles),'memory_mb':sum(p['memory_mb'] for p in profiles),'cpu':math.fsum(p['cpu'] for p in profiles)}
            conn.execute('COMMIT');return result
