import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import allocation as a
from public_course import render

NATIVE_HOME_FILESYSTEM_UUID = a.home_filesystem_uuid


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = self.root/'policy.json'
        self.p = {'version':1,'worker_id':'synthetic-worker','memory_budget_mb':8192,'cpu_budget':3,
                  'host_reserve_mb':2048,'host_reserve_cpu':1,'state_directory':str(self.root/'state'),
                  'owner':'Synthetic operator'}
        self.write(self.config,self.p)
        self.info = {'OSType':'linux','ID':'daemon-fixture','NCPU':4,'MemTotal':12*1024**3}
        for target,value in [('allocation.AUTHORITY',self.root/'authority-parent'/'worker-allocation'),
                             ('allocation.ROOT_UID',os.geteuid())]:
            p = patch(target,value);p.start();self.addCleanup(p.stop)
        native = patch('deployment.require_native_worker');native.start();self.addCleanup(native.stop)
        d = patch('allocation.daemon',side_effect=lambda:(self.info,a.digest({'endpoint':'unix:///var/run/docker.sock','id':self.info['ID']})))
        d.start();self.addCleanup(d.stop)
        inspect = patch('deployment._inspect_course',return_value={},create=True)
        self.inspect=inspect.start();self.addCleanup(inspect.stop)
        docker=patch('allocation.subprocess.run',return_value=subprocess.CompletedProcess([],0,stdout=''))
        self.docker=docker.start();self.addCleanup(docker.stop)
        fs = patch('allocation.home_filesystem_uuid',side_effect=lambda root:str(uuid.uuid5(uuid.NAMESPACE_URL,str(root))))
        self.filesystem = fs.start();self.addCleanup(fs.stop)
        self.one,self.env1=self.course('one',18001)
        self.two,self.env2=self.course('two',18002)

    def write(self,path,value):
        path.write_text(json.dumps(value));path.chmod(0o600)

    def course(self,name,port):
        directory=self.root/name;directory.mkdir(mode=0o700)
        homes=self.root/(name+'-homes');homes.mkdir(mode=0o700)
        _,_,env=render(name+'.course.example.org','https://id.example.org/realms/'+name,
                       name,name,['same-subject'],str(homes))
        env.update(WORKSPACE_ENV_FILE=str(directory/'.env'),WORKSPACE_PORT=str(port),
                   WORKSPACE_MEMORY_MB='512',WORKSPACE_CPU_LIMIT='0.25',WORKSPACE_ACTIVE_LIMIT='1',
                   WORKSPACE_OIDC_CLIENT_SECRET='fixture-private-secret')
        (directory/'.env').write_text(''.join(k+'='+v+'\n' for k,v in env.items()))
        (directory/'.env').chmod(0o600)
        return directory,env

    def rewrite_env(self,directory,env):
        (directory/'.env').write_text(''.join(k+'='+v+'\n' for k,v in env.items()))

    def test_two_courses_reserved_even_when_stopped_and_public_report_is_bounded(self):
        a.enroll(self.one,self.config);result=a.enroll(self.two,self.config)
        self.assertEqual((result['active_courses'],result['memory_reserved_mb'],result['cpu_reserved']),(2,3072,2.5))
        self.assertEqual(a.check(self.one,self.env1)['active_courses'],2)
        report=json.dumps(a.inventory(self.config))
        for private in ['same-subject','fixture-private-secret','Synthetic operator',str(self.one),'course.example.org']:
            self.assertNotIn(private,report)
        self.assertEqual(a.read_marker(self.one)['status'],'ENROLLED')

    def test_budget_refusal_does_not_create_pending_marker(self):
        self.p['cpu_budget']=1.5;self.write(self.config,self.p)
        a.enroll(self.one,self.config)
        with self.assertRaisesRegex(a.AllocationError,'reservations_exceed_budget'):
            a.enroll(self.two,self.config)
        self.assertFalse((self.two/a.MARKER).exists())
        self.assertEqual(a.inventory(self.config)['active_courses'],1)

    def test_policy_must_leave_actual_host_reserve(self):
        self.info['NCPU']=3
        with self.assertRaisesRegex(a.AllocationError,'policy_exceeds_worker_capacity'):
            a.enroll(self.one,self.config)
        self.assertFalse(a.AUTHORITY.exists())
        self.info['NCPU']=4;self.info['MemTotal']=9*1024**3
        with self.assertRaisesRegex(a.AllocationError,'policy_exceeds_worker_capacity'):
            a.enroll(self.one,self.config)

    def test_released_budget_freed_but_claim_stays_and_admission_refuses(self):
        a.enroll(self.one,self.config)
        result=a.release(self.one)
        self.assertEqual((result['active_courses'],result['released_courses']),(0,1))
        with self.assertRaisesRegex(a.AllocationError,'active_course_reservation_required'):
            a.check(self.one,self.env1)
        result=a.enroll(self.one,self.config)
        self.assertEqual((result['active_courses'],result['released_courses']),(1,0))

    def test_released_tombstone_still_prevents_identity_storage_collision(self):
        a.enroll(self.one,self.config);a.release(self.one)
        env=dict(self.env2,WORKSPACE_PORT=self.env1['WORKSPACE_PORT'])
        self.rewrite_env(self.two,env)
        with self.assertRaisesRegex(a.AllocationError,'identity_or_storage_collision'):
            a.enroll(self.two,self.config)

    def test_collisions_for_nested_dns_port_instance_client_and_overlapping_storage(self):
        a.enroll(self.one,self.config)
        variants=[{'WORKSPACE_PORT':self.env1['WORKSPACE_PORT']},
                  {'WORKSPACE_INSTANCE':self.env1['WORKSPACE_INSTANCE']},
                  {'WORKSPACE_PUBLIC_URL':'https://student.one.course.example.org'},
                  {'WORKSPACE_HOME_ROOT':self.env1['WORKSPACE_HOME_ROOT']},
                  {'WORKSPACE_OIDC_ISSUER':self.env1['WORKSPACE_OIDC_ISSUER'],'WORKSPACE_OIDC_CLIENT_ID':self.env1['WORKSPACE_OIDC_CLIENT_ID']}]
        for changes in variants:
            with self.subTest(changes=changes):
                self.rewrite_env(self.two,dict(self.env2,**changes))
                with self.assertRaisesRegex(a.AllocationError,'identity_or_storage_collision'):
                    a.enroll(self.two,self.config)

    def test_same_subject_in_distinct_courses_does_not_collide(self):
        a.enroll(self.one,self.config);a.enroll(self.two,self.config)
        self.assertNotEqual(a.read_marker(self.one)['claim_sha256'],a.read_marker(self.two)['claim_sha256'])

    def test_policy_authority_daemon_and_marker_changes_fail_closed(self):
        a.enroll(self.one,self.config)
        original=copy.deepcopy(self.p)
        self.p['owner']='New owner';self.write(self.config,self.p)
        with self.assertRaisesRegex(a.AllocationError,'enrollment_changed'):
            a.check(self.one,self.env1)
        self.write(self.config,original)
        self.info['ID']='replacement-daemon'
        with self.assertRaisesRegex(a.AllocationError,'enrollment_changed'):
            a.check(self.one,self.env1)
        self.info['ID']='daemon-fixture'
        marker=self.one/a.MARKER;marker.unlink()
        with self.assertRaisesRegex(a.AllocationError,'explicit_worker_enrollment_required'):
            a.check(self.one,self.env1)

    def test_second_policy_cannot_create_independent_worker_budget(self):
        a.enroll(self.one,self.config)
        config=self.root/'second.json';self.write(config,dict(self.p,state_directory=str(self.root/'second-state'),worker_id='other'))
        with self.assertRaisesRegex(a.AllocationError,'authority_binding_changed'):
            a.enroll(self.two,config)
        self.assertFalse((self.root/'second-state').exists())

    def test_pending_after_marker_before_ledger_resumes_exact_claim(self):
        original=a.write
        def interrupt(path,value):
            if path.name=='ledger.json' and value['claims']:
                raise OSError('synthetic interruption')
            return original(path,value)
        with patch('allocation.write',side_effect=interrupt):
            with self.assertRaises(a.AllocationError):a.enroll(self.one,self.config)
        self.assertEqual(a.read_marker(self.one)['status'],'PENDING')
        with self.assertRaisesRegex(a.AllocationError,'unfinished_worker_enrollment'):a.check(self.one,self.env1)
        self.assertEqual(a.enroll(self.one,self.config)['active_courses'],1)

    def test_pending_after_ledger_before_enrolled_resumes_without_double_reservation(self):
        original=a.write
        def interrupt(path,value):
            if path.name==a.MARKER and value['status']=='ENROLLED':raise OSError('synthetic interruption')
            return original(path,value)
        with patch('allocation.write',side_effect=interrupt):
            with self.assertRaises(a.AllocationError):a.enroll(self.one,self.config)
        self.assertEqual(a.inventory(self.config)['active_courses'],1)
        self.assertEqual(a.enroll(self.one,self.config)['memory_reserved_mb'],1536)

    def test_restore_gate_clears_only_after_durable_verified_enrollment(self):
        self.write(self.one/a.REQUIRED,{'version':'educloud-worker-allocation-required-v1','source':'snapshot_restore'})
        original=a.write
        def interrupt(path,value):
            if path.name==a.MARKER and value['status']=='ENROLLED':raise OSError('synthetic interruption')
            return original(path,value)
        with patch('allocation.write',side_effect=interrupt):
            with self.assertRaises(a.AllocationError):a.enroll(self.one,self.config)
        self.assertTrue((self.one/a.REQUIRED).exists())
        a.enroll(self.one,self.config)
        self.assertFalse((self.one/a.REQUIRED).exists())
        self.assertEqual(a.check(self.one,self.env1)['active_courses'],1)

    def test_pending_changed_claim_cannot_resume(self):
        original=a.write
        def interrupt(path,value):
            if path.name==a.MARKER and value['status']=='ENROLLED':raise OSError('synthetic interruption')
            return original(path,value)
        with patch('allocation.write',side_effect=interrupt):
            with self.assertRaises(a.AllocationError):a.enroll(self.one,self.config)
        self.rewrite_env(self.one,dict(self.env1,WORKSPACE_MEMORY_MB='1024'))
        with self.assertRaisesRegex(a.AllocationError,'enrollment_changed'):a.enroll(self.one,self.config)

    def test_missing_corrupt_or_linked_ledger_refuses(self):
        a.enroll(self.one,self.config)
        ledger=self.root/'state'/'ledger.json';original=ledger.read_bytes()
        for action in ['missing','corrupt','linked']:
            ledger.unlink(missing_ok=True)
            if action=='corrupt':ledger.write_text('{}');ledger.chmod(0o600)
            if action=='linked':ledger.symlink_to(self.root/'missing')
            with self.assertRaises(a.AllocationError):a.check(self.one,self.env1)
            ledger.unlink(missing_ok=True);ledger.write_bytes(original);ledger.chmod(0o600)

    def test_unenrolled_and_missing_authority_pointer_refuse_legacy_fallback(self):
        a.enroll(self.one,self.config)
        (a.AUTHORITY/'authority.json').unlink()
        with self.assertRaisesRegex(a.AllocationError,'explicit_worker_enrollment_required'):a.check(self.two,self.env2)
        with self.assertRaises(a.AllocationError):a.check(self.one,self.env1)

    def test_private_policy_bounds_and_duplicate_keys(self):
        for raw in ['{"version":1,"version":1}',json.dumps(dict(self.p,cpu_budget=float('inf'))),'{}','[]']:
            self.config.write_text(raw)
            with self.assertRaises(a.AllocationError):a.policy(self.config)
        self.write(self.config,self.p);self.config.chmod(0o644)
        with self.assertRaises(a.AllocationError):a.policy(self.config)

    def test_retired_source_and_restored_course_cannot_enroll(self):
        (self.one/'.course-retired.json').symlink_to(self.root/'missing')
        with self.assertRaises(a.AllocationError):a.enroll(self.one,self.config)
        self.assertFalse((self.one/a.MARKER).exists())

    def test_running_owned_hub_and_foreign_home_writer_block_release(self):
        a.enroll(self.one,self.config)
        self.docker.side_effect=lambda args,**kw:subprocess.CompletedProcess([],0,stdout='a'*64+'\n' if args[1]=='ps' else '')
        owned={'Name':'/one-hub','Config':{'Labels':{}},'Mounts':[]}
        foreign={'Name':'/foreign','Config':{'Labels':{}},'Mounts':[{'Name':'one-home-'+'f'*24,'Source':'/unrelated'}]}
        bind={'Name':'/foreign','Config':{'Labels':{}},'Mounts':[{'Source':self.env1['WORKSPACE_HOME_ROOT']+'/one-home-fixture'}]}
        hub={'Name':'/foreign','Config':{'Labels':{}},'Mounts':[{'Name':'one-hub-data'}]}
        for item in (owned,foreign,bind,hub):
            with patch('deployment.docker_json',return_value=[item]):
                with self.assertRaises(a.AllocationError):a.release(self.one)
        self.docker.side_effect=None
        self.docker.return_value=subprocess.CompletedProcess([],0,stdout='')
        self.assertEqual(a.inventory(self.config)['active_courses'],1)

    def test_unknown_running_course_blocks_enrollment(self):
        self.docker.side_effect=lambda args,**kw:subprocess.CompletedProcess([],0,stdout='a'*64+'\n' if args[1]=='ps' else '')
        item={'Name':'/unknown-hub','Config':{'Labels':{'educloud.workspace.instance':'unknown'}},'Mounts':[]}
        with patch('deployment.docker_json',return_value=[item]):
            with self.assertRaisesRegex(a.AllocationError,'unmanaged_running_course'):a.enroll(self.one,self.config)
        self.assertFalse((self.one/a.MARKER).exists())

    def test_admission_holds_global_lock_through_caller_and_retains_failed_start_reservation(self):
        a.enroll(self.one,self.config)
        with self.assertRaisesRegex(a.AllocationError,'allocation_refused_or_incomplete'):
            with a.admission(self.one,self.env1):
                with self.assertRaisesRegex(a.AllocationError,'worker_operation_in_progress'):
                    a.inventory(self.config)
                raise RuntimeError('synthetic startup failure')
        self.assertEqual(a.inventory(self.config)['active_courses'],1)

    def test_legacy_unmanaged_only_before_authority(self):
        self.assertIsNone(a.check(self.one,self.env1))
        with a.admission(self.one,self.env1):pass
        self.assertFalse(a.AUTHORITY.exists())
        a.enroll(self.one,self.config);a.release(self.one)
        with self.assertRaisesRegex(a.AllocationError,'explicit_worker_enrollment_required'):
            with a.admission(self.two,self.env2):pass

    def test_foreign_bind_into_actual_hub_volume_blocks_release(self):
        a.enroll(self.one,self.config)
        self.docker.side_effect=lambda args,**kw:subprocess.CompletedProcess([],0,stdout='a'*64+'\n' if args[1]=='ps' else 'one-hub-data\n')
        item={'Name':'/foreign','Config':{'Labels':{}},'Mounts':[{'Source':'/var/lib/docker/volumes/one-hub-data/_data/nested'}]}
        volume={'Name':'one-hub-data','Driver':'local','Options':{},'Mountpoint':'/var/lib/docker/volumes/one-hub-data/_data'}
        with patch('deployment.docker_json',side_effect=[[item],[volume]]):
            with self.assertRaisesRegex(a.AllocationError,'stop_all_hub_volume_writers'):a.release(self.one)

    def test_native_legacy_admission_excludes_other_live_course(self):
        self.docker.side_effect=lambda args,**kw:subprocess.CompletedProcess([],0,stdout='a'*64+'\n')
        item={'Name':'/other-hub','Config':{'Labels':{'educloud.workspace.instance':'other'}},'Mounts':[]}
        with patch('deployment.docker_json',return_value=[item]):
            with self.assertRaisesRegex(a.AllocationError,'unmanaged_multiple_courses_refused'):
                with a.admission(self.one,self.env1):pass

    def test_native_local_trial_serializes_first_enrollment(self):
        env=dict(self.env1,WORKSPACE_AUTH_MODE='local-test')
        with patch('allocation.sys.platform','linux'):
            with a.admission(self.one,env):
                with self.assertRaisesRegex(a.AllocationError,'worker_operation_in_progress'):
                    a.enroll(self.two,self.config)
        self.assertFalse(a.AUTHORITY.exists())

    def test_desktop_trial_checks_other_live_courses_before_activation(self):
        env=dict(self.env1,WORKSPACE_AUTH_MODE='local-test')
        self.docker.side_effect=lambda args,**kw:subprocess.CompletedProcess([],0,stdout='a'*64+'\n')
        item={'Name':'/other-hub','Config':{'Labels':{'educloud.workspace.instance':'other'}},'Mounts':[]}
        with patch('allocation.sys.platform','darwin'),patch('deployment.docker_json',return_value=[item]):
            with self.assertRaisesRegex(a.AllocationError,'unmanaged_multiple_courses_refused'):
                with a.admission(self.one,env):pass

    def test_local_trial_cannot_bypass_present_managed_authority(self):
        a.enroll(self.one,self.config)
        with self.assertRaisesRegex(a.AllocationError,'institutional_enrollment_required'):
            with a.admission(self.two,dict(self.env2,WORKSPACE_AUTH_MODE='local-test')):pass

    def alias_fixture(self, mount):
        home='one-home-'+'f'*24
        self.docker.side_effect=lambda args,**kw:subprocess.CompletedProcess([],0,stdout='a'*64+'\n' if args[1]=='ps' else home+'\nforeign-alias\n')
        item={'Name':'/foreign','Config':{'Labels':{}},'Mounts':[mount]}
        rows=[{'Name':home,'Driver':'local','Mountpoint':'/var/lib/docker/volumes/'+home+'/_data',
               'Options':{'type':'none','o':'bind','device':self.env1['WORKSPACE_HOME_ROOT']+'/'+home}},
              {'Name':'foreign-alias','Driver':'local','Mountpoint':'/var/lib/docker/volumes/foreign-alias/_data',
               'Options':{'type':'none','o':'bind','device':self.env1['WORKSPACE_HOME_ROOT']+'/'+home}}]
        return item,rows

    def test_foreign_bind_mount_of_owned_home_docker_mountpoint_blocks_release(self):
        a.enroll(self.one,self.config)
        item,rows=self.alias_fixture({'Source':'/var/lib/docker/volumes/one-home-'+'f'*24+'/_data/nested'})
        with patch('deployment.docker_json',side_effect=[[item],rows]):
            with self.assertRaisesRegex(a.AllocationError,'stop_all_home_volume_writers'):a.release(self.one)
        self.docker.side_effect=None
        self.assertEqual(a.inventory(self.config)['active_courses'],1)

    def test_foreign_named_local_bind_volume_into_owned_home_blocks_release(self):
        a.enroll(self.one,self.config)
        item,rows=self.alias_fixture({'Name':'foreign-alias','Source':'/var/lib/docker/volumes/foreign-alias/_data'})
        with patch('deployment.docker_json',side_effect=[[item],rows]):
            with self.assertRaisesRegex(a.AllocationError,'stop_all_home_volume_writers'):a.release(self.one)

    def test_foreign_bind_into_alias_mountpoint_and_alias_chain_blocks_release(self):
        a.enroll(self.one,self.config)
        item,rows=self.alias_fixture({'Source':'/var/lib/docker/volumes/foreign-alias/_data/nested'})
        rows[1]['Options']['device']=rows[0]['Mountpoint']+'/nested'
        with patch('deployment.docker_json',side_effect=[[item],rows]):
            with self.assertRaisesRegex(a.AllocationError,'stop_all_home_volume_writers'):a.release(self.one)

    def test_foreign_device_symlink_into_owned_home_blocks_release(self):
        a.enroll(self.one,self.config)
        link=self.root/'home-alias';link.symlink_to(self.env1['WORKSPACE_HOME_ROOT'],target_is_directory=True)
        item,rows=self.alias_fixture({'Name':'foreign-alias','Source':'/var/lib/docker/volumes/foreign-alias/_data'})
        rows[1]['Options']['device']=str(link)
        with patch('deployment.docker_json',side_effect=[[item],rows]):
            with self.assertRaisesRegex(a.AllocationError,'stop_all_home_volume_writers'):a.release(self.one)

    def test_legitimate_other_course_and_nonoverlap_named_volume_allow_release(self):
        a.enroll(self.one,self.config);a.enroll(self.two,self.config)
        name='two-home-'+'e'*24
        self.docker.side_effect=lambda args,**kw:subprocess.CompletedProcess([],0,stdout='a'*64+'\n' if args[1]=='ps' else name+'\nunrelated\n')
        item={'Name':'/two-learner','Config':{'Labels':{'educloud.workspace.instance':'two'}},
              'Mounts':[{'Name':name,'Source':'/var/lib/docker/volumes/'+name+'/_data'},
                        {'Name':'unrelated','Source':'/var/lib/docker/volumes/unrelated/_data'}]}
        rows=[{'Name':name,'Driver':'local','Options':{'device':self.env2['WORKSPACE_HOME_ROOT']+'/'+name},
               'Mountpoint':'/var/lib/docker/volumes/'+name+'/_data'},
              {'Name':'unrelated','Driver':'local','Options':{'device':'/srv/unrelated'},
               'Mountpoint':'/var/lib/docker/volumes/unrelated/_data'}]
        with patch('deployment.docker_json',side_effect=[[item],rows]):
            result=a.release(self.one)
        self.assertEqual((result['active_courses'],result['released_courses']),(1,1))

    def test_distinct_home_roots_on_same_filesystem_refused_before_lease(self):
        identity=str(uuid.uuid4())
        self.filesystem.side_effect=lambda root:identity
        a.enroll(self.one,self.config)
        with self.assertRaisesRegex(a.AllocationError,'identity_or_storage_collision'):
            a.enroll(self.two,self.config)
        self.assertFalse((self.two/a.MARKER).exists())
        self.assertEqual(a.inventory(self.config)['active_courses'],1)

    def test_retained_filesystem_uuid_tombstone_refuses_clone_after_release(self):
        identity=str(uuid.uuid4())
        self.filesystem.side_effect=lambda root:identity
        a.enroll(self.one,self.config);a.release(self.one)
        with self.assertRaisesRegex(a.AllocationError,'identity_or_storage_collision'):
            a.enroll(self.two,self.config)
        self.assertEqual(a.inventory(self.config)['released_courses'],1)

    def test_changed_home_filesystem_identity_refuses_admission_and_release(self):
        a.enroll(self.one,self.config)
        self.filesystem.side_effect=lambda root:str(uuid.uuid4())
        with self.assertRaisesRegex(a.AllocationError,'enrollment_changed'):a.check(self.one,self.env1)
        with self.assertRaisesRegex(a.AllocationError,'enrollment_changed'):a.release(self.one)
        self.assertEqual(a.inventory(self.config)['active_courses'],1)

    def test_native_filesystem_reader_requires_bounded_xfs_uuid(self):
        original=NATIVE_HOME_FILESYSTEM_UUID
        for row in [{'fstype':'ext4','uuid':str(uuid.uuid4())},{'fstype':'xfs','uuid':None},
                    {'fstype':'xfs','uuid':'not-an-identity'},{'fstype':'xfs','uuid':str(uuid.UUID(int=0))}]:
            self.docker.return_value=subprocess.CompletedProcess([],0,stdout=json.dumps({'filesystems':[dict(row,target=str(self.root))]}))
            with self.assertRaises(ValueError):original(self.root)
        identity=str(uuid.uuid4())
        self.docker.return_value=subprocess.CompletedProcess([],0,stdout=json.dumps({'filesystems':[{'target':str(self.root),'fstype':'xfs','uuid':identity.upper()}]}))
        self.assertEqual(original(self.root),identity)
        self.assertEqual(self.docker.call_args.args[0][-1],'TARGET,FSTYPE,UUID')
        for target in [str(self.root.parent),str(self.root/'different'),None]:
            self.docker.return_value=subprocess.CompletedProcess([],0,stdout=json.dumps({'filesystems':[{'target':target,'fstype':'xfs','uuid':identity}]}))
            with self.assertRaisesRegex(ValueError,'dedicated_xfs_identity_required'):original(self.root)
        self.docker.return_value=subprocess.CompletedProcess([],0,stdout='x'*65537)
        with self.assertRaisesRegex(ValueError,'inventory_limit'):original(self.root)

    def test_tampered_claim_schema_or_digest_refuses(self):
        a.enroll(self.one,self.config)
        path=self.root/'state'/'ledger.json';value=a.read(path)
        entry=next(iter(value['claims'].values()));entry['claim']['memory_mb']=1
        entry['claim_sha256']=a.digest(entry['claim']);self.write(path,value)
        with self.assertRaisesRegex(a.AllocationError,'ledger_invalid'):a.inventory(self.config)


if __name__=='__main__':unittest.main()
