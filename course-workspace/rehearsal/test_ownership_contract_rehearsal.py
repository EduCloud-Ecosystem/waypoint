"""Owned processes and durable fixture fencing are independently exercised."""
import os
import json
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import verify_ownership_contract as r


def report():
    values={key:'a'*64 for key in r.REPORT_HASHES}
    values.update(waypoint_revision='a'*40,checks=sorted(r.CHECKS),initial_plan_sha256='b'*64,
        concurrent_winner_plan_sha256='c'*64,final_plan_sha256='d'*64,final_retained_sha256='e'*64)
    return values


class OwnershipEvidenceTests(unittest.TestCase):
    def test_exact_controls_private_bounded_scope_and_cleanup_required_before_exclusive_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for check in r.CHECKS:
                value=report(); value['checks'].remove(check)
                with self.subTest(check=check),self.assertRaises(ValueError): r.publish_pass(root,value,True)
            for cleanup in (False,None,1):
                with self.assertRaises(ValueError): r.publish_pass(root,report(),cleanup)
            self.assertEqual(list(root.iterdir()),[])
            r.publish_pass(root,report(),True)
            path=root/'OWNERSHIP-CONTRACT-PASS.json'; raw=path.read_bytes(); value=json.loads(raw)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(value['logical_workers'],2); self.assertEqual(value['real_worker_hosts'],0)
            self.assertIs(value['fixture_only'],True)
            for flag in ('runtime_enforcement','production_activation_adapter','provider_adapter','authenticated_remote_transport',
                         'actual_two_host_isolation','encrypted_backup_verified','offhost_backup_verified','measured_profile_enforcement'):
                self.assertIs(value[flag],False)
            self.assertEqual(value['snapshot_scope'],'opaque-local-fixture-marker')
            with self.assertRaises(FileExistsError): r.publish_pass(root,report(),True)
            self.assertEqual(path.read_bytes(),raw)

    def test_private_data_extra_flags_invalid_hashes_duplicate_controls_and_false_divergence_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); cases=[]
            for key in ('plan','receipt','ledger','authority_id','worker_id','pid','nonce','saved_content','real_worker_hosts'):
                value=report(); value[key]='private'; cases.append(value)
            value=report(); value['initial_retained_sha256']=value['final_retained_sha256']; cases.append(value)
            value=report(); value['final_plan_sha256']=value['initial_plan_sha256']; cases.append(value)
            value=report(); value['fixture_sha256']='latest'; cases.append(value)
            value=report(); value['checks'].append(value['checks'][0]); cases.append(value)
            for value in cases:
                with self.subTest(value=value),self.assertRaises(ValueError): r.publish_pass(root,value,True)
            self.assertEqual(list(root.iterdir()),[])

    def test_failed_publication_leaves_no_pass_or_scratch(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with patch.object(r.os,'link',side_effect=OSError('synthetic output failure')):
                with self.assertRaises(OSError): r.publish_pass(root,report(),True)
            self.assertEqual(list(root.iterdir()),[])

    def test_dirty_source_refuses_before_output_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'run'
            with patch.object(r.sys,'argv',['verify_ownership_contract.py','--output',str(output)]), \
                    patch.object(r,'_source',return_value={'revision':'a'*40,'dirty':True}):
                with self.assertRaises(SystemExit): r.main()
            self.assertFalse(output.exists())

    def test_complete_real_authority_process_flow_with_unit_only_provenance_stub_and_owned_cleanup(self):
        # This temporary unit packet is deleted. It is not accepted clean-source evidence.
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory).resolve()/'run'
            with patch.object(r.sys,'argv',['verify_ownership_contract.py','--output',str(output)]), \
                    patch.object(r,'_source',return_value={'revision':'a'*40,'dirty':False}):
                r.main()
            value=json.loads((output/'OWNERSHIP-CONTRACT-PASS.json').read_text())
            self.assertEqual(set(value['checks']),r.CHECKS)
            self.assertEqual(list(output.iterdir()),[output/'OWNERSHIP-CONTRACT-PASS.json'])

    def test_main_partial_owned_service_start_failure_reaps_process_and_never_publishes(self):
        original=r.FixtureBackend.start; captured=[]
        def partial(backend,*args):
            result=original(backend,*args); captured.append((backend,result))
            raise RuntimeError('synthetic failure after actual service activation')
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory).resolve()/'run'
            with patch.object(r.sys,'argv',['verify_ownership_contract.py','--output',str(output)]), \
                    patch.object(r,'_source',return_value={'revision':'a'*40,'dirty':False}), \
                    patch.object(r.FixtureBackend,'start',partial),patch.object(r,'publish_pass') as publication:
                with self.assertRaises(RuntimeError): r.main()
            self.assertEqual(len(captured),1)
            self.assertIsNotNone(captured[0][1].poll())
            self.assertTrue(captured[0][0].processes.cleanup_verified)
            self.assertEqual(list(output.iterdir()),[]); publication.assert_not_called()


