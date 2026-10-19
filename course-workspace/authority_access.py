"""Bounded private mTLS authority access; callbacks own runtime/ownership effects.

No secret-body logging, automatic retries, reusable grants or replay pruning.
"""
from contextlib import contextmanager
from collections import deque
from functools import wraps
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import logging
import os
from pathlib import Path
import re
import sqlite3
import ssl
import stat
import tempfile
import threading
import time
import uuid

from hub.authority_client import AuthorityError, VERSION, PATH, MAX_FRAME, OPERATIONS, HEX32, SLUG, encoded, parsed, material

HEX64=re.compile(r'[0-9a-f]{64}\Z')
ROLES={'operator':frozenset(op for op in OPERATIONS if op.startswith('ownership.')),
       'hub':frozenset(op for op in OPERATIONS if op.startswith('hub.')),'guardian':frozenset({'guardian.observe'})}
MAX_NONCES=100000
REPLAY_STAGES=frozenset({'queue','binding','begin','validate','append','commit'})
REPLAY_FAILURES=frozenset({'sqlite_busy','sqlite_locked','sqlite_io','sqlite_corrupt','sqlite_other',
                         'binding','filesystem','integrity','deadline','other'})


def replay_diagnostic(stage,error):
    """Fixed enums only: no exception text, paths, nonces or credentials."""
    if isinstance(error,sqlite3.Error):
        code=getattr(error,'sqlite_errorcode',0)
        code=code & 0xff if type(code) is int else 0
        category={sqlite3.SQLITE_BUSY:'sqlite_busy',sqlite3.SQLITE_LOCKED:'sqlite_locked',
                  sqlite3.SQLITE_IOERR:'sqlite_io',sqlite3.SQLITE_CORRUPT:'sqlite_corrupt'}.get(code,'sqlite_other')
    elif isinstance(error,TimeoutError):category='deadline'
    elif isinstance(error,AuthorityError):category='binding'
    elif isinstance(error,OSError):category='filesystem'
    elif isinstance(error,(ValueError,KeyError,TypeError)):category='integrity'
    else:category='other'
    try:logging.getLogger(__name__).warning('authority_replay_failure stage=%s category=%s',
        stage if stage in REPLAY_STAGES else 'binding',category)
    except Exception:pass


def fixed(fn):
    @wraps(fn)
    def guarded(*args,**kwargs):
        try:return fn(*args,**kwargs)
        except AuthorityError:raise
        except Exception:raise AuthorityError('authority_access_refused') from None
    return guarded


def private(path,directory=False):
    path=Path(path)
    if not path.is_absolute() or path.resolve()!=path:raise AuthorityError('authority_access_refused')
    info=path.lstat()
    if (info.st_uid!=os.geteuid() or info.st_mode&0o077 or not directory and info.st_nlink!=1
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):raise AuthorityError('authority_access_refused')
    return info


def fsync_directory(path):
    descriptor=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)


def read_private(path,protected=None):
    path=Path(path);info=private(path)
    if info.st_size>MAX_FRAME or protected==(info.st_dev,info.st_ino):raise AuthorityError('authority_access_refused')
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(descriptor,'rb') as source:
        actual=os.fstat(source.fileno())
        if (actual.st_dev,actual.st_ino)!=(info.st_dev,info.st_ino):raise AuthorityError('authority_access_refused')
        raw=source.read(MAX_FRAME+1)
    if len(raw)>MAX_FRAME:raise AuthorityError('authority_access_refused')
    return parsed(raw)


