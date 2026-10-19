"""Reviewed credential lifecycle on real private SQLite; no runtime providers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker_enrollment as e


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve(); self.now=100; self.seq=0; self.authority='a'*32
        self.binding={'runtime_config_sha256':'a'*64,'ca_sha256':'b'*64,'home_root_bindings_sha256':'c'*64,
            'workers':{'one':{'provider_id':'provider-one','boot_id':'1'*32,'evidence_sha256':'d'*64,'courses':['main','peer']},
                       'two':{'provider_id':'provider-two','boot_id':'2'*32,'evidence_sha256':'e'*64,'courses':['main']}}}
        self.store=e.EnrollmentStore.create(self.root/'registry',self.authority,self.binding,lambda:self.now)
    def cert(self,before=0,after=1000):
        self.seq+=1
        return {'fingerprint':format(self.seq,'064x'),'spki_sha256':format(self.seq+10000,'064x'),'not_before':before,'not_after':after}
    def credentials(self,worker='one'):return {slot:self.cert() for slot in sorted(self.store._slots(worker))}
    def apply(self,plan,callback=None):return self.store.apply(plan['plan_id'],plan['plan_sha256'],callback or (lambda:deepcopy(self.binding)))
    def enroll(self,worker='one',credentials=None):
        credentials=credentials or self.credentials(worker);self.apply(self.store.preview('enroll',worker,credentials=credentials));return credentials
    def stage(self,slot='hub:main',cert=None):
        cert=cert or self.cert();self.apply(self.store.preview('stage','one',slot=slot,credentials=cert));return cert
    def activate(self,slot='hub:main',duration=10):
        plan=self.store.preview('activate','one',slot=slot,overlap_seconds=duration);self.apply(plan);return plan
    def refuse(self,code,call):
        with self.assertRaises(e.EnrollmentError) as caught:call()
        self.assertEqual(caught.exception.code,code);return caught.exception
    def state(self):
        with self.store._connection() as conn:
            conn.execute('BEGIN');meta,state=self.store._validate(conn,full=True);conn.execute('COMMIT')
        return meta,state

    def test_first_enrollment_exact_derived_scopes_and_private_plan(self):
        credentials=self.credentials();plan=self.store.preview('enroll','one',credentials=credentials)
        self.assertEqual(self.store.entries(),{});self.assertEqual(plan['revision'],0)
        self.assertEqual(set(plan),{'status','plan_id','plan_sha256','action','worker_id','revision'})
        self.assertEqual(self.store.certificates_for_plan(plan['plan_id'],plan['plan_sha256']),credentials)
        self.assertEqual(self.apply(plan)['revision'],1)
        for slot,cert in credentials.items():
            self.assertEqual(self.store.entries()[cert['fingerprint']],{'role':'guardian' if slot=='guardian' else 'hub','workers':['one'],
                'courses':['main','peer'] if slot=='guardian' else [slot[4:]]})
        self.assertEqual(self.state()[1]['workers']['two'],{'enrolled':False,'revoked':False})

    def test_same_second_lookups_fully_validate_fresh_read_snapshots_without_writes(self):
        self.enroll(); traces=[]; raw=self.store._raw
        def traced():
            conn=raw();conn.set_trace_callback(traces.append);return conn
        with patch.object(self.store,'_raw',side_effect=traced), \
                patch.object(self.store,'_validate',wraps=self.store._validate) as validate:
            for _ in range(4):self.assertEqual(len(self.store.entries()),3)
        self.assertEqual(validate.call_count,4)
        self.assertTrue(all(call.kwargs=={'full':True} for call in validate.call_args_list))
        self.assertEqual(traces.count('BEGIN'),4);self.assertEqual(traces.count('COMMIT'),4)
        self.assertNotIn('BEGIN IMMEDIATE',traces)
        self.assertFalse(any(sql.startswith('UPDATE ') for sql in traces))

    @contextmanager
    def between_read_and_write(self, action):
        original=self.store._connection;called=[]
        @contextmanager
        def connection():
            with original() as conn:
                class View:
                    def execute(inner, sql, *args):
                        result=conn.execute(sql,*args)
                        if sql=='ROLLBACK' and not called:
                            called.append(True);action()
                        return result
                yield View()
        with patch.object(self.store,'_connection',side_effect=connection):yield
        self.assertEqual(called,[True])

    def test_clock_transition_reloads_revocation_before_returning_entries(self):
        old=self.enroll();plan=self.store.preview('revoke','one',target_fingerprint=old['hub:main']['fingerprint'])
        self.now=101
        with self.between_read_and_write(lambda:self.apply(plan)):
            entries=self.store.entries()
        self.assertNotIn(old['hub:main']['fingerprint'],entries)
        self.assertEqual(self.state()[0]['clock_floor'],'101')

    def test_clock_rollback_between_read_and_write_never_revives_expired_certificate(self):
        credentials=self.credentials();credentials['hub:main']['not_after']=101
        self.enroll(credentials=credentials);self.now=101
        with self.between_read_and_write(lambda:setattr(self,'now',100)):
            self.refuse('worker_enrollment_clock_rollback',self.store.entries)
        self.assertEqual(self.state()[0]['clock_floor'],'100')

    def test_concurrent_floor_advance_is_rechecked_after_read_transaction(self):
        self.enroll();self.now=101
        def advance():
            self.now=102;self.store.entries();self.now=101
        with self.between_read_and_write(advance):
            self.refuse('worker_enrollment_clock_rollback',self.store.entries)
        self.assertEqual(self.state()[0]['clock_floor'],'102')

    def test_history_corruption_between_read_and_write_refuses(self):
        self.enroll();self.now=101
        def corrupt():
            with self.store._connection() as conn:conn.execute("UPDATE metadata SET value=? WHERE key='head'",('f'*64,))
        with self.between_read_and_write(corrupt):
            self.refuse('worker_enrollment_state_unavailable',self.store.entries)

    def test_unapproved_worker_slots_and_incomplete_enrollment_publish_nothing(self):
        cases=[]
        missing=self.credentials();missing.pop('guardian');cases.append(('enroll','one',{'credentials':missing}))
        extra=self.credentials();extra['hub:other']=self.cert();cases.append(('enroll','one',{'credentials':extra}))
        cases.extend([('enroll','other',{'credentials':self.credentials()}),('stage','one',{'slot':'hub:other','credentials':self.cert()})])
        for action,worker,args in cases:
            with self.subTest(action=action,worker=worker):
                with self.assertRaises(e.EnrollmentError):self.store.preview(action,worker,**args)
                self.assertEqual(self.state()[0]['plan_count'],'0');self.assertEqual(self.store.entries(),{})

    def test_staging_is_inert_activation_bounded_overlap_and_no_expiry_deletion(self):
        old=self.enroll();new=self.stage();self.assertNotIn(new['fingerprint'],self.store.entries())
        plan=self.store.preview('activate','one',slot='hub:main',overlap_seconds=10)
        self.assertEqual(self.store.certificates_for_plan(plan['plan_id'],plan['plan_sha256']),{'hub:main':new})
        self.apply(plan);self.assertIn(new['fingerprint'],self.store.entries());self.assertIn(old['hub:main']['fingerprint'],self.store.entries())
        self.now=109;self.assertIn(old['hub:main']['fingerprint'],self.store.entries())
        self.now=110;self.assertNotIn(old['hub:main']['fingerprint'],self.store.entries())
        record=self.state()[1]['certificates'][old['hub:main']['fingerprint']]
        self.assertEqual((record['state'],record['overlap_until'],record['overlap_seconds']),('OVERLAP',110,10))
        self.assertEqual(len(self.state()[1]['certificates']),4)

    def test_old_certificate_expiry_caps_overlap_and_history_reopens(self):
        old=self.credentials();old['hub:main']['not_after']=105;self.enroll(credentials=old);new=self.stage();self.activate(duration=300)
        self.now=104;self.assertIn(old['hub:main']['fingerprint'],self.store.entries())
        self.now=105;self.assertNotIn(old['hub:main']['fingerprint'],self.store.entries());self.assertIn(new['fingerprint'],self.store.entries())
        self.now=1000
        reopened=e.EnrollmentStore.open(self.store.directory,self.authority,self.binding,lambda:self.now)
        self.assertEqual(reopened.entries(),{});self.assertEqual(len(self.state()[1]['certificates']),4)

    def test_retire_and_revoke_immediately_deny_and_preserve_tombstones(self):
        old=self.enroll();new=self.stage();self.activate()
        self.apply(self.store.preview('retire','one',target_fingerprint=old['hub:main']['fingerprint']))
        self.assertNotIn(old['hub:main']['fingerprint'],self.store.entries());self.assertIn(new['fingerprint'],self.store.entries())
        self.apply(self.store.preview('revoke','one',target_fingerprint=new['fingerprint']))
        self.assertNotIn(new['fingerprint'],self.store.entries())
        self.assertEqual(self.state()[1]['certificates'][old['hub:main']['fingerprint']]['state'],'RETIRED')
        self.assertEqual(self.state()[1]['certificates'][new['fingerprint']]['state'],'REVOKED')
        replacement=self.stage();self.activate();self.assertIn(replacement['fingerprint'],self.store.entries())

    def test_worker_revoke_is_scoped_permanent_and_preserves_other_worker(self):
        self.enroll();two=self.enroll('two');self.stage();self.apply(self.store.preview('revoke_worker','one'))
        self.assertEqual(set(self.store.entries()),{cert['fingerprint'] for cert in two.values()})
        state=self.state()[1];self.assertTrue(state['workers']['one']['revoked'])
        self.assertTrue(all(c['state']=='REVOKED' for c in state['certificates'].values() if c['worker_id']=='one'))
        self.refuse('worker_enrollment_already_enrolled',lambda:self.store.preview('enroll','one',credentials=self.credentials()))
        self.refuse('worker_enrollment_worker_unavailable',lambda:self.store.preview('stage','one',slot='guardian',credentials=self.cert()))

    def test_leaf_and_spki_cannot_be_reused_across_slots_workers_or_revoked_history(self):
        old=self.enroll()
        self.refuse('worker_enrollment_certificate_reuse',lambda:self.store.preview('stage','one',slot='guardian',credentials=old['hub:main']))
        key_reuse=dict(self.cert(),spki_sha256=old['guardian']['spki_sha256'])
        self.refuse('worker_enrollment_certificate_reuse',lambda:self.store.preview('stage','one',slot='hub:main',credentials=key_reuse))
        two=self.credentials('two');two['guardian']=dict(self.cert(),spki_sha256=old['guardian']['spki_sha256'])
        self.refuse('worker_enrollment_certificate_reuse',lambda:self.store.preview('enroll','two',credentials=two))
        self.apply(self.store.preview('revoke','one',target_fingerprint=old['guardian']['fingerprint']))
        self.now=1000;candidate=dict(self.cert(after=2000),spki_sha256=old['guardian']['spki_sha256'])
        self.refuse('worker_enrollment_certificate_reuse',lambda:self.store.preview('stage','one',slot='guardian',credentials=candidate))

    def test_duplicate_keys_in_initial_enrollment_roll_back_every_slot(self):
        for field in ('fingerprint','spki_sha256'):
            credentials=self.credentials();credentials['hub:peer'][field]=credentials['guardian'][field]
            self.refuse('worker_enrollment_certificate_reuse',lambda:self.store.preview('enroll','one',credentials=credentials))
            self.assertEqual(self.store.entries(),{});self.assertEqual(self.state()[0]['revision'],'0')

    def test_certificate_metadata_exact_types_no_paths_and_current_validity(self):
        for field,value in [('not_before',True),('not_after',100.0),('not_after',float('inf')),('fingerprint','latest'),('spki_sha256','a'*63)]:
            credentials=self.credentials();credentials['guardian'][field]=value
            with self.subTest(field=field):
                with self.assertRaises(e.EnrollmentError):self.store.preview('enroll','one',credentials=credentials)
        credentials=self.credentials();credentials['guardian']['key_path']='/private/canary'
        error=self.refuse('worker_enrollment_refused',lambda:self.store.preview('enroll','one',credentials=credentials));self.assertNotIn('canary',str(error))
        self.enroll()
        for cert in (self.cert(before=101),self.cert(after=100)):
            self.refuse('worker_enrollment_certificate_not_current',lambda cert=cert:self.store.preview('stage','one',slot='guardian',credentials=cert))

    def test_expired_preview_or_staged_activation_refuses_before_verifier(self):
        credentials=self.credentials();credentials['guardian']['not_after']=101
        plan=self.store.preview('enroll','one',credentials=credentials);self.now=101;calls=[]
        self.refuse('worker_enrollment_certificate_not_current',lambda:self.apply(plan,lambda:calls.append(True)))
        self.assertEqual(calls,[]);self.assertEqual(self.store.entries(),{})
        self.enroll();self.stage(cert=self.cert(after=102));plan=self.store.preview('activate','one',slot='hub:main',overlap_seconds=2)
        self.now=102
        self.refuse('worker_enrollment_certificate_not_current',lambda:self.store.certificates_for_plan(plan['plan_id'],plan['plan_sha256']))
        self.refuse('worker_enrollment_certificate_not_current',lambda:self.apply(plan,lambda:calls.append(True)))
        self.assertEqual(calls,[]);self.assertEqual(len(self.store.entries()),3)

    def test_overlap_duration_and_multiple_pending_rotation_are_refused(self):
        self.enroll();self.stage()
        self.refuse('worker_enrollment_rotation_pending',lambda:self.store.preview('stage','one',slot='hub:main',credentials=self.cert()))
        for value in (0,301,True,1.0,'10',None):
            with self.assertRaises(e.EnrollmentError):self.store.preview('activate','one',slot='hub:main',overlap_seconds=value)
        self.activate();self.refuse('worker_enrollment_rotation_pending',lambda:self.store.preview('stage','one',slot='hub:main',credentials=self.cert()))
        self.now=110;self.stage();self.activate();self.assertEqual(len([c for c in self.state()[1]['certificates'].values() if c['state']=='ACTIVE']),3)

    def test_fresh_binding_or_worker_changes_refuse_without_consumption(self):
        plan=self.store.preview('enroll','one',credentials=self.credentials())
        for field in ('runtime_config_sha256','ca_sha256','home_root_bindings_sha256'):
            changed=deepcopy(self.binding);changed[field]='f'*64
            self.refuse('worker_enrollment_binding_changed',lambda changed=changed:self.apply(plan,lambda:changed))
        for field,value in [('boot_id','9'*32),('provider_id','other-provider'),('evidence_sha256','f'*64),('courses',['main'])]:
            changed=deepcopy(self.binding);changed['workers']['one'][field]=value
            self.refuse('worker_enrollment_binding_changed',lambda changed=changed:self.apply(plan,lambda:changed))
        self.assertEqual(self.store.entries(),{});self.assertEqual(self.state()[0]['revision'],'0');self.apply(plan)

    def test_wrong_open_incarnation_binding_and_provider_alias_never_recreate(self):
        self.refuse('worker_enrollment_state_unavailable',lambda:e.EnrollmentStore.open(self.store.directory,'b'*32,self.binding,lambda:self.now))
        changed=deepcopy(self.binding);changed['workers']['one']['boot_id']='8'*32
        self.refuse('worker_enrollment_state_unavailable',lambda:e.EnrollmentStore.open(self.store.directory,self.authority,changed,lambda:self.now))
        changed['workers']['two']['provider_id']=changed['workers']['one']['provider_id']
        self.refuse('worker_enrollment_binding_changed',lambda:e.EnrollmentStore.create(self.root/'other',self.authority,changed,lambda:self.now))
        self.assertFalse((self.root/'other').exists())

    def test_callback_has_no_registry_transaction_and_observes_precommit_state(self):
        plan=self.store.preview('enroll','one',credentials=self.credentials());observations=[]
        def verify():
            with self.store._connection() as conn:
                conn.execute('BEGIN IMMEDIATE');meta,state=self.store._validate(conn,full=True)
                observations.append((meta['revision'],len(state['certificates'])));conn.execute('ROLLBACK')
            return deepcopy(self.binding)
        self.apply(plan,verify);self.assertEqual(observations,[('0',0)])

    def test_invalid_stale_and_consumed_approvals_do_not_call_verifier(self):
        plan=self.store.preview('enroll','one',credentials=self.credentials());calls=[]
        self.refuse('worker_enrollment_stale_plan',lambda:self.store.apply(plan['plan_id'],'f'*64,lambda:calls.append(True)))
        other=self.store.preview('enroll','two',credentials=self.credentials('two'));self.apply(other)
        self.refuse('worker_enrollment_stale_plan',lambda:self.apply(plan,lambda:calls.append(True)))
        self.refuse('worker_enrollment_plan_consumed',lambda:self.apply(other,lambda:calls.append(True)));self.assertEqual(calls,[])

    def test_competing_plans_and_exact_repeat_have_one_transactional_winner(self):
        for same in (False,True):
            with self.subTest(same=same):
                store=e.EnrollmentStore.create(self.root/('same' if same else 'different'),self.authority,self.binding,lambda:self.now)
                plans=[store.preview('enroll',w,credentials=self.credentials(w)) for w in ('one','two')]
                if same:plans[1]=plans[0]
                barrier=threading.Barrier(2)
                def apply(plan):
                    try:return store.apply(plan['plan_id'],plan['plan_sha256'],lambda:(barrier.wait(timeout=3),deepcopy(self.binding))[1])['status']
                    except e.EnrollmentError as error:return error.code
                with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(apply,plans))
                self.assertCountEqual(results,['COMPLETE','worker_enrollment_plan_consumed' if same else 'worker_enrollment_stale_plan'])
                self.assertTrue(store.entries())

    def test_callback_private_exception_is_fixed_and_does_not_consume(self):
        plan=self.store.preview('enroll','one',credentials=self.credentials())
        def failure():raise RuntimeError('private credential canary')
        error=self.refuse('worker_enrollment_state_unavailable',lambda:self.apply(plan,failure));self.assertNotIn('canary',str(error))
        self.assertEqual(self.store.entries(),{});self.apply(plan)

    def test_precommit_fault_rolls_back_state_plan_consumption_and_event(self):
        plan=self.store.preview('enroll','one',credentials=self.credentials());original=self.store._raw
        class Interrupted:
            def __init__(self,conn):self.conn=conn
            def __getattr__(self,name):return getattr(self.conn,name)
            def execute(self,statement,*args):
                if statement.startswith('INSERT INTO events'):raise RuntimeError('interrupted')
                return self.conn.execute(statement,*args)
        with patch.object(self.store,'_raw',side_effect=lambda:Interrupted(original())):
            self.refuse('worker_enrollment_state_unavailable',lambda:self.apply(plan))
        self.assertEqual(self.state()[0]['revision'],'0');self.assertEqual(self.store.entries(),{});self.apply(plan)

    def test_lost_commit_reply_preserves_durable_state_and_denies_duplicate_apply(self):
        plan=self.store.preview('enroll','one',credentials=self.credentials());original=self.store._raw
        class LostReply:
            def __init__(self,conn):self.conn=conn;self.wrote=False
            def __getattr__(self,name):return getattr(self.conn,name)
            def execute(self,statement,*args):
                if statement.startswith('INSERT INTO events'):self.wrote=True
                result=self.conn.execute(statement,*args)
                if statement=='COMMIT' and self.wrote:raise RuntimeError('lost reply')
                return result
        with patch.object(self.store,'_raw',side_effect=lambda:LostReply(original())):
            self.refuse('worker_enrollment_state_unavailable',lambda:self.apply(plan))
        self.assertEqual(len(self.store.entries()),3);self.assertEqual(self.state()[0]['revision'],'1')
        calls=[];self.refuse('worker_enrollment_plan_consumed',lambda:self.apply(plan,lambda:calls.append(True)));self.assertEqual(calls,[])

    def test_clock_floor_is_durable_and_rollback_denies_before_verification(self):
        plan=self.store.preview('enroll','one',credentials=self.credentials());self.now=99;calls=[]
        self.refuse('worker_enrollment_clock_rollback',lambda:self.apply(plan,lambda:calls.append(True)));self.assertEqual(calls,[])
        self.now=100;self.apply(plan);self.now=200;self.store.entries();self.now=199
        self.refuse('worker_enrollment_clock_rollback',self.store.entries)
        self.refuse('worker_enrollment_clock_rollback',lambda:e.EnrollmentStore.open(self.store.directory,self.authority,self.binding,lambda:self.now))
        self.now=200;self.assertEqual(len(self.store.entries()),3)

    def test_missing_replaced_or_aliased_state_never_recreates(self):
        for kind in ('database','sentinel','replacement','hardlink','public'):
            with self.subTest(kind=kind):
                store=e.EnrollmentStore.create(self.root/kind,self.authority,self.binding,lambda:self.now)
                if kind=='replacement':
                    copy=self.root/'copy.sqlite';shutil.copyfile(store.database,copy);copy.chmod(0o600);os.replace(copy,store.database)
                elif kind=='hardlink':os.link(store.sentinel,self.root/'alias')
                elif kind=='public':store.sentinel.chmod(0o644)
                else:(store.database if kind=='database' else store.sentinel).unlink()
                self.refuse('worker_enrollment_state_unavailable',store.entries)
                self.refuse('worker_enrollment_state_unavailable',lambda:e.EnrollmentStore.open(store.directory,self.authority,self.binding,lambda:self.now))
                if kind=='database':self.assertFalse(store.database.exists())
        link=self.root/'link';link.symlink_to(self.store.directory)
        self.refuse('worker_enrollment_state_unavailable',lambda:e.EnrollmentStore.open(link,self.authority,self.binding,lambda:self.now))

    def test_old_event_corruption_and_missing_history_deny_current_authorization(self):
        self.enroll();self.stage()
        with self.store._connection() as conn:
            original=conn.execute('SELECT record FROM events WHERE sequence=1').fetchone()['record']
            value=json.loads(original);value['applied_at']-=1;conn.execute('UPDATE events SET record=? WHERE sequence=1',(json.dumps(value),))
        with self.assertRaises(e.EnrollmentError):self.store.entries()
        with self.store._connection() as conn:conn.execute('UPDATE events SET record=? WHERE sequence=1',(original,))
        self.assertEqual(len(self.store.entries()),3)
        with self.store._connection() as conn:conn.execute('DELETE FROM events WHERE sequence=1')
        self.refuse('worker_enrollment_state_unavailable',self.store.entries)

    def test_tampered_plan_denies_before_binding_verifier(self):
        plan=self.store.preview('enroll','one',credentials=self.credentials())
        with self.store._connection() as conn:
            value=json.loads(conn.execute('SELECT record FROM plans WHERE plan_id=?',(plan['plan_id'],)).fetchone()['record'])
            value['worker_id']='two';conn.execute('UPDATE plans SET record=? WHERE plan_id=?',(json.dumps(value),plan['plan_id']))
        calls=[];self.refuse('worker_enrollment_state_unavailable',lambda:self.apply(plan,lambda:calls.append(True)));self.assertEqual(calls,[])

    def test_scope_of_retire_revoke_and_unknown_actions_never_changes_other_worker(self):
        one=self.enroll();two=self.enroll('two');before=self.store.entries()
        self.refuse('worker_enrollment_target_unavailable',lambda:self.store.preview('revoke','one',target_fingerprint=two['guardian']['fingerprint']))
        self.refuse('worker_enrollment_target_unavailable',lambda:self.store.preview('retire','one',target_fingerprint=one['guardian']['fingerprint']))
        with self.assertRaises(e.EnrollmentError):self.store.preview('provision','one')
        self.assertEqual(self.store.entries(),before)

    def test_sqlite_durability_and_readonly_proof_copy(self):
        with self.store._connection() as conn:
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0],'delete');self.assertEqual(conn.execute('PRAGMA synchronous').fetchone()[0],3)
            self.assertEqual(conn.execute('PRAGMA busy_timeout').fetchone()[0],1000)
        plan=self.store.preview('enroll','one',credentials=self.credentials());before=self.state()
        value=self.store.plan(plan['plan_id'],plan['plan_sha256']);value['worker_id']='two'
        self.store.certificates_for_plan(plan['plan_id'],plan['plan_sha256']);self.assertEqual(self.state(),before)
        self.assertEqual(self.store.plan(plan['plan_id'],plan['plan_sha256'])['worker_id'],'one')

    def test_hard_bounds_reject_without_partial_state_or_plan(self):
        credentials=self.credentials()
        with patch.object(e,'MAX_CERTIFICATES',2):self.refuse('worker_enrollment_quota_exceeded',lambda:self.store.preview('enroll','one',credentials=credentials))
        self.assertEqual(self.state()[0]['plan_count'],'0')
        plan=self.store.preview('enroll','one',credentials=credentials)
        with patch.object(e,'MAX_ROWS',1):self.refuse('worker_enrollment_quota_exceeded',lambda:self.store.preview('enroll','two',credentials=self.credentials('two')))
        self.apply(plan);self.assertEqual(len(self.store.entries()),3)

    def test_wrong_state_singleton_never_reports_complete_or_consumes_approval(self):
        plan=self.store.preview('enroll','one',credentials=self.credentials())
        with self.store._connection() as conn:
            conn.execute('PRAGMA ignore_check_constraints=ON');conn.execute('UPDATE state SET singleton=2')
        calls=[]
        self.refuse('worker_enrollment_state_unavailable',lambda:self.apply(plan,lambda:calls.append(True)))
        self.assertEqual(calls,[])
        with self.store._connection() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM events').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT consumed FROM plans').fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()[0],'0')

    def test_zero_state_update_rolls_back_instead_of_publishing_complete(self):
        plan=self.store.preview('enroll','one',credentials=self.credentials());original=self.store._raw
        class LostState:
            def __init__(self,conn):self.conn=conn
            def __getattr__(self,name):return getattr(self.conn,name)
            def execute(self,statement,*args):
                if statement.startswith('UPDATE state'):return type('Cursor',(),{'rowcount':0})()
                return self.conn.execute(statement,*args)
        with patch.object(self.store,'_raw',side_effect=lambda:LostState(original())):
            self.refuse('worker_enrollment_state_unavailable',lambda:self.apply(plan))
        self.assertEqual(self.state()[0]['revision'],'0');self.assertEqual(self.store.entries(),{})
        self.apply(plan);self.assertEqual(self.state()[0]['revision'],'1')

    def test_boolean_history_sequence_is_refused_even_with_recomputed_digest(self):
        self.enroll()
        with self.store._connection() as conn:
            event=json.loads(conn.execute('SELECT record FROM events').fetchone()['record']);event['sequence']=True
            event['digest']=e.digest({key:value for key,value in event.items() if key!='digest'})
            conn.execute('UPDATE events SET record=?',(e.encoded(event).decode(),))
            conn.execute("UPDATE metadata SET value=? WHERE key='head'",(event['digest'],))
        self.refuse('worker_enrollment_state_unavailable',self.store.entries)


if __name__=='__main__':unittest.main()
