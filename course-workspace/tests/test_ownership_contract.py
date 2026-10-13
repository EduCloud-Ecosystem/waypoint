"""Unit evidence for standalone contract only; FakeBackend is no provider proof."""
from contextlib import contextmanager, ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import types
import threading
import uuid

import inspect
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import ownership_contract as oc


class Raises:
    def __init__(self,kind,match=None):self.kind=kind;self.match=match;self.value=None
    def __enter__(self):return self
    def __exit__(self,kind,value,traceback):
        if kind is None:raise AssertionError('expected refusal')
        if not issubclass(kind,self.kind):return False
        if self.match is not None:assert re.search(self.match,str(value))
        self.value=value;return True


class Support:
    def fixture(self,fn):return fn
    def raises(self,kind,match=None):return Raises(kind,match)
    @staticmethod
    def parametrize(fields,values):
        def decorate(fn):fn.parameters=(fields.split(','),values);return fn
        return decorate
    @property
    def mark(self):return self

support=Support()



class FakeBackend:
    fixture_only=True
    scope='course'

    def __init__(self):
        self.workers={};self.courses={};self.barriers={};self.proofs={};self.locks={}
        self.unavailable=set();self.proof_change=None;self.callback=None;self.held=set()

    def worker(self,name,budget=8192,cpu=8,provider=None):
        self.workers[name]={'worker_id':name,'provider_id':provider or 'provider-'+name,'boot_id':uuid.uuid4().hex,
            'policy_sha256':'a'*64,'memory_budget_mb':budget,'cpu_budget':cpu,'capacity_memory_mb':budget,'capacity_cpu':cpu}

    def course(self,name,worker,identity=None):
        self.courses[name,worker]={'logical_identity':identity or {'origin':'https://'+name+'.example.test','dns_subtree':name+'.example.test',
            'issuer':'https://auth.example.test/realms/course','client_id':name},
            'profile':{'active_limit':1,'learner_memory_mb':512,'learner_cpu':.25,'memory_mb':1536,'cpu':1.25},
            'snapshot_sha256':'b'*64,'runtime_sha256':'c'*64,'retained_tree_sha256':'d'*64,'admission_floor':1,'running_count':0}

    def inspect_worker(self,worker):
        if self.callback:self.callback()
        if worker in self.unavailable:raise OSError('private backend details')
        return deepcopy(self.workers[worker])

    def inspect_course(self,course,worker):
        if self.callback:self.callback()
        if worker in self.unavailable:raise OSError('private backend details')
        value=deepcopy(self.courses[course,worker])
        value['admission_floor']=max(value['admission_floor'],self.barriers.get((course,worker),0)+1)
        return value

    @contextmanager
    def locks_for(self,keys):
        with ExitStack() as stack:
            for key in sorted(keys):
                stack.enter_context(self.locks.setdefault(key,threading.RLock()));self.held.add(key)
            try:yield
            finally:self.held.difference_update(keys)

    @contextmanager
    def fence_context(self,request,*,authorize=None):
        cid=request['course_id'];source=request['source_worker_id'];receiver=request['receiver_worker_id']
        with self.locks_for([(cid,source),(cid,receiver)]):
            if authorize is not None:authorize()
            for role,worker in [('source',source),('receiver',receiver)]:
                actual=self.inspect_worker(worker)
                if (actual['provider_id']!=request[role+'_provider_id'] or actual['boot_id']!=request[role+'_boot_id']):raise oc.OwnershipError('fixture_boot_changed')
            if self.inspect_course(cid,receiver)['running_count']:raise oc.OwnershipError('receiver_course_must_be_stopped')
            self.barriers[cid,source]=max(request['old_epoch'],self.barriers.get((cid,source),0))
            self.courses[cid,source]['running_count']=0
            key=request['transaction_id']
            if key not in self.proofs:
                self.proofs[key]={'request_sha256':oc.digest(request),'barrier_id':uuid.uuid4().hex,'barrier_sha256':oc.digest({'request':request,'deny':self.barriers[cid,source]}),
                    'scope':'course','denied_through_epoch':self.barriers[cid,source],'observed_running_count':0}
            backend=self
            class Context:
                def inspect(self):
                    assert (cid,source) in backend.held and (cid,receiver) in backend.held
                    value=deepcopy(backend.proofs[key])
                    value['observed_running_count']=backend.inspect_course(cid,source)['running_count']
                    if backend.barriers.get((cid,source),0)<request['old_epoch']:raise oc.OwnershipError('fixture_barrier_missing')
                    if backend.proof_change:backend.proof_change(value)
                    return value
            yield Context()

    @contextmanager
    def reconciliation_context(self,request,*,authorize):
        cid=request['course_id'];endpoints=(request['source_worker_id'],request['receiver_worker_id']);tx=request['transaction_id']
        with self.locks_for([(cid,worker) for worker in endpoints]):
            authorize()
            for role in ('source','receiver'):
                worker=request[role+'_worker_id'];actual=self.inspect_worker(worker)
                if actual['provider_id']!=request[role+'_provider_id'] or actual['boot_id']!=request[role+'_boot_id']:raise oc.OwnershipError('fixture_boot_changed')
                self.barriers[cid,worker]=max(request['old_epoch'],self.barriers.get((cid,worker),0))
                self.courses[cid,worker]['running_count']=0
            if tx not in self.proofs:
                self.proofs[tx]={'request_sha256':oc.digest(request),'scope':'course','endpoints':{
                    worker:{'barrier_id':uuid.uuid4().hex,'barrier_sha256':oc.digest({'request':request,'worker':worker}),
                    'denied_through_epoch':self.barriers[cid,worker],'observed_running_count':0} for worker in endpoints}}
            backend=self
            class Context:
                def inspect(self):
                    assert all((cid,worker) in backend.held for worker in endpoints)
                    value=deepcopy(backend.proofs[tx])
                    for worker in endpoints:
                        if backend.barriers.get((cid,worker),0)<request['old_epoch']:raise oc.OwnershipError('fixture_barrier_missing')
                        value['endpoints'][worker]['observed_running_count']=backend.inspect_course(cid,worker)['running_count']
                    if backend.proof_change:backend.proof_change(value)
                    return value
            yield Context()

    @contextmanager
    def activation_context(self,request):
        cid=request['course_id'];worker=request['worker_id']
        with self.locks_for([(cid,worker)]):
            actual=self.inspect_worker(worker)
            if actual['boot_id']!=request['boot_id'] or actual['provider_id']!=request['provider_id']:raise oc.OwnershipError('fixture_boot_changed')
            if self.inspect_course(cid,worker)['admission_floor']>request['epoch']:raise oc.OwnershipError('fixture_revived_epoch_denied')
            yield


