"""Unit evidence for standalone contract only; FakeBackend is no provider proof."""
from contextlib import contextmanager, ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sqlite3
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
    def fence_context(self,request):
        cid=request['course_id'];source=request['source_worker_id'];receiver=request['receiver_worker_id']
        with self.locks_for([(cid,source),(cid,receiver)]):
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
