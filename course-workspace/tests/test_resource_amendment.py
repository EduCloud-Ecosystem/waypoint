import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import allocation as a
import resource_amendment as r
from public_course import render


class AmendmentTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve()
        self.config=self.root/'policy.json'
        self.policy={'version':1,'worker_id':'synthetic-worker','memory_budget_mb':8192,'cpu_budget':3,
                     'host_reserve_mb':2048,'host_reserve_cpu':1,'state_directory':str(self.root/'state'),
                     'owner':'Synthetic operator'}
        self.write(self.config,self.policy)
        self.info={'OSType':'linux','ID':'fixture-daemon','NCPU':4,'MemTotal':12*1024**3,'Architecture':'x86_64'}
        for name,value in [('allocation.AUTHORITY',self.root/'authority-parent'/'worker-allocation'),('allocation.ROOT_UID',os.geteuid())]:
            patched=patch(name,value);patched.start();self.addCleanup(patched.stop)
        for name,kw in [('allocation.daemon',{'side_effect':lambda:(self.info,a.digest(self.info['ID']))}),
                        ('allocation.home_filesystem_uuid',{'side_effect':lambda root:str(uuid.uuid5(uuid.NAMESPACE_URL,str(root)))}),
                        ('deployment._inspect_course',{'return_value':{},'create':True}),
                        ('allocation.subprocess.run',{'return_value':subprocess.CompletedProcess([],0,stdout='')}),
                        ('deployment.docker_json',{'return_value':[{'Id':'sha256:'+'a'*64,'Os':'linux','Architecture':'amd64'}]}),
                        ('homes.inspect',{'side_effect':lambda root,instance:{'volumes':{instance+'-home-'+hashlib.sha256(b'same-subject').hexdigest()[:24]:{'quota_bytes':5120*1024**2,'inode_limit':100000,'project_id':1}}}})]:
            patched=patch(name,**kw);patched.start();self.addCleanup(patched.stop)
        self.directory,self.env=self.course('one',18001)
        self.peer,self.peer_env=self.course('two',18002)
        a.enroll(self.directory,self.config);a.enroll(self.peer,self.config)
        self.plan=self.root/'plan.json'

    def write(self,path,value):
        path.write_text(json.dumps(value));path.chmod(0o600)

    def course(self,name,port):
        directory=self.root/name;directory.mkdir(mode=0o700)
        home=self.root/(name+'-homes');home.mkdir(mode=0o700)
        _,_,env=render(name+'.example.org','https://id.example.org/realms/'+name,name,name,['same-subject'],str(home))
        env.update(WORKSPACE_ENV_FILE=str(directory/'.env'),WORKSPACE_PORT=str(port),WORKSPACE_MEMORY_MB='512',
                   WORKSPACE_CPU_LIMIT='0.25',WORKSPACE_ACTIVE_LIMIT='1',WORKSPACE_OIDC_CLIENT_SECRET='synthetic-secret')
        (directory/'.env').write_text(''.join(key+'='+value+'\n' for key,value in env.items()))
        (directory/'.env').chmod(0o600)
        return directory,env

    def preview(self,**kwargs):
        return r.preview(self.directory,self.plan,kwargs.get('memory_mb',512),kwargs.get('cpu',0.25),kwargs.get('active_limit',2))

    def state(self):
        return a.read(self.root/'state'/'ledger.json')

    def test_expansion_then_contraction_updates_only_target_and_never_starts(self):
        before=self.state();peer=before['claims'][a.path_digest(self.peer)]
        output=self.preview();self.assertFalse(output['deployment_started'])
        with patch('deployment._compose') as compose,patch('resource_amendment.write_environment',wraps=r.write_environment) as envwrite:
            complete=r.apply(self.directory,self.plan)
            compose.assert_not_called();envwrite.assert_called_once()
        self.assertEqual(complete['status'],'COMPLETE')
        self.assertEqual(a.inventory(self.config)['memory_reserved_mb'],3584)
        self.assertEqual(self.state()['claims'][a.path_digest(self.peer)],peer)
        self.assertFalse((self.directory/r.PENDING).exists())
        self.assertEqual(a.check(self.directory,r.read_env(self.directory)[1])['active_courses'],2)
        self.plan=self.root/'contract.json';self.preview(active_limit=1)
        r.apply(self.directory,self.plan)
        self.assertEqual(a.inventory(self.config)['memory_reserved_mb'],3072)

    def test_preview_public_summary_has_no_private_identity_or_credentials(self):
        report=json.dumps(self.preview())
        for value in ('same-subject','synthetic-secret',str(self.root),'id.example.org','Synthetic operator'):
            self.assertNotIn(value,report)
        self.assertEqual(self.plan.stat().st_mode&0o777,0o600)

    def test_componentwise_peak_is_conservative_for_mixed_changes(self):
        # Target active1/memory1024 matches old aggregate active2/memory512,
        # but the conservative transitional active2/memory1024 needs more RAM.
        self.preview();r.apply(self.directory,self.plan)
        # Check the actual proposed reservation against a narrower pool while
        # preserving the fixture's authoritative policy and original binding.
        self.plan=self.root/'mixed.json'
        with patch('allocation.check_capacity',wraps=a.check_capacity) as check:
            report=self.preview(memory_mb=1024,active_limit=1)
        self.assertEqual(report['peak']['memory_mb'],3072)
        args=check.call_args.args
        self.assertEqual(sum(e['claim']['memory_mb'] for e in args[2].values()),4608)
        with self.assertRaises(a.AllocationError):a.check_capacity(dict(self.policy,memory_budget_mb=3584),self.info,args[2])

    def test_overbudget_preview_creates_no_plan_and_leaves_existing_claims(self):
        before=self.state()
        with self.assertRaisesRegex(r.AmendmentError,'reservations_exceed_budget'):
            self.preview(active_limit=4)
        self.assertFalse(self.plan.exists());self.assertEqual(self.state(),before)

    def test_manifest_resource_bounds_enforced(self):
        for limits in [{'memory_mb':127},{'memory_mb':1048577},{'cpu':0.099},{'cpu':float('inf')},{'cpu':1025},
                       {'active_limit':0},{'active_limit':1001}]:
            with self.subTest(limits=limits),self.assertRaisesRegex(r.AmendmentError,'invalid_resource_limits'):
                self.preview(**limits)
        self.assertFalse(self.plan.exists())

    def test_stale_full_env_including_comments_refuses_before_journal(self):
        self.preview()
        with (self.directory/'.env').open('a') as out:out.write('# changed approval context\n')
        with self.assertRaisesRegex(r.AmendmentError,'stale_resource_approval'):r.apply(self.directory,self.plan)
        self.assertFalse((self.directory/r.PENDING).exists())

    def test_appearing_trial_configuration_refuses_stale_institutional_topology(self):
        self.preview();path=self.directory/'pilot.env';path.symlink_to(self.root/'missing')
        with self.assertRaisesRegex(r.AmendmentError,'institutional_deployment_required'):r.apply(self.directory,self.plan)
        self.assertFalse((self.directory/r.PENDING).exists())
        self.assertEqual(a.inventory(self.config)['memory_reserved_mb'],3072)

    def test_crafted_overbudget_valid_plan_refuses_before_pending_publication(self):
        self.preview();plan=a.read(self.plan)
        plan['new_env']=r.env_target(plan['old_env'],512,0.25,4)
        plan['new_claim']=a.claim(self.directory,r.parse_env(self.directory,plan['new_env']))
        plan['peak_claim']=r.peak_claim(plan['old_claim'],plan['new_claim'])
        plan['new_marker']=dict(plan['old_marker'],claim_sha256=a.digest(plan['new_claim']))
        plan['digest']=a.digest({key:value for key,value in plan.items() if key!='digest'});self.write(self.plan,plan)
        with self.assertRaisesRegex(r.AmendmentError,'reservations_exceed_budget'):r.apply(self.directory,self.plan)
        self.assertFalse((self.directory/r.PENDING).exists())

    def test_peer_lease_change_before_initial_apply_is_stale(self):
        self.preview();a.release(self.peer)
        with self.assertRaisesRegex(r.AmendmentError,'stale_resource_approval'):r.apply(self.directory,self.plan)
        self.assertFalse((self.directory/r.PENDING).exists())

    def test_changed_policy_daemon_or_filesystem_refuses(self):
        self.preview()
        self.write(self.config,dict(self.policy,owner='Different operator'))
        with self.assertRaisesRegex(r.AmendmentError,'worker_binding_changed'):r.apply(self.directory,self.plan)
        self.write(self.config,self.policy)
        self.info['ID']='different-daemon'
        with self.assertRaisesRegex(r.AmendmentError,'worker_binding_changed'):r.apply(self.directory,self.plan)
        self.info['ID']='fixture-daemon'
        with patch('allocation.home_filesystem_uuid',return_value=str(uuid.uuid4())):
            with self.assertRaisesRegex(r.AmendmentError,'claim_binding_changed'):r.apply(self.directory,self.plan)

    def test_setup_files_and_actual_image_changes_refuse(self):
        (self.directory/'nginx.conf').write_text('private fixture config');(self.directory/'nginx.conf').chmod(0o600)
        self.preview()
        (self.directory/'nginx.conf').write_text('changed config')
        with self.assertRaisesRegex(r.AmendmentError,'runtime_setup_binding_changed'):r.apply(self.directory,self.plan)
        (self.directory/'nginx.conf').write_text('private fixture config')
        with patch('deployment.docker_json',return_value=[{'Id':'sha256:'+'b'*64,'Os':'linux','Architecture':'amd64'}]):
            with self.assertRaisesRegex(r.AmendmentError,'runtime_setup_binding_changed'):r.apply(self.directory,self.plan)

    def test_tampered_plan_digest_and_nonresource_change_refuse(self):
        self.preview();plan=a.read(self.plan)
        plan['new_env']=plan['new_env'].replace('synthetic-secret','different-secret')
        self.write(self.plan,plan)
        with self.assertRaisesRegex(r.AmendmentError,'invalid_or_stale_resource_plan'):r.apply(self.directory,self.plan)
        plan['digest']=a.digest({key:value for key,value in plan.items() if key!='digest'});self.write(self.plan,plan)
        with self.assertRaisesRegex(r.AmendmentError,'resource_only_change_required'):r.apply(self.directory,self.plan)

    def test_recomputed_plan_digest_cannot_bypass_resource_minimums(self):
        self.preview();plan=a.read(self.plan)
        plan['new_env']=plan['new_env'].replace('WORKSPACE_MEMORY_MB=512','WORKSPACE_MEMORY_MB=1')
        plan['digest']=a.digest({key:value for key,value in plan.items() if key!='digest'})
        self.write(self.plan,plan)
        with self.assertRaisesRegex(r.AmendmentError,'invalid_resource_limits'):r.apply(self.directory,self.plan)
        self.assertFalse((self.directory/r.PENDING).exists())

    def test_retired_released_unenrolled_restored_and_other_pending_sources_refuse(self):
        for marker in ('.course-retired.json',a.REQUIRED,'.maintenance-pending.json','.upgrade-pending.json','.retirement-pending.json'):
            path=self.directory/marker;path.symlink_to(self.root/'missing')
            with self.assertRaises(r.AmendmentError):self.preview()
            path.unlink()
        a.release(self.directory)
        with self.assertRaisesRegex(r.AmendmentError,'active_course_reservation_required'):self.preview()
        a.enroll(self.directory,self.config)
        (self.directory/a.MARKER).unlink()
        with self.assertRaisesRegex(r.AmendmentError,'explicit_worker_enrollment_required'):self.preview()

    def test_live_or_foreign_writer_refuses_preview_and_apply(self):
        with patch('allocation.stopped',side_effect=a.AllocationError('stop_all_home_volume_writers')):
            with self.assertRaisesRegex(r.AmendmentError,'stop_all_home_volume_writers'):self.preview()
        self.preview()
        with patch('allocation.stopped',side_effect=a.AllocationError('stop_all_hub_volume_writers')):
            with self.assertRaisesRegex(r.AmendmentError,'stop_all_hub_volume_writers'):r.apply(self.directory,self.plan)
        self.assertFalse((self.directory/r.PENDING).exists())

    def test_interrupt_every_write_phase_resumes_without_peer_claim_loss(self):
        for phase in ('journal','peak','env','marker','target','receipt'):
            with self.subTest(phase=phase):
                # Create an isolated course expansion/contraction for each phase.
                plan_path=self.root/('phase-'+phase+'.json')
                raw,env=r.read_env(self.directory)
                current=int(env['WORKSPACE_ACTIVE_LIMIT']);target=2 if current==1 else 1
                r.preview(self.directory,plan_path,512,0.25,target)
                before_peer=self.state()['claims'][a.path_digest(self.peer)]
                original=a.write;counter={'ledger':0}
                def fault(path,value):
                    if path.name=='ledger.json':counter['ledger']+=1
                    selected=(phase=='journal' and path.name==r.PENDING or phase=='peak' and path.name=='ledger.json' and counter['ledger']==1
                              or phase=='marker' and path.name==a.MARKER or phase=='target' and path.name=='ledger.json' and counter['ledger']==2
                              or phase=='receipt' and path.name==r.RECEIPT)
                    if selected:raise OSError('synthetic interruption')
                    return original(path,value)
                with patch('allocation.write',side_effect=fault),patch('resource_amendment.write_environment',side_effect=OSError('synthetic interruption') if phase=='env' else r.write_environment):
                    with self.assertRaises(r.AmendmentError):r.apply(self.directory,plan_path)
                if phase!='journal':self.assertTrue((self.directory/r.PENDING).exists())
                self.assertEqual(self.state()['claims'][a.path_digest(self.peer)],before_peer)
                complete=r.resume(self.directory,plan_path)
                self.assertEqual(complete['status'],'COMPLETE')
                self.assertEqual(self.state()['claims'][a.path_digest(self.peer)],before_peer)
                self.assertFalse((self.directory/r.PENDING).exists())

    def test_peak_pending_allows_other_course_release_and_resume_merges_current_ledger(self):
        self.preview()
        with patch('resource_amendment.write_environment',side_effect=OSError('interrupt')):
            with self.assertRaises(r.AmendmentError):r.apply(self.directory,self.plan)
        self.assertEqual(a.inventory(self.config)['memory_reserved_mb'],3584)
        self.assertEqual(a.check(self.peer,self.peer_env)['active_courses'],2)
        a.release(self.peer)
        r.resume(self.directory,self.plan)
        self.assertEqual(self.state()['claims'][a.path_digest(self.peer)]['status'],'RELEASED')

    def test_pre_peak_interruption_refuses_resume_if_peer_uses_available_capacity(self):
        self.preview()
        original=a.write
        def interrupt(path,value):
            if path.name=='ledger.json':raise OSError('before peak publication')
            return original(path,value)
        with patch('allocation.write',side_effect=interrupt):
            with self.assertRaises(r.AmendmentError):r.apply(self.directory,self.plan)
        self.assertEqual(a.inventory(self.config)['cpu_reserved'],2.5)
        peer_plan=self.root/'peer-expansion.json'
        r.preview(self.peer,peer_plan,512,0.25,3);r.apply(self.peer,peer_plan)
        self.assertEqual(a.inventory(self.config)['cpu_reserved'],3)
        with self.assertRaisesRegex(r.AmendmentError,'reservations_exceed_budget'):r.resume(self.directory,self.plan)
        self.assertTrue((self.directory/r.PENDING).exists())
        self.assertEqual(self.state()['claims'][a.path_digest(self.directory)]['claim']['active_limit'],1)
        self.assertEqual(a.check(self.peer,r.read_env(self.peer)[1])['active_courses'],2)

    def test_resume_refuses_unordered_env_marker_ledger_combination(self):
        self.preview();plan=a.read(self.plan)
        with patch('resource_amendment.write_environment',side_effect=OSError('interrupt')):
            with self.assertRaises(r.AmendmentError):r.apply(self.directory,self.plan)
        a.write(self.directory/a.MARKER,plan['new_marker'])
        with self.assertRaisesRegex(r.AmendmentError,'resource_resume_state_changed'):r.resume(self.directory,self.plan)
        self.assertTrue((self.directory/r.PENDING).exists())
        self.assertEqual(a.inventory(self.config)['memory_reserved_mb'],3584)

    def test_resume_refuses_tampered_journal_or_environment(self):
        self.preview()
        with patch('resource_amendment.write_environment',side_effect=OSError('interrupt')):
            with self.assertRaises(r.AmendmentError):r.apply(self.directory,self.plan)
        journal=a.read(self.directory/r.PENDING);self.write(self.directory/r.PENDING,dict(journal,extra=True))
        with self.assertRaisesRegex(r.AmendmentError,'resource_resume_state_changed'):r.resume(self.directory,self.plan)
        self.write(self.directory/r.PENDING,journal)
        with (self.directory/'.env').open('a') as out:out.write('# changed pending environment\n')
        with self.assertRaisesRegex(r.AmendmentError,'resource_resume_state_changed'):r.resume(self.directory,self.plan)

    def test_receipt_before_gate_cleanup_supports_idempotent_retry(self):
        self.preview()
        original_unlink=Path.unlink
        def interrupt(path,*args,**kwargs):
            if path.name==r.PENDING:raise OSError('interrupt cleanup')
            return original_unlink(path,*args,**kwargs)
        with patch('pathlib.Path.unlink',new=interrupt):
            with self.assertRaises(r.AmendmentError):r.apply(self.directory,self.plan)
        r.validate_receipt(a.read(self.directory/r.RECEIPT))
        self.assertTrue((self.directory/r.PENDING).exists())
        r.resume(self.directory,self.plan)
        with patch('resource_amendment.write_environment') as writer:
            self.assertEqual(r.apply(self.directory,self.plan)['status'],'COMPLETE');writer.assert_not_called()
        with (self.directory/'.env').open('a') as out:out.write('# later changed config\n')
        with self.assertRaisesRegex(r.AmendmentError,'durable_resource_target_unverified'):r.apply(self.directory,self.plan)

    def test_historical_receipt_validates_independently_of_later_runtime_changes(self):
        self.preview();r.apply(self.directory,self.plan);receipt=a.read(self.directory/r.RECEIPT)
        (self.directory/'.env').write_text('later legitimate private configuration')
        self.assertEqual(r.validate_receipt(receipt),receipt)
        with self.assertRaisesRegex(r.AmendmentError,'invalid_resource_amendment_receipt'):
            r.validate_receipt(dict(receipt,claim_sha256='f'*64))

    def test_plan_publication_is_atomic_no_replace_under_concurrent_appearance(self):
        original=r.runtime_binding
        def concurrent(path,env):
            value=original(path,env)
            self.plan.write_text('{"another":"reviewed plan"}');self.plan.chmod(0o600)
            return value
        with patch('resource_amendment.runtime_binding',side_effect=concurrent):
            with self.assertRaisesRegex(r.AmendmentError,'new_private_resource_plan_required'):self.preview()
        self.assertEqual(self.plan.read_text(),'{'+'"another":"reviewed plan"'+'}')
        self.assertEqual(list(self.root.glob('.resource-plan-*')),[])

    def test_plan_destination_cannot_overlap_peer_retained_state(self):
        self.plan=self.peer/'resource-plan.json'
        with self.assertRaisesRegex(r.AmendmentError,'resource_plan_overlaps_retained_state'):self.preview()
        self.assertFalse(self.plan.exists())

    def test_actual_quota_mount_or_image_architecture_drift_refuses_before_mutation(self):
        self.preview()
        with patch('homes.inspect',side_effect=ValueError('private mount mismatch')):
            with self.assertRaises(r.AmendmentError):r.apply(self.directory,self.plan)
        with patch('homes.inspect',return_value={'volumes':{}}):
            with self.assertRaisesRegex(r.AmendmentError,'retained_home_binding_invalid'):r.apply(self.directory,self.plan)
        with patch('deployment.docker_json',return_value=[{'Id':'sha256:'+'a'*64,'Os':'linux','Architecture':'arm64'}]):
            with self.assertRaisesRegex(r.AmendmentError,'runtime_image_binding_invalid'):r.apply(self.directory,self.plan)
        self.assertFalse((self.directory/r.PENDING).exists())

    def test_receipt_resource_profiles_are_interpretable_and_arithmetic_checked(self):
        self.preview();r.apply(self.directory,self.plan);receipt=a.read(self.directory/r.RECEIPT)
        self.assertEqual((receipt['old']['active_limit'],receipt['new']['active_limit']),(1,2))
        receipt['new']['memory_mb']=1
        receipt['digest']=a.digest({key:value for key,value in receipt.items() if key!='digest'})
        with self.assertRaisesRegex(r.AmendmentError,'invalid_resource_amendment_receipt'):r.validate_receipt(receipt)

    def test_completed_expand_contract_cycle_cannot_replay_old_approval(self):
        self.preview();first_plan=self.plan;first_env=r.read_env(self.directory)[0]
        r.apply(self.directory,first_plan)
        contraction=self.root/'contraction.json'
        r.preview(self.directory,contraction,512,0.25,1);r.apply(self.directory,contraction)
        self.assertNotEqual(r.read_env(self.directory)[0],first_env)
        self.assertEqual(r.read_env(self.directory)[1]['WORKSPACE_ACTIVE_LIMIT'],'1')
        with self.assertRaisesRegex(r.AmendmentError,'resource_receipt_history_changed'):
            r.apply(self.directory,first_plan)
        self.assertFalse((self.directory/r.PENDING).exists())
        self.assertEqual(a.inventory(self.config)['memory_reserved_mb'],3072)

    def test_changed_or_malformed_prior_receipt_refuses_fresh_apply(self):
        self.preview();r.apply(self.directory,self.plan)
        contraction=self.root/'contraction.json';r.preview(self.directory,contraction,512,0.25,1)
        receipt=a.read(self.directory/r.RECEIPT)
        changed=dict(receipt,plan_sha256='f'*64)
        changed['digest']=a.digest({key:value for key,value in changed.items() if key!='digest'})
        self.write(self.directory/r.RECEIPT,changed)
        with self.assertRaisesRegex(r.AmendmentError,'resource_receipt_history_changed'):r.apply(self.directory,contraction)
        self.write(self.directory/r.RECEIPT,dict(receipt,digest='f'*64))
        with self.assertRaisesRegex(r.AmendmentError,'invalid_resource_amendment_receipt'):r.apply(self.directory,contraction)
        self.assertFalse((self.directory/r.PENDING).exists())

    def test_resume_refuses_foreign_completed_receipt_when_pending_not_final(self):
        self.preview()
        with patch('resource_amendment.write_environment',side_effect=OSError('interrupt')):
            with self.assertRaises(r.AmendmentError):r.apply(self.directory,self.plan)
        foreign=r.receipt_record(a.read(self.plan))
        self.write(self.directory/r.RECEIPT,foreign)
        with self.assertRaisesRegex(r.AmendmentError,'resource_receipt_history_changed'):r.resume(self.directory,self.plan)
        self.assertTrue((self.directory/r.PENDING).exists())

    def test_preview_refuses_malformed_history_before_new_approval(self):
        self.write(self.directory/r.RECEIPT,{'version':r.VERSION})
        with self.assertRaisesRegex(r.AmendmentError,'invalid_resource_amendment_receipt'):self.preview()
        self.assertFalse(self.plan.exists())

    def test_foreign_or_stale_completed_history_refuses_preview(self):
        self.preview();r.apply(self.directory,self.plan)
        receipt=a.read(self.directory/r.RECEIPT);self.plan=self.root/'next.json'
        for key,value in [('directory_sha256',a.path_digest(self.peer)),('claim_sha256','f'*64)]:
            altered=dict(receipt,**{key:value})
            altered['digest']=a.digest({k:v for k,v in altered.items() if k!='digest'})
            self.write(self.directory/r.RECEIPT,altered)
            with self.assertRaisesRegex(r.AmendmentError,'resource_receipt_(directory|claim)_mismatch'):
                self.preview(active_limit=1)
            self.assertFalse(self.plan.exists())

    def test_deleted_latest_receipt_cannot_replay_approval_after_round_trip(self):
        self.preview();original=self.plan;r.apply(self.directory,original)
        contraction=self.root/'round-trip.json';r.preview(self.directory,contraction,512,0.25,1);r.apply(self.directory,contraction)
        revision=r.read_env(self.directory)[1][r.REVISION]
        (self.directory/r.RECEIPT).unlink()
        with self.assertRaisesRegex(r.AmendmentError,'completed_resource_history_required'):r.apply(self.directory,original)
        with self.assertRaisesRegex(r.AmendmentError,'completed_resource_history_required'):r.completed_receipt(self.directory)
        self.plan=self.root/'new-approval.json'
        with self.assertRaisesRegex(r.AmendmentError,'completed_resource_history_required'):self.preview()
        self.assertEqual(r.read_env(self.directory)[1][r.REVISION],revision)
        self.assertFalse((self.directory/r.PENDING).exists())

    def test_new_env_revision_before_first_receipt_requires_valid_pending_resume(self):
        self.preview();original=a.write
        def fault(path,value):
            if path.name==a.MARKER:raise OSError('after revised env before marker')
            return original(path,value)
        with patch('allocation.write',side_effect=fault):
            with self.assertRaises(r.AmendmentError):r.apply(self.directory,self.plan)
        self.assertIn(r.REVISION,r.read_env(self.directory)[1])
        self.assertFalse((self.directory/r.RECEIPT).exists())
        with self.assertRaisesRegex(r.AmendmentError,'completed_resource_history_required'):r.completed_receipt(self.directory)
        r.resume(self.directory,self.plan)
        self.assertEqual(r.completed_receipt(self.directory)['revision'],r.read_env(self.directory)[1][r.REVISION])

    def test_revision_format_freshness_and_receipt_binding_are_required(self):
        self.preview();plan=a.read(self.plan)
        for revision in ('not-a-revision','A'*32):
            altered=copy.deepcopy(plan)
            original=r.parse_env(self.directory,altered['new_env'])[r.REVISION]
            altered['new_env']=altered['new_env'].replace(original,revision)
            altered['digest']=a.digest({k:v for k,v in altered.items() if k!='digest'});self.write(self.plan,altered)
            with self.assertRaisesRegex(r.AmendmentError,'invalid_resource_revision'):r.apply(self.directory,self.plan)
        self.write(self.plan,plan);r.apply(self.directory,self.plan)
        receipt=a.read(self.directory/r.RECEIPT);receipt['revision']=uuid.uuid4().hex
        receipt['digest']=a.digest({k:v for k,v in receipt.items() if k!='digest'});self.write(self.directory/r.RECEIPT,receipt)
        with self.assertRaisesRegex(r.AmendmentError,'resource_revision_history_mismatch'):r.completed_receipt(self.directory)

    def test_mocked_uuid_reuse_cannot_publish_nonfresh_revision(self):
        self.preview();r.apply(self.directory,self.plan)
        current=r.read_env(self.directory)[1][r.REVISION]
        self.plan=self.root/'repeat-revision.json'
        with patch('resource_amendment.uuid.uuid4',return_value=SimpleNamespace(hex=current)):
            with self.assertRaisesRegex(r.AmendmentError,'fresh_resource_revision_required'):self.preview(active_limit=1)
        self.assertFalse(self.plan.exists())

    def test_later_pending_transaction_requires_its_previous_completed_receipt(self):
        self.preview();r.apply(self.directory,self.plan)
        self.plan=self.root/'next-transaction.json';self.preview(active_limit=1)
        with patch('resource_amendment.write_environment',side_effect=OSError('before new env')):
            with self.assertRaises(r.AmendmentError):r.apply(self.directory,self.plan)
        (self.directory/r.RECEIPT).unlink()
        with self.assertRaisesRegex(r.AmendmentError,'resource_receipt_history_changed'):r.resume(self.directory,self.plan)
        self.assertTrue((self.directory/r.PENDING).exists())

    def test_plan_and_journal_require_private_regular_bounded_records(self):
        self.preview();self.plan.chmod(0o644)
        with self.assertRaises(r.AmendmentError):r.apply(self.directory,self.plan)
        self.plan.chmod(0o600);self.plan.unlink();self.plan.symlink_to(self.root/'missing')
        with self.assertRaises(r.AmendmentError):r.apply(self.directory,self.plan)


if __name__=='__main__':unittest.main()