@support.fixture
def setup(tmp_path):
    tmp_path=tmp_path.resolve();tmp_path.chmod(0o700)
    backend=FakeBackend()
    for worker in ('one','two','three'):
        backend.worker(worker)
        backend.course('course',worker)
    authority=oc.Authority.create(tmp_path/'authority',backend)
    for worker in backend.workers:authority.register_worker(worker)
    authority.register_course('course','one')
    return authority,backend,tmp_path


def plan(setup,name='plan.json',receiver='two'):
    a,b,p=setup;a.preview('course',receiver,p/name);return p/name


def refused(code,fn):
    with support.raises(oc.OwnershipError) as caught:fn()
    assert caught.value.code==code
    assert 'private backend details' not in str(caught.value)


def owner(a):
    with a._connection() as conn:return dict(conn.execute("SELECT * FROM courses WHERE course_id='course'").fetchone())


def test_transfer_no_start_fresh_grant_and_exact_retry(setup):
    a,b,p=setup;b.courses['course','one']['running_count']=1
    target=plan(setup);result=a.apply(target)
    assert result['status']=='COMPLETE' and result['epoch']==2
    assert owner(a)['owner']=='two' and all(not v['running_count'] for v in b.courses.values())
    with a.admission('course','two') as grant:
        assert ('course','two') in b.held and grant['epoch']==2
        for key in ('production_admission','services_started','runtime_enforcement','provider_adapter','authenticated_remote_transport'):assert grant[key] is False
    with support.raises(oc.OwnershipError,match='current_owner_required'):
        with a.admission('course','one'):pass
    assert b.barriers['course','one']==1
    assert a.resume(target)==result
    assert a.inventory()['reservations']['one']['count']==0


@support.mark.parametrize('stage',['prepared','fenced','committed','complete'])
def test_durable_faults_resume_exact_without_freeing_uncertain_source(setup,monkeypatch,stage):
    a,b,p=setup;target=plan(setup)
    def interrupt(current,tx):
        if current==stage:raise RuntimeError('lost reply')
    monkeypatch.setattr(oc,'fault_hook',interrupt)
    refused('ownership_contract_refused_or_incomplete',lambda:a.apply(target))
    reopened=oc.Authority.open(a.directory,a.authority_id,b);inventory=reopened.inventory()
    assert inventory['pending_transfers']==(0 if stage=='complete' else 1)
    assert inventory['reservations']['one']['count']==(0 if stage=='complete' else 1)
    assert inventory['reservations']['two']['count']==1
    if stage!='complete':
        for worker in ('one','two'):
            with support.raises(oc.OwnershipError):
                with reopened.admission('course',worker):pass
    monkeypatch.setattr(oc,'fault_hook',lambda *args:None)
    assert reopened.resume(target)['status']=='COMPLETE' and owner(reopened)['epoch']==2


def test_competing_approved_plans_single_owner_no_receiver_overwrite(setup):
    a,b,p=setup;first=plan(setup);second=plan(setup,'other.json','three')
    barrier=threading.Barrier(2);results=[]
    def run(path):
        barrier.wait()
        try:results.append(a.apply(path)['status'])
        except oc.OwnershipError as exc:results.append(exc.code)
    threads=[threading.Thread(target=run,args=(path,)) for path in (first,second)]
    for t in threads:t.start()
    for t in threads:t.join(10);assert not t.is_alive()
    assert results.count('COMPLETE')==1 and owner(a)['epoch']==2
    assert a.inventory()['pending_transfers']==0
    assert sum(v['count'] for v in a.inventory()['reservations'].values())==1


