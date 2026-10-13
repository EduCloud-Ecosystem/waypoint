#!/usr/bin/env python3
"""Reconcile pending ownership using two logical workers and owned processes."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
from unittest.mock import patch
import uuid

from verify_ownership_contract import (ROOT, FixtureBackend, private_json, read_private, digest,
    retained_digest, refuse, interrupt_at, cleanup_fixture)
from evidence import _source,sha256_file
sys.path.insert(0,str(ROOT))

VERSION='educloud-pending-transfer-reconciliation-rehearsal-v1'
HASH=re.compile(r'[0-9a-f]{64}\Z')
FINGERPRINT_FILES={
    'ownership_contract':ROOT/'ownership_contract.py',
    'ownership_core_tests':ROOT/'tests/test_ownership_contract.py',
    'original_verifier':ROOT/'rehearsal/verify_ownership_contract.py',
    'original_rehearsal_tests':ROOT/'rehearsal/test_ownership_contract_rehearsal.py',
    'reconciliation_verifier':Path(__file__),
    'reconciliation_rehearsal_tests':ROOT/'rehearsal/test_pending_reconciliation_rehearsal.py',
}
MATRIX={(stage,selected) for stage in ('prepared','fenced','committed') for selected in ('source','receiver')}
CHECKS=frozenset({
    'two-fixed-logical-workers-one-private-authority','original-three-stages-both-endpoint-selections',
    'nested-three-recovery-stages-fresh-reviewed-selection','original-and-linked-parent-rows-immutable',
    'divergent-retained-bytes-preserved-on-both-endpoints','independent-opaque-snapshot-history-retained',
    'changed-snapshot-approval-refused','changed-incarnation-approval-refused','tampered-reconciliation-plan-refused',
    'live-endpoint-preview-refused','reconciliation-prepared-kept-both-reservations',
    'reconciliation-fenced-kept-both-reservations','reconciliation-committed-kept-both-reservations',
    'both-pending-endpoints-admission-refused','dual-persistent-barriers-deny-current-old-epoch',
    'fresh-stale-clients-on-both-endpoints-refused','dual-endpoint-locks-held-through-proof-cas',
    'unavailable-inspector-never-completes-recovery','lost-indexed-barrier-refused-without-recreation',
    'barrier-mismatch-refused-without-recreation','second-endpoint-prewrite-failure-preserved-first-publication',
    'second-endpoint-prewrite-failure-exact-resume','backend-barrier-index-write-gap-stays-closed',
    'exact-recovery-retry-idempotent','completed-recovery-never-started-services',
    'selected-future-epoch-real-process-and-retained-bytes','nonselected-endpoint-admission-refused',
    'concurrent-reviewed-recovery-one-winner','stale-competing-plan-refused',
    'paused-ordinary-controller-preserved-new-source-pid-and-barriers',
    'paused-typed-controller-preserved-new-selected-pid-and-barriers',
    'post-reconciliation-ordinary-transfer-kept-barrier-identity','old-recovery-replay-after-later-epoch-refused',
    'missing-linked-history-refused','peer-pid-nonce-and-bytes-preserved-through-all-recoveries',
})


class ReconciliationBackend(FixtureBackend):
    """Independent persistent barrier history; no reset/unfence operation."""
    def __init__(self,root):
        super().__init__(root)
        self.index=self.processes.backend/'barrier-history'; self.index.mkdir(mode=0o700)
        self.reconciliation_inspections=0; self.dual_lock_verified=False

    def _history(self,worker,course):
        key=self.processes.identity(worker,course); index_path=self.index/(key+'.json')
        saved=read_private(index_path) if os.path.lexists(index_path) else None
        path=self.processes.barrier_path(worker,course)
        if saved is None:
            if os.path.lexists(path): raise ValueError('fixture unindexed barrier refused')
            return None
        record=read_private(path)
        actual={'barrier_id':record['barrier_id'],'denied_through_epoch':record['denied_through_epoch'],
                'record_sha256':sha256_file(path)}
        if (actual!=saved or record['authority_id']!=self.processes.authority_id
                or record['worker_id']!=worker or record['course_id']!=course):
            raise ValueError('fixture persistent barrier history changed')
        return record

    def _remember(self,worker,course,record):
        key=self.processes.identity(worker,course); path=self.index/(key+'.json')
        old=read_private(path) if os.path.lexists(path) else None
        if old is not None and (old['barrier_id']!=record['barrier_id']
                or old['denied_through_epoch']>record['denied_through_epoch']):
            raise ValueError('fixture persistent barrier cannot reset or lower')
        private_json(path,{'barrier_id':record['barrier_id'],'denied_through_epoch':record['denied_through_epoch'],
                          'record_sha256':sha256_file(self.processes.barrier_path(worker,course))})

    def write_barrier(self,worker,course,record):
        """Named pre-write fault seam; two-file crash atomicity is not claimed."""
        private_json(self.processes.barrier_path(worker,course),record)

    def inspect_course(self,course_id,worker_id):
        self._history(worker_id,course_id)
        observed=super().inspect_course(course_id,worker_id)
        directory=self.workers[worker_id]/'courses'/course_id
        selector=directory/'snapshot-current.json'
        if os.path.lexists(selector):
            value=read_private(selector)
            if (set(value)!={'version','name','scope'} or value['version']!=1 or value['scope']!='opaque-local-fixture-marker'
                    or not re.fullmatch(r'snapshot-[0-9a-f]{32}\.fixture',value['name'])):
                raise ValueError('fixture snapshot selector invalid')
            observed['snapshot_sha256']=sha256_file(directory/value['name'])
        return observed

    def refresh_snapshot(self,worker,course,label):
        directory=self.workers[worker]/'courses'/course; name='snapshot-'+uuid.uuid4().hex+'.fixture'
        path=directory/name
        with path.open('xb') as output:
            os.fchmod(output.fileno(),0o600)
            output.write(('OPAQUE LOCAL FIXTURE MARKER; NO ENCRYPTED OR OFFHOST ACCEPTANCE\n'+label+'\n'+retained_digest(self.home(course,worker))+'\n').encode())
            output.flush(); os.fsync(output.fileno())
        private_json(directory/'snapshot-current.json',{'version':1,'name':name,'scope':'opaque-local-fixture-marker'})
        return path

    @contextmanager
    def fence_context(self,request,*,authorize):
        worker,course=request['source_worker_id'],request['course_id']
        def authorize_history():
            authorize()
            for endpoint in (worker,request['receiver_worker_id']): self._history(endpoint,course)
        with super().fence_context(request,authorize=authorize_history) as context:
            record=read_private(self.processes.barrier_path(worker,course))
            self._remember(worker,course,record)
            yield context

    @contextmanager
    def reconciliation_context(self,request,*,authorize):
        course=request['course_id']; endpoints=(request['source_worker_id'],request['receiver_worker_id'])
        with self.processes.activation_locks([(worker,course) for worker in endpoints]):
            authorize()
            expected={}
            for worker,role in zip(endpoints,('source','receiver')):
                observed=self.inspect_worker(worker)
                if (observed['provider_id']!=request[role+'_provider_id'] or observed['boot_id']!=request[role+'_boot_id']
                        or request['authority_id']!=self.processes.authority_id):
                    raise ValueError('fixture reconciliation endpoint incarnation changed')
                old=self._history(worker,course)
                if old is not None and old['denied_through_epoch']>request['old_epoch']:
                    raise ValueError('fixture newer persistent barrier cannot be lowered')
                path=self.processes.barrier_path(worker,course)
                record={'version':1,'authority_id':request['authority_id'],'course_id':course,'worker_id':worker,
                    'provider_id':observed['provider_id'],'boot_id':observed['boot_id'],
                    'barrier_id':old['barrier_id'] if old else uuid.uuid4().hex,
                    'request_sha256':digest(request),'denied_through_epoch':request['old_epoch']}
                if old!=record:
                    self.write_barrier(worker,course,record); self._remember(worker,course,record)
                self.processes.stop(worker,course)
                expected[worker]=(observed,record)
            backend=self
            class ReconciliationContext:
                def inspect(self):
                    if not backend.available: raise RuntimeError('fixture independent endpoint inspector unavailable')
                    proof={}
                    for worker in endpoints:
                        observed,record=expected[worker]
                        if backend.inspect_worker(worker)!=observed or backend._history(worker,course)!=record:
                            raise ValueError('fixture reconciliation barrier or endpoint changed')
                        descriptor=os.open(backend.processes.activation_lock_path(worker,course),os.O_RDWR)
                        try:
                            try: fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
                            except BlockingIOError: pass
                            else: raise AssertionError('fixture endpoint exclusion released before reconciliation CAS')
                        finally: os.close(descriptor)
                        proof[worker]={'barrier_id':record['barrier_id'],'barrier_sha256':sha256_file(backend.processes.barrier_path(worker,course)),
                            'denied_through_epoch':record['denied_through_epoch'],
                            'observed_running_count':backend.processes.running_count(worker,course)}
                    backend.dual_lock_verified=True; backend.reconciliation_inspections+=1
                    return {'request_sha256':digest(request),'scope':'course','endpoints':proof}
            yield ReconciliationContext()


def fingerprints(): return {key:sha256_file(path) for key,path in FINGERPRINT_FILES.items()}


def row(authority,transaction_id):
    with authority._connection() as connection:
        value=connection.execute('SELECT * FROM transfers WHERE transaction_id=?',(transaction_id,)).fetchone()
        assert value is not None
        return dict(value)


def assert_accounting(authority,*,pending):
    value=authority.inventory()
    assert value['registered_workers']==2 and value['registered_courses']==11
    assert value['pending_transfers']==pending
    assert sum(item['count'] for item in value['reservations'].values())==11+pending
    for item in value['reservations'].values():
        assert item['memory_mb']==1536*item['count'] and item['cpu']==1.25*item['count']
        assert item['memory_mb']<=20480 and item['cpu']<=20
    return value


def peer_live(backend,pid,nonce,expected):
    observed=backend.processes.services[backend.processes.identity('worker-one','peer-course')]
    assert observed[0].pid==pid and observed[1]==nonce
    assert (backend.home('peer-course','worker-one')/'saved.txt').read_bytes()==expected
    assert backend.processes.live('worker-one','peer-course')==pid


def append_work(path,content):
    with Path(path).open('ab') as output:
        output.write(content); output.flush(); os.fsync(output.fileno())


def snapshot_witness(backend,worker,course,label,history):
    artifact=backend.refresh_snapshot(worker,course,label)
    raw=artifact.read_bytes()
    assert raw.decode().splitlines()[-1]==retained_digest(backend.home(course,worker))
    history[artifact]=raw
    return artifact


def endpoints_preserved(backend,course,expected):
    for worker,raw in expected.items():
        assert (backend.home(course,worker)/'saved.txt').read_bytes()==raw


def barriers(backend,course):
    return {worker:backend.processes.barrier_path(worker,course).read_bytes() for worker in ('worker-one','worker-two')}


def pending_admission_refused(authority,backend,contract,course):
    before=len(backend.processes.children)
    for worker in ('worker-one','worker-two'):
        refuse(lambda worker=worker:backend.start(authority,course,worker),contract,
               {'ownership_transfer_pending','current_owner_required'})
    assert len(backend.processes.children)==before


def interrupted_call(authority,contract,path,stage):
    with patch.object(contract,'fault_hook',side_effect=interrupt_at(stage)):
        refuse(lambda:authority.reconcile_resume(path),contract,{'ownership_contract_refused_or_incomplete'})


def stale_proof_controls(authority,backend,contract,course,path,completed):
    original=barriers(backend,course)
    for worker in ('worker-one','worker-two'):
        barrier=backend.processes.barrier_path(worker,course); held=barrier.with_suffix('.held')
        barrier.rename(held)
        try:
            refuse(lambda:authority.reconcile_resume(path),contract,{'ownership_contract_refused_or_incomplete'})
            assert not os.path.lexists(barrier)
        finally: held.rename(barrier)
        assert barriers(backend,course)==original
    completed.add('lost-indexed-barrier-refused-without-recreation')
    barrier=backend.processes.barrier_path('worker-two',course)
    try:
        changed=read_private(barrier); changed['barrier_id']=uuid.uuid4().hex; private_json(barrier,changed)
        refuse(lambda:authority.reconcile_resume(path),contract,{'ownership_contract_refused_or_incomplete'})
    finally: barrier.write_bytes(original['worker-two'])
    assert barriers(backend,course)==original; completed.add('barrier-mismatch-refused-without-recreation')
    backend.available=False
    try: refuse(lambda:authority.reconcile_resume(path),contract,{'ownership_contract_refused_or_incomplete'})
    finally: backend.available=True
    assert barriers(backend,course)==original; completed.add('unavailable-inspector-never-completes-recovery')


def preview_negatives(authority,backend,contract,course,selected,root,completed):
    path=root/(course+'-review.json'); value=authority.reconcile_preview(course,selected,path)
    directory=backend.workers['worker-two']/'courses'/course
    selector=directory/'snapshot-current.json'
    snapshot=directory/read_private(selector)['name'] if selector.exists() else directory/'snapshot.fixture'
    before=snapshot.read_bytes()
    try:
        snapshot.write_bytes(before+b'changed synthetic snapshot\n')
        refuse(lambda:authority.reconcile_apply(path),contract,{'reconciliation_evidence_changed'})
    finally: snapshot.write_bytes(before)
    completed.add('changed-snapshot-approval-refused')
    identity=backend.workers['worker-one']/'identity.json'; before=identity.read_bytes()
    try:
        changed=read_private(identity); changed['boot_id']=uuid.uuid4().hex; private_json(identity,changed)
        refuse(lambda:authority.reconcile_apply(path),contract,
               {'worker_incarnation_policy_or_capacity_changed','reconciliation_binding_changed'})
    finally: identity.write_bytes(before)
    completed.add('changed-incarnation-approval-refused')
    before=path.read_bytes()
    try:
        path.write_text('{}'); refuse(lambda:authority.reconcile_apply(path),contract,{'invalid_reconciliation_plan'})
    finally: path.write_bytes(before)
    completed.add('tampered-reconciliation-plan-refused')
    return path,value


def recover(authority,backend,contract,course,selected,path,plan,root,completed,*,partial_second=False,race=False):
    if race:
        competing=root/(course+'-competing.json'); authority.reconcile_preview(course,selected,competing)
        ready=threading.Barrier(2)
        def contender(candidate):
            client=contract.Authority.open(authority.directory,authority.authority_id,backend)
            observe=client._observe; first=[True]
            def simultaneous_observations(*args):
                observed=observe(*args)
                if first[0]: first[0]=False; ready.wait(timeout=5)
                return observed
            with patch.object(client,'_observe',side_effect=simultaneous_observations):
                try: return client.reconcile_apply(candidate)
                except contract.OwnershipError as error: return error.code
        with ThreadPoolExecutor(max_workers=2) as pool: outcomes=list(pool.map(contender,(path,competing)))
        wins=[value for value in outcomes if isinstance(value,dict)]; losses=[value for value in outcomes if isinstance(value,str)]
        assert len(wins)==len(losses)==1
        assert losses[0] in ('stale_reconciliation_approval','reconciliation_pending_history_changed')
        result=wins[0]
        if read_private(path)['digest']!=result['plan_sha256']: path=competing; plan=result
        loser=competing if path!=competing else root/(course+'-review.json')
        refuse(lambda:authority.reconcile_apply(loser),contract,{'stale_reconciliation_approval'})
        completed.update(('concurrent-reviewed-recovery-one-winner','stale-competing-plan-refused'))
    else:
        interrupted_call(authority,contract,path,'reconciliation_prepared'); assert_accounting(authority,pending=1)
        completed.add('reconciliation-prepared-kept-both-reservations')
        pending_admission_refused(authority,backend,contract,course); completed.add('both-pending-endpoints-admission-refused')
        if partial_second:
            write=backend.write_barrier; failed=[]
            def prewrite(worker,target,record):
                if target==course and worker=='worker-two' and not failed:
                    failed.append(True); raise RuntimeError('synthetic second endpoint pre-write interruption')
                return write(worker,target,record)
            with patch.object(backend,'write_barrier',side_effect=prewrite):
                refuse(lambda:authority.reconcile_resume(path),contract,{'ownership_contract_refused_or_incomplete'})
            first=backend.processes.barrier_path('worker-one',course).read_bytes()
            assert backend._history('worker-one',course) is not None
            assert not os.path.lexists(backend.processes.barrier_path('worker-two',course))
            completed.add('second-endpoint-prewrite-failure-preserved-first-publication')
        interrupted_call(authority,contract,path,'reconciliation_fenced'); assert_accounting(authority,pending=1)
        completed.add('reconciliation-fenced-kept-both-reservations')
        if partial_second:
            assert backend.processes.barrier_path('worker-one',course).read_bytes()==first
            completed.add('second-endpoint-prewrite-failure-exact-resume')
        current=read_private(path)['old_epoch']
        for worker in ('worker-one','worker-two'):
            assert backend._history(worker,course)['denied_through_epoch']==current
            assert backend.processes.running_count(worker,course)==0
            backend.processes.launch(worker,course,backend.home(course,worker),epoch=current,expect_refusal=True)
        completed.update(('dual-persistent-barriers-deny-current-old-epoch','fresh-stale-clients-on-both-endpoints-refused'))
        stale_proof_controls(authority,backend,contract,course,path,completed)
        interrupted_call(authority,contract,path,'reconciliation_committed'); assert_accounting(authority,pending=1)
        pending_admission_refused(authority,backend,contract,course)
        completed.add('reconciliation-committed-kept-both-reservations')
        result=authority.reconcile_resume(path)
    assert result['status']=='COMPLETE' and result['epoch']==read_private(path)['old_epoch']+1
    assert result['services_started'] is False
    assert all(backend.processes.running_count(worker,course)==0 for worker in ('worker-one','worker-two'))
    completed.add('completed-recovery-never-started-services')
    before=authority.inventory(); assert authority.reconcile_resume(path)==result; assert authority.inventory()==before
    completed.add('exact-recovery-retry-idempotent')
    assert backend.dual_lock_verified and backend.reconciliation_inspections>0
    completed.add('dual-endpoint-locks-held-through-proof-cas')
    assert_accounting(authority,pending=0)
    return path,result


@contextmanager
def paused_controller(backend,method,course):
    """Park a real old controller just before it acquires endpoint exclusions."""
    entered=threading.Event(); release=threading.Event(); original=getattr(backend,method)
    @contextmanager
    def pause(request,*,authorize):
        if request['course_id']==course and threading.current_thread().name.startswith('stale-controller'):
            entered.set()
            if not release.wait(30): raise RuntimeError('controlled stale controller pause expired')
        with original(request,authorize=authorize) as context: yield context
    with patch.object(backend,method,side_effect=pause):
        try: yield entered,release
        finally: release.set()


def controller_result(operation,contract):
    try: return operation()
    except contract.OwnershipError as error: return error.code


def assert_selected(authority,backend,contract,course,worker,expected,epoch,completed):
    child=backend.start(authority,course,worker)
    record=backend.processes.services[backend.processes.identity(worker,course)]
    assert backend.processes.live(worker,course)==child.pid and (backend.home(course,worker)/'saved.txt').read_bytes()==expected[worker]
    with authority.admission(course,worker) as grant: assert grant['epoch']==epoch
    other='worker-two' if worker=='worker-one' else 'worker-one'
    refuse(lambda:backend.start(authority,course,other),contract,{'current_owner_required'})
    completed.update(('selected-future-epoch-real-process-and-retained-bytes','nonselected-endpoint-admission-refused'))
    return child,record[1]


def prove_stale_controller(future,release,backend,course,child,nonce,expected,completed,control):
    frozen=barriers(backend,course); release.set(); result=future.result(timeout=30)
    assert result in ('transfer_pending_history_changed','reconciliation_pending_history_changed')
    observed=backend.processes.services[backend.processes.identity('worker-one' if control.startswith('paused-ordinary') else 'worker-two',course)]
    assert observed[0].pid==child.pid and observed[1]==nonce and child.poll() is None
    assert barriers(backend,course)==frozen; endpoints_preserved(backend,course,expected)
    completed.add(control)


def missing_link(authority,contract,transaction_id):
    before=authority.inventory(); saved=row(authority,transaction_id)
    connection=authority._raw_connection()
    try:
        connection.execute('BEGIN IMMEDIATE'); connection.execute('DELETE FROM transfers WHERE transaction_id=?',(transaction_id,))
        connection.execute('COMMIT')
    finally: connection.close()
    try:
        refuse(authority.inventory,contract,{'authority_history_missing_or_changed','authority_reconciliation_history_invalid'})
    finally:
        connection=authority._raw_connection()
        try:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('INSERT INTO transfers(transaction_id,course_id,plan,status,proof,receipt) VALUES(?,?,?,?,?,?)',
                tuple(saved[key] for key in ('transaction_id','course_id','plan','status','proof','receipt')))
            connection.execute('COMMIT')
        finally: connection.close()
    assert authority.inventory()==before and row(authority,transaction_id)==saved


def publish_pass(root,report,cleanup_verified):
    expected={'waypoint_revision','fingerprints','matrix','nested','normal_followup','authority_sha256','peer_work_sha256','checks'}
    if cleanup_verified is not True or not isinstance(report,dict) or set(report)!=expected:
        raise ValueError('pending reconciliation PASS requires bounded evidence and exact cleanup')
    if (not isinstance(report['waypoint_revision'],str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',report['waypoint_revision'])
            or not isinstance(report['fingerprints'],dict) or set(report['fingerprints'])!=set(FINGERPRINT_FILES)
            or any(not isinstance(value,str) or not HASH.fullmatch(value) for value in report['fingerprints'].values())
            or any(not isinstance(report[key],str) or not HASH.fullmatch(report[key]) for key in ('authority_sha256','peer_work_sha256'))
            or not isinstance(report['checks'],list) or any(not isinstance(value,str) for value in report['checks'])
            or set(report['checks'])!=CHECKS or len(report['checks'])!=len(CHECKS)):
        raise ValueError('pending reconciliation evidence fingerprints or controls invalid')
    matrix_keys={'pending_stage','selected_endpoint','old_epoch','new_epoch','original_record_sha256','plan_sha256',
                 'source_tree_sha256','receiver_tree_sha256','source_snapshot_sha256','receiver_snapshot_sha256'}
    nested_keys={'pending_stage','selected_endpoint','old_epoch','new_epoch','parent_sha256','plan_sha256',
                 'source_tree_sha256','receiver_tree_sha256'}
    for key,count,fields in (('matrix',6,matrix_keys),('nested',3,nested_keys)):
        values=report[key]
        if not isinstance(values,list) or len(values)!=count: raise ValueError('reconciliation case matrix incomplete')
        for value in values:
            if (not isinstance(value,dict) or set(value)!=fields or value['pending_stage'] not in ('prepared','fenced','committed')
                    or value['selected_endpoint'] not in ('source','receiver') or type(value['old_epoch']) is not int
                    or type(value['new_epoch']) is not int or value['old_epoch']!=(2 if value['pending_stage']=='committed' else 1)
                    or value['new_epoch']!=value['old_epoch']+1
                    or any(not isinstance(value[field],str) or not HASH.fullmatch(value[field]) for field in fields-{'pending_stage','selected_endpoint','old_epoch','new_epoch'})
                    or value['source_tree_sha256']==value['receiver_tree_sha256']
                    or key=='matrix' and value['source_snapshot_sha256']==value['receiver_snapshot_sha256']
                    or key=='nested' and value['selected_endpoint']!='receiver'):
                raise ValueError('reconciliation case evidence invalid')
    if {(value['pending_stage'],value['selected_endpoint']) for value in report['matrix']}!=MATRIX:
        raise ValueError('original reconciliation matrix incomplete')
    if {value['pending_stage'] for value in report['nested']}!={'prepared','fenced','committed'}:
        raise ValueError('nested reconciliation matrix incomplete')
    normal=report['normal_followup']
    normal_hashes={'barrier_identity_sha256','gap_barrier_sha256','source_tree_sha256','receiver_tree_sha256',
                   'recovery_plan_sha256','ordinary_plan_sha256','gap_plan_sha256'}
    if (not isinstance(normal,dict) or set(normal)!=normal_hashes|{'recovery_epoch','ordinary_epoch'}
            or type(normal['recovery_epoch']) is not int or normal['recovery_epoch']!=2
            or type(normal['ordinary_epoch']) is not int or normal['ordinary_epoch']!=3
            or any(not isinstance(normal[key],str) or not HASH.fullmatch(normal[key]) for key in normal_hashes)
            or normal['source_tree_sha256']!=normal['receiver_tree_sha256']):
        raise ValueError('ordinary followup or failclosed gap evidence invalid')
    all_plans=[value['plan_sha256'] for value in report['matrix']+report['nested']]+[normal[key] for key in
        ('recovery_plan_sha256','ordinary_plan_sha256','gap_plan_sha256')]
    if len(set(all_plans))!=len(all_plans): raise ValueError('distinct reviewed decisions required')
    data=(json.dumps(dict(report,version=VERSION,status='PASS',scope='standalone-disposable-logical-worker-pending-reconciliation',
        source_clean=True,fixture_only=True,logical_workers=2,real_worker_hosts=0,runtime_enforcement=False,
        production_activation_adapter=False,provider_adapter=False,authenticated_remote_transport=False,actual_two_host_isolation=False,
        encrypted_backup_verified=False,offhost_backup_verified=False,measured_profile_enforcement=False,
        fixture_process_restart_only=True,snapshot_scope='opaque-local-fixture-marker',successful_recovery_cases=10,
        intentionally_unresolved_backend_gap_probes=1,pending_transfers_after_probe=1,backend_write_gap_recovery=False,
        apply_started_services=False,concurrent_winners=1,cleanup_verified=True),sort_keys=True,indent=2,allow_nan=False)+'\n').encode()
    if len(data)>64*1024: raise ValueError('pending reconciliation output budget exceeded')
    root=Path(root)
    if root.is_symlink() or not root.is_dir() or root.stat().st_mode & 0o077:
        raise ValueError('pending reconciliation output directory must be private')
    fd,scratch=tempfile.mkstemp(prefix='.pending-reconciliation-evidence-',dir=root)
    try:
        with os.fdopen(fd,'wb') as output:
            os.fchmod(output.fileno(),0o600); output.write(data); output.flush(); os.fsync(output.fileno())
        os.link(scratch,root/'PENDING-RECONCILIATION-PASS.json')
    finally: os.unlink(scratch)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    if not __debug__: parser.error('pending reconciliation rehearsal requires Python assertions')
    parser.add_argument('--output',type=Path,required=True); args=parser.parse_args()
    import ownership_contract as contract
    source=_source(ROOT.parent)
    if source['dirty']: parser.error('accepted reconciliation evidence requires clean source')
    frozen=fingerprints()
    args.output.mkdir(mode=0o700,parents=True,exist_ok=False); root=args.output.resolve()
    fixture=root/'private-fixture'; fixture.mkdir(mode=0o700); backend=None; completed=set(); matrix=[]; nested=[]
    try:
        backend=ReconciliationBackend(fixture/'backend')
        for worker,provider in (('worker-one','logical-provider-one'),('worker-two','logical-provider-two')):
            backend.add_worker(worker,provider,memory=20480,cpu=20)
        authority=contract.Authority.create(fixture/'authority',backend); backend.processes.authority_id=authority.authority_id
        for worker in ('worker-one','worker-two'): authority.register_worker(worker)
        courses=[stage+'-'+selected for stage,selected in sorted(MATRIX)]+['nested-'+stage for stage in ('prepared','fenced','committed')]+['normal-followup']
        for course in ['peer-course',*courses]:
            backend.add_course(course,'worker-one'); authority.register_course(course,'worker-one')
            if course!='peer-course': backend.add_course(course,'worker-two',copy_from='worker-one')
        assert_accounting(authority,pending=0); completed.add('two-fixed-logical-workers-one-private-authority')
        peer=backend.start(authority,'peer-course','worker-one')
        peer_nonce=backend.processes.services[backend.processes.identity('worker-one','peer-course')][1]
        peer_bytes=(backend.home('peer-course','worker-one')/'saved.txt').read_bytes()
        active=[]; originals={}; marker_files={}
        for course in courses:
            if course.startswith('nested-'): stage=course.removeprefix('nested-'); selected='receiver'; is_nested=True
            elif course=='normal-followup': stage='fenced'; selected='source'; is_nested=False
            else: stage,selected=course.rsplit('-',1); is_nested=False
            chosen='worker-one' if selected=='source' else 'worker-two'
            backend.start(authority,course,'worker-one')
            original_path=fixture/(course+'-original.json'); original=authority.preview(course,'worker-two',original_path)
            original_stage='prepared' if is_nested else stage
            with patch.object(contract,'fault_hook',side_effect=interrupt_at(original_stage)):
                refuse(lambda:authority.apply(original_path),contract,{'ownership_contract_refused_or_incomplete'})
            originals[original['transaction_id']]=row(authority,original['transaction_id'])
            assert_accounting(authority,pending=1)
            if original_stage=='prepared':
                rejected=fixture/(course+'-live-preview.json')
                refuse(lambda:authority.reconcile_preview(course,chosen,rejected),contract,{'reconciliation_endpoint_must_be_stopped'})
                assert not rejected.exists(); completed.add('live-endpoint-preview-refused')
            for worker in ('worker-one','worker-two'): backend.processes.stop(worker,course)
            if course!='normal-followup':
                for worker in ('worker-one','worker-two'):
                    append_work(backend.home(course,worker)/'saved.txt',('independent '+worker+' coursework\n').encode())
                    snapshot_witness(backend,worker,course,'current independent '+worker+' fixture marker',marker_files)
            expected={worker:(backend.home(course,worker)/'saved.txt').read_bytes() for worker in ('worker-one','worker-two')}
            for worker in ('worker-one','worker-two'):
                directory=backend.workers[worker]/'courses'/course
                for artifact in directory.glob('*.fixture'): marker_files[artifact]=artifact.read_bytes()
            path,review=preview_negatives(authority,backend,contract,course,'worker-one' if is_nested else chosen,fixture,completed)
            if is_nested:
                interrupted_call(authority,contract,path,'reconciliation_'+stage)
                parent=row(authority,review['transaction_id']); originals[review['transaction_id']]=parent
                with paused_controller(backend,'reconciliation_context',course) as (entered,release),ThreadPoolExecutor(max_workers=1,thread_name_prefix='stale-controller') as pool:
                    old=pool.submit(controller_result,lambda:authority.reconcile_resume(path),contract)
                    assert entered.wait(5)
                    for worker in ('worker-one','worker-two'):
                        append_work(backend.home(course,worker)/'saved.txt',('later independent '+worker+' work\n').encode())
                        snapshot_witness(backend,worker,course,'later independent '+worker+' fixture marker',marker_files)
                    expected={worker:(backend.home(course,worker)/'saved.txt').read_bytes() for worker in ('worker-one','worker-two')}
                    fresh_path=fixture/(course+'-fresh.json'); fresh=authority.reconcile_preview(course,chosen,fresh_path)
                    fresh_path,result=recover(authority,backend,contract,course,chosen,fresh_path,fresh,fixture,completed)
                    child,nonce=assert_selected(authority,backend,contract,course,chosen,expected,result['epoch'],completed)
                    prove_stale_controller(old,release,backend,course,child,nonce,expected,completed,
                        'paused-typed-controller-preserved-new-selected-pid-and-barriers')
                assert row(authority,review['transaction_id'])==parent
                nested.append({'pending_stage':stage,'selected_endpoint':'receiver','old_epoch':read_private(fresh_path)['old_epoch'],
                    'new_epoch':result['epoch'],'parent_sha256':digest(parent),'plan_sha256':result['plan_sha256'],
                    'source_tree_sha256':retained_digest(backend.home(course,'worker-one')),
                    'receiver_tree_sha256':retained_digest(backend.home(course,'worker-two'))})
                active.append((course,chosen,child,nonce,expected))
            elif course=='normal-followup':
                with paused_controller(backend,'fence_context',course) as (entered,release),ThreadPoolExecutor(max_workers=1,thread_name_prefix='stale-controller') as pool:
                    old=pool.submit(controller_result,lambda:authority.resume(original_path),contract)
                    assert entered.wait(5)
                    path,result=recover(authority,backend,contract,course,chosen,path,review,fixture,completed)
                    child,nonce=assert_selected(authority,backend,contract,course,chosen,expected,result['epoch'],completed)
                    prove_stale_controller(old,release,backend,course,child,nonce,expected,completed,
                        'paused-ordinary-controller-preserved-new-source-pid-and-barriers')
                prior_id=backend._history('worker-one',course)['barrier_id']
                ordinary_path=fixture/'followup-ordinary.json'; authority.preview(course,'worker-two',ordinary_path)
                ordinary_result=authority.apply(ordinary_path)
                assert ordinary_result['epoch']==result['epoch']+1 and child.poll() is not None
                assert backend._history('worker-one',course)['barrier_id']==prior_id
                next_child,_=assert_selected(authority,backend,contract,course,'worker-two',expected,ordinary_result['epoch'],completed)
                completed.add('post-reconciliation-ordinary-transfer-kept-barrier-identity')
                refuse(lambda:authority.reconcile_resume(path),contract,{'stale_completed_reconciliation_decision'})
                completed.add('old-recovery-replay-after-later-epoch-refused')
                gap_path=fixture/'owned-write-gap.json'; gap_plan=authority.preview(course,'worker-one',gap_path)
                remember=backend._remember
                def index_failure(worker,target,record):
                    if target==course and worker=='worker-two': raise RuntimeError('synthetic barrier/index publication gap')
                    return remember(worker,target,record)
                with patch.object(backend,'_remember',side_effect=index_failure):
                    refuse(lambda:authority.apply(gap_path),contract,{'ownership_contract_refused_or_incomplete'})
                assert next_child.poll() is not None
                gap_barrier=backend.processes.barrier_path('worker-two',course).read_bytes()
                refuse(lambda:authority.resume(gap_path),contract,{'ownership_contract_refused_or_incomplete'})
                refuse(lambda:authority.reconcile_preview(course,'worker-one',fixture/'unsupported-gap-review.json'),contract,
                       {'ownership_contract_refused_or_incomplete'})
                assert backend.processes.barrier_path('worker-two',course).read_bytes()==gap_barrier
                pending_admission_refused(authority,backend,contract,course)
                endpoints_preserved(backend,course,expected); assert_accounting(authority,pending=1)
                completed.add('backend-barrier-index-write-gap-stays-closed')
                normal={'recovery_epoch':result['epoch'],'ordinary_epoch':ordinary_result['epoch'],
                    'recovery_plan_sha256':result['plan_sha256'],'ordinary_plan_sha256':ordinary_result['plan_sha256'],
                    'gap_plan_sha256':gap_plan['plan_sha256'],
                    'barrier_identity_sha256':digest(prior_id),'gap_barrier_sha256':sha256_file(backend.processes.barrier_path('worker-two',course)),
                    'source_tree_sha256':retained_digest(backend.home(course,'worker-one')),
                    'receiver_tree_sha256':retained_digest(backend.home(course,'worker-two'))}
            else:
                path,result=recover(authority,backend,contract,course,chosen,path,review,fixture,completed,
                    partial_second=course=='prepared-source',race=course=='prepared-receiver')
                child,nonce=assert_selected(authority,backend,contract,course,chosen,expected,result['epoch'],completed)
                evidence={worker:backend.inspect_course(course,worker) for worker in ('worker-one','worker-two')}
                matrix.append({'pending_stage':stage,'selected_endpoint':selected,'old_epoch':read_private(path)['old_epoch'],
                    'new_epoch':result['epoch'],'original_record_sha256':digest(originals[original['transaction_id']]),
                    'plan_sha256':result['plan_sha256'],'source_tree_sha256':evidence['worker-one']['retained_tree_sha256'],
                    'receiver_tree_sha256':evidence['worker-two']['retained_tree_sha256'],
                    'source_snapshot_sha256':evidence['worker-one']['snapshot_sha256'],
                    'receiver_snapshot_sha256':evidence['worker-two']['snapshot_sha256']})
                active.append((course,chosen,child,nonce,expected))
            endpoints_preserved(backend,course,expected)
            peer_live(backend,peer.pid,peer_nonce,peer_bytes)
            for transaction_id,saved_row in originals.items(): assert row(authority,transaction_id)==saved_row
            for artifact,raw in marker_files.items(): assert artifact.read_bytes()==raw
        assert {(value['pending_stage'],value['selected_endpoint']) for value in matrix}==MATRIX
        assert {value['pending_stage'] for value in nested}=={'prepared','fenced','committed'}
        completed.update(('original-three-stages-both-endpoint-selections','nested-three-recovery-stages-fresh-reviewed-selection',
            'original-and-linked-parent-rows-immutable','divergent-retained-bytes-preserved-on-both-endpoints',
            'independent-opaque-snapshot-history-retained'))
        for course,worker,child,nonce,expected in active:
            record=backend.processes.services[backend.processes.identity(worker,course)]
            assert record[0].pid==child.pid and record[1]==nonce and backend.processes.live(worker,course)==child.pid
            endpoints_preserved(backend,course,expected)
        peer_live(backend,peer.pid,peer_nonce,peer_bytes); completed.add('peer-pid-nonce-and-bytes-preserved-through-all-recoveries')
        # Missing link is a deliberate disposable SQL corruption control; restore
        # its exact original value rather than recreate any authority/history.
        linked=next(key for key,value in originals.items() if json.loads(value['plan'])['version']==contract.RECONCILIATION_VERSION)
        missing_link(authority,contract,linked); completed.add('missing-linked-history-refused')
        assert _source(ROOT.parent)==source and fingerprints()==frozen
        report={'waypoint_revision':source['revision'],'fingerprints':frozen,'matrix':matrix,'nested':nested,'normal_followup':normal,
            'authority_sha256':digest(authority.authority_id),'peer_work_sha256':sha256_file(backend.home('peer-course','worker-one')/'saved.txt'),'checks':sorted(completed)}
    finally: cleanup_verified=cleanup_fixture(backend,fixture)
    publish_pass(root,report,cleanup_verified)
    print('PASS: ten reviewed local recovery cases; two logical workers, no production/provider/remote-host acceptance; one intentional backend write-gap probe remains closed; owned cleanup verified.',flush=True)


if __name__=='__main__':main()