def publish(path,value):
    path=Path(path);private(path.parent,True);fd,scratch=tempfile.mkstemp(prefix='.authority-access-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as target:target.write(encoded(value));target.flush();os.fsync(target.fileno())
        os.link(scratch,path);fsync_directory(path.parent)
    finally:
        Path(scratch).unlink(missing_ok=True);fsync_directory(path.parent)


class AccessPolicy:
    def __init__(self,acl_file,authority_id,*,enrollment=None,binding_verifier=None):
        if not isinstance(authority_id,str) or not HEX32.fullmatch(authority_id):raise AuthorityError('authority_access_refused')
        self.path=Path(acl_file);self.authority_id=authority_id;self.enrollment=enrollment;self.binding_verifier=binding_verifier
        if enrollment is not None and (getattr(enrollment,'authority_id',None)!=authority_id or not callable(getattr(enrollment,'entries',None))):raise AuthorityError('authority_access_refused')
        if binding_verifier is not None and (enrollment is None or not callable(binding_verifier)):raise AuthorityError('authority_access_refused')
        self._read()
        if enrollment is not None:self._managed()

    def _read(self):
        try:
            value=read_private(self.path)
            if not isinstance(value,dict) or set(value)!={'version','authority_id','clients'} or value['version']!=VERSION or value['authority_id']!=self.authority_id or not isinstance(value['clients'],dict) or len(value['clients'])>128:
                raise AuthorityError('authority_access_refused')
            for fingerprint,entry in value['clients'].items():
                if (not isinstance(fingerprint,str) or not HEX64.fullmatch(fingerprint) or not isinstance(entry,dict)
                        or set(entry)!={'role','workers','courses'} or entry['role'] not in ROLES):raise AuthorityError('authority_access_refused')
                for key in ('workers','courses'):
                    scopes=entry[key]
                    if not isinstance(scopes,list) or not 1<=len(scopes)<=128 or any(not isinstance(scope,str) or not SLUG.fullmatch(scope) for scope in scopes) or len(set(scopes))!=len(scopes):raise AuthorityError('authority_access_refused')
            return value['clients']
        except AuthorityError:raise
        except Exception:raise AuthorityError('authority_access_refused') from None

    def _managed(self):
        try:
            from worker_enrollment import MAX_CERTIFICATES
            entries=self.enrollment.entries()
            if not isinstance(entries,dict) or len(entries)>MAX_CERTIFICATES:raise ValueError()
            for fingerprint,entry in entries.items():
                if (not isinstance(fingerprint,str) or not HEX64.fullmatch(fingerprint) or not isinstance(entry,dict)
                        or set(entry)!={'role','workers','courses'} or entry['role'] not in ('hub','guardian')):raise ValueError()
                for key in ('workers','courses'):
                    scopes=entry[key]
                    if not isinstance(scopes,list) or not 1<=len(scopes)<=128 or len(set(scopes))!=len(scopes) or any(not isinstance(scope,str) or not SLUG.fullmatch(scope) for scope in scopes):raise ValueError()
            return entries
        except Exception:raise AuthorityError('authority_access_refused') from None

    def _entry(self,fingerprint,role,worker,course):
        static=self._read()
        if self.enrollment is not None and role!='operator':
            if self.binding_verifier is not None:
                try:self.binding_verifier(worker,course)
                except Exception:raise AuthorityError('authority_access_refused') from None
            managed=self._managed()
            # Managed runtime roles never fall back to static entries. An
            # operator leaf cannot gain a runtime role through enrollment.
            if fingerprint in static and static[fingerprint]['role']=='operator':raise AuthorityError('authority_access_refused')
            entry=managed.get(fingerprint)
        else:entry=static.get(fingerprint)
        if entry is None or entry['role']!=role or worker not in entry['workers'] or course not in entry['courses']:raise AuthorityError('authority_access_refused')
        return entry

    @fixed
    def authorize_fingerprint(self,cert_sha256,role,worker_id,course_id):
        if (not isinstance(cert_sha256,str) or not HEX64.fullmatch(cert_sha256) or not isinstance(role,str) or role not in ROLES
                or any(not isinstance(value,str) or not SLUG.fullmatch(value) for value in (worker_id,course_id))):raise AuthorityError('authority_access_refused')
        self._entry(cert_sha256,role,worker_id,course_id)

    def principal(self,fingerprint,frame):
        role=next((role for role,operations in ROLES.items() if frame['operation'] in operations),None)
        if role is None:raise AuthorityError('authority_access_refused')
        entry=self._entry(fingerprint,role,frame['worker_id'],frame['course_id'])
        payload=frame['payload'];operation=frame['operation']
        if operation in ('ownership.preview','ownership.reconcile_preview'):
            field='receiver_worker_id' if operation=='ownership.preview' else 'selected_worker_id'
            if set(payload)!={field} or payload[field] not in entry['workers']:raise AuthorityError('authority_access_refused')
        elif operation in ('ownership.apply','ownership.reconcile_apply'):
            if (set(payload)!={'plan_id','plan_sha256'} or not isinstance(payload['plan_id'],str) or not HEX32.fullmatch(payload['plan_id'])
                    or not isinstance(payload['plan_sha256'],str) or not HEX64.fullmatch(payload['plan_sha256'])):raise AuthorityError('authority_frame_invalid')
        elif operation in ('ownership.inventory','hub.authorize'):
            if payload:raise AuthorityError('authority_frame_invalid')
        elif operation=='guardian.observe':
            if set(payload)!={'guardian_container_id','start_identity'} or any(not isinstance(value,str) or not HEX64.fullmatch(value) for value in payload.values()):raise AuthorityError('authority_frame_invalid')
        elif operation=='hub.spawn':
            if (set(payload)!={'subject','environment','expected_epoch'} or not isinstance(payload['environment'],dict)
                    or type(payload['expected_epoch']) is not int or not 1<=payload['expected_epoch']<=10**9):raise AuthorityError('authority_frame_invalid')
        elif operation in ('hub.poll','hub.stop'):
            if (set(payload)!={'subject','server_id','epoch'} or not isinstance(payload['server_id'],str) or not HEX64.fullmatch(payload['server_id'])
                    or type(payload['epoch']) is not int or not 1<=payload['epoch']<=10**9):raise AuthorityError('authority_frame_invalid')
        if operation in ('hub.spawn','hub.poll','hub.stop'):
            if not isinstance(payload['subject'],str) or not 1<=len(payload['subject'])<=512 or any(ord(c)<32 for c in payload['subject']):raise AuthorityError('authority_frame_invalid')
        def authorize():
            if self._entry(fingerprint,role,frame['worker_id'],frame['course_id'])!=entry:raise AuthorityError('authority_access_refused')
        return {'cert_sha256':fingerprint,'role':role,'worker_id':frame['worker_id'],'course_id':frame['course_id'],
                'workers':tuple(entry['workers']),'courses':tuple(entry['courses']),'authorize':authorize}


class ReplayStore:
    def __init__(self,directory,authority_id):
        self.directory=Path(directory);self.database=self.directory/'replay.sqlite';self.sentinel=self.directory/'replay.json';self.authority_id=authority_id
        self._writers=threading.Condition();self._waiting=deque()

    @contextmanager
    def _writer(self):
        # SQLite busy polling is not fair: one HTTP thread can repeatedly win
        # while other distinct nonces time out. Queue this service's writers
        # BEFORE opening a connection. SQLite still arbitrates other processes.
        # Queue and database lock waits share the existing one-second budget.
        deadline=time.monotonic()+1;ticket=threading.Event()
        with self._writers:
            if len(self._waiting)>=32:raise TimeoutError()
            self._waiting.append(ticket)
            if len(self._waiting)==1:ticket.set()
            self._writers.notify_all()
        try:
            remaining=deadline-time.monotonic()
            if remaining<=0 or not ticket.wait(remaining):raise TimeoutError()
            yield deadline
        finally:
            with self._writers:
                head=self._waiting[0] is ticket
                self._waiting.remove(ticket)
                # Wake only the next writer, not all HTTP threads on a small
                # controller CPU allowance. Expired tickets never strand it.
                if head and self._waiting:self._waiting[0].set()
                self._writers.notify_all()

    @staticmethod
    def _busy_budget(deadline):
        if deadline is None:return 1000
        remaining=int((deadline-time.monotonic())*1000)
        if remaining<=0:raise TimeoutError()
        return min(1000,remaining)

    @classmethod
    def create(cls,directory,authority_id):
        self=cls(directory,authority_id)
        try:
            if not isinstance(authority_id,str) or not HEX32.fullmatch(authority_id):raise ValueError()
            private(self.directory.parent,True);self.directory.mkdir(mode=0o700,exist_ok=False)
            descriptor=os.open(self.database,os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW,0o600);os.close(descriptor)
            identity=uuid.uuid4().hex;conn=self._raw()
            try:
                conn.executescript('BEGIN IMMEDIATE;CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);CREATE TABLE requests(sequence INTEGER PRIMARY KEY,nonce TEXT UNIQUE NOT NULL,certificate TEXT NOT NULL,request_sha256 TEXT NOT NULL,operation TEXT NOT NULL,consumed_at INTEGER NOT NULL,previous_sha256 TEXT NOT NULL,digest TEXT NOT NULL);')
                for key,value in {'version':VERSION,'authority_id':authority_id,'store_id':identity,'count':'0','head':'0'*64}.items():conn.execute('INSERT INTO metadata VALUES(?,?)',(key,value))
                conn.execute('COMMIT')
            finally:conn.close()
            info=private(self.database);publish(self.sentinel,{'version':VERSION,'authority_id':authority_id,'store_id':identity,'device':info.st_dev,'inode':info.st_ino})
            return cls.open(directory,authority_id)
        except Exception:raise AuthorityError('authority_replay_state_unavailable') from None

    @classmethod
    def open(cls,directory,pinned_authority_id):
        self=cls(directory,pinned_authority_id)
        try:
            with self._connection() as conn:
                conn.execute('BEGIN');self._validate(conn,full=True);conn.execute('COMMIT')
            return self
        except Exception:raise AuthorityError('authority_replay_state_unavailable') from None

    def _raw(self,deadline=None):
        budget=self._busy_budget(deadline)
        conn=sqlite3.connect(self.database.as_uri()+'?mode=rw',uri=True,timeout=budget/1000,isolation_level=None);conn.row_factory=sqlite3.Row
        try:
            conn.execute('PRAGMA journal_mode=DELETE');conn.execute('PRAGMA synchronous=EXTRA');conn.execute('PRAGMA busy_timeout='+str(budget))
            if (conn.execute('PRAGMA journal_mode').fetchone()[0]!='delete' or conn.execute('PRAGMA synchronous').fetchone()[0]!=3 or conn.execute('PRAGMA busy_timeout').fetchone()[0]!=budget):raise ValueError()
            return conn
        except Exception:conn.close();raise

    @contextmanager
    def _connection(self,deadline=None):
        private(self.directory,True);info=private(self.database)
        if info.st_size>64*1024**2:raise ValueError()
        value=read_private(self.sentinel,protected=(info.st_dev,info.st_ino))
        if (not isinstance(value,dict) or set(value)!={'version','authority_id','store_id','device','inode'} or value['version']!=VERSION
                or value['authority_id']!=self.authority_id or value['device']!=info.st_dev or value['inode']!=info.st_ino
                or not isinstance(value['store_id'],str) or not HEX32.fullmatch(value['store_id'])):raise ValueError()
        conn=self._raw(deadline) if deadline is not None else self._raw()
        try:yield conn
        finally:conn.close()

    def _validate(self,conn,full=False):
        value=dict(conn.execute('SELECT key,value FROM metadata'));sentinel=read_private(self.sentinel,protected=(self.database.lstat().st_dev,self.database.lstat().st_ino))
        if (set(value)!={'version','authority_id','store_id','count','head'} or any(value[key]!=sentinel[key] for key in ('version','authority_id','store_id'))
                or not re.fullmatch(r'[0-9]{1,6}',value['count']) or int(value['count'])>MAX_NONCES or not HEX64.fullmatch(value['head'])):raise ValueError()
        count=conn.execute('SELECT count(*) FROM requests').fetchone()[0]
        if count!=int(value['count']):raise ValueError()
        rows=(conn.execute('SELECT * FROM requests ORDER BY sequence LIMIT ?', (MAX_NONCES+1,)).fetchall() if full else conn.execute('SELECT * FROM requests ORDER BY sequence DESC LIMIT 1').fetchall())
        previous='0'*64
        for index,row in enumerate(rows,1):
            record=dict(row)
            if (row['digest']!=hashlib.sha256(encoded({key:data for key,data in record.items() if key!='digest'})).hexdigest()
                    or not HEX32.fullmatch(row['nonce']) or not HEX64.fullmatch(row['certificate']) or not HEX64.fullmatch(row['request_sha256'])
                    or row['operation'] not in OPERATIONS or type(row['consumed_at']) is not int
                    or full and (row['sequence']!=index or row['previous_sha256']!=previous)):raise ValueError()
            previous=row['digest']
        if (count==0 and value['head']!='0'*64 or rows and rows[-1]['digest']!=value['head'] or rows and not full and rows[0]['sequence']!=count):raise ValueError()
        return value

    def consume(self,frame,certificate):
        stage='queue'
        try:
            with self._writer() as deadline:
                stage='binding'
                with self._connection(deadline) as conn:
                    stage='begin';conn.execute('PRAGMA busy_timeout='+str(self._busy_budget(deadline)))
                    conn.execute('BEGIN IMMEDIATE')
                    stage='validate';state=self._validate(conn)
                    if conn.execute('SELECT 1 FROM requests WHERE nonce=?',(frame['nonce'],)).fetchone():raise AuthorityError('authority_replay_refused')
                    count=int(state['count'])
                    if count>=MAX_NONCES:raise ValueError()
                    now=int(time.time())
                    latest=conn.execute('SELECT consumed_at FROM requests ORDER BY sequence DESC LIMIT 1').fetchone()
                    if latest is not None and now+5<latest['consumed_at']:raise ValueError()
                    value={'sequence':count+1,'nonce':frame['nonce'],'certificate':certificate,'request_sha256':hashlib.sha256(encoded(frame)).hexdigest(),
                           'operation':frame['operation'],'consumed_at':now,'previous_sha256':state['head']}
                    value['digest']=hashlib.sha256(encoded(value)).hexdigest()
                    stage='append'
                    conn.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?)',tuple(value[key] for key in ('sequence','nonce','certificate','request_sha256','operation','consumed_at','previous_sha256','digest')))
                    conn.execute("UPDATE metadata SET value=? WHERE key='count'",(str(count+1),));conn.execute("UPDATE metadata SET value=? WHERE key='head'",(value['digest'],))
                    stage='commit';conn.execute('PRAGMA busy_timeout='+str(self._busy_budget(deadline)))
                    conn.execute('COMMIT')
        except AuthorityError:raise
        except Exception as error:
            try:replay_diagnostic(stage,error)
            except Exception:pass
            raise AuthorityError('authority_replay_state_unavailable') from None