def test_three_worker_repeat_keeps_historical_source_peer_capacity(setup,monkeypatch):
    a,b,p=setup; a.apply(plan(setup));b.course('peer','one');a.register_course('peer','one')
    before=a.inventory()['reservations']['one']
    nextplan=plan(setup,'third.json','three')
    def stop(stage,tx):
        if stage=='committed':raise RuntimeError('interrupt')
    monkeypatch.setattr(oc,'fault_hook',stop)
    refused('ownership_contract_refused_or_incomplete',lambda:a.apply(nextplan))
    assert a.inventory()['reservations']['one']==before
    assert a.inventory()['reservations']['two']['count']==1
    with a.admission('peer','one'):pass
    monkeypatch.setattr(oc,'fault_hook',lambda *args:None);a.resume(nextplan)
    assert a.inventory()['reservations']['one']==before


def test_pending_resume_merges_unrelated_registration(setup,monkeypatch):
    a,b,p=setup;target=plan(setup)
    monkeypatch.setattr(oc,'fault_hook',lambda stage,tx:(_ for _ in ()).throw(RuntimeError()) if stage=='prepared' else None)
    refused('ownership_contract_refused_or_incomplete',lambda:a.apply(target))
    b.course('peer','three');a.register_course('peer','three')
    monkeypatch.setattr(oc,'fault_hook',lambda *args:None);a.resume(target)
    assert a.inventory()['registered_courses']==2
    with a.admission('peer','three'):pass


@support.mark.parametrize('field',['snapshot_sha256','runtime_sha256','retained_tree_sha256'])
def test_changed_actual_evidence_refuses_before_reservation(setup,field):
    a,b,p=setup;target=plan(setup);before=a.inventory();b.courses['course','two'][field]='e'*64
    refused('retained_backup_runtime_or_profile_changed',lambda:a.apply(target));assert a.inventory()==before


@support.mark.parametrize('change,code',[
    ('running','receiver_course_must_be_stopped'),('floor','backend_admission_epoch_unavailable'),
    ('boot','worker_incarnation_policy_or_capacity_changed'),('unreachable','ownership_contract_refused_or_incomplete')])
def test_fresh_receiver_and_unreachable_source_closed(setup,change,code):
    a,b,p=setup;target=plan(setup);before=a.inventory()
    if change=='running':b.courses['course','two']['running_count']=1
    if change=='floor':b.courses['course','two']['admission_floor']=3
    if change=='boot':b.workers['two']['boot_id']='f'*32
    if change=='unreachable':b.unavailable.add('one')
    refused(code,lambda:a.apply(target));assert a.inventory()==before


@support.mark.parametrize('field,value',[('scope','host'),('observed_running_count',1),('request_sha256','f'*64),('denied_through_epoch',False)])
def test_unverified_barrier_leaves_conservative_pending(setup,field,value):
    a,b,p=setup;target=plan(setup);b.proof_change=lambda proof:proof.update({field:value})
    refused('persistent_source_fence_unverified',lambda:a.apply(target))
    assert a.inventory()['pending_transfers']==1
    assert a.inventory()['reservations']['one']['count']==a.inventory()['reservations']['two']['count']==1


def test_barrier_loss_after_commit_does_not_recreate_completed_proof(setup):
    a,b,p=setup;target=plan(setup);a.apply(target)
    b.proofs.clear()
    refused('completed_ownership_state_changed',lambda:a.resume(target))
    assert owner(a)['owner']=='two' and owner(a)['epoch']==2


def test_round_trip_stale_approval_refuses_even_identical_evidence(setup):
    a,b,p=setup;old=plan(setup);a.apply(old);a.apply(plan(setup,'back.json','one'))
    refused('stale_completed_ownership_decision',lambda:a.apply(old));assert owner(a)['epoch']==3
    new=plan(setup,'forward.json','two');a.apply(new);assert owner(a)['epoch']==4


def test_duplicate_provider_and_global_identity_collisions(setup):
    a,b,p=setup;b.worker('duplicate',provider=b.workers['one']['provider_id'])
    refused('duplicate_provider_identity',lambda:a.register_worker('duplicate'))
    b.course('subcourse','two',{'origin':'https://student.course.example.test','dns_subtree':'student.course.example.test','issuer':'https://auth.example.test/realms/new','client_id':'different'})
    refused('global_identity_collision',lambda:a.register_course('subcourse','two'))
    b.course('clientcollision','two',{'origin':'https://unrelated.example.test','dns_subtree':'unrelated.example.test','issuer':'https://auth.example.test/realms/course','client_id':'course'})
    refused('global_identity_collision',lambda:a.register_course('clientcollision','two'))


def test_overcapacity_no_reservation_or_plan(setup):
    a,b,p=setup;b.course('peer','two');a.register_course('peer','two')
    b.workers['two']['memory_budget_mb']=2000
    # New registration boot/policy is immutable; build a separate constrained authority.
    other=oc.Authority.create(p/'small',b)
    for worker in ('one','two'):other.register_worker(worker)
    other.register_course('course','one');other.register_course('peer','two')
    refused('receiver_capacity_unavailable',lambda:other.preview('course','two',p/'too-large.json'))
    assert not (p/'too-large.json').exists() and other.inventory()['pending_transfers']==0


@support.mark.parametrize('field,value',[('active_limit',True),('learner_cpu',float('nan')),('learner_memory_mb',1),('active_limit',1001)])
def test_private_plan_malformed_profile_even_rehashed(setup,field,value):
    a,b,p=setup;target=plan(setup);record=json.loads(target.read_text());record['evidence']['two']['profile'][field]=value
    if isinstance(value,float) and value!=value:
        target.write_text(json.dumps(record));refused('nonfinite_record',lambda:a.apply(target));return
    record['digest']=oc.digest({k:v for k,v in record.items() if k!='digest'});target.write_bytes(oc.encoded(record))
    refused('invalid_contract_profile',lambda:a.apply(target));assert a.inventory()['pending_transfers']==0


