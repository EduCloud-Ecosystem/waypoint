"""Bounded cutover evidence, trusted relay isolation, and partial cleanup."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import verify_cutover as r


def report():
    initial=dict(active_courses=2,released_courses=0,transferred_courses=0,memory_reserved_mb=3072,cpu_reserved=2.5,
                 memory_budget_mb=8192,cpu_budget=2.75,host_reserve_mb=4096,host_reserve_cpu=1.25)
    transferred=dict(initial,transferred_courses=1)
    return dict({key:'a'*64 for key in r.REPORT_HASHES},waypoint_revision='a'*40,
        source_manifests_sha256=['b'*64,'c'*64],images={'hub':'sha256:'+'d'*64,'course':'sha256:'+'e'*64},
        initial_allocation=initial,pending_allocation=copy.deepcopy(initial),transferred_allocation=transferred,
        rollback_allocation=copy.deepcopy(transferred),final_allocation=copy.deepcopy(transferred),checks=sorted(r.CHECKS))


class CutoverEvidenceTests(unittest.TestCase):
    def test_all_controls_and_verified_cleanup_required_for_exclusive_private_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            for check in r.CHECKS:
                value=report(); value['checks'].remove(check)
                with self.subTest(check=check),self.assertRaises(ValueError): r.publish_pass(root,value,True)
            for cleanup in (False,None,1):
                with self.assertRaises(ValueError): r.publish_pass(root,report(),cleanup)
            self.assertEqual(list(root.iterdir()),[])
            r.publish_pass(root,report(),True)
            path=root/'CUTOVER-PASS.json'; raw=path.read_bytes(); value=json.loads(raw)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertFalse(value['apply_started_services'])
            self.assertTrue(value['rollback_completed_before_receiver_use'])
            self.assertFalse(value['rollback_after_receiver_divergence'])
            self.assertTrue(value['closed_history_recovery_only'])
            with self.assertRaises(FileExistsError): r.publish_pass(root,report(),True)
            self.assertEqual(path.read_bytes(),raw)

    def test_private_values_mutable_images_missing_peer_false_transfers_and_nonfinite_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); cases=[]
            for key in ('environment','ledger','plan','receipt','revision','owner','roster','relay_gateway','path'):
                value=report(); value[key]='private'; cases.append(value)
            value=report(); value['source_manifests_sha256'][1]=value['source_manifests_sha256'][0]; cases.append(value)
            value=report(); value['images']['hub']='hub:latest'; cases.append(value)
            for key,field,number in (('initial_allocation','transferred_courses',1),('pending_allocation','active_courses',1),
                                    ('transferred_allocation','transferred_courses',0),('rollback_allocation','released_courses',1),
                                    ('final_allocation','memory_reserved_mb',1536),('final_allocation','cpu_reserved',3),
                                    ('final_allocation','host_reserve_cpu',1),('final_allocation','active_courses',True),
                                    ('final_allocation','cpu_reserved',float('nan')),('final_allocation','cpu_reserved',float('inf')),
                                    ('final_allocation','memory_reserved_mb',10**1000)):
                value=report(); value[key][field]=number; cases.append(value)
            value=report(); value['checks'].append(value['checks'][0]); cases.append(value)
            for value in cases:
                with self.subTest(value=value),self.assertRaises(ValueError): r.publish_pass(root,value,True)
            self.assertEqual(list(root.iterdir()),[])

    def test_publication_failure_has_no_pass_or_scratch(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            with patch.object(r.os,'link',side_effect=OSError('synthetic disk failure')):
                with self.assertRaises(OSError): r.publish_pass(root,report(),True)
            self.assertEqual(list(root.iterdir()),[])

    def test_partial_initial_start_always_cleans_and_never_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); first=Mock(); peer=Mock()
            first.start.side_effect=RuntimeError('partial fixture start')
            argv=['verify_cutover.py','--output',str(root/'run')]
            with patch.object(r.sys,'argv',argv),patch.object(r.sys,'platform','linux'),patch.object(r.os,'geteuid',return_value=0), \
                    patch.object(r,'AUTHORITY',root/'authority'),patch.object(r,'_source',return_value={'dirty':False,'revision':'a'*40}), \
                    patch.object(r,'sha256_file',return_value='a'*64),patch.object(r,'Notebook',side_effect=[first,peer]), \
                    patch.object(r,'cleanup_cutover',return_value=True) as cleanup,patch.object(r,'publish_pass') as publish:
                with self.assertRaises(RuntimeError): r.main()
            cleanup.assert_called_once_with(first,peer,root/'run',root/'run/worker-policy.json')
            peer.start.assert_not_called(); publish.assert_not_called()

    def test_existing_authority_and_optimized_python_refuse_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); authority=root/'authority'; authority.mkdir(); output=root/'run'
            with patch.object(r.sys,'argv',['verify_cutover.py','--output',str(output)]),patch.object(r.sys,'platform','linux'), \
                    patch.object(r.os,'geteuid',return_value=0),patch.object(r,'AUTHORITY',authority),patch.object(r,'Notebook') as fixture:
                with self.assertRaises(SystemExit): r.main()
            fixture.assert_not_called(); self.assertFalse(output.exists())
            result=subprocess.run([sys.executable,'-O',r.__file__,'--output',str(output)],capture_output=True,text=True,timeout=30,
                                  env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
            self.assertNotEqual(result.returncode,0); self.assertRegex(result.stderr,r'optimized Python|Python assertions')
            self.assertFalse(output.exists())


class CutoverFixtureTests(unittest.TestCase):
    def test_fence_loss_proves_exact_transferred_lease_refusals_and_restores_fence_on_failure(self):
        import allocation
        for failed in (False,True):
            with self.subTest(failed=failed),tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve(); fence=root/'.course-replaced.json'; fence.write_bytes(b'private fence')
                for name in ('.env',allocation.MARKER,'.cutover-receipt.json','ledger.json'):
                    (root/name).write_bytes(b'private retained state')
                source=types.SimpleNamespace(deployment=root,env_file=root/'.env')
                cut=types.SimpleNamespace(FENCE=fence.name,RECEIPT='.cutover-receipt.json')
                def refusal(code):
                    def operation(*args):
                        self.assertFalse(fence.exists())
                        raise allocation.AllocationError(code)
                    return operation
                with patch.object(r,'read_environment',return_value={}),patch.object(r,'assert_stopped'), \
                        patch.object(allocation,'check',side_effect=refusal('other' if failed else 'active_course_reservation_required')), \
                        patch.object(allocation,'enroll',side_effect=refusal('transferred_course_cannot_enroll')) as enroll, \
                        patch.object(allocation,'release',side_effect=refusal('transferred_course_cannot_release')) as release, \
                        patch.object(allocation,'worker_lock',side_effect=AssertionError('outer lock obscures actual refusal')):
                    if failed:
                        with self.assertRaises(AssertionError): r.fence_loss_control(cut,source,root/'policy.json',root/'ledger.json')
                        enroll.assert_not_called(); release.assert_not_called()
                    else:
                        r.fence_loss_control(cut,source,root/'policy.json',root/'ledger.json')
                        enroll.assert_called_once(); release.assert_called_once()
                self.assertEqual(fence.read_bytes(),b'private fence')
                self.assertEqual(fence.stat().st_mode & 0o777,0o600)

    def test_missing_history_restored_private_even_if_refusal_is_unexpected_or_control_accepted(self):
        class Error(ValueError):
            def __init__(self,code): self.code=code; super().__init__(code)
        for failure in (None,'unexpected','accepted'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve(); receipt=root/'.receipt'; receipt.write_bytes(b'completed private history'); receipt.chmod(0o600)
                env=root/'.env'; env.write_bytes(b'private env')
                source=types.SimpleNamespace(deployment=root,instance='source')
                receiver=types.SimpleNamespace(deployment=root,instance='receiver')
                def preview(*args):
                    self.assertFalse(receipt.exists())
                    if failure=='unexpected': raise OSError('backend failure')
                    if failure=='accepted': return {}
                    raise Error('completed_cutover_history_required')
                cut=types.SimpleNamespace(RECEIPT=receipt.name,CutoverError=Error,preview=preview)
                with patch.object(r,'assert_stopped'):
                    if failure:
                        with self.assertRaises((OSError,AssertionError)): r.missing_history_control(cut,source,receiver,root/'plan.json',(env,))
                    else: r.missing_history_control(cut,source,receiver,root/'plan.json',(env,))
                self.assertEqual(receipt.read_bytes(),b'completed private history')
                self.assertEqual(receipt.stat().st_mode & 0o777,0o600)
                self.assertEqual(env.read_bytes(),b'private env')

    def test_cleanup_attempts_partial_restores_source_peer_independently_and_never_deletes_authority_after_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            for name in ('receiver','closed-history'):
                (root/name).mkdir(); (root/name/'.env').write_text('private fixture')
            first=Mock(instance='rehearsal-'+'a'*10,deployment=root/'source/deployment',cleanup_verified=True)
            peer=Mock(deployment=root/'peer/deployment',cleanup_verified=True)
            first.close.return_value=True; peer.close.return_value=True
            receiver=Mock(cleanup_verified=True); receiver.close.side_effect=RuntimeError('receiver cleanup failed')
            closed=Mock(cleanup_verified=True); closed.close.return_value=True
            with patch.object(r,'read_environment',side_effect=[{'WORKSPACE_INSTANCE':'recovered-'+'b'*12},{'WORKSPACE_INSTANCE':'recovered-'+'c'*12}]), \
                    patch.object(r,'restored_view',side_effect=[receiver,closed]),patch.object(r.os.path,'ismount',return_value=True), \
                    patch.object(r,'xfs_run') as unmount,patch.object(r,'docker',return_value=''),patch.object(r,'cleanup_worker') as authority:
                with self.assertRaises(RuntimeError): r.cleanup_cutover(first,peer,root,root/'worker-policy.json')
            receiver.close.assert_called_once(); closed.close.assert_called_once()
            first.close.assert_called_once(); peer.close.assert_called_once()
            self.assertEqual(unmount.call_count,2); authority.assert_not_called()

    def test_cleanup_authority_only_after_every_namespace_absent_with_three_ledger_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); (root/'receiver').mkdir(); (root/'receiver/.env').write_text('private')
            first=Mock(instance='rehearsal-'+'a'*10,deployment=root/'source/deployment',cleanup_verified=True)
            peer=Mock(deployment=root/'peer/deployment',cleanup_verified=True)
            first.close.return_value=True; peer.close.return_value=True
            receiver=Mock(cleanup_verified=True); receiver.close.return_value=True
            with patch.object(r,'read_environment',return_value={'WORKSPACE_INSTANCE':'recovered-'+'b'*12}), \
                    patch.object(r,'restored_view',return_value=receiver),patch.object(r.os.path,'ismount',return_value=False), \
                    patch.object(r,'docker',return_value=''),patch.object(r,'cleanup_worker',return_value=True) as authority:
                self.assertTrue(r.cleanup_cutover(first,peer,root,root/'worker-policy.json'))
            authority.assert_called_once_with(root/'worker-policy.json',[first.deployment,peer.deployment,root/'receiver'])

    def test_relay_uses_owned_bridge_loopback_only_pinned_unprivileged_bounded_mountless_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); instance='rehearsal-'+'a'*10; image='sha256:'+'b'*64
            first=types.SimpleNamespace(instance=instance,image=image,network=instance+'-fixture',root=root)
            receiver=types.SimpleNamespace(env={'WORKSPACE_PORT':'12345'})
            (root/'nginx.conf').write_text('proxy_pass http://'+instance+'-hub:8000; proxy_pass http://identity:8080;')
            calls=[]
            def docker(*args):
                calls.append(args)
                if args[:2]==('network','inspect'):
                    return json.dumps([{'Name':first.network,'Driver':'bridge','Labels':{'educloud.rehearsal':instance},
                                        'IPAM':{'Config':[{'Gateway':'172.19.0.1'}]}}])
                if args[0]=='ps': return ''
                if args[0]=='run': return 'c'*64
                if args[0]=='inspect':
                    return json.dumps([{'Image':image,'State':{'Running':True},'Config':{'Labels':{'educloud.rehearsal':instance},'User':'1000:1000'},
                        'Mounts':[],'HostConfig':{'NetworkMode':'host','ReadonlyRootfs':True,'Memory':64*1024**2,'NanoCpus':50000000}}])
                return ''
            with patch.object(r,'docker',side_effect=docker),patch.object(r,'port',return_value=23456),patch.object(r,'wait_proxy') as ready:
                self.assertEqual(r.start_relay(first,receiver),'c'*64)
            launch=next(call for call in calls if call[0]=='run')
            for flag,value in (('--network','host'),('--user','1000:1000'),('--entrypoint','python3'),('--cpus','0.05'),('--pids-limit','16'),('--cap-drop','ALL')):
                self.assertEqual(launch[launch.index(flag)+1],value)
            self.assertIn('--read-only',launch); self.assertNotIn('-v',launch); self.assertNotIn('--mount',launch)
            self.assertEqual(launch[-7:],(image,'-I','-S','-c',r.RELAY_SCRIPT,'172.19.0.1','23456','12345')[-7:])
            self.assertEqual(launch[launch.index('--entrypoint')+2],image)
            self.assertIn('BoundedSemaphore(8)',r.RELAY_SCRIPT); self.assertIn('("127.0.0.1", target)',r.RELAY_SCRIPT)
            self.assertNotIn('print(',r.RELAY_SCRIPT); self.assertNotIn('open(',r.RELAY_SCRIPT)
            self.assertIn('http://172.19.0.1:23456',(root/'nginx.conf').read_text())
            self.assertIn('http://identity:8080',(root/'nginx.conf').read_text()); ready.assert_called_once_with(first)

    def test_receiver_start_uses_real_bundled_operator_path_and_retains_same_hub_after_trust_restart(self):
        fixture=types.SimpleNamespace(root=Path('/synthetic/source'),network='owned-network')
        receiver=types.SimpleNamespace(instance='recovered-'+'a'*12,homes=Path('/synthetic/homes'),deployment=Path('/synthetic/receiver'),hub_image='sha256:'+'b'*64)
        def docker(*args):
            if args[0]=='inspect': return json.dumps([{'Id':'c'*64,'Image':receiver.hub_image}])
            return ''
        with patch.object(r,'inspect') as quota,patch.object(r.subprocess,'run') as start, \
                patch.object(r,'docker',side_effect=docker) as daemon,patch.object(r,'compose') as stop:
            r.receiver_start_with_fixture_trust(fixture,receiver)
        self.assertEqual(quota.call_count,2); self.assertEqual(start.call_count,2)
        for call in start.call_args_list:
            self.assertEqual(call.args[0],[sys.executable,'/synthetic/receiver/runtime/deployment.py','start','--directory','/synthetic/receiver'])
            self.assertTrue(call.kwargs['check'])
        stop.assert_called_once_with(receiver.deployment,'stop','hub')
        self.assertTrue(any(call.args[:3]==('network','connect','owned-network') for call in daemon.call_args_list))

    def test_restored_view_preserves_source_identity_and_does_not_mutate_source_fixture(self):
        first=types.SimpleNamespace(instance='source',env={'old':'private'},deployment=Path('/source'),homes=Path('/source/homes'),env_file=Path('/source/.env'),origin='https://original')
        with patch.object(r.os.path,'ismount',return_value=True):
            restored=r.restored_view(first,Path('/receiver'),Path('/receiver-homes'),{'WORKSPACE_INSTANCE':'recovered-'+'a'*12})
        self.assertEqual(first.instance,'source'); self.assertEqual(first.deployment,Path('/source'))
        self.assertEqual(restored.origin,first.origin); self.assertEqual(restored.homes,Path('/receiver-homes'))
        self.assertTrue(restored.mounted)

    def test_peer_proof_requires_unchanged_identity_python_and_saved_r_bytes_with_real_r_execution(self):
        fixture=types.SimpleNamespace(kernel=Mock()); session=Mock()
        session.get.return_value=types.SimpleNamespace(status_code=200,json=lambda:{'content':'peer R saved\n'})
        expected={'course.py':'peer Python saved\n','course-r.txt':'peer R saved\n'}
        with patch.object(r,'same_peer') as base:
            r.verify_peer(fixture,session,'https://peer/user/subject',expected,{'learner'},'hub')
        base.assert_called_once_with(fixture,session,'https://peer/user/subject',expected,{'learner'},'hub')
        self.assertEqual(fixture.kernel.call_args.args[2],'ir')
        self.assertIn('readLines("course-r.txt")',fixture.kernel.call_args.args[3])
        session.get.return_value=types.SimpleNamespace(status_code=200,json=lambda:{'content':'changed'})
        fixture.kernel.reset_mock()
        with patch.object(r,'same_peer'),self.assertRaises(AssertionError):
            r.verify_peer(fixture,session,'https://peer/user/subject',expected,{'learner'},'hub')
        fixture.kernel.assert_not_called()


if __name__=='__main__': unittest.main()