class AuthorityServer(HTTPServer):
    # Rebind after our closed connections enter TIME_WAIT; this does not enable
    # SO_REUSEPORT or allow a second live listener on the same address/port.
    allow_reuse_address=True
    @fixed
    def __init__(self,address,*,authority_id,certfile,keyfile,ca_file,acl_file,replay_store,dispatch,timeout=3,max_threads=8,access_policy=None):
        if type(timeout) not in (int,float) or not 0<timeout<=10 or type(max_threads) is not int or not 1<=max_threads<=32:raise AuthorityError('authority_access_refused')
        self.authority_id=authority_id;self.policy=access_policy if access_policy is not None else AccessPolicy(acl_file,authority_id);self.replay=replay_store;self.dispatch=dispatch;self.request_timeout=timeout
        if self.replay.authority_id!=authority_id or not isinstance(self.policy,AccessPolicy) or self.policy.authority_id!=authority_id or self.policy.path!=Path(acl_file):raise AuthorityError('authority_access_refused')
        self.tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);self.tls.minimum_version=ssl.TLSVersion.TLSv1_2;self.tls.verify_mode=ssl.CERT_REQUIRED
        self.tls.load_verify_locations(cafile=material(ca_file));self.tls.load_cert_chain(material(certfile),material(keyfile,True))
        self.slots=threading.BoundedSemaphore(max_threads);self.threads=set();self.threads_lock=threading.Lock()
        super().__init__(address,AuthorityHandler)

    def get_request(self):
        raw,address=self.socket.accept();raw.settimeout(self.request_timeout)
        try:return self.tls.wrap_socket(raw,server_side=True,do_handshake_on_connect=False),address
        except Exception:raw.close();raise

    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):self.shutdown_request(request);return
        def serve():
            try:self.finish_request(request,address)
            except Exception:pass
            finally:
                self.shutdown_request(request);self.slots.release()
                with self.threads_lock:self.threads.discard(threading.current_thread())
        thread=threading.Thread(target=serve,daemon=True)
        with self.threads_lock:self.threads.add(thread)
        try:thread.start()
        except Exception:
            with self.threads_lock:self.threads.discard(thread)
            self.slots.release();self.shutdown_request(request)

    def handle_error(self,request,client_address):pass

    def server_close(self):
        super().server_close();deadline=time.monotonic()+self.request_timeout+1
        with self.threads_lock:threads=list(self.threads)
        for thread in threads:thread.join(max(0,deadline-time.monotonic()))