def test_authority_missing_replaced_database_and_wrong_pin_refuse(setup):
    a,b,p=setup
    refused('authority_identity_or_database_changed',lambda:oc.Authority.open(a.directory,'f'*32,b))
    a.sentinel.rename(a.directory/'sentinel.saved')
    refused('ownership_contract_refused_or_incomplete',lambda:oc.Authority.open(a.directory,a.authority_id,b))
    (a.directory/'sentinel.saved').rename(a.sentinel)
    shutil.copy2(a.database,p/'replacement.sqlite');os.replace(p/'replacement.sqlite',a.database)
    refused('authority_identity_or_database_changed',lambda:oc.Authority.open(a.directory,a.authority_id,b))


def test_missing_chain_or_tampered_receipt_refuses_without_current_state_rewrite(setup):
    a,b,p=setup;a.apply(plan(setup))
    with a._connection() as conn:
        conn.execute('BEGIN IMMEDIATE');conn.execute("UPDATE transfers SET receipt='{}'")
        a._event(conn,'tamper',None);conn.execute('COMMIT')
    refused('authority_transfer_history_invalid',a.inventory)


def test_no_backend_callback_during_sql_transaction(setup,monkeypatch):
    a,b,p=setup;connections=[];original=a._connection
    @contextmanager
    def track():
        with original() as conn:
            connections.append(conn)
            try:yield conn
            finally:connections.remove(conn)
    monkeypatch.setattr(a,'_connection',track)
    def check():assert all(not conn.in_transaction for conn in connections)
    b.callback=check
    b.course('peer','three');a.register_course('peer','three')
    target=plan(setup);a.apply(target)
    with a.admission('course','two'):pass
    a.resume(target)


def test_sqlite_durability_and_private_no_follow_records(setup):
    a,b,p=setup
    with a._connection() as conn:
        assert conn.execute('PRAGMA journal_mode').fetchone()[0]=='delete'
        assert conn.execute('PRAGMA synchronous').fetchone()[0]==3
        assert conn.execute('PRAGMA busy_timeout').fetchone()[0]==1000
    target=plan(setup);(p/'alias.json').symlink_to(target)
    refused('canonical_contract_path_required',lambda:a.apply(p/'alias.json'))
    target.chmod(0o644);refused('private_contract_state_required',lambda:a.apply(target))


def test_stale_whole_authority_preview_refuses_before_side_effect(setup):
    a,b,p=setup;target=plan(setup);b.course('peer','three');a.register_course('peer','three')
    refused('stale_ownership_approval',lambda:a.apply(target))
    assert a.inventory()['pending_transfers']==0 and not b.barriers


@support.mark.parametrize('change',['missing','profile'])
def test_exact_pending_reservation_schema_refuses_before_fencing(setup,monkeypatch,change):
    a,b,p=setup;target=plan(setup)
    monkeypatch.setattr(oc,'fault_hook',lambda stage,tx:(_ for _ in ()).throw(RuntimeError()) if stage=='prepared' else None)
    refused('ownership_contract_refused_or_incomplete',lambda:a.apply(target))
    with a._connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if change=='missing':conn.execute("DELETE FROM reservations WHERE worker_id='two' AND course_id='course'")
        else:
            different={'active_limit':2,'learner_memory_mb':512,'learner_cpu':.25,'memory_mb':2048,'cpu':1.5}
            conn.execute("UPDATE reservations SET profile=? WHERE worker_id='two'",(oc.encoded(different).decode(),))
        a._event(conn,'tamper',None);conn.execute('COMMIT')
    monkeypatch.setattr(oc,'fault_hook',lambda *args:None)
    refused('authority_reservations_invalid',lambda:a.resume(target));assert not b.barriers


def test_missing_event_history_does_not_bootstrap(setup):
    a,b,p=setup
    with a._connection() as conn:conn.execute('DELETE FROM events')
    refused('authority_history_missing_or_changed',lambda:oc.Authority.open(a.directory,a.authority_id,b))
    refused('ownership_contract_refused_or_incomplete',lambda:oc.Authority.create(a.directory,b))


def test_backend_generic_error_is_fixed_and_redacted(setup):
    a,b,p=setup;target=plan(setup)
    def fail(*args):raise Exception('private backend credential or endpoint')
    b.inspect_course=fail
    refused('ownership_contract_refused_or_incomplete',lambda:a.apply(target))


def test_existing_private_plan_is_not_overwritten(setup):
    a,b,p=setup;target=p/'existing.json';target.write_text('{"private":"keep"}');target.chmod(0o600)
    refused('new_private_contract_record_required',lambda:a.preview('course','two',target))
    assert target.read_text()=='{"private":"keep"}'


def test_host_or_production_backends_refused(setup):
    a,b,p=setup;b.scope='host'
    refused('fixture_course_backend_required',lambda:oc.Authority.open(a.directory,a.authority_id,b))
    b.scope='course';b.fixture_only=False
    refused('fixture_course_backend_required',lambda:oc.Authority.open(a.directory,a.authority_id,b))


