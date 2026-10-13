import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import allocation as a
import cutover as c
from public_course import render


class CutoverTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name).resolve()
        self.config=self.root/'policy.json';self.policy={'version':1,'worker_id':'synthetic-worker','memory_budget_mb':8192,'cpu_budget':3,
            'host_reserve_mb':2048,'host_reserve_cpu':1,'state_directory':str(self.root/'state'),'owner':'Synthetic operator'}
        self.write(self.config,self.policy)
        self.info={'OSType':'linux','ID':'fixture-daemon','NCPU':4,'MemTotal':12*1024**3,'Architecture':'x86_64'}
        self.reports={}
        for name,value in [('allocation.AUTHORITY',self.root/'authority-parent'/'worker-allocation'),('allocation.ROOT_UID',os.geteuid())]:
            p=patch(name,value);p.start();self.addCleanup(p.stop)
        for name,kw in [('allocation.daemon',{'side_effect':lambda:(self.info,a.digest(self.info['ID']))}),
                        ('allocation.home_filesystem_uuid',{'side_effect':lambda root:str(uuid.uuid5(uuid.NAMESPACE_URL,str(root)))}),
                        ('deployment._inspect_course',{'return_value':{},'create':True}),
                        ('allocation.subprocess.run',{'side_effect':self.docker_command}),
                        ('deployment.docker_json',{'return_value':[{'Id':'sha256:'+'a'*64,'Os':'linux','Architecture':'amd64'}]}),
                        ('homes.inspect',{'side_effect':lambda root,instance:self.reports[instance]})]:
            p=patch(name,**kw);p.start();self.addCleanup(p.stop)
        self.source,self.es=self.course('source',18001)
        self.destination,self.ed=self.course('receiver',18002)
        self.peer,self.ep=self.course('peer',18003,logical='peer')
        a.enroll(self.source,self.config);a.enroll(self.peer,self.config)
        self.prepare_restore(self.source,self.destination)
        self.plan=self.root/'cutover-plan.json'

    def docker_command(self,args,**kwargs):
        if args[:4]==['docker','volume','ls','-q']:
            for instance,report in self.reports.items():
                if 'name=^'+instance+'-home-' in args or 'label=educloud.workspace.instance='+instance in args:
                    return subprocess.CompletedProcess([],0,stdout=''.join(name+'\n' for name in sorted(report['volumes'])))
        return subprocess.CompletedProcess([],0,stdout='')

    def write(self,path,value):path.write_text(json.dumps(value));path.chmod(0o600)

    def course(self,instance,port,logical='course'):
        directory=self.root/instance;directory.mkdir(mode=0o700)
        home=self.root/(instance+'-homes');home.mkdir(mode=0o700)
        _,_,env=render(logical+'.example.org','https://id.example.org/realms/'+logical,logical,instance,['alice-id'],str(home))
        env.update(WORKSPACE_ENV_FILE=str(directory/'.env'),WORKSPACE_PORT=str(port),WORKSPACE_MEMORY_MB='512',
                   WORKSPACE_CPU_LIMIT='0.25',WORKSPACE_ACTIVE_LIMIT='1',WORKSPACE_OIDC_CLIENT_SECRET='synthetic-secret')
        self.write_env(directory,env)
        volumes={}
        for subject in ('alice-id','removed-bob'):
            name=instance+'-home-'+hashlib.sha256(subject.encode()).hexdigest()[:24]
            path=home/name;path.mkdir(mode=0o700);(path/'work').mkdir(mode=0o700)
            (path/'work'/'saved.txt').write_text('saved-'+subject);(path/'work'/'saved.txt').chmod(0o600)
            (path/'.cache').mkdir(mode=0o700);(path/'.cache'/'runtime').write_text('retained hidden state')
            (path/'link').symlink_to('../../outside-sensitive-file')
            volumes[name]={'quota_bytes':5120*1024**2,'inode_limit':100000,'project_id':len(volumes)+1}
        self.reports[instance]={'volumes':volumes}
        return directory,env

    def write_env(self,directory,env):
        (directory/'.env').write_text(''.join(k+'='+v+'\n' for k,v in env.items()));(directory/'.env').chmod(0o600)

    def prepare_restore(self,source,destination):
        self.write(destination/a.REQUIRED,{'version':'educloud-worker-allocation-required-v1','source':'snapshot_restore'})
        self.write(destination/'source-worker-allocation.json',a.read_marker(source))
        self.write(destination/'recovery-source.json',{'snapshot_id':'a'*64,'source_instance':c.environment(source)[1]['WORKSPACE_INSTANCE'],
                   'fresh_hub_sessions':True,'current_roster_required':True})

    def state(self):return a.read(self.root/'state'/'ledger.json')
    def preview(self):return c.preview(self.source,self.destination,self.plan)
    def forward(self):self.preview();return c.apply(self.source,self.destination,self.plan)

    def test_forward_keeps_one_reservation_and_preserves_peer_and_retained_removed_home(self):
        before=self.state();peer=before['claims'][a.path_digest(self.peer)]
        result=self.forward();after=self.state()
        self.assertEqual(result['status'],'COMPLETE');self.assertFalse(result['deployment_started']);self.assertEqual(result['retained_homes'],2)
        self.assertEqual(after['claims'][a.path_digest(self.peer)],peer)
        self.assertEqual(a.inventory(self.config)['memory_reserved_mb'],3072)
        self.assertEqual(a.inventory(self.config)['transferred_courses'],1)
        self.assertEqual(a.inventory(self.config)['released_courses'],0)
        self.assertEqual(after['claims'][a.path_digest(self.source)]['status'],'TRANSFERRED')
        self.assertEqual(c.completed_receipt(self.source)['role'],'REPLACED')
        self.assertEqual(c.completed_receipt(self.destination)['role'],'ACTIVE')
        self.assertFalse((self.destination/a.REQUIRED).exists())
        self.assertFalse((self.source/c.PENDING).exists());self.assertTrue((self.source/c.FENCE).exists())
        self.assertEqual(a.check(self.destination,c.environment(self.destination)[1])['active_courses'],2)
        self.assertEqual(c.apply(self.source,self.destination,self.plan)['status'],'COMPLETE')

    def test_forward_rollback_forward_roundtrip_uses_new_plans_and_acyclic_lineage(self):
        self.forward();original=self.plan
        rollback=self.root/'rollback.json';c.rollback_preview(self.destination,self.source,rollback);c.apply(self.destination,self.source,rollback)
        self.assertFalse((self.source/c.FENCE).exists());self.assertTrue((self.destination/c.FENCE).exists())
        self.assertEqual(self.state()['claims'][a.path_digest(self.source)]['status'],'ACTIVE')
        self.assertEqual(self.state()['claims'][a.path_digest(self.destination)]['successor'],a.path_digest(self.source))
        with self.assertRaises(c.CutoverError):c.apply(self.source,self.destination,original)
        self.plan=self.root/'forward-again.json';self.forward()
        self.assertEqual(len(set(a.lineage_roots(self.state()['claims']).values())),2)

    def test_deleted_receipts_and_fence_do_not_restore_source_admission_or_replay(self):
        self.forward();(self.source/c.FENCE).unlink()
        with self.assertRaises(a.AllocationError):a.check(self.source,c.environment(self.source)[1])
        with self.assertRaisesRegex(a.AllocationError,'transferred_course_cannot_enroll'):a.enroll(self.source,self.config)
        with self.assertRaisesRegex(a.AllocationError,'transferred_course_cannot_release'):a.release(self.source)
        (self.source/c.RECEIPT).unlink()
        with self.assertRaisesRegex(c.CutoverError,'completed_cutover_history_required'):c.apply(self.source,self.destination,self.plan)

    def test_ordinary_receiver_enrollment_and_new_unrelated_identity_collision_refuse(self):
        with self.assertRaisesRegex(a.AllocationError,'identity_or_storage_collision'):a.enroll(self.destination,self.config)
        self.forward()
        third,env=self.course('third',18004)
        with self.assertRaisesRegex(a.AllocationError,'identity_or_storage_collision'):a.enroll(third,self.config)
        a.release(self.destination);a.enroll(self.destination,self.config)
        self.assertEqual(a.inventory(self.config)['active_courses'],2)

    def test_old_allocator_rejects_transferred_schema(self):
        self.forward()
        import subprocess as sp
        process=sp.Popen(['git','show','8eb31be151fc896c6ccb7dfad28b4077a8b6100d:course-workspace/allocation.py'],cwd=Path(c.__file__).resolve().parents[1],stdout=sp.PIPE,stderr=sp.PIPE,text=True)
        source,error=process.communicate(timeout=10);self.assertEqual(process.returncode,0,error)
        namespace={'__name__':'old_allocator'};exec(compile(source,'old_allocator.py','exec'),namespace)
        namespace['AUTHORITY']=a.AUTHORITY;namespace['ROOT_UID']=os.geteuid()
        marker=a.read_marker(self.destination);p,ph=a.policy(self.config);_,dh=a.daemon()
        with self.assertRaises(ValueError):namespace['ledger'](a.binding(self.config,p,ph,dh))

    def test_repeated_replacement_and_immediate_rollback_preserve_ancestors(self):
        self.forward()
        third,env=self.course('third',18004);self.prepare_restore(self.destination,third)
        plan=self.root/'second-replacement.json';c.preview(self.destination,third,plan);c.apply(self.destination,third,plan)
        roots=a.lineage_roots(self.state()['claims'])
        self.assertEqual(roots[a.path_digest(self.source)],a.path_digest(third))
        back=self.root/'back-to-second.json';c.rollback_preview(third,self.destination,back);c.apply(third,self.destination,back)
        roots=a.lineage_roots(self.state()['claims'])
        self.assertEqual(roots[a.path_digest(self.source)],a.path_digest(self.destination))
        self.assertEqual(roots[a.path_digest(third)],a.path_digest(self.destination))

    def test_every_retained_home_byte_hidden_state_mode_and_literal_link_target_matters(self):
        root=Path(self.ed['WORKSPACE_HOME_ROOT']);removed=next(root/name for name in self.reports['receiver']['volumes'] if name.endswith(hashlib.sha256(b'removed-bob').hexdigest()[:24]))
        targets=[removed/'work'/'saved.txt',removed/'.cache'/'runtime']
        for path in targets:
            old=path.read_bytes();path.write_bytes(old+b' changed')
            with self.assertRaisesRegex(c.CutoverError,'retained_home_trees_differ'):self.preview()
            path.write_bytes(old)
        path=removed/'work'/'saved.txt';mode=stat.S_IMODE(path.stat().st_mode);path.chmod(0o640)
        with self.assertRaisesRegex(c.CutoverError,'retained_home_trees_differ'):self.preview()
        path.chmod(mode);(removed/'link').unlink();(removed/'link').symlink_to('different-literal-target')
        with self.assertRaisesRegex(c.CutoverError,'retained_home_trees_differ'):self.preview()

    def test_symlink_outside_content_is_not_followed_and_special_files_refuse(self):
        outside=self.root/'outside-sensitive-file';outside.write_text('private original')
        before=c.tree_evidence(Path(self.es['WORKSPACE_HOME_ROOT']),'source');outside.write_text('private changed')
        after=c.tree_evidence(Path(self.es['WORKSPACE_HOME_ROOT']),'source');self.assertEqual(before,after)
        home=Path(self.es['WORKSPACE_HOME_ROOT'])/next(iter(self.reports['source']['volumes']))
        os.mkfifo(home/'fifo')
        with self.assertRaisesRegex(c.CutoverError,'retained_home_special_file'):c.tree_evidence(Path(self.es['WORKSPACE_HOME_ROOT']),'source')

    def test_rollback_refuses_any_replacement_only_work_or_hidden_changes(self):
        self.forward();home=Path(self.ed['WORKSPACE_HOME_ROOT'])/next(iter(self.reports['receiver']['volumes']))
        (home/'work'/'replacement-only.txt').write_text('cannot silently lose this work')
        with self.assertRaisesRegex(c.CutoverError,'retained_home_trees_differ'):c.rollback_preview(self.destination,self.source,self.root/'rollback.json')

    def test_stale_content_and_peer_lease_approval_refuse_before_journals(self):
        self.preview();home=Path(self.es['WORKSPACE_HOME_ROOT'])/next(iter(self.reports['source']['volumes']))
        (home/'work'/'saved.txt').write_text('changed after approval')
        with self.assertRaisesRegex(c.CutoverError,'cutover_retained_state_changed'):c.apply(self.source,self.destination,self.plan)
        self.assertFalse((self.source/c.PENDING).exists())

    def test_peer_changed_initial_ledger_is_stale(self):
        self.preview();a.release(self.peer)
        with self.assertRaisesRegex(c.CutoverError,'stale_cutover_approval'):c.apply(self.source,self.destination,self.plan)
        self.assertFalse((self.source/c.PENDING).exists())

    def test_identity_resources_roster_quota_image_and_recovery_provenance_must_match(self):
        for change,code in [({'WORKSPACE_OIDC_CLIENT_ID':'different'},'exact_logical_identity_required'),
                            ({'WORKSPACE_ACTIVE_LIMIT':'2'},'matching_cutover_resources_required'),
                            ({'WORKSPACE_ALLOWED_SUBJECTS':'removed-bob'},'current_roster_mismatch')]:
            self.write_env(self.destination,dict(self.ed,**change))
            with self.assertRaisesRegex(c.CutoverError,code):self.preview()
        self.write_env(self.destination,self.ed)
        value=a.read(self.destination/'recovery-source.json');self.write(self.destination/'recovery-source.json',dict(value,source_instance='other'))
        with self.assertRaisesRegex(c.CutoverError,'exact_source_recovery_required'):self.preview()

    def test_live_foreign_writer_blocks_both_participants(self):
        with patch('allocation.stopped',side_effect=a.AllocationError('stop_all_home_volume_writers')):
            with self.assertRaisesRegex(c.CutoverError,'stop_all_home_volume_writers'):self.preview()
        self.assertFalse(self.plan.exists())

    def test_tree_and_record_limits_are_bounded(self):
        with patch('cutover.MAX_ENTRIES',2):
            with self.assertRaisesRegex(c.CutoverError,'retained_home_entry_limit'):self.preview()
        with patch('cutover.MAX_FILE',1):
            with self.assertRaisesRegex(c.CutoverError,'retained_home_byte_limit'):self.preview()

    def test_ledger_cycles_missing_successors_and_physical_collision_refuse(self):
        self.forward();original=self.state();ks,kd=a.path_digest(self.source),a.path_digest(self.destination)
        for transform in ('cycle','missing','physical','wrong-identity'):
            value=copy.deepcopy(original)
            if transform=='cycle':value['claims'][kd]=c.entry(value['claims'][kd]['claim'],'TRANSFERRED',ks)
            if transform=='missing':value['claims'][ks]['successor']='f'*64
            if transform=='physical':
                row=value['claims'][kd];row['claim']['home_filesystem_uuid']=value['claims'][ks]['claim']['home_filesystem_uuid'];row['claim_sha256']=a.digest(row['claim'])
            if transform=='wrong-identity':
                row=value['claims'][kd];row['claim']['client_id']='wrong';row['claim_sha256']=a.digest(row['claim'])
            with self.assertRaises(a.AllocationError):a.validate_claims(value['claims'])

    def test_live_apply_refuses_before_any_retained_state_hash_or_quota_read(self):
        self.preview()
        with patch('allocation.stopped',side_effect=a.AllocationError('stop_course_before_allocation_change')),             patch('cutover.tree_evidence') as tree,patch('cutover.r.runtime_binding') as runtime:
            with self.assertRaisesRegex(c.CutoverError,'stop_course_before_allocation_change'):c.apply(self.source,self.destination,self.plan)
            tree.assert_not_called();runtime.assert_not_called()
        self.assertFalse((self.source/c.PENDING).exists())

    def test_active_receiver_resource_amendment_and_peer_admission_survive_lineage(self):
        import resource_amendment as resource
        self.forward();(self.source/c.FENCE).unlink()
        self.assertEqual(a.check(self.peer,self.ep)['active_courses'],2)
        plan=self.root/'receiver-resources.json'
        resource.preview(self.destination,plan,512,0.25,2);resource.apply(self.destination,plan)
        self.assertEqual(a.inventory(self.config)['memory_reserved_mb'],3584)
        self.assertEqual(a.check(self.destination,c.environment(self.destination)[1])['active_courses'],2)
        self.assertEqual(c.completed_receipt(self.destination)['role'],'ACTIVE')
        c.validate_receipt(a.read(self.destination/c.RECEIPT))

    def test_full_tree_late_drift_does_not_clear_receiver_gate(self):
        self.preview();original=a.write
        home=Path(self.ed['WORKSPACE_HOME_ROOT'])/next(iter(self.reports['receiver']['volumes']))
        def late_drift(path,value):
            result=original(path,value)
            if path.name==c.RECEIPT and path.parent==self.destination:(home/'work'/'late.txt').write_text('late writer drift')
            return result
        with patch('allocation.write',side_effect=late_drift):
            with self.assertRaisesRegex(c.CutoverError,'cutover_retained_state_changed'):c.apply(self.source,self.destination,self.plan)
        self.assertTrue((self.source/c.PENDING).exists());self.assertTrue((self.destination/c.PENDING).exists())
        self.assertTrue((self.destination/a.REQUIRED).exists())

    def test_orphan_physical_home_missing_home_and_symlink_membership_refuse(self):
        root=Path(self.es['WORKSPACE_HOME_ROOT']);orphan=root/('source-home-'+'f'*24)
        orphan.mkdir();(orphan/'saved.txt').write_text('unregistered retained work')
        with self.assertRaisesRegex(c.CutoverError,'retained_home_membership_mismatch'):self.preview()
        (orphan/'saved.txt').unlink();orphan.rmdir();orphan.symlink_to(self.root/'outside',target_is_directory=True)
        with self.assertRaisesRegex(c.CutoverError,'retained_home_membership_mismatch'):self.preview()
        orphan.unlink()
        name=next(iter(self.reports['source']['volumes']));old=root/name;renamed=root/'temporarily-missing';old.rename(renamed)
        with self.assertRaisesRegex(c.CutoverError,'retained_home_membership_mismatch'):self.preview()

    def test_orphan_owned_or_named_home_docker_volume_refuses(self):
        original=self.docker_command
        for mode in ('name','label'):
            def orphan(args,**kwargs):
                result=original(args,**kwargs)
                selected='name=^source-home-' in args if mode=='name' else 'label=educloud.workspace.instance=source' in args
                return subprocess.CompletedProcess([],0,stdout=result.stdout+('source-home-'+'f'*24+'\n' if selected else ''))
            with patch('allocation.subprocess.run',side_effect=orphan):
                with self.assertRaisesRegex(c.CutoverError,'retained_home_volume_membership_mismatch'):self.preview()
        self.assertFalse(self.plan.exists())

    def test_receiver_only_uid_gid_difference_refuses_full_home_equivalence(self):
        root=Path(self.ed['WORKSPACE_HOME_ROOT']);home=root/next(iter(self.reports['receiver']['volumes']))
        target=home/'work'/'saved.txt';identity=target.stat().st_ino
        real_stat=os.stat;real_fstat=os.fstat
        from types import SimpleNamespace
        def altered(info):
            if info.st_ino!=identity:return info
            return SimpleNamespace(**{key:(info.st_uid+1 if key=='st_uid' else info.st_gid+1 if key=='st_gid' else getattr(info,key))
                for key in ('st_dev','st_ino','st_mode','st_uid','st_gid','st_size','st_mtime_ns','st_ctime_ns')})
        with patch('cutover.os.stat',side_effect=lambda *args,**kwargs:altered(real_stat(*args,**kwargs))),             patch('cutover.os.fstat',side_effect=lambda *args,**kwargs:altered(real_fstat(*args,**kwargs))):
            with self.assertRaisesRegex(c.CutoverError,'retained_home_trees_differ'):self.preview()
        self.assertFalse(self.plan.exists())

    def test_name_enumeration_stops_at_budget_plus_one(self):
        from types import SimpleNamespace
        consumed=[]
        class Entries:
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def __iter__(self):
                for index in range(100):
                    consumed.append(index);yield SimpleNamespace(name=str(index))
        with patch('cutover.os.scandir',return_value=Entries()):
            with self.assertRaisesRegex(c.CutoverError,'retained_home_entry_limit'):c.bounded_names(123,3)
        self.assertEqual(consumed,[0,1,2,3])

    def test_deleted_pair_history_after_repeated_cycle_cannot_replay_approval(self):
        self.forward()
        back=self.root/'rollback-first.json';c.rollback_preview(self.destination,self.source,back);c.apply(self.destination,self.source,back)
        replay=self.root/'second-forward.json';c.preview(self.source,self.destination,replay);c.apply(self.source,self.destination,replay)
        again=self.root/'rollback-second.json';c.rollback_preview(self.destination,self.source,again);c.apply(self.destination,self.source,again)
        (self.source/c.RECEIPT).unlink();(self.destination/c.RECEIPT).unlink()
        with self.assertRaisesRegex(c.CutoverError,'completed_cutover_history_required'):c.apply(self.source,self.destination,replay)
        self.assertFalse((self.source/c.PENDING).exists());self.assertFalse((self.destination/c.PENDING).exists())
        self.assertEqual(self.state()['claims'][a.path_digest(self.source)]['status'],'ACTIVE')

    def test_interrupt_at_each_published_phase_resumes_safe_transfer(self):
        for phase in ('first-journal','second-journal','fence','source-env','dest-env','ledger','marker','first-receipt','second-receipt','receiver-open','first-clear','second-clear'):
            with self.subTest(phase=phase):
                # Restore this fixture to source ACTIVE between cases through a
                # reviewed rollback, preserving real ledger/receipt history.
                if self.state()['claims'][a.path_digest(self.source)]['status']!='ACTIVE':
                    back=self.root/('back-'+phase+'.json');c.rollback_preview(self.destination,self.source,back);c.apply(self.destination,self.source,back)
                self.plan=self.root/('fault-'+phase+'.json');self.preview()
                original=a.write;original_env=c.r.write_environment;original_remove=c.remove
                counts={'journal':0,'receipt':0,'env':0,'clear':0}
                def fault(path,value):
                    if path.name==c.PENDING:counts['journal']+=1
                    if path.name==c.RECEIPT:counts['receipt']+=1
                    selected=(phase=='first-journal' and path.name==c.PENDING and counts['journal']==1
                              or phase=='second-journal' and path.name==c.PENDING and counts['journal']==2
                              or phase=='fence' and path.name==c.FENCE or phase=='ledger' and path.name=='ledger.json'
                              or phase=='marker' and path.name==a.MARKER or phase=='first-receipt' and path.name==c.RECEIPT and counts['receipt']==1
                              or phase=='second-receipt' and path.name==c.RECEIPT and counts['receipt']==2)
                    if selected:raise OSError('synthetic interruption')
                    return original(path,value)
                def env_fault(path,raw):
                    counts['env']+=1
                    if phase=='source-env' and counts['env']==1 or phase=='dest-env' and counts['env']==2:raise OSError('synthetic interruption')
                    return original_env(path,raw)
                def remove_fault(path):
                    if path.name==c.PENDING:counts['clear']+=1
                    if phase=='receiver-open' and path.name in (c.FENCE,a.REQUIRED) or phase=='first-clear' and path.name==c.PENDING and counts['clear']==1 or phase=='second-clear' and path.name==c.PENDING and counts['clear']==2:raise OSError('synthetic interruption')
                    return original_remove(path)
                with patch('allocation.write',side_effect=fault),patch('cutover.r.write_environment',side_effect=env_fault),patch('cutover.remove',side_effect=remove_fault):
                    with self.assertRaises(c.CutoverError):c.apply(self.source,self.destination,self.plan)
                self.assertEqual(a.inventory(self.config)['memory_reserved_mb'],3072)
                c.resume(self.source,self.destination,self.plan)
                self.assertEqual(self.state()['claims'][a.path_digest(self.destination)]['status'],'ACTIVE')
                self.assertFalse((self.source/c.PENDING).exists());self.assertFalse((self.destination/c.PENDING).exists())


if __name__=='__main__':unittest.main()