class AuthorityHandler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*args):pass
    def send_error(self,*args,**kwargs):self.reply(400,'authority_frame_invalid')
    def handle_expect_100(self):self.reply(400,'authority_frame_invalid');return False

    def reply(self,status,error=None,frame=None,result=None):
        frame=frame or {}
        value={'version':VERSION,'authority_id':self.server.authority_id,'nonce':frame.get('nonce','0'*32),
               'operation':frame.get('operation',''),'worker_id':frame.get('worker_id',''),'course_id':frame.get('course_id',''),
               'ok':error is None,'result':result if error is None else None,'error':error}
        raw=encoded(value)
        if len(raw)>MAX_FRAME:raise AuthorityError('authority_operation_refused')
        self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.send_header('Connection','close');self.end_headers()
        self.wfile.write(raw);self.close_connection=True

    def do_POST(self):
        frame=None
        try:
            if (self.path!=PATH or self.headers.get('Transfer-Encoding') is not None or self.headers.get('Content-Encoding') is not None
                    or self.headers.get_all('Content-Type')!=['application/json'] or self.headers.get('Expect') is not None):raise AuthorityError('authority_frame_invalid')
            lengths=self.headers.get_all('Content-Length') or []
            if len(lengths)!=1 or not re.fullmatch(r'[0-9]{1,6}',lengths[0]) or not 0<int(lengths[0])<=MAX_FRAME:raise AuthorityError('authority_frame_invalid')
            raw=self.rfile.read(int(lengths[0]))
            if len(raw)!=int(lengths[0]):raise AuthorityError('authority_frame_invalid')
            value=parsed(raw)
            if (not isinstance(value,dict) or set(value)!={'version','authority_id','nonce','issued_at','operation','worker_id','course_id','payload'}
                    or not isinstance(value['nonce'],str) or not HEX32.fullmatch(value['nonce']) or type(value['issued_at']) is not int
                    or not isinstance(value['operation'],str) or value['operation'] not in OPERATIONS
                    or any(not isinstance(value[key],str) or not SLUG.fullmatch(value[key]) for key in ('worker_id','course_id'))
                    or not isinstance(value['payload'],dict)):raise AuthorityError('authority_frame_invalid')
            frame=value
            if frame['version']!=VERSION or frame['authority_id']!=self.server.authority_id:raise AuthorityError('authority_identity_or_version_mismatch')
            now=int(time.time())
            if not now-30<=frame['issued_at']<=now+5:raise AuthorityError('authority_frame_invalid')
            certificate=self.connection.getpeercert(binary_form=True)
            if not certificate:raise AuthorityError('authority_access_refused')
            fingerprint=hashlib.sha256(certificate).hexdigest();principal=self.server.policy.principal(fingerprint,frame)
            self.server.replay.consume(frame,fingerprint);principal['authorize']()
            try:result=self.server.dispatch(principal,frame['operation'],frame['course_id'],frame['payload'])
            except Exception:raise AuthorityError('authority_operation_refused') from None
            if not isinstance(result,dict):raise AuthorityError('authority_operation_refused')
            self.reply(200,frame=frame,result=result)
        except AuthorityError as error:
            status=409 if error.code=='authority_replay_refused' else 503 if error.code in ('authority_replay_state_unavailable','authority_operation_refused') else 403 if error.code=='authority_access_refused' else 400
            try:self.reply(status,error.code,frame)
            except Exception:pass
        except Exception:
            try:self.reply(400,'authority_frame_invalid',frame)
            except Exception:pass

    def do_GET(self):self.reply(400,'authority_frame_invalid')
    do_PUT=do_GET
    do_DELETE=do_GET
    do_PATCH=do_GET