def pending(setup,monkeypatch,stage='prepared',name='ordinary.json'):
    a,b,p=setup;target=plan(setup,name)
    def stop(current,tx):
        if current==stage:raise RuntimeError('lost reply')
    monkeypatch.setattr(oc,'fault_hook',stop)
    refused('ownership_contract_refused_or_incomplete',lambda:a.apply(target))
    monkeypatch.setattr(oc,'fault_hook',lambda *args:None)
    with a._connection() as conn:original=dict(conn.execute('SELECT * FROM transfers').fetchone())
    return target,original


@support.mark.parametrize('stage,selected',[('prepared','one'),('prepared','two'),('fenced','one'),('fenced','two'),('committed','one'),('committed','two')])
def test_reviewed_reconciliation_divergent_retained_bytes_both_choices(setup,monkeypatch,stage,selected):
    a,b,p=setup;ordinary,original=pending(setup,monkeypatch,stage)
    b.courses['course','one']['retained_tree_sha256']='e'*64;b.courses['course','two']['snapshot_sha256']='f'*64
    before=deepcopy(b.courses);target=p/'resolve.json';a.reconcile_preview('course',selected,target)
    result=a.reconcile_apply(target);expected=3 if stage=='committed' else 2
    assert result['status']=='COMPLETE' and result['epoch']==expected and owner(a)['owner']==selected
    assert b.courses==before and a.inventory()['pending_transfers']==0
    with a._connection() as conn:assert dict(conn.execute('SELECT * FROM transfers WHERE transaction_id=?',(original['transaction_id'],)).fetchone())==original
    assert a.inventory()['reservations'][selected]['count']==1
    refused('transfer_pending_history_changed',lambda:a.resume(ordinary))
    assert a.reconcile_resume(target)==result
    with a.admission('course',selected):pass
    for worker in ('one','two'):assert b.barriers['course',worker]==expected-1


@support.mark.parametrize('stage',['reconciliation_prepared','reconciliation_fenced','reconciliation_committed','reconciliation_complete'])
def test_reconciliation_faults_both_capacity_and_exact_reopen_resume(setup,monkeypatch,stage):
    a,b,p=setup;ordinary,original=pending(setup,monkeypatch,'committed');b.courses['course','two']['retained_tree_sha256']='e'*64
    target=p/'resolve.json';a.reconcile_preview('course','one',target)
    monkeypatch.setattr(oc,'fault_hook',lambda current,tx:(_ for _ in ()).throw(RuntimeError()) if current==stage else None)
    refused('ownership_contract_refused_or_incomplete',lambda:a.reconcile_apply(target))
    a=oc.Authority.open(a.directory,a.authority_id,b);inventory=a.inventory();assert inventory['pending_transfers']==(0 if stage=='reconciliation_complete' else 1)
    assert inventory['reservations']['one']['count']==1
    assert inventory['reservations']['two']['count']==(0 if stage=='reconciliation_complete' else 1)
    refused('transfer_pending_history_changed',lambda:a.resume(ordinary))
    monkeypatch.setattr(oc,'fault_hook',lambda *args:None);a.reconcile_resume(target);assert owner(a)['epoch']==3


@support.mark.parametrize('change',['tree','snapshot','runtime','identity','profile','boot','floor','running','unreachable'])
def test_reconciliation_stale_fresh_evidence_no_new_pending_sideeffects(setup,monkeypatch,change):
    a,b,p=setup;ordinary,original=pending(setup,monkeypatch);target=p/'resolve.json';a.reconcile_preview('course','one',target)
    before=a.inventory()
    if change=='tree':b.courses['course','two']['retained_tree_sha256']='e'*64
    elif change=='snapshot':b.courses['course','two']['snapshot_sha256']='e'*64
    elif change=='runtime':b.courses['course','two']['runtime_sha256']='e'*64
    elif change=='identity':b.courses['course','two']['logical_identity']['client_id']='other'
    elif change=='profile':b.courses['course','two']['profile']={'active_limit':2,'learner_memory_mb':512,'learner_cpu':.25,'memory_mb':2048,'cpu':1.5}
    elif change=='boot':b.workers['two']['boot_id']='e'*32
    elif change=='floor':b.courses['course','two']['admission_floor']=3
    elif change=='running':b.courses['course','two']['running_count']=1
    elif change=='unreachable':b.unavailable.add('two')
    with support.raises(oc.OwnershipError):a.reconcile_apply(target)
    assert a.inventory()==before and owner(a)['pending']==original['transaction_id']


def test_reconciliation_competing_decisions_one_fresh_epoch(setup,monkeypatch):
    a,b,p=setup;pending(setup,monkeypatch);paths=[]
    for selected in ('one','two'):
        target=p/(selected+'.json');a.reconcile_preview('course',selected,target);paths.append(target)
    barrier=threading.Barrier(2);results=[]
    def run(target):
        barrier.wait()
        try:results.append(a.reconcile_apply(target)['status'])
        except oc.OwnershipError as error:results.append(error.code)
    threads=[threading.Thread(target=run,args=(target,)) for target in paths]
    for thread in threads:thread.start()
    for thread in threads:thread.join(10);assert not thread.is_alive()
    assert results.count('COMPLETE')==1 and owner(a)['epoch']==2 and a.inventory()['pending_transfers']==0


