"""Local process/backend unit evidence, never clean-source/provider acceptance."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import verify_pending_reconciliation as r
import ownership_contract as contract


class ReconciliationBackendTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve();self.backend=r.ReconciliationBackend(self.root/'backend');self.addCleanup(self.backend.close)
        for worker,provider in [('worker-one','provider-one'),('worker-two','provider-two')]:self.backend.add_worker(worker,provider)
        self.backend.add_course('course','worker-one');self.backend.add_course('course','worker-two',copy_from='worker-one')
        self.backend.add_course('peer','worker-one')
        self.authority=contract.Authority.create(self.root/'authority',self.backend);self.backend.processes.authority_id=self.authority.authority_id
        for worker in ('worker-one','worker-two'):self.authority.register_worker(worker)
        self.authority.register_course('course','worker-one');self.authority.register_course('peer','worker-one')

    def pending(self):
        original=self.root/'ordinary.json';self.authority.preview('course','worker-two',original)
        with patch.object(contract,'fault_hook',side_effect=r.interrupt_at('prepared')):
            with self.assertRaises(contract.OwnershipError):self.authority.apply(original)
        return original

    def recovery(self,selected='worker-one'):
        target=self.root/(uuid.uuid4().hex+'.json');self.authority.reconcile_preview('course',selected,target)
        return target

    def assert_dual_locks(self):
        for worker in ('worker-one','worker-two'):
            descriptor=os.open(self.backend.processes.activation_lock_path(worker,'course'),os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally:os.close(descriptor)

    def test_both_backend_contexts_authorize_under_dual_locks_before_barrier_or_process_effects(self):
        self.pending();target=self.recovery();plan=json.loads(target.read_text())
        source=self.backend.processes.launch('worker-one','course',self.backend.home('course','worker-one'))
        receiver=self.backend.processes.launch('worker-two','course',self.backend.home('course','worker-two'))
        before={worker:(self.backend.home('course',worker)/'saved.txt').read_bytes() for worker in ('worker-one','worker-two')}
        for method,request in [(self.backend.fence_context,self.authority._request(plan)),
                               (self.backend.reconciliation_context,self.authority._reconciliation_request(plan))]:
            called=[]
            def refuse():
                called.append(True);self.assert_dual_locks();self.assertIsNone(source.poll());self.assertIsNone(receiver.poll())
                for worker in ('worker-one','worker-two'):self.assertFalse(self.backend.processes.barrier_path(worker,'course').exists())
                raise RuntimeError('authorize refused before effects')
            with self.assertRaisesRegex(RuntimeError,'before effects'):
                with method(request,authorize=refuse):self.fail('rejected authorization yielded')
            self.assertEqual(called,[True])
        self.assertEqual(self.backend.processes.live('worker-one','course'),source.pid)
        self.assertEqual(self.backend.processes.live('worker-two','course'),receiver.pid)
        r.endpoints_preserved(self.backend,'course',before)

    def test_second_endpoint_prewrite_failure_retains_first_and_exact_resume_publishes_second(self):
        self.pending();target=self.recovery();write=self.backend.write_barrier
        def fail_second(worker,course,record):
            if worker=='worker-two':raise RuntimeError('prewrite second endpoint')
            return write(worker,course,record)
        with patch.object(self.backend,'write_barrier',side_effect=fail_second):
            with self.assertRaises(contract.OwnershipError):self.authority.reconcile_apply(target)
        first=self.backend.processes.barrier_path('worker-one','course');first_raw=first.read_bytes()
        self.assertIsNotNone(self.backend._history('worker-one','course'))
        self.assertFalse(self.backend.processes.barrier_path('worker-two','course').exists())
        self.assertEqual(self.authority.inventory()['pending_transfers'],1)
        self.assertEqual(sum(value['count'] for value in self.authority.inventory()['reservations'].values()),3)
        result=self.authority.reconcile_resume(target)
        self.assertEqual(result['status'],'COMPLETE');self.assertEqual(first.read_bytes(),first_raw)
        self.assertIsNotNone(self.backend._history('worker-two','course'));self.assertTrue(self.backend.dual_lock_verified)
        self.assertEqual(self.authority.inventory()['pending_transfers'],0)

    def test_missing_indexed_barrier_and_mismatched_id_refuse_without_recreation(self):
        self.pending();target=self.recovery()
        with patch.object(contract,'fault_hook',side_effect=r.interrupt_at('reconciliation_fenced')):
            with self.assertRaises(contract.OwnershipError):self.authority.reconcile_apply(target)
        frozen=r.barriers(self.backend,'course')
        for worker in ('worker-one','worker-two'):
            path=self.backend.processes.barrier_path(worker,'course');saved=path.with_suffix('.held');path.rename(saved)
            try:
                with self.assertRaises(contract.OwnershipError):self.authority.reconcile_resume(target)
                self.assertFalse(path.exists());self.assertEqual(saved.read_bytes(),frozen[worker])
            finally:saved.rename(path)
        path=self.backend.processes.barrier_path('worker-two','course');changed=r.read_private(path);changed['barrier_id']=uuid.uuid4().hex;r.private_json(path,changed)
        try:
            with self.assertRaises(contract.OwnershipError):self.authority.reconcile_resume(target)
            self.assertEqual(r.read_private(path)['barrier_id'],changed['barrier_id'])
        finally:path.write_bytes(frozen['worker-two'])
        self.assertEqual(self.authority.inventory()['pending_transfers'],1)
        self.authority.reconcile_resume(target)

    def test_barrier_index_write_gap_remains_pending_and_is_never_silently_recreated(self):
        self.pending();target=self.recovery();remember=self.backend._remember
        def fail_index(worker,course,record):
            if worker=='worker-two':raise RuntimeError('barrier durable but index absent')
            return remember(worker,course,record)
        with patch.object(self.backend,'_remember',side_effect=fail_index):
            with self.assertRaises(contract.OwnershipError):self.authority.reconcile_apply(target)
        path=self.backend.processes.barrier_path('worker-two','course');raw=path.read_bytes()
        for _ in range(2):
            with self.assertRaises(contract.OwnershipError):self.authority.reconcile_resume(target)
            self.assertEqual(path.read_bytes(),raw)
        self.assertEqual(self.authority.inventory()['pending_transfers'],1)
        for worker in ('worker-one','worker-two'):
            with self.assertRaises(contract.OwnershipError):self.backend.start(self.authority,'course',worker)
        self.assertFalse((self.backend.index/(self.backend.processes.identity('worker-two','course')+'.json')).exists())

    def test_snapshot_refresh_is_append_only_and_current_selector_binds_actual_marker(self):
        directory=self.backend.workers['worker-one']/'courses/course';original=directory/'snapshot.fixture';original_raw=original.read_bytes()
        first=self.backend.refresh_snapshot('worker-one','course','first');first_raw=first.read_bytes()
        second=self.backend.refresh_snapshot('worker-one','course','second')
        self.assertNotEqual(first,second);self.assertEqual(first.read_bytes(),first_raw);self.assertEqual(original.read_bytes(),original_raw)
        observed=self.backend.inspect_course('course','worker-one');self.assertEqual(observed['snapshot_sha256'],r.sha256_file(second))
        selector=directory/'snapshot-current.json';record=r.read_private(selector)
        for name in ('../snapshot.fixture','snapshot-'+uuid.uuid4().hex+'.fixture'):
            changed=dict(record,name=name);r.private_json(selector,changed)
            with self.assertRaises((ValueError,OSError)):self.backend.inspect_course('course','worker-one')
        r.private_json(selector,record)
        self.assertEqual(self.backend.inspect_course('course','worker-one')['snapshot_sha256'],r.sha256_file(second))

    def test_snapshot_witness_rejects_stale_retained_association_without_rewriting_history(self):
        history={};first=r.snapshot_witness(self.backend,'worker-one','course','initial',history);first_raw=first.read_bytes()
        r.append_work(self.backend.home('course','worker-one')/'saved.txt',b'new retained coursework\n')
        with patch.object(self.backend,'refresh_snapshot',return_value=first):
            with self.assertRaises(AssertionError):r.snapshot_witness(self.backend,'worker-one','course','stale',history)
        self.assertEqual(first.read_bytes(),first_raw);self.assertEqual(history,{first:first_raw})
        second=r.snapshot_witness(self.backend,'worker-one','course','current',history)
        self.assertNotEqual(first,second);self.assertEqual(history[first],first_raw)
        self.assertEqual(second.read_text().splitlines()[-1],r.retained_digest(self.backend.home('course','worker-one')))
        self.assertEqual(self.backend.inspect_course('course','worker-one')['snapshot_sha256'],r.sha256_file(second))

    def test_actual_stale_ordinary_and_typed_controllers_preserve_new_owned_pid_barriers_and_peer(self):
        original=self.pending();peer=self.backend.start(self.authority,'peer','worker-one');peer_record=self.backend.processes.services[self.backend.processes.identity('worker-one','peer')]
        peer_bytes=(self.backend.home('peer','worker-one')/'saved.txt').read_bytes()
        with r.paused_controller(self.backend,'fence_context','course') as (entered,release),ThreadPoolExecutor(max_workers=1,thread_name_prefix='stale-controller') as pool:
            future=pool.submit(r.controller_result,lambda:self.authority.resume(original),contract);self.assertTrue(entered.wait(5))
            recovery=self.recovery();self.authority.reconcile_apply(recovery);child=self.backend.start(self.authority,'course','worker-one')
            raw=r.barriers(self.backend,'course');release.set();self.assertEqual(future.result(timeout=10),'transfer_pending_history_changed')
            self.assertIsNone(child.poll());self.assertEqual(self.backend.processes.live('worker-one','course'),child.pid);self.assertEqual(r.barriers(self.backend,'course'),raw)
        self.backend.processes.stop('worker-one','course')
        # Ordinary forward transfer then another reviewed pending recovery provides a typed stale controller.
        next_plan=self.root/'next.json';self.authority.preview('course','worker-two',next_plan)
        with patch.object(contract,'fault_hook',side_effect=r.interrupt_at('prepared')):
            with self.assertRaises(contract.OwnershipError):self.authority.apply(next_plan)
        pending_recovery=self.recovery('worker-one')
        with patch.object(contract,'fault_hook',side_effect=r.interrupt_at('reconciliation_prepared')):
            with self.assertRaises(contract.OwnershipError):self.authority.reconcile_apply(pending_recovery)
        with r.paused_controller(self.backend,'reconciliation_context','course') as (entered,release),ThreadPoolExecutor(max_workers=1,thread_name_prefix='stale-controller') as pool:
            future=pool.submit(r.controller_result,lambda:self.authority.reconcile_resume(pending_recovery),contract);self.assertTrue(entered.wait(5))
            latest=self.recovery('worker-two');self.authority.reconcile_apply(latest);child=self.backend.start(self.authority,'course','worker-two')
            raw=r.barriers(self.backend,'course');release.set();self.assertEqual(future.result(timeout=10),'reconciliation_pending_history_changed')
            self.assertIsNone(child.poll());self.assertEqual(self.backend.processes.live('worker-two','course'),child.pid);self.assertEqual(r.barriers(self.backend,'course'),raw)
        self.assertEqual(self.backend.processes.live('worker-one','peer'),peer.pid)
        self.assertEqual(peer_record[1],self.backend.processes.services[self.backend.processes.identity('worker-one','peer')][1])
        self.assertEqual((self.backend.home('peer','worker-one')/'saved.txt').read_bytes(),peer_bytes)


def example_report():
    matrix=[];nested=[]
    for stage,selected in sorted(r.MATRIX):
        matrix.append({'pending_stage':stage,'selected_endpoint':selected,'old_epoch':2 if stage=='committed' else 1,
            'new_epoch':3 if stage=='committed' else 2,'original_record_sha256':r.digest(['original',stage,selected]),
            'plan_sha256':r.digest(['plan',stage,selected]),'source_tree_sha256':'a'*64,'receiver_tree_sha256':'b'*64,
            'source_snapshot_sha256':'c'*64,'receiver_snapshot_sha256':'d'*64})
    for stage in ('prepared','fenced','committed'):
        nested.append({'pending_stage':stage,'selected_endpoint':'receiver','old_epoch':2 if stage=='committed' else 1,
            'new_epoch':3 if stage=='committed' else 2,'parent_sha256':r.digest(['parent',stage]),
            'plan_sha256':r.digest(['nested-plan',stage]),'source_tree_sha256':'a'*64,'receiver_tree_sha256':'b'*64})
    return {'waypoint_revision':'a'*40,'fingerprints':r.fingerprints(),'matrix':matrix,'nested':nested,
        'normal_followup':{'recovery_epoch':2,'ordinary_epoch':3,'barrier_identity_sha256':'a'*64,
            'gap_barrier_sha256':'b'*64,'source_tree_sha256':'c'*64,'receiver_tree_sha256':'c'*64,
            'recovery_plan_sha256':r.digest('normal-recovery'),'ordinary_plan_sha256':r.digest('normal-ordinary'),
            'gap_plan_sha256':r.digest('normal-gap')},'authority_sha256':'a'*64,'peer_work_sha256':'b'*64,'checks':sorted(r.CHECKS)}


class ReconciliationPublicationTests(unittest.TestCase):
    def test_all_controls_exact_cleanup_scope_private_exclusive_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for check in r.CHECKS:
                value=example_report();value['checks'].remove(check)
                with self.subTest(check=check),self.assertRaises(ValueError):r.publish_pass(root,value,True)
            for cleanup in (False,None,1):
                with self.subTest(cleanup=cleanup),self.assertRaises(ValueError):r.publish_pass(root,example_report(),cleanup)
            self.assertEqual(list(root.iterdir()),[])
            r.publish_pass(root,example_report(),True);path=root/'PENDING-RECONCILIATION-PASS.json';raw=path.read_bytes();value=json.loads(raw)
            self.assertEqual(path.stat().st_mode & 0o777,0o600);self.assertEqual(value['version'],r.VERSION)
            self.assertEqual(value['successful_recovery_cases'],10);self.assertEqual(value['intentionally_unresolved_backend_gap_probes'],1)
            self.assertEqual(value['pending_transfers_after_probe'],1);self.assertIs(value['backend_write_gap_recovery'],False)
            self.assertEqual(value['logical_workers'],2);self.assertEqual(value['real_worker_hosts'],0)
            for key in ('runtime_enforcement','production_activation_adapter','provider_adapter','authenticated_remote_transport',
                        'actual_two_host_isolation','encrypted_backup_verified','offhost_backup_verified','measured_profile_enforcement','apply_started_services'):
                self.assertIs(value[key],False)
            self.assertIs(value['cleanup_verified'],True)
            with self.assertRaises(FileExistsError):r.publish_pass(root,example_report(),True)
            self.assertEqual(path.read_bytes(),raw)

    def test_private_fields_hashes_duplicate_controls_and_case_counts_refuse(self):
        cases=[]
        for key in ('worker_id','authority_id','receipt','saved_bytes','pid','nonce','cleanup_verified','successful_recovery_cases'):
            value=example_report();value[key]='private';cases.append(value)
        value=example_report();value['checks'].append(value['checks'][0]);cases.append(value)
        value=example_report();value['waypoint_revision']='main';cases.append(value)
        for key in r.FINGERPRINT_FILES:
            value=example_report();value['fingerprints'][key]='latest';cases.append(value)
        value=example_report();value['fingerprints']['private_extra']='a'*64;cases.append(value)
        for key in ('matrix','nested'):
            value=example_report();value[key].pop();cases.append(value)
            value=example_report();value[key][-1]=deepcopy(value[key][0]);cases.append(value)
            value=example_report();value[key][0]['new_epoch']=True;cases.append(value)
            value=example_report();value[key][0]['new_epoch']=99;cases.append(value)
            value=example_report();value[key][0]['source_tree_sha256']=value[key][0]['receiver_tree_sha256'];cases.append(value)
        value=example_report();value['matrix'][0]['source_snapshot_sha256']=value['matrix'][0]['receiver_snapshot_sha256'];cases.append(value)
        value=example_report();value['nested'][0]['selected_endpoint']='source';cases.append(value)
        value=example_report();value['normal_followup']['source_tree_sha256']='d'*64;cases.append(value)
        value=example_report();value['normal_followup']['recovery_epoch']=True;cases.append(value)
        value=example_report();value['normal_followup']['ordinary_plan_sha256']=value['matrix'][0]['plan_sha256'];cases.append(value)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for index,value in enumerate(cases):
                with self.subTest(case=index),self.assertRaises(ValueError):r.publish_pass(root,value,True)
            self.assertEqual(list(root.iterdir()),[])

    def test_nonprivate_or_symlink_output_and_failed_publication_leave_no_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);root.chmod(0o755)
            with self.assertRaises(ValueError):r.publish_pass(root,example_report(),True)
            root.chmod(0o700);alias=root/'alias';alias.symlink_to(root,target_is_directory=True)
            with self.assertRaises(ValueError):r.publish_pass(alias,example_report(),True)
            alias.unlink()
            with patch.object(r.os,'link',side_effect=OSError('synthetic publication failure')):
                with self.assertRaises(OSError):r.publish_pass(root,example_report(),True)
            self.assertEqual(list(root.iterdir()),[])

    def test_dirty_source_refuses_before_output_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'run'
            with patch.object(r.sys,'argv',['verify_pending_reconciliation.py','--output',str(output)]),patch.object(r,'_source',return_value={'revision':'a'*40,'dirty':True}):
                with self.assertRaises(SystemExit):r.main()
            self.assertFalse(output.exists())

    def test_complete_ten_case_real_process_flow_with_unit_only_provenance_stub(self):
        # The temporary packet is deleted; mocked provenance is NOT clean-source acceptance.
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory).resolve()/'run'
            with patch.object(r.sys,'argv',['verify_pending_reconciliation.py','--output',str(output)]),patch.object(r,'_source',return_value={'revision':'a'*40,'dirty':False}):r.main()
            value=json.loads((output/'PENDING-RECONCILIATION-PASS.json').read_text())
            self.assertEqual(set(value['checks']),r.CHECKS);self.assertEqual(value['successful_recovery_cases'],10)
            self.assertEqual(value['pending_transfers_after_probe'],1);self.assertIs(value['backend_write_gap_recovery'],False)
            self.assertEqual(list(output.iterdir()),[output/'PENDING-RECONCILIATION-PASS.json'])

    def test_partial_process_failure_and_cleanup_failure_never_publish_false_pass(self):
        start=r.ReconciliationBackend.start;captured=[];cleanup=r.cleanup_fixture
        def partial(backend,*args):
            result=start(backend,*args);captured.append((backend,result));raise RuntimeError('synthetic partial real start failure')
        def failed_cleanup(backend,fixture):
            cleanup(backend,fixture);raise RuntimeError('synthetic cleanup verification failure')
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory).resolve()/'run'
            with patch.object(r.sys,'argv',['verify_pending_reconciliation.py','--output',str(output)]),patch.object(r,'_source',return_value={'revision':'a'*40,'dirty':False}), \
                    patch.object(r.ReconciliationBackend,'start',partial),patch.object(r,'cleanup_fixture',side_effect=failed_cleanup),patch.object(r,'publish_pass') as publication:
                with self.assertRaisesRegex(RuntimeError,'cleanup verification'):r.main()
            self.assertEqual(len(captured),1);self.assertIsNotNone(captured[0][1].poll());self.assertTrue(captured[0][0].processes.cleanup_verified)
            self.assertEqual(list(output.iterdir()),[]);publication.assert_not_called()


if __name__=='__main__':unittest.main()
