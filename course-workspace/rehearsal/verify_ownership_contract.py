#!/usr/bin/env python3
"""Two logical-worker ownership contract with supervised synthetic processes.

This fixture never calls production allocation, deployment, Hub, or providers.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import threading
import uuid
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from evidence import _source, sha256_file

HASH=re.compile(r'[0-9a-f]{64}\Z')
CHECKS=frozenset({
    'two-worker-incarnations-pinned','duplicate-provider-identity-refused','global-course-identity-collision-refused',
    'source-capacity-overcommit-refused','actual-source-peer-processes-live','copied-synthetic-bytes-and-fingerprints-match',
    'wrong-identity-snapshot-runtime-profile-tree-refused','wrong-worker-incarnation-refused','tampered-plan-refused',
    'live-unapproved-receiver-refused',
    'prepared-interruption-kept-both-reservations','both-pending-endpoints-refuse-admission',
    'unreachable-inspector-not-fencing-proof','fenced-interruption-kept-both-reservations',
    'persistent-course-barrier-and-real-source-stop','new-source-client-revival-refused',
    'barrier-independent-of-source-worker-ledger','peer-live-through-pending-and-fencing',
    'both-endpoint-locks-held-through-fresh-proof-cas','exact-resume-and-no-automatic-start',
    'completed-retry-idempotent','ordinary-old-source-admission-refused','concurrent-decisions-have-one-winner',
    'later-reviewed-epoch-honors-prior-barrier','stale-decisions-replay-refused','wrong-pinned-authority-refused',
    'missing-authority-not-recreated','missing-completed-history-refused','restored-history-validates-exactly',
    'fresh-reviewed-final-transfer','receiver-actual-process-and-retained-bytes','receiver-divergent-work-preserved',
    'divergent-return-decision-refused','target-capacity-overcommit-refused','peer-pid-nonce-bytes-preserved-throughout',
})
REPORT_HASHES={'fixture_sha256','contract_sha256','authority_sha256','initial_plan_sha256','concurrent_winner_plan_sha256',
    'final_plan_sha256','initial_retained_sha256','final_retained_sha256','peer_work_sha256','snapshot_marker_sha256',
    'runtime_sha256','initial_inventory_sha256','pending_inventory_sha256','final_inventory_sha256'}


def private_json(path,value):
    path=Path(path)
    raw=(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()
    if len(raw)>64*1024: raise ValueError('fixture record exceeds bound')
    fd,scratch=tempfile.mkstemp(prefix='.fixture-record-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            os.fchmod(stream.fileno(),0o600); stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(scratch,path)
        directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(scratch): os.unlink(scratch)


SERVICE_SCRIPT=r'''import os,sys,resource,fcntl,hashlib,json
resource.setrlimit(resource.RLIMIT_CPU,(5,5))
if sys.platform=="linux":resource.setrlimit(resource.RLIMIT_AS,(128*1024**2,128*1024**2))
resource.setrlimit(resource.RLIMIT_FSIZE,(1024*1024,1024*1024))
resource.setrlimit(resource.RLIMIT_NOFILE,(32,32))
home,barrier,lock_path,nonce,worker,course,epoch,authority,held_fd=sys.argv[1:]
with (os.fdopen(int(held_fd),"a+b") if held_fd!="-1" else open(lock_path,"a+b")) as gate:
    if held_fd=="-1":fcntl.flock(gate,fcntl.LOCK_EX)
    if os.path.lexists(barrier):
        try:
            with open(barrier,"rb") as record:
                raw=record.read(65537)
                if len(raw)>65536:raise ValueError()
                value=json.loads(raw)
            deny=(value["authority_id"]!=authority or value["course_id"]!=course or value["worker_id"]!=worker
                or type(value["denied_through_epoch"]) is not int or int(epoch)<=value["denied_through_epoch"])
        except (OSError,ValueError,KeyError,TypeError):deny=True
        if deny:print("REVIVAL_REFUSED",flush=True);sys.exit(23)
    print("READY "+nonce,flush=True)
for line in sys.stdin:
    if line.strip()=="PING":
        with open(os.path.join(home,"saved.txt"),"rb") as work: digest=hashlib.sha256(work.read(1024*1024+1)).hexdigest()
        print("ALIVE "+nonce+" "+digest,flush=True)
    elif line.strip()=="STOP":break
    else:sys.exit(24)
'''


class FixtureProcesses:
    """Exact Popen handles, bounded pipes, and a durable independent barrier."""
    def __init__(self,backend,authority_id='a'*32):
        self.backend=Path(backend)
        self.backend.mkdir(mode=0o700,parents=False,exist_ok=False)
        self.lock_path=self.backend/'activation.lock'
        fd=os.open(self.lock_path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600); os.close(fd)
        self.children=[]; self.services={}; self.endpoint_children={}; self.cleanup_verified=False
        self.authority_id=authority_id; self.local=threading.local()

    @staticmethod
    def identity(worker,course):
        return hashlib.sha256((str(worker)+'\0'+str(course)).encode()).hexdigest()

    def barrier_path(self,worker,course):
        return self.backend/(self.identity(worker,course)+'.revival-deny.json')

    def activation_lock_path(self,worker=None,course=None):
        if worker is None: return self.lock_path
        path=self.backend/(self.identity(worker,course)+'.activation.lock')
        fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600); os.close(fd)
        return path

    @contextmanager
    def activation_lock(self,worker=None,course=None):
        with self.activation_locks([(worker,course)]): yield

    @contextmanager
    def activation_locks(self,endpoints):
        if getattr(self.local,'fds',{}): raise RuntimeError('fixture activation lock already held')
        descriptors={}
        try:
            selected=sorted((str(self.activation_lock_path(worker,course)),worker,course) for worker,course in endpoints)
            for path,worker,course in selected:
                key=self.identity(worker,course) if worker is not None else None
                if key in descriptors: raise RuntimeError('duplicate fixture endpoint lock')
                fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
                descriptors[key]=fd; deadline=time.monotonic()+3
                while True:
                    try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); break
                    except BlockingIOError:
                        if time.monotonic()>=deadline: raise RuntimeError('fixture endpoint exclusion timed out')
                        time.sleep(0.01)
            self.local.fds=descriptors
            yield
        finally:
            self.local.fds={}
            for fd in reversed(list(descriptors.values())):
                fcntl.flock(fd,fcntl.LOCK_UN); os.close(fd)

    @staticmethod
    def receive(child):
        line=b''; deadline=time.monotonic()+3
        while b'\n' not in line:
            remaining=deadline-time.monotonic()
            if remaining<=0 or not select.select([child.stdout],[],[],remaining)[0]:
                raise RuntimeError('owned fixture process response timed out')
            chunk=os.read(child.stdout.fileno(),1025-len(line))
            if not chunk: raise RuntimeError('owned fixture process response ended unexpectedly')
            line+=chunk
            if len(line)>1024: raise RuntimeError('owned fixture process response exceeded bound')
        if not line.endswith(b'\n') or line.count(b'\n')!=1:
            raise RuntimeError('owned fixture process response invalid')
        return line.decode('ascii').strip()

    def launch(self,worker,course,home,*,epoch=1,expect_refusal=False):
        key=self.identity(worker,course); nonce=uuid.uuid4().hex
        if key in self.services and self.services[key][0].poll() is None:
            raise RuntimeError('fixture course already has an owned service')
        held=getattr(self.local,'fds',{})
        fd=held.get(key)
        if held and fd is None:
            raise RuntimeError('held fixture activation lock belongs to another endpoint')
        child=subprocess.Popen([sys.executable,'-I','-u','-c',SERVICE_SCRIPT,str(home),
            str(self.barrier_path(worker,course)),str(self.activation_lock_path(worker,course)),nonce,
            str(worker),str(course),str(epoch),self.authority_id,str(fd if fd is not None else -1)],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,start_new_session=True,bufsize=0,
            pass_fds=(fd,) if fd is not None else ())
        self.children.append(child)
        self.endpoint_children[child]=key
        reply=self.receive(child)
        if expect_refusal:
            if reply!='REVIVAL_REFUSED' or child.wait(timeout=3)!=23:
                raise AssertionError('new source client bypassed durable revival barrier')
            return None
        if reply!='READY '+nonce or child.poll() is not None:
            raise RuntimeError('owned fixture service did not start')
        self.services[key]=(child,nonce,Path(home))
        return child

    def live(self,worker,course):
        child,nonce,home=self.services[self.identity(worker,course)]
        if child.poll() is not None: raise AssertionError('owned fixture service stopped unexpectedly')
        child.stdin.write(b'PING\n'); child.stdin.flush()
        expected='ALIVE '+nonce+' '+sha256_file(home/'saved.txt')
        if self.receive(child)!=expected: raise AssertionError('owned fixture process or saved work changed')
        return child.pid

    def stop(self,worker,course):
        key=self.identity(worker,course)
        for child in self.children:
            if self.endpoint_children.get(child)==key: self.reap(child)

    def running_count(self,worker,course):
        key=self.identity(worker,course)
        return sum(self.endpoint_children.get(child)==key and child.poll() is None for child in self.children)

    @staticmethod
    def reap(child):
        if child.poll() is None:
            child.terminate()
            try: child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait(timeout=3)
        if child.poll() is None: raise RuntimeError('owned fixture process termination unverified')
        for stream in (child.stdin,child.stdout):
            if stream is not None: stream.close()

    def close(self):
        failures=[]
        for child in self.children:
            try: self.reap(child)
            except Exception: failures.append('owned-process-cleanup-failed')
        if failures or any(child.poll() is None for child in self.children):
            raise RuntimeError('ownership fixture cleanup unverified')
        self.cleanup_verified=True
        return True


def read_private(path):
    path=Path(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as source:
        info=os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size>64*1024:
            raise ValueError('fixture private record invalid')
        raw=source.read(64*1024+1)
    if len(raw)>64*1024: raise ValueError('fixture private record exceeds bound')
    return json.loads(raw)


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def retained_digest(root):
    root=Path(root); paths=[root]; pending=[root]
    while pending:
        parent=pending.pop()
        with os.scandir(parent) as entries:
            for entry in entries:
                if len(paths)>=1000: raise ValueError('fixture retained tree exceeds entry bound')
                path=Path(entry.path); paths.append(path)
                if entry.is_dir(follow_symlinks=False): pending.append(path)
    rows=[]; total=0
    for path in sorted(paths):
        info=path.lstat(); name=path.relative_to(root).as_posix()
        if len(rows)>=1000: raise ValueError('fixture retained tree exceeds entry bound')
        if stat.S_ISDIR(info.st_mode): item=[name,'directory',stat.S_IMODE(info.st_mode),info.st_uid,info.st_gid]
        elif stat.S_ISREG(info.st_mode):
            total+=info.st_size
            if total>1024*1024: raise ValueError('fixture retained bytes exceed bound')
            item=[name,'file',stat.S_IMODE(info.st_mode),info.st_uid,info.st_gid,sha256_file(path)]
        elif stat.S_ISLNK(info.st_mode): item=[name,'symlink',os.readlink(path),info.st_uid,info.st_gid]
        else: raise ValueError('fixture retained tree special entry refused')
        rows.append(item)
    return digest(rows)


def refuse(operation,contract,codes):
    try: operation()
    except contract.OwnershipError as error:
        if error.code not in codes: raise AssertionError('contract refused for an unexpected reason') from None
        return
    raise AssertionError('unsafe ownership contract operation was accepted')


def interrupt_at(expected):
    def hook(stage,transaction_id):
        if stage==expected: raise RuntimeError('synthetic ownership interruption')
    return hook


def assert_reservations(inventory,source_count,target_count,pending):
    assert inventory['registered_workers']==2 and inventory['registered_courses']==3
    assert inventory['pending_transfers']==pending
    assert inventory['reservations']=={
        'worker-one':{'count':source_count,'memory_mb':source_count*1536,'cpu':source_count*1.25},
        'worker-two':{'count':target_count,'memory_mb':target_count*1536,'cpu':target_count*1.25}}


def verify_peer(backend,pid,nonce,expected_sha):
    record=backend.processes.services[backend.processes.identity('worker-one','peer-course')]
    assert record[0].pid==pid and record[1]==nonce
    assert sha256_file(backend.home('peer-course','worker-one')/'saved.txt')==expected_sha
    assert backend.processes.live('worker-one','peer-course')==pid


def missing_history(authority,contract,transaction_id):
    """Adversarial SQL control in this disposable DB; never recreate authority."""
    before=authority.inventory()
    connection=authority._raw_connection()
    try:
        connection.execute('BEGIN IMMEDIATE')
        receipt=connection.execute('SELECT receipt FROM transfers WHERE transaction_id=?',(transaction_id,)).fetchone()['receipt']
        assert receipt is not None
        connection.execute('UPDATE transfers SET receipt=NULL WHERE transaction_id=?',(transaction_id,)); connection.execute('COMMIT')
    finally: connection.close()
    try:
        refuse(authority.inventory,contract,{'authority_history_missing_or_changed','authority_transfer_history_invalid'})
        def admission():
            with authority.admission('peer-course','worker-one'): raise AssertionError('missing history permitted admission')
        refuse(admission,contract,{'authority_history_missing_or_changed','authority_transfer_history_invalid'})
    finally:
        connection=authority._raw_connection()
        try:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('UPDATE transfers SET receipt=? WHERE transaction_id=?',(receipt,transaction_id)); connection.execute('COMMIT')
        finally: connection.close()
    assert authority.inventory()==before


def cleanup_fixture(backend,fixture):
    fixture=Path(fixture)
    if backend is not None:
        if backend.close() is not True or backend.processes.cleanup_verified is not True:
            raise RuntimeError('owned ownership processes cleanup unverified')
    if fixture.is_symlink() or fixture.resolve()!=fixture or fixture.stat().st_mode & 0o077:
        raise RuntimeError('owned private fixture cleanup binding invalid')
    shutil.rmtree(fixture)
    if os.path.lexists(fixture): raise RuntimeError('owned private fixture files remain')
    return True


def publish_pass(root,report,cleanup_verified):
    if (cleanup_verified is not True or not isinstance(report,dict)
            or set(report)!=REPORT_HASHES|{'waypoint_revision','checks'}):
        raise ValueError('ownership PASS requires bounded evidence and verified exact cleanup')
    if (not isinstance(report['waypoint_revision'],str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',report['waypoint_revision'])
            or any(not isinstance(report[key],str) or not HASH.fullmatch(report[key]) for key in REPORT_HASHES)
            or not isinstance(report['checks'],list) or any(not isinstance(value,str) for value in report['checks'])
            or set(report['checks'])!=CHECKS or len(report['checks'])!=len(CHECKS)
            or report['initial_retained_sha256']==report['final_retained_sha256']
            or len({report[key] for key in ('initial_plan_sha256','concurrent_winner_plan_sha256','final_plan_sha256')})!=3):
        raise ValueError('ownership evidence fingerprints or required controls invalid')
    data=(json.dumps(dict(report,version=1,status='PASS',scope='standalone-disposable-logical-worker-contract',
        source_clean=True,fixture_only=True,logical_workers=2,real_worker_hosts=0,runtime_enforcement=False,
        production_activation_adapter=False,provider_adapter=False,
        authenticated_remote_transport=False,actual_two_host_isolation=False,encrypted_backup_verified=False,
        offhost_backup_verified=False,measured_profile_enforcement=False,fixture_process_restart_only=True,
        snapshot_scope='opaque-local-fixture-marker',concurrent_winners=1,cleanup_verified=True),
        sort_keys=True,indent=2,allow_nan=False)+'\n').encode()
    if len(data)>64*1024: raise ValueError('ownership evidence output exceeds bound')
    root=Path(root)
    if root.is_symlink() or not root.is_dir() or root.stat().st_mode & 0o077:
        raise ValueError('ownership evidence output directory must be private')
    fd,scratch=tempfile.mkstemp(prefix='.ownership-evidence-',dir=root)
    try:
        with os.fdopen(fd,'wb') as output:
            os.fchmod(output.fileno(),0o600); output.write(data); output.flush(); os.fsync(output.fileno())
        os.link(scratch,root/'OWNERSHIP-CONTRACT-PASS.json')
    finally: os.unlink(scratch)


class FixtureBackend:
    """Local file/process adapter; deliberately has no production capabilities."""
    fixture_only=True
    scope='course'

    def __init__(self,root):
        self.root=Path(root); self.root.mkdir(mode=0o700)
        self.processes=FixtureProcesses(self.root/'independent-control-backend')
        self.workers={}; self.available=True; self.context_inspections=0
        self.fence_lock_verified=False

    def add_worker(self,worker_id,provider_id,*,memory=3072,cpu=2.5):
        directory=self.root/worker_id; directory.mkdir(mode=0o700)
        ledger=directory/'worker-ledger'; ledger.mkdir(mode=0o700)
        private_json(ledger/'local-state.json',{'worker_id':worker_id,'fixture_only':True})
        private_json(directory/'identity.json',{'worker_id':worker_id,'provider_id':provider_id,'boot_id':uuid.uuid4().hex})
        private_json(directory/'policy.json',{'memory_budget_mb':memory,'cpu_budget':cpu,
                     'capacity_memory_mb':memory,'capacity_cpu':cpu})
        (directory/'courses').mkdir(mode=0o700)
        self.workers[worker_id]=directory

    def home(self,course,worker):
        return self.workers[worker]/'courses'/course/'retained'

    def add_course(self,course,worker,*,logical=None,copy_from=None):
        directory=self.workers[worker]/'courses'/course; directory.mkdir(mode=0o700)
        if copy_from is not None:
            source=self.workers[copy_from]/'courses'/course
            for name in ('logical.json','profile.json','runtime.py','snapshot.fixture'):
                shutil.copyfile(source/name,directory/name); (directory/name).chmod(0o600)
            shutil.copytree(source/'retained',directory/'retained',copy_function=shutil.copy2)
        else:
            identity=logical or {'origin':'https://'+course+'.ownership-fixture.test',
                'dns_subtree':course+'.ownership-fixture.test','issuer':'https://identity.ownership-fixture.test/realms/'+course,
                'client_id':course}
            private_json(directory/'logical.json',identity)
            private_json(directory/'profile.json',{'active_limit':1,'learner_memory_mb':512,'learner_cpu':0.25,'memory_mb':1536,'cpu':1.25})
            (directory/'runtime.py').write_text(SERVICE_SCRIPT); (directory/'runtime.py').chmod(0o600)
            (directory/'retained').mkdir(mode=0o700)
            (directory/'retained/saved.txt').write_bytes(('synthetic retained coursework for '+course+'\n').encode())
            (directory/'retained/saved.txt').chmod(0o600)
            (directory/'snapshot.fixture').write_text('OPAQUE LOCAL FIXTURE BACKUP MARKER\n'+retained_digest(directory/'retained')+'\n')
            (directory/'snapshot.fixture').chmod(0o600)

    def inspect_worker(self,worker_id):
        if not self.available: raise RuntimeError('fixture inspector unavailable')
        directory=self.workers[worker_id]
        identity=read_private(directory/'identity.json'); policy=read_private(directory/'policy.json')
        return dict(identity,policy_sha256=sha256_file(directory/'policy.json'),**policy)

    def inspect_course(self,course_id,worker_id):
        if not self.available: raise RuntimeError('fixture inspector unavailable')
        directory=self.workers[worker_id]/'courses'/course_id
        barrier=self.processes.barrier_path(worker_id,course_id)
        floor=1
        if os.path.lexists(barrier):
            record=read_private(barrier)
            if record['authority_id']!=self.processes.authority_id or record['worker_id']!=worker_id or record['course_id']!=course_id:
                raise ValueError('fixture barrier binding changed')
            floor=record['denied_through_epoch']+1
        return {'logical_identity':read_private(directory/'logical.json'),'profile':read_private(directory/'profile.json'),
                'snapshot_sha256':sha256_file(directory/'snapshot.fixture'),'runtime_sha256':sha256_file(directory/'runtime.py'),
                'retained_tree_sha256':retained_digest(directory/'retained'),'admission_floor':floor,
                'running_count':self.processes.running_count(worker_id,course_id)}

    @contextmanager
    def activation_context(self,request):
        worker,course=request['worker_id'],request['course_id']
        with self.processes.activation_lock(worker,course):
            observed=self.inspect_worker(worker)
            if (observed['provider_id']!=request['provider_id'] or observed['boot_id']!=request['boot_id']
                    or request['authority_id']!=self.processes.authority_id):
                raise ValueError('fixture activation endpoint changed')
            if request['epoch']<self.inspect_course(course,worker)['admission_floor']:
                raise ValueError('fixture durable revival barrier refused activation')
            yield

    @contextmanager
    def fence_context(self,request):
        worker,course=request['source_worker_id'],request['course_id']; receiver=request['receiver_worker_id']
        with self.processes.activation_locks([(worker,course),(receiver,course)]):
            observed=self.inspect_worker(worker)
            if (observed['provider_id']!=request['source_provider_id'] or observed['boot_id']!=request['source_boot_id']
                    or request['authority_id']!=self.processes.authority_id):
                raise ValueError('fixture source endpoint changed')
            receiver_observed=self.inspect_worker(receiver)
            if (receiver_observed['provider_id']!=request['receiver_provider_id']
                    or receiver_observed['boot_id']!=request['receiver_boot_id']):
                raise ValueError('fixture receiver endpoint changed')
            if self.inspect_course(course,receiver)['admission_floor']>request['old_epoch']+1:
                raise ValueError('fixture receiver epoch unavailable')
            if self.processes.running_count(receiver,course): raise ValueError('fixture receiver must be stopped')
            path=self.processes.barrier_path(worker,course)
            request_hash=digest(request)
            old=read_private(path) if os.path.lexists(path) else None
            if old is not None and old['request_sha256']==request_hash:
                record=old
            else:
                record={'version':1,'authority_id':request['authority_id'],'course_id':course,'worker_id':worker,
                    'provider_id':request['source_provider_id'],'boot_id':request['source_boot_id'],
                    'barrier_id':uuid.uuid4().hex,'request_sha256':request_hash,
                    'denied_through_epoch':max(request['old_epoch'],old['denied_through_epoch'] if old else 0)}
                private_json(path,record)
            self.processes.stop(worker,course)
            backend=self
            class FenceContext:
                def inspect(self):
                    if not backend.available: raise RuntimeError('fixture barrier inspector unavailable')
                    current=read_private(path)
                    if current!=record: raise ValueError('fixture durable barrier changed')
                    actual=backend.inspect_worker(worker)
                    if actual['provider_id']!=request['source_provider_id'] or actual['boot_id']!=request['source_boot_id']:
                        raise ValueError('fixture source incarnation changed')
                    if (backend.inspect_worker(receiver)!=receiver_observed
                            or backend.inspect_course(course,receiver)['admission_floor']>request['old_epoch']+1
                            or backend.processes.running_count(receiver,course)):
                        raise ValueError('fixture receiver eligibility changed')
                    running=backend.processes.running_count(worker,course)
                    # Another independently opened descriptor must remain excluded
                    # until the ownership CAS exits this fencing context.
                    for endpoint in (worker,receiver):
                        descriptor=os.open(backend.processes.activation_lock_path(endpoint,course),os.O_RDWR)
                        try:
                            try: fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
                            except BlockingIOError: backend.fence_lock_verified=True
                            else: raise AssertionError('endpoint activation lock released before ownership CAS')
                        finally: os.close(descriptor)
                    backend.context_inspections+=1
                    return {'request_sha256':request_hash,'barrier_id':current['barrier_id'],'barrier_sha256':sha256_file(path),
                            'scope':'course','denied_through_epoch':current['denied_through_epoch'],'observed_running_count':running}
            yield FenceContext()

    def start(self,authority,course,worker):
        with authority.admission(course,worker) as grant:
            assert grant['fixture_only'] is True and grant['production_admission'] is False
            return self.processes.launch(worker,course,self.home(course,worker),epoch=grant['epoch'])

    def close(self):
        return self.processes.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    if not __debug__: parser.error('ownership contract rehearsal requires Python assertions')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    import ownership_contract as contract
    source_state=_source(ROOT.parent)
    if source_state['dirty']: parser.error('accepted ownership contract evidence requires clean source')
    fixture_hash=sha256_file(__file__); contract_hash=sha256_file(ROOT/'ownership_contract.py')
    args.output.mkdir(mode=0o700,parents=True,exist_ok=False); root=args.output.resolve()
    fixture=root/'private-fixture'; fixture.mkdir(mode=0o700)
    backend=None; checks=[]
    try:
        backend=FixtureBackend(fixture/'backend')
        for worker,provider in (('worker-one','logical-provider-one'),('worker-two','logical-provider-two')):
            backend.add_worker(worker,provider)
        authority=contract.Authority.create(fixture/'authority',backend)
        backend.processes.authority_id=authority.authority_id
        authority.register_worker('worker-one')
        identity_path=backend.workers['worker-two']/'identity.json'; original=identity_path.read_bytes()
        try:
            duplicate=read_private(identity_path); duplicate['provider_id']='logical-provider-one'; private_json(identity_path,duplicate)
            refuse(lambda:authority.register_worker('worker-two'),contract,{'duplicate_provider_identity'})
        finally: identity_path.write_bytes(original)
        checks.append('duplicate-provider-identity-refused')
        authority.register_worker('worker-two')
        reopened=contract.Authority.open(authority.directory,authority.authority_id,backend)
        assert reopened.authority_id==authority.authority_id
        checks.append('two-worker-incarnations-pinned')
        for course,worker in (('course-one','worker-one'),('peer-course','worker-one'),('target-filler','worker-two')):
            backend.add_course(course,worker); authority.register_course(course,worker)
        backend.add_course('course-one','worker-two',copy_from='worker-one')
        backend.add_course('identity-collision','worker-one',logical=backend.inspect_course('course-one','worker-one')['logical_identity'])
        refuse(lambda:authority.register_course('identity-collision','worker-one'),contract,{'global_identity_collision'})
        checks.append('global-course-identity-collision-refused')
        backend.add_course('source-overflow','worker-one')
        initial=authority.inventory()
        refuse(lambda:authority.register_course('source-overflow','worker-one'),contract,{'receiver_capacity_unavailable'})
        assert authority.inventory()==initial
        checks.append('source-capacity-overcommit-refused')
        source_process=backend.start(authority,'course-one','worker-one')
        peer_process=backend.start(authority,'peer-course','worker-one')
        source_bytes=(backend.home('course-one','worker-one')/'saved.txt').read_bytes()
        peer_sha=sha256_file(backend.home('peer-course','worker-one')/'saved.txt')
        peer_nonce=backend.processes.services[backend.processes.identity('worker-one','peer-course')][1]
        backend.processes.live('worker-one','course-one'); verify_peer(backend,peer_process.pid,peer_nonce,peer_sha)
        checks.append('actual-source-peer-processes-live')
        source_evidence=backend.inspect_course('course-one','worker-one')
        assert contract.stable_evidence(source_evidence)==contract.stable_evidence(backend.inspect_course('course-one','worker-two'))
        assert (backend.home('course-one','worker-two')/'saved.txt').read_bytes()==source_bytes
        checks.append('copied-synthetic-bytes-and-fingerprints-match')
        receiver_directory=backend.workers['worker-two']/'courses/course-one'
        variants={
            'logical.json':lambda raw:dict(json.loads(raw),origin='https://changed.ownership-fixture.test',dns_subtree='changed.ownership-fixture.test'),
            'profile.json':lambda raw:dict(json.loads(raw),active_limit=2,memory_mb=2048,cpu=1.5),
            'snapshot.fixture':lambda raw:raw+b'changed snapshot marker\n',
            'runtime.py':lambda raw:raw+b'\n# changed synthetic runtime\n',
            'retained/saved.txt':lambda raw:raw+b'changed retained bytes\n',
        }
        for name,alter in variants.items():
            path=receiver_directory/name; before=path.read_bytes(); rejected=fixture/('rejected-'+Path(name).name+'.json')
            try:
                changed=alter(before)
                if isinstance(changed,dict): private_json(path,changed)
                else: path.write_bytes(changed)
                refuse(lambda:authority.preview('course-one','worker-two',rejected),contract,
                       {'matching_retained_backup_runtime_and_profile_required'})
                assert not rejected.exists() and authority.inventory()==initial
            finally: path.write_bytes(before)
        checks.append('wrong-identity-snapshot-runtime-profile-tree-refused')
        plan_path=fixture/'initial-plan.json'; plan=authority.preview('course-one','worker-two',plan_path)
        unapproved=backend.processes.launch('worker-two','course-one',backend.home('course-one','worker-two'))
        try:
            refuse(lambda:authority.apply(plan_path),contract,{'receiver_course_must_be_stopped'})
            assert authority.inventory()==initial
        finally: backend.processes.stop('worker-two','course-one')
        assert unapproved.poll() is not None; checks.append('live-unapproved-receiver-refused')
        source_identity=backend.workers['worker-one']/'identity.json'; before=source_identity.read_bytes()
        try:
            changed=read_private(source_identity); changed['boot_id']=uuid.uuid4().hex; private_json(source_identity,changed)
            refuse(lambda:authority.apply(plan_path),contract,{'worker_incarnation_policy_or_capacity_changed'})
        finally: source_identity.write_bytes(before)
        assert authority.inventory()==initial; checks.append('wrong-worker-incarnation-refused')
        raw_plan=plan_path.read_bytes()
        try:
            plan_path.write_text('{}'); refuse(lambda:authority.apply(plan_path),contract,{'invalid_ownership_plan'})
        finally: plan_path.write_bytes(raw_plan)
        checks.append('tampered-plan-refused')
        with patch.object(contract,'fault_hook',side_effect=interrupt_at('prepared')):
            refuse(lambda:authority.apply(plan_path),contract,{'ownership_contract_refused_or_incomplete'})
        pending=authority.inventory(); assert_reservations(pending,2,2,1)
        assert backend.processes.live('worker-one','course-one')==source_process.pid
        checks.append('prepared-interruption-kept-both-reservations')
        for worker in ('worker-one','worker-two'):
            refuse(lambda worker=worker:backend.start(authority,'course-one',worker),contract,
                   {'ownership_transfer_pending','current_owner_required'})
        checks.append('both-pending-endpoints-refuse-admission')
        backend.available=False
        try:
            refuse(lambda:authority.resume(plan_path),contract,{'ownership_contract_refused_or_incomplete'})
            assert backend.processes.live('worker-one','course-one')==source_process.pid
            assert authority.inventory()==pending
        finally: backend.available=True
        checks.append('unreachable-inspector-not-fencing-proof')
        with patch.object(contract,'fault_hook',side_effect=interrupt_at('fenced')):
            refuse(lambda:authority.resume(plan_path),contract,{'ownership_contract_refused_or_incomplete'})
        assert authority.inventory()==pending; assert source_process.poll() is not None
        checks.append('fenced-interruption-kept-both-reservations')
        barrier=backend.processes.barrier_path('worker-one','course-one')
        assert read_private(barrier)['denied_through_epoch']==1
        checks.append('persistent-course-barrier-and-real-source-stop')
        backend.processes.launch('worker-one','course-one',backend.home('course-one','worker-one'),epoch=1,expect_refusal=True)
        checks.append('new-source-client-revival-refused')
        source_ledger=backend.workers['worker-one']/'worker-ledger'
        (source_ledger/'local-state.json').unlink(); source_ledger.rmdir()
        backend.processes.launch('worker-one','course-one',backend.home('course-one','worker-one'),epoch=1,expect_refusal=True)
        checks.append('barrier-independent-of-source-worker-ledger')
        with authority.admission('peer-course','worker-one') as grant:
            assert grant['fixture_only'] is True and grant['epoch']==1
            verify_peer(backend,peer_process.pid,peer_nonce,peer_sha)
        checks.append('peer-live-through-pending-and-fencing')
        completed=authority.resume(plan_path)
        assert completed['status']=='COMPLETE' and completed['epoch']==2 and completed['services_started'] is False
        assert backend.processes.running_count('worker-two','course-one')==0
        assert source_process.poll() is not None; assert_reservations(authority.inventory(),1,2,0)
        checks.append('exact-resume-and-no-automatic-start')
        assert backend.fence_lock_verified and backend.context_inspections>=3
        checks.append('both-endpoint-locks-held-through-fresh-proof-cas')
        before=authority.inventory(); assert authority.apply(plan_path)==completed; assert authority.inventory()==before
        checks.append('completed-retry-idempotent')
        refuse(lambda:backend.start(authority,'course-one','worker-one'),contract,{'current_owner_required'})
        checks.append('ordinary-old-source-admission-refused')
        left=fixture/'concurrent-left.json'; right=fixture/'concurrent-right.json'
        authority.preview('course-one','worker-one',left); authority.preview('course-one','worker-one',right)
        def contender(path):
            client=contract.Authority.open(authority.directory,authority.authority_id,backend)
            try: return client.apply(path)
            except contract.OwnershipError as error: return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(contender,(left,right)))
        winners=[value for value in outcomes if isinstance(value,dict) and value['status']=='COMPLETE']
        losers=[value for value in outcomes if isinstance(value,str)]
        assert len(winners)==1 and len(losers)==1
        assert losers[0] in ('stale_ownership_approval','transfer_pending_history_changed')
        winner=winners[0]; assert winner['epoch']==3
        assert_reservations(authority.inventory(),2,1,0)
        checks.append('concurrent-decisions-have-one-winner')
        returned=backend.start(authority,'course-one','worker-one')
        assert backend.processes.live('worker-one','course-one')==returned.pid
        assert read_private(barrier)['denied_through_epoch']==1
        checks.append('later-reviewed-epoch-honors-prior-barrier')
        refuse(lambda:authority.apply(plan_path),contract,{'stale_completed_ownership_decision'})
        for old in (left,right):
            if read_private(old)['digest']!=winner['plan_sha256']:
                refuse(lambda old=old:authority.apply(old),contract,{'stale_ownership_approval'})
        checks.append('stale-decisions-replay-refused')
        refuse(lambda:contract.Authority.open(authority.directory,uuid.uuid4().hex,backend),contract,
               {'authority_identity_or_database_changed'})
        checks.append('wrong-pinned-authority-refused')
        held_sentinel=fixture/'held-authority-sentinel.json'; authority.sentinel.rename(held_sentinel)
        try:
            refuse(lambda:contract.Authority.open(authority.directory,authority.authority_id,backend),contract,
                   {'ownership_contract_refused_or_incomplete'})
            assert not authority.sentinel.exists()
        finally: held_sentinel.rename(authority.sentinel)
        checks.append('missing-authority-not-recreated')
        missing_history(authority,contract,winner['transaction_id'])
        checks+=['missing-completed-history-refused','restored-history-validates-exactly']
        verify_peer(backend,peer_process.pid,peer_nonce,peer_sha)
        final_plan_path=fixture/'final-plan.json'; final_plan=authority.preview('course-one','worker-two',final_plan_path)
        final_result=authority.apply(final_plan_path)
        assert final_result['epoch']==4 and final_result['services_started'] is False
        assert returned.poll() is not None
        checks.append('fresh-reviewed-final-transfer')
        receiver=backend.start(authority,'course-one','worker-two')
        assert backend.processes.live('worker-two','course-one')==receiver.pid
        assert (backend.home('course-one','worker-two')/'saved.txt').read_bytes()==source_bytes
        checks.append('receiver-actual-process-and-retained-bytes')
        saved=backend.home('course-one','worker-two')/'saved.txt'
        with saved.open('ab') as output:
            output.write(b'new receiver-only coursework\n'); output.flush(); os.fsync(output.fileno())
        final_tree=retained_digest(backend.home('course-one','worker-two'))
        assert final_tree!=source_evidence['retained_tree_sha256']
        receiver_bytes=saved.read_bytes(); backend.processes.live('worker-two','course-one')
        checks.append('receiver-divergent-work-preserved')
        rejected=fixture/'divergent-return.json'
        refuse(lambda:authority.preview('course-one','worker-one',rejected),contract,
               {'matching_retained_backup_runtime_and_profile_required'})
        assert not rejected.exists() and saved.read_bytes()==receiver_bytes
        assert (backend.home('course-one','worker-one')/'saved.txt').read_bytes()==source_bytes
        checks.append('divergent-return-decision-refused')
        backend.add_course('peer-course','worker-two',copy_from='worker-one')
        rejected=fixture/'target-overcommit.json'; before=authority.inventory()
        refuse(lambda:authority.preview('peer-course','worker-two',rejected),contract,{'receiver_capacity_unavailable'})
        assert not rejected.exists() and authority.inventory()==before
        checks.append('target-capacity-overcommit-refused')
        verify_peer(backend,peer_process.pid,peer_nonce,peer_sha)
        checks.append('peer-pid-nonce-bytes-preserved-throughout')
        final=authority.inventory(); assert_reservations(final,1,2,0)
        assert _source(ROOT.parent)==source_state and sha256_file(__file__)==fixture_hash
        assert sha256_file(ROOT/'ownership_contract.py')==contract_hash
        report={'waypoint_revision':source_state['revision'],'fixture_sha256':fixture_hash,'contract_sha256':contract_hash,
            'authority_sha256':digest(authority.authority_id),'initial_plan_sha256':plan['plan_sha256'],
            'concurrent_winner_plan_sha256':winner['plan_sha256'],'final_plan_sha256':final_plan['plan_sha256'],
            'initial_retained_sha256':source_evidence['retained_tree_sha256'],'final_retained_sha256':final_tree,
            'peer_work_sha256':peer_sha,'snapshot_marker_sha256':source_evidence['snapshot_sha256'],
            'runtime_sha256':source_evidence['runtime_sha256'],'initial_inventory_sha256':digest(initial),
            'pending_inventory_sha256':digest(pending),'final_inventory_sha256':digest(final),'checks':sorted(checks)}
    finally:
        cleanup_verified=cleanup_fixture(backend,fixture)
    publish_pass(root,report,cleanup_verified)
    print('PASS: standalone two-logical-worker contract; real owned processes, persistent fixture fencing and retained bytes; no production/provider/remote-host acceptance.',flush=True)


if __name__=='__main__': main()