def test_reconciliation_prepared_excludes_old_resume_even_restored_old_evidence(setup,monkeypatch):
    a,b,p=setup;ordinary,original=pending(setup,monkeypatch);target=p/'resolve.json';a.reconcile_preview('course','one',target)
    monkeypatch.setattr(oc,'fault_hook',lambda stage,tx:(_ for _ in ()).throw(RuntimeError()) if stage=='reconciliation_prepared' else None)
    refused('ownership_contract_refused_or_incomplete',lambda:a.reconcile_apply(target))
    refused('transfer_pending_history_changed',lambda:a.resume(ordinary))
    for worker in ('one','two'):
        with support.raises(oc.OwnershipError):
            with a.admission('course',worker):pass
    # A new immutable decision can supersede the pending recovery.
    a.reconcile_preview('course','two',p/'recursive.json')


def test_reconciliation_peer_changes_resume_and_historical_third_host_accounting(setup,monkeypatch):
    a,b,p=setup;a.apply(plan(setup));b.course('peer','one');a.register_course('peer','one')
    ordinary=p/'third.json';a.preview('course','three',ordinary)
    monkeypatch.setattr(oc,'fault_hook',lambda stage,tx:(_ for _ in ()).throw(RuntimeError()) if stage=='committed' else None)
    refused('ownership_contract_refused_or_incomplete',lambda:a.apply(ordinary));monkeypatch.setattr(oc,'fault_hook',lambda *args:None)
    before=a.inventory()['reservations']['one'];target=p/'resolve.json';a.reconcile_preview('course','two',target)
    monkeypatch.setattr(oc,'fault_hook',lambda stage,tx:(_ for _ in ()).throw(RuntimeError()) if stage=='reconciliation_committed' else None)
    refused('ownership_contract_refused_or_incomplete',lambda:a.reconcile_apply(target));assert a.inventory()['reservations']['one']==before
    b.course('extra','one');a.register_course('extra','one');monkeypatch.setattr(oc,'fault_hook',lambda *args:None);a.reconcile_resume(target)
    assert a.inventory()['reservations']['one']['count']==2
    with a.admission('peer','one'):pass


@support.mark.parametrize('change',['proof','original','missing'])
def test_reconciliation_tampered_history_refuses(setup,monkeypatch,change):
    a,b,p=setup;ordinary,original=pending(setup,monkeypatch,'committed');target=p/'resolve.json';a.reconcile_preview('course','one',target);a.reconcile_apply(target)
    with a._connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if change=='proof':conn.execute("UPDATE transfers SET proof='{}' WHERE transaction_id!=?",(original['transaction_id'],))
        elif change=='original':conn.execute("UPDATE transfers SET status='COMPLETE' WHERE transaction_id=?",(original['transaction_id'],))
        else:conn.execute('DELETE FROM transfers WHERE transaction_id=?',(original['transaction_id'],))
        a._event(conn,'tamper',None);conn.execute('COMMIT')
    with support.raises(oc.OwnershipError):a.inventory()


def test_reconciliation_lost_barrier_proof_and_stale_retry_after_next_transfer(setup,monkeypatch):
    a,b,p=setup;pending(setup,monkeypatch);target=p/'resolve.json';a.reconcile_preview('course','one',target);a.reconcile_apply(target)
    recovery=json.loads(target.read_text());saved=b.proofs.pop(recovery['transaction_id'])
    refused('completed_reconciliation_state_changed',lambda:a.reconcile_resume(target))
    b.proofs[recovery['transaction_id']]=saved;a.apply(plan(setup,'next.json','two'))
    refused('stale_completed_reconciliation_decision',lambda:a.reconcile_resume(target))


