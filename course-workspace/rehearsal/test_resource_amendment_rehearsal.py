"""Resource amendment rehearsal cannot hide weak runtime checks or failed cleanup."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

import verify_resource_amendment as r


def report():
    initial = {'active_courses':2,'released_courses':0,'memory_reserved_mb':3072,'cpu_reserved':2.5,
               'memory_budget_mb':8192,'cpu_budget':3,'host_reserve_mb':4096,'host_reserve_cpu':1}
    expanded = dict(initial,memory_reserved_mb=4096,cpu_reserved=3)
    return {'waypoint_revision':'a'*40,'fixture_sha256':'b'*64,'amendment_sha256':'c'*64,'allocator_sha256':'d'*64,
            'source_manifests_sha256':['e'*64,'f'*64],'policy_sha256':'a'*64,
            'expansion_plan_sha256':'b'*64,'contraction_plan_sha256':'c'*64,'snapshot_id':'d'*64,'receipt_history_sha256':'e'*64,
            'images':{'hub':'sha256:'+'f'*64,'course':'sha256:'+'a'*64},
            'initial_allocation':initial,'expanded_allocation':expanded,'pending_allocation':copy.deepcopy(expanded),
            'final_allocation':copy.deepcopy(initial),'checks':sorted(r.CHECKS)}


class ResourceAmendmentEvidenceTests(unittest.TestCase):
    def test_every_control_and_verified_cleanup_required_before_private_exclusive_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for check in r.CHECKS:
                value = report(); value['checks'].remove(check)
                with self.subTest(check=check),self.assertRaises(ValueError): r.publish_pass(root,value,True)
            for cleanup in (False,None,1):
                with self.assertRaises(ValueError): r.publish_pass(root,report(),cleanup)
            self.assertEqual(list(root.iterdir()),[])
            r.publish_pass(root,report(),True)
            path = root/'RESOURCE-AMENDMENT-PASS.json'; original = path.read_bytes()
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            value = json.loads(original)
            self.assertFalse(value['apply_started_services']); self.assertTrue(value['operator_started_services'])
            self.assertTrue(value['closed_recovery_only'])
            with self.assertRaises(FileExistsError): r.publish_pass(root,report(),True)
            self.assertEqual(path.read_bytes(),original)

    def test_private_fields_mutable_images_missing_peer_and_early_lease_release_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); cases = []
            for key in ('environment','roster','owner','plan','receipt','policy_path','ledger','export_path'):
                value = report(); value[key] = 'private'; cases.append(value)
            value = report(); value['source_manifests_sha256'][1] = value['source_manifests_sha256'][0]; cases.append(value)
            value = report(); value['images']['hub'] = 'hub:latest'; cases.append(value)
            value = report(); value['pending_allocation']['memory_reserved_mb'] = 3072; cases.append(value)
            value = report(); value['pending_allocation']['cpu_reserved'] = 2.5; cases.append(value)
            value = report(); value['final_allocation']['active_courses'] = 1; cases.append(value)
            value = report(); value['expanded_allocation']['cpu_reserved'] = 3.1; cases.append(value)
            value = report(); value['initial_allocation']['active_courses'] = True; cases.append(value)
            value = report(); value['final_allocation']['cpu_reserved'] = float('nan'); cases.append(value)
            value = report(); value['expanded_allocation']['memory_reserved_mb'] = 10**1000; cases.append(value)
            for value in cases:
                with self.subTest(value=value),self.assertRaises(ValueError): r.publish_pass(root,value,True)
            self.assertEqual(list(root.iterdir()),[])

    def test_partial_first_start_runs_owned_cleanup_and_never_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); first = Mock(instance='rehearsal-one'); second = Mock(instance='rehearsal-two')
            first.start.side_effect = RuntimeError('partial fixture start')
            argv = ['verify_resource_amendment.py','--output',str(root/'run')]
            with patch.object(r.sys,'argv',argv),patch.object(r.sys,'platform','linux'),patch.object(r.os,'geteuid',return_value=0), \
                    patch.object(r,'AUTHORITY',root/'authority'),patch.object(r,'_source',return_value={'revision':'a'*40,'dirty':False}), \
                    patch.object(r,'sha256_file',return_value='b'*64),patch.object(r,'Notebook',side_effect=[first,second]), \
                    patch.object(r,'cleanup_courses',return_value=True) as cleanup,patch.object(r,'publish_pass') as publish, \
                    patch.dict(sys.modules,{'allocation':types.ModuleType('allocation'),'resource_amendment':types.ModuleType('resource_amendment')}):
                with self.assertRaises(RuntimeError): r.main()
            cleanup.assert_called_once_with(first,second,root/'run/course-one','rehearsal-one',None,root/'run/worker-policy.json')
            second.start.assert_not_called(); publish.assert_not_called()
            self.assertFalse((root/'run/RESOURCE-AMENDMENT-PASS.json').exists())

    def test_failed_publication_leaves_neither_pass_nor_scratch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch.object(r.os,'link',side_effect=OSError('synthetic publication failure')):
                with self.assertRaises(OSError): r.publish_pass(root,report(),True)
            self.assertEqual(list(root.iterdir()),[])

    def test_preexisting_authority_refused_before_mutation_or_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); authority = root/'authority'; authority.mkdir()
            argv = ['verify_resource_amendment.py','--output',str(root/'run')]
            with patch.object(r.sys,'argv',argv),patch.object(r.sys,'platform','linux'),patch.object(r.os,'geteuid',return_value=0), \
                    patch.object(r,'AUTHORITY',authority),patch.object(r,'Notebook') as notebook:
                with self.assertRaises(SystemExit): r.main()
            notebook.assert_not_called(); self.assertFalse((root/'run').exists())

    def test_optimized_python_refuses_before_output_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'run'
            result = subprocess.run([sys.executable,'-O',r.__file__,'--output',str(output)],capture_output=True,text=True,timeout=30,
                                    env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
            self.assertNotEqual(result.returncode,0); self.assertRegex(result.stderr,r'optimized Python|Python assertions')
            self.assertFalse(output.exists())


class ResourceRuntimeProofTests(unittest.TestCase):
    def test_missing_receipt_replay_restores_private_history_even_on_unexpected_failure(self):
        class Error(ValueError):
            def __init__(self,code): self.code=code; super().__init__(code)
        for failure in (None,'unexpected','accepted'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve(); receipt=root/'.receipt.json'; original=b'private completed receipt\n'
                receipt.write_bytes(original); receipt.chmod(0o600)
                protected=root/'.env'; protected.write_bytes(b'private environment\n'); protected.chmod(0o600)
                fixture=types.SimpleNamespace(deployment=root,instance='rehearsal-owned')
                def apply(*args):
                    self.assertFalse(receipt.exists())
                    if failure=='unexpected': raise OSError('unexpected backend failure')
                    if failure=='accepted': return {}
                    raise Error('completed_resource_history_required')
                amend=types.SimpleNamespace(RECEIPT=receipt.name,PENDING='.pending.json',AmendmentError=Error,apply=apply)
                with patch.object(r,'docker',return_value=''):
                    if failure:
                        with self.assertRaises((OSError,AssertionError)):
                            r.verify_missing_receipt_replay(amend,fixture,root/'old-plan.json',(protected,))
                    else: r.verify_missing_receipt_replay(amend,fixture,root/'old-plan.json',(protected,))
                self.assertEqual(receipt.read_bytes(),original); self.assertEqual(receipt.stat().st_mode & 0o777,0o600)
                self.assertEqual(protected.read_bytes(),b'private environment\n')

    def test_negative_controls_require_exact_reasons_and_propagate_unexpected_failures(self):
        class Error(ValueError):
            def __init__(self,code): self.code=code; super().__init__(code)
        r.expect_refusal(Mock(side_effect=Error('expected')),Error,{'expected'})
        with self.assertRaises(AssertionError): r.expect_refusal(Mock(side_effect=Error('other')),Error,{'expected'})
        with self.assertRaises(AssertionError): r.expect_refusal(Mock())
        with self.assertRaises(OSError): r.expect_refusal(Mock(side_effect=OSError('transport failure')))

    def test_runtime_proof_uses_actual_hub_environment_and_both_memory_cpu_enforcement(self):
        fixture = types.SimpleNamespace(instance='rehearsal-'+'a'*10,image='sha256:'+'b'*64)
        hub = {'Config':{'Env':['WORKSPACE_ACTIVE_LIMIT=2','WORKSPACE_MEMORY_MB=768','WORKSPACE_CPU_LIMIT=0.375']}}
        good = {'Image':fixture.image,'HostConfig':{'Memory':768*1024**2,'NanoCpus':375000000}}
        for failure in (None,'count','memory','cpu','hub'):
            with self.subTest(failure=failure):
                current_hub = copy.deepcopy(hub); first = copy.deepcopy(good)
                if failure=='memory': first['HostConfig']['Memory']=512*1024**2
                if failure=='cpu': first['HostConfig']['NanoCpus']=250000000
                if failure=='hub': current_hub['Config']['Env'][0]='WORKSPACE_ACTIVE_LIMIT=1'
                ids = {'one'} if failure=='count' else {'one','two'}
                def docker(*args):
                    return json.dumps([current_hub if args[1].endswith('-hub') else first])
                with patch.object(r,'active_learners',return_value=ids),patch.object(r,'docker',side_effect=docker):
                    if failure:
                        with self.assertRaises(AssertionError): r.verify_runtime_policy(fixture,2,768,0.375)
                    else: r.verify_runtime_policy(fixture,2,768,0.375)
        self.assertEqual(r.effective_cpu({'NanoCpus':0,'CpuQuota':37500,'CpuPeriod':100000}),0.375)
        for limits in ({},{'CpuQuota':-1,'CpuPeriod':100000},{'NanoCpus':True}):
            with self.assertRaises(AssertionError): r.effective_cpu(limits)

    def test_normal_start_does_not_force_recreation_and_verifies_quotas_before_operator_up(self):
        fixture = Mock(env_file=Path('/owned/.env'),homes=Path('/owned/homes'),instance='rehearsal-owned')
        events=[]
        fixture.compose.side_effect=lambda *args:events.append(('compose',args))
        with patch.object(r,'read_environment',side_effect=lambda path:events.append(('env',path)) or {'resources':'new'}), \
                patch.object(r,'inspect',side_effect=lambda *args:events.append(('inspect',args))), \
                patch.object(r,'wait_hub',side_effect=lambda book:events.append(('wait',book))):
            r.start_normally(fixture)
        self.assertEqual(fixture.env,{'resources':'new'})
        self.assertEqual([item[0] for item in events],['env','inspect','compose','wait'])
        fixture.compose.assert_called_once_with('up','-d','--no-build','--pull','never','hub')

    def test_peer_proof_rejects_replaced_hub_even_if_saved_work_would_pass(self):
        fixture = types.SimpleNamespace(instance='rehearsal-owned')
        with patch.object(r,'docker',return_value=json.dumps([{'Id':'different'}])),patch.object(r,'assert_live') as live:
            with self.assertRaises(AssertionError): r.same_peer(fixture,Mock(),'url',{'course.py':'saved'}, {'learner'},'original')
            live.assert_not_called()

    def test_private_history_bundle_requires_exact_receipt_runtime_and_managed_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for failure in (None,'receipt','runtime','metadata'):
                def restore(cfg,*args):
                    target = Path(args[args.index('--target')+1]); bundle = target/'bundle'; (bundle/'runtime').mkdir(parents=True)
                    (bundle/'resource-amendment-receipt.json').write_text('bad' if failure=='receipt' else 'receipt')
                    (bundle/'runtime/resource_amendment.py').write_bytes(b'bad' if failure=='runtime' else (r.ROOT/'resource_amendment.py').read_bytes())
                    (bundle/'bundle.json').write_text(json.dumps({'version':3 if failure=='metadata' else 4,'retirement_protected':False}))
                receipt = root/'receipt'; receipt.write_text('receipt'); expected=r.sha256_file(receipt)
                with self.subTest(failure=failure),patch.object(r,'restic',side_effect=restore):
                    if failure:
                        with self.assertRaises(AssertionError): r.verify_history_bundle({},'a'*64,root,expected)
                    else: r.verify_history_bundle({},'a'*64,root,expected)
                self.assertFalse(list(root.glob('.resource-history-proof-*')))


class StoppedStateDiagnosticsTests(unittest.TestCase):
    def model(self):
        fixture=types.SimpleNamespace(instance='recovered-'+'a'*12,image='sha256:'+'b'*64,hub_image='sha256:'+'c'*64)
        records={}; calls=[]
        for index,role in enumerate(('hub','learner'),1):
            identity=format(index,'064x')
            records[identity]={'Id':identity,'Name':'/'+fixture.instance+('-hub' if role=='hub' else '-learner-'+r.SUBJECTS['alice']),
                'Image':fixture.hub_image if role=='hub' else fixture.image,
                'Config':{'Labels':{'educloud.workspace.instance':fixture.instance,'educloud.workspace.kind':role}},
                'State':{'Running':role=='learner','OOMKilled':False,'ExitCode':0}}
        def invoke(args):
            calls.append(args)
            if args[0]=='ps': return '\n'.join(records)
            if args[0]=='inspect': return json.dumps([records[args[1]]])
            if args[0]=='logs':
                return 'private-token-path-cookie-canary\nCleaning up single-user servers...\nFailed to stop user: private-subject\n...done\n'
            raise AssertionError('unexpected command')
        return fixture,records,calls,invoke

    def test_fixed_roles_states_and_shutdown_categories_exclude_private_material(self):
        fixture,records,calls,invoke=self.model()
        result=r.stopped_state_diagnostic(fixture,invoke=invoke)
        self.assertTrue(result['available']); self.assertEqual(result['unverified_count'],0)
        self.assertEqual(result['roles']['hub']['running_count'],0)
        self.assertEqual(result['roles']['learner']['states'],[{'running':True,'oom_killed':False,'exit_code':0}])
        self.assertEqual(result['hub_shutdown'],{'available':True,'cleanup_requested':1,'cleanup_completed':1,
            'stop_user_failed':1,'cleanup_disabled':0})
        public=json.dumps(result)
        for private in ('private',fixture.instance,fixture.image,*records): self.assertNotIn(private,public)
        self.assertTrue(all(args[0] in ('ps','inspect','logs') for args in calls))

    def test_foreign_or_malformed_container_is_never_used_for_logs_or_state(self):
        for mutate in (lambda v:v['Config']['Labels'].update({'educloud.workspace.instance':'foreign'}),
                lambda v:v.update(Image='sha256:'+'d'*64),lambda v:v['State'].update(Running=1)):
            fixture,records,calls,invoke=self.model(); identity=next(iter(records)); mutate(records[identity])
            result=r.stopped_state_diagnostic(fixture,invoke=invoke)
            self.assertEqual(result['roles']['hub']['count'],0); self.assertEqual(result['unverified_count'],1)
            self.assertFalse(result['hub_shutdown']['available'])
            self.assertFalse(any(args[0]=='logs' for args in calls))

    def test_inventory_and_inspection_counts_are_bounded(self):
        fixture,records,calls,invoke=self.model()
        def inventory(args):
            if args[0]=='ps': return '\n'.join(format(i,'064x') for i in range(10))
            return invoke(args)
        result=r.stopped_state_diagnostic(fixture,invoke=inventory)
        self.assertTrue(result['truncated']); self.assertEqual(sum(args[0]=='inspect' for args in calls),8)
        result=r.stopped_state_diagnostic(fixture,invoke=lambda args:'\n'.join(format(i,'064x') for i in range(65)))
        self.assertFalse(result['available'])

    def test_existing_assertion_is_unchanged_and_diagnostic_failure_cannot_mask_it(self):
        fixture,_,_,_=self.model()
        with patch.object(r,'docker',return_value=''),patch.object(r,'stopped_state_diagnostic') as diagnostic:
            r.assert_stopped(fixture); diagnostic.assert_not_called()
        with patch.object(r,'docker',return_value='private-container'), \
                patch.object(r,'stopped_state_diagnostic',side_effect=ValueError('private-diagnostic')), \
                patch('builtins.print') as output:
            with self.assertRaisesRegex(AssertionError,'amendment started owned services'):r.assert_stopped(fixture)
        output.assert_not_called()

    def test_cli_reader_bounds_bytes_time_and_reaps_only_its_own_process(self):
        processes=[]
        def launch(args,**kwargs):
            self.assertEqual(args,['docker','logs','id'])
            process=subprocess.Popen([sys.executable,'-c','import os;os.write(1,b"x"*131072)'],**kwargs)
            processes.append(process); return process
        with self.assertRaisesRegex(ValueError,'stopped_diagnostic_unavailable'):
            r._stopped_diagnostic_command(('logs','id'),time.monotonic()+3,popen=launch)
        self.assertIsNotNone(processes[0].poll()); self.assertTrue(processes[0].stdout.closed)
        def waiting(args,**kwargs):
            process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],**kwargs)
            processes.append(process); return process
        with self.assertRaisesRegex(ValueError,'stopped_diagnostic_unavailable'):
            r._stopped_diagnostic_command(('logs','id'),time.monotonic()+.05,popen=waiting)
        self.assertIsNotNone(processes[-1].poll()); self.assertTrue(processes[-1].stdout.closed)

    def test_log_reader_includes_bounded_stderr_and_other_commands_discard_it(self):
        streams=[]
        def launch(args,**kwargs):
            streams.append(kwargs['stderr'])
            return subprocess.Popen([sys.executable,'-c','import os;os.write(2,b"Cleaning up single-user servers...\\n")'],**kwargs)
        raw=r._stopped_diagnostic_command(('logs','id'),time.monotonic()+3,popen=launch)
        self.assertIn('Cleaning up single-user servers...',raw)
        raw=r._stopped_diagnostic_command(('inspect','id'),time.monotonic()+3,popen=launch)
        self.assertEqual(raw,''); self.assertEqual(streams,[subprocess.STDOUT,subprocess.DEVNULL])

    def test_unknown_learner_subject_is_not_counted(self):
        fixture,records,_,invoke=self.model()
        records[format(2,'064x')]['Name']='/'+fixture.instance+'-learner-unknown-subject'
        result=r.stopped_state_diagnostic(fixture,invoke=invoke)
        self.assertEqual(result['unverified_count'],1); self.assertEqual(result['roles']['learner']['count'],0)


if __name__=='__main__': unittest.main()