class OwnershipBackendTests(unittest.TestCase):
    def test_barrier_dangling_symlink_is_not_absent_and_bounded_tree_refuses_large_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); backend=r.FixtureBackend(root/'backend')
            backend.add_worker('worker-one','provider-one'); backend.add_course('course-one','worker-one')
            try:
                barrier=backend.processes.barrier_path('worker-one','course-one'); barrier.symlink_to(root/'missing')
                with self.assertRaises(OSError): backend.inspect_course('course-one','worker-one')
                home=backend.home('course-one','worker-one')
                for index in range(1000): (home/str(index)).write_bytes(b'')
                with self.assertRaisesRegex(ValueError,'entry bound'): r.retained_digest(home)
            finally: backend.close()


class OwnershipFixtureProcessTests(unittest.TestCase):
    def test_real_services_remain_separate_and_durable_barrier_survives_source_client_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/'source'; peer=root/'peer'; target=root/'target'
            for home in (source,peer,target): home.mkdir(mode=0o700); (home/'saved.txt').write_bytes(b'synthetic retained work\n')
            worker_state=root/'source-worker-state'; worker_state.mkdir(); (worker_state/'ledger.json').write_text('synthetic worker ledger')
            processes=r.FixtureProcesses(root/'independent-revival-backend')
            try:
                source_process=processes.launch('worker-one','course-one',source)
                peer_process=processes.launch('worker-one','peer-course',peer)
                target_process=processes.launch('worker-two','course-one',target)
                self.assertEqual(processes.live('worker-one','course-one'),source_process.pid)
                self.assertEqual(processes.live('worker-one','peer-course'),peer_process.pid)
                self.assertNotEqual(source_process.pid,target_process.pid)
                with processes.activation_lock('worker-one','course-one'):
                    r.private_json(processes.barrier_path('worker-one','course-one'),{'authority_id':processes.authority_id,
                        'worker_id':'worker-one','course_id':'course-one','denied_through_epoch':1})
                    processes.stop('worker-one','course-one')
                    self.assertIsNotNone(source_process.poll())
                (worker_state/'ledger.json').unlink(); worker_state.rmdir()
                processes.launch('worker-one','course-one',source,expect_refusal=True)
                self.assertEqual(processes.live('worker-one','peer-course'),peer_process.pid)
                self.assertEqual(processes.live('worker-two','course-one'),target_process.pid)
                self.assertEqual((source/'saved.txt').read_bytes(),(target/'saved.txt').read_bytes())
            finally:
                self.assertTrue(processes.close())
            self.assertTrue(processes.cleanup_verified)
            self.assertTrue(all(child.poll() is not None for child in processes.children))

    def test_activation_lock_blocks_fresh_client_until_durable_barrier_can_be_seen(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); home=root/'home'; home.mkdir(); (home/'saved.txt').write_bytes(b'synthetic')
            processes=r.FixtureProcesses(root/'barriers'); child=None
            try:
                with processes.activation_lock('worker','course'):
                    r.private_json(processes.barrier_path('worker','course'),{'authority_id':processes.authority_id,
                        'worker_id':'worker','course_id':'course','denied_through_epoch':1})
                    child=subprocess.Popen([sys.executable,'-I','-u','-c',r.SERVICE_SCRIPT,str(home),
                        str(processes.barrier_path('worker','course')),str(processes.activation_lock_path('worker','course')),
                        'a'*32,'worker','course','1',processes.authority_id,'-1'],
                        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,start_new_session=True,bufsize=0)
                    processes.children.append(child)
                    self.assertFalse(select.select([child.stdout],[],[],0.1)[0])
                    self.assertIsNone(child.poll())
                self.assertEqual(processes.receive(child),'REVIVAL_REFUSED')
                self.assertEqual(child.wait(timeout=3),23)
            finally: processes.close()

    def test_partial_process_start_failure_still_tracks_and_reaps_exact_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); home=root/'home'; home.mkdir(); (home/'saved.txt').write_bytes(b'synthetic')
            processes=r.FixtureProcesses(root/'barriers')
            try:
                with patch.object(processes,'receive',side_effect=RuntimeError('synthetic startup reply failure')):
                    with self.assertRaises(RuntimeError): processes.launch('worker','course',home)
                self.assertEqual(len(processes.children),1)
            finally: processes.close()
            self.assertIsNotNone(processes.children[0].poll())

    def test_owned_launch_shares_parent_lock_without_deadlock_or_unlocking_it_early(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); home=root/'home'; home.mkdir(); (home/'saved.txt').write_bytes(b'synthetic')
            processes=r.FixtureProcesses(root/'barriers')
            try:
                with processes.activation_lock('worker','course'):
                    child=processes.launch('worker','course',home)
                    self.assertEqual(processes.live('worker','course'),child.pid)
                    other=os.open(processes.activation_lock_path('worker','course'),os.O_RDWR)
                    try:
                        with self.assertRaises(BlockingIOError): r.fcntl.flock(other,r.fcntl.LOCK_EX|r.fcntl.LOCK_NB)
                    finally: os.close(other)
            finally: processes.close()

    def test_later_reviewed_epoch_can_start_without_deleting_prior_barrier(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); home=root/'home'; home.mkdir(); (home/'saved.txt').write_bytes(b'synthetic')
            processes=r.FixtureProcesses(root/'barriers')
            try:
                barrier=processes.barrier_path('worker','course')
                r.private_json(barrier,{'authority_id':processes.authority_id,'worker_id':'worker','course_id':'course','denied_through_epoch':2})
                original=barrier.read_bytes()
                processes.launch('worker','course',home,epoch=2,expect_refusal=True)
                processes.launch('worker','course',home,epoch=3)
                processes.live('worker','course')
                self.assertEqual(barrier.read_bytes(),original)
            finally: processes.close()

    def test_inherited_source_lock_cannot_authorize_another_worker_or_course(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); home=root/'home'; home.mkdir(); (home/'saved.txt').write_bytes(b'synthetic')
            processes=r.FixtureProcesses(root/'barriers')
            try:
                with processes.activation_lock('source-worker','course-one'):
                    for worker,course in (('target-worker','course-one'),('source-worker','peer-course')):
                        with self.subTest(worker=worker,course=course),self.assertRaisesRegex(RuntimeError,'another endpoint'):
                            processes.launch(worker,course,home)
                self.assertEqual(processes.children,[])
            finally: processes.close()

    def test_held_endpoint_exclusion_has_bounded_refusal_without_stealing_other_owner_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            processes=r.FixtureProcesses(Path(directory)/'barriers')
            descriptor=os.open(processes.activation_lock_path('worker','course'),os.O_RDWR)
            r.fcntl.flock(descriptor,r.fcntl.LOCK_EX)
            try:
                with patch.object(r.time,'monotonic',side_effect=[0,4]),self.assertRaisesRegex(RuntimeError,'timed out'):
                    with processes.activation_lock('worker','course'): self.fail('other owner lock bypassed')
                observer=os.open(processes.activation_lock_path('worker','course'),os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError): r.fcntl.flock(observer,r.fcntl.LOCK_EX|r.fcntl.LOCK_NB)
                finally: os.close(observer)
            finally:
                r.fcntl.flock(descriptor,r.fcntl.LOCK_UN); os.close(descriptor); processes.close()

    def test_live_check_rejects_dead_service_without_boolean_substitution(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); home=root/'home'; home.mkdir(); (home/'saved.txt').write_bytes(b'synthetic')
            processes=r.FixtureProcesses(root/'barriers')
            try:
                child=processes.launch('worker','course',home); processes.reap(child)
                with self.assertRaises(AssertionError): processes.live('worker','course')
            finally: processes.close()

    def test_oversized_child_reply_refused_and_cleanup_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            processes=r.FixtureProcesses(Path(directory)/'barriers')
            child=subprocess.Popen([sys.executable,'-I','-u','-c','import time; print("x"*2048,flush=True); time.sleep(5)'],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,start_new_session=True,bufsize=0)
            processes.children.append(child)
            try:
                with self.assertRaisesRegex(RuntimeError,'exceeded bound'): processes.receive(child)
            finally: self.assertTrue(processes.close())

    def test_private_durable_barrier_write_has_no_partial_scratch_and_mode_0600(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); path=root/'barrier.json'; r.private_json(path,{'deny':True})
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            original=path.read_bytes()
            with patch.object(r.os,'replace',side_effect=OSError('synthetic disk failure')):
                with self.assertRaises(OSError): r.private_json(path,{'deny':False})
            self.assertEqual(path.read_bytes(),original); self.assertEqual(list(root.iterdir()),[path])


if __name__=='__main__': unittest.main()