def test_preexisting_v1_authority_is_opened_without_schema_reset(setup,monkeypatch):
    a,b,p=setup
    source=subprocess.check_output(['git','show','1ebecdbe0c712c295f83de3ac76be5ef580791f0:course-workspace/ownership_contract.py'],cwd=Path(__file__).resolve().parents[2],text=True)
    historic=types.ModuleType('historical_ownership');exec(compile(source,'historical_ownership.py','exec'),historic.__dict__)
    old=historic.Authority.create(p/'existing-v1',b)
    for worker in ('one','two'):old.register_worker(worker)
    old.register_course('course','one');ordinary=p/'legacy-plan.json';old.preview('course','two',ordinary)
    with patch.object(historic,'fault_hook',side_effect=lambda stage,tx:(_ for _ in ()).throw(RuntimeError()) if stage=='prepared' else None):
        with support.raises(historic.OwnershipError):old.apply(ordinary)
    with old._connection() as conn:
        schema=[tuple(row) for row in conn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name')]
    current=oc.Authority.open(old.directory,old.authority_id,b);target=p/'resolve-legacy.json';current.reconcile_preview('course','one',target);current.reconcile_apply(target)
    with current._connection() as conn:assert [tuple(row) for row in conn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name')]==schema
    with support.raises(historic.OwnershipError):historic.Authority.open(old.directory,old.authority_id,b)


def test_paused_old_resume_refuses_before_any_fence_or_new_owner_stop(setup,monkeypatch):
    a,b,p=setup;ordinary,original=pending(setup,monkeypatch);reached=threading.Event();release=threading.Event();failures=[]
    initial=b.fence_context
    @contextmanager
    def pause(request,*,authorize):
        if threading.current_thread().name=='stale-controller':
            reached.set();assert release.wait(10)
        with initial(request,authorize=authorize) as context:yield context
    monkeypatch.setattr(b,'fence_context',pause)
    def old_resume():
        try:a.resume(ordinary)
        except oc.OwnershipError as error:failures.append(error.code)
    thread=threading.Thread(target=old_resume,name='stale-controller');thread.start();assert reached.wait(10)
    target=p/'resolve.json';a.reconcile_preview('course','one',target);a.reconcile_apply(target)
    with a.admission('course','one'):b.courses['course','one']['running_count']=1
    before=deepcopy((b.barriers,b.proofs,b.courses));release.set();thread.join(10);assert not thread.is_alive()
    assert failures==['transfer_pending_history_changed'] and (b.barriers,b.proofs,b.courses)==before


def test_pending_reconciliation_no_backend_callback_inside_sql(setup,monkeypatch):
    a,b,p=setup;pending(setup,monkeypatch);connections=[];original=a._connection
    @contextmanager
    def track():
        with original() as conn:
            connections.append(conn)
            try:yield conn
            finally:connections.remove(conn)
    monkeypatch.setattr(a,'_connection',track)
    b.callback=lambda:all(not conn.in_transaction for conn in connections) or (_ for _ in ()).throw(AssertionError())
    target=p/'resolve.json';a.reconcile_preview('course','one',target);a.reconcile_apply(target);a.reconcile_resume(target)


@support.mark.parametrize('stage,selected',[(stage,selected) for stage in ('reconciliation_prepared','reconciliation_fenced','reconciliation_committed') for selected in ('one','two')])
def test_nested_reviewed_pending_reconciliation_preserves_every_parent_and_data(setup,monkeypatch,stage,selected):
    a,b,p=setup;ordinary,base=pending(setup,monkeypatch);first=p/'first-recovery.json';a.reconcile_preview('course',selected,first)
    monkeypatch.setattr(oc,'fault_hook',lambda current,tx:(_ for _ in ()).throw(RuntimeError()) if current==stage else None)
    refused('ownership_contract_refused_or_incomplete',lambda:a.reconcile_apply(first));monkeypatch.setattr(oc,'fault_hook',lambda *args:None)
    with a._connection() as conn:parents=[dict(row) for row in conn.execute('SELECT * FROM transfers ORDER BY transaction_id')]
    before_epoch=owner(a)['epoch'];b.courses['course','one']['retained_tree_sha256']='e'*64;b.courses['course','two']['snapshot_sha256']='f'*64
    bytes_before=deepcopy(b.courses);other='two' if selected=='one' else 'one';second=p/'second-recovery.json';a.reconcile_preview('course',other,second)
    assert json.loads(second.read_text())['original_transaction_id']==json.loads(first.read_text())['transaction_id']
    a.reconcile_apply(second);assert owner(a)['epoch']==before_epoch+1 and owner(a)['owner']==other and b.courses==bytes_before
    with a._connection() as conn:
        for parent in parents:assert dict(conn.execute('SELECT * FROM transfers WHERE transaction_id=?',(parent['transaction_id'],)).fetchone())==parent
    assert a.inventory()['pending_transfers']==0 and sum(row['count'] for row in a.inventory()['reservations'].values())==1
    refused('transfer_pending_history_changed',lambda:a.resume(ordinary))
    refused('reconciliation_pending_history_changed',lambda:a.reconcile_resume(first))
    with a.admission('course',other):pass


def test_nested_prepared_keeps_prior_committed_reservation_roles_and_capacity(setup,monkeypatch):
    a,b,p=setup;pending(setup,monkeypatch);first=p/'r1.json';a.reconcile_preview('course','two',first)
    monkeypatch.setattr(oc,'fault_hook',lambda stage,tx:(_ for _ in ()).throw(RuntimeError()) if stage=='reconciliation_committed' else None)
    refused('ownership_contract_refused_or_incomplete',lambda:a.reconcile_apply(first));monkeypatch.setattr(oc,'fault_hook',lambda *args:None)
    b.courses['course','two']['retained_tree_sha256']='e'*64;second=p/'r2.json';a.reconcile_preview('course','one',second)
    monkeypatch.setattr(oc,'fault_hook',lambda stage,tx:(_ for _ in ()).throw(RuntimeError()) if stage=='reconciliation_prepared' else None)
    refused('ownership_contract_refused_or_incomplete',lambda:a.reconcile_apply(second))
    with a._connection() as conn:assert dict(conn.execute('SELECT worker_id,status FROM reservations WHERE course_id=?',('course',)))=={'one':'FENCED','two':'OWNER'}
    assert a.inventory()['reservations']['one']['count']==a.inventory()['reservations']['two']['count']==1
    reopened=oc.Authority.open(a.directory,a.authority_id,b);monkeypatch.setattr(oc,'fault_hook',lambda *args:None);reopened.reconcile_resume(second)
    assert owner(reopened)['epoch']==3


def test_stale_nested_recovery_controller_refuses_before_selected_process_effect(setup,monkeypatch):
    a,b,p=setup;pending(setup,monkeypatch);first=p/'r1.json';a.reconcile_preview('course','one',first)
    monkeypatch.setattr(oc,'fault_hook',lambda stage,tx:(_ for _ in ()).throw(RuntimeError()) if stage=='reconciliation_prepared' else None)
    refused('ownership_contract_refused_or_incomplete',lambda:a.reconcile_apply(first));monkeypatch.setattr(oc,'fault_hook',lambda *args:None)
    reached=threading.Event();release=threading.Event();errors=[];initial=b.reconciliation_context
    @contextmanager
    def pause(request,*,authorize):
        if threading.current_thread().name=='old-recovery':reached.set();assert release.wait(10)
        with initial(request,authorize=authorize) as context:yield context
    monkeypatch.setattr(b,'reconciliation_context',pause)
    def old():
        try:a.reconcile_resume(first)
        except oc.OwnershipError as error:errors.append(error.code)
    thread=threading.Thread(target=old,name='old-recovery');thread.start();assert reached.wait(10)
    second=p/'r2.json';a.reconcile_preview('course','one',second);a.reconcile_apply(second)
    with a.admission('course','one'):b.courses['course','one']['running_count']=1
    before=deepcopy((b.barriers,b.proofs,b.courses));release.set();thread.join(10);assert not thread.is_alive()
    assert errors==['reconciliation_pending_history_changed'] and (b.barriers,b.proofs,b.courses)==before


@support.mark.parametrize('change',['orphan','multiple','cycle','complete-parent','wrong-roles'])
def test_nested_graph_malformed_or_role_drift_refuses(setup,monkeypatch,change):
    a,b,p=setup;pending(setup,monkeypatch);first=p/'r1.json';a.reconcile_preview('course','two',first)
    monkeypatch.setattr(oc,'fault_hook',lambda stage,tx:(_ for _ in ()).throw(RuntimeError()) if stage=='reconciliation_committed' else None)
    refused('ownership_contract_refused_or_incomplete',lambda:a.reconcile_apply(first));monkeypatch.setattr(oc,'fault_hook',lambda *args:None)
    second=p/'r2.json';a.reconcile_preview('course','one',second)
    monkeypatch.setattr(oc,'fault_hook',lambda stage,tx:(_ for _ in ()).throw(RuntimeError()) if stage=='reconciliation_prepared' else None)
    refused('ownership_contract_refused_or_incomplete',lambda:a.reconcile_apply(second))
    record=json.loads(second.read_text());first_record=json.loads(first.read_text())
    with a._connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if change=='orphan':record['original_transaction_id']='f'*32
        elif change=='multiple':record['original_transaction_id']=first_record['original_transaction_id']
        elif change=='cycle':record['original_transaction_id']=record['transaction_id']
        elif change=='complete-parent':conn.execute("UPDATE transfers SET status='COMPLETE' WHERE transaction_id=?",(first_record['transaction_id'],))
        else:
            conn.execute("UPDATE reservations SET status='OWNER' WHERE worker_id='one'");conn.execute("UPDATE reservations SET status='FENCED' WHERE worker_id='two'")
        if change in ('orphan','multiple','cycle'):
            record['digest']=oc.digest({k:v for k,v in record.items() if k!='digest'});conn.execute('UPDATE transfers SET plan=? WHERE transaction_id=?',(oc.encoded(record).decode(),record['transaction_id']))
        a._event(conn,'tamper',None);conn.execute('COMMIT')
    with support.raises(oc.OwnershipError):a.inventory()


def test_nested_complete_chain_allows_subsequent_ordinary_transfer_and_blocks_all_old_decisions(setup,monkeypatch):
    a,b,p=setup;ordinary,base=pending(setup,monkeypatch);first=p/'r1.json';a.reconcile_preview('course','two',first)
    monkeypatch.setattr(oc,'fault_hook',lambda stage,tx:(_ for _ in ()).throw(RuntimeError()) if stage=='reconciliation_committed' else None)
    refused('ownership_contract_refused_or_incomplete',lambda:a.reconcile_apply(first));monkeypatch.setattr(oc,'fault_hook',lambda *args:None)
    second=p/'r2.json';a.reconcile_preview('course','one',second);a.reconcile_apply(second)
    assert owner(a)['epoch']==3
    b.courses['course','three']=deepcopy(b.courses['course','one']);b.courses['course','three']['admission_floor']=1
    next_plan=p/'next.json';a.preview('course','three',next_plan);a.apply(next_plan)
    assert owner(a)['epoch']==4 and owner(a)['owner']=='three'
    refused('transfer_pending_history_changed',lambda:a.resume(ordinary))
    refused('reconciliation_pending_history_changed',lambda:a.reconcile_resume(first))
    refused('stale_completed_reconciliation_decision',lambda:a.reconcile_resume(second))
    with a.admission('course','three'):pass


class OwnershipContractTests(unittest.TestCase):
    pass


def make_test(fn,parameters):
    def run(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as patches:
            class MonkeyPatch:
                def setattr(self,obj,name,value):patches.enter_context(patch.object(obj,name,value))
            arguments={'setup':setup(Path(directory)), 'monkeypatch':MonkeyPatch()}
            arguments.update(parameters)
            fn(**{name:arguments[name] for name in inspect.signature(fn).parameters})
    return run


for name,fn in list(globals().items()):
    if name.startswith('test_') and callable(fn):
        fields,values=getattr(fn,'parameters',([], [()]))
        for index,values_for_test in enumerate(values):
            if len(fields)==1:values_for_test=(values_for_test,)
            setattr(OwnershipContractTests,name+'_'+str(index),make_test(fn,dict(zip(fields,values_for_test))))

if __name__=='__main__':unittest.main()
