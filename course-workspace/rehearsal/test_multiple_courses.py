"""Multiple-course evidence, failure cleanup and actual fixture option boundaries."""
from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import verify_multiple_courses as r
from notebook import Notebook


def report():
    active = {'active_courses': 2, 'released_courses': 0, 'memory_reserved_mb': 3072,
              'cpu_reserved': 2.5, 'memory_budget_mb': 8192, 'cpu_budget': 3,
              'host_reserve_mb': 4096, 'host_reserve_cpu': 1}
    retired = dict(active, active_courses=1, released_courses=1,
                   memory_reserved_mb=1536, cpu_reserved=1.25)
    return {'waypoint_revision': 'a' * 40, 'fixture_sha256': 'b' * 64,
            'allocator_sha256': 'c' * 64, 'source_manifests_sha256': ['d' * 64, 'e' * 64],
            'policy_sha256': 'f' * 64, 'snapshot_id': 'a' * 64,
            'source_export_sha256': 'b' * 64, 'restored_export_sha256': 'c' * 64,
            'images': {'hub': 'sha256:' + 'd' * 64, 'course': 'sha256:' + 'e' * 64},
            'allocation': active, 'retired_allocation': retired, 'checks': sorted(r.CHECKS)}


class MultipleCourseAcceptanceTests(unittest.TestCase):
    def test_foreign_writer_control_cleans_exact_owned_names_even_after_partial_run_failure(self):
        class AllocationError(ValueError):
            def __init__(self, code): self.code=code; super().__init__(code)
        for failure in (None, 'partial-run', 'release-accepted', 'wrong-refusal'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve(); homes=root/'homes'; homes.mkdir()
                instance='rehearsal-'+'a'*10; home=homes/(instance+'-home-'+'b'*24); home.mkdir()
                (home/'preserved.py').write_text('retained coursework')
                fixture=types.SimpleNamespace(instance=instance,homes=homes,deployment=root/'deployment',image='sha256:'+'c'*64)
                alias=instance+'-foreign-home-alias'; helper=instance+'-foreign-home-writer'; cid='d'*64
                state={'volume':False,'container':False}; calls=[]
                def docker(*args):
                    calls.append(args)
                    if args[0]=='ps': return cid if state['container'] else ''
                    if args[:2]==('volume','ls'): return alias if state['volume'] else ''
                    if args[:2]==('volume','create'): state['volume']=True; return alias
                    if args[0]=='run':
                        state['container']=True
                        if failure=='partial-run': raise RuntimeError('partial Docker run failure')
                        return cid
                    if args[0]=='inspect':
                        return json.dumps([{'Name':'/'+helper,'Image':fixture.image,'State':{'Running':True},
                                            'Config':{'Labels':{'educloud.rehearsal':instance}},
                                            'Mounts':[{'Name':alias,'RW':True}]}])
                    if args[:2]==('volume','inspect'):
                        return json.dumps([{'Name':alias,'Labels':{'educloud.rehearsal':instance}}])
                    if args[0]=='rm': state['container']=False; return cid
                    if args[:2]==('volume','rm'): state['volume']=False; return alias
                    raise AssertionError('unexpected Docker command')
                error=AllocationError('unrelated' if failure=='wrong-refusal' else 'stop_all_home_volume_writers')
                release=Mock(return_value={}) if failure=='release-accepted' else Mock(side_effect=error)
                allocator=types.SimpleNamespace(AllocationError=AllocationError,release=release,
                    inventory=Mock(return_value={'active_courses':2,'released_courses':0}))
                with patch.object(r,'docker',side_effect=docker),patch.dict(sys.modules,{'allocation':allocator}):
                    if failure:
                        with self.assertRaises((RuntimeError,AssertionError)):
                            r.foreign_writer_control(fixture,root/'policy.json',home)
                    else: r.foreign_writer_control(fixture,root/'policy.json',home)
                self.assertEqual(state,{'volume':False,'container':False})
                self.assertEqual((home/'preserved.py').read_text(),'retained coursework')
                removals=[args for args in calls if args[0]=='rm' or args[:2]==('volume','rm')]
                self.assertEqual(removals,[('rm','--force',cid),('volume','rm',alias)])
                launch=next(args for args in calls if args[0]=='run')
                self.assertIn('--read-only',launch); self.assertIn('--network',launch); self.assertIn('none',launch)
                self.assertIn('--cap-drop',launch); self.assertIn('ALL',launch)
                self.assertEqual(launch[launch.index('--entrypoint')+1], 'python3')
                self.assertEqual(launch[-3:],('-I','-c','import time; time.sleep(120)'))

    def test_foreign_writer_existing_name_refuses_before_creating_or_removing_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); homes=root/'homes'; homes.mkdir()
            instance='rehearsal-'+'a'*10; home=homes/(instance+'-home-'+'b'*24); home.mkdir()
            fixture=types.SimpleNamespace(instance=instance,homes=homes)
            with patch.dict(sys.modules,{'allocation':types.ModuleType('allocation')}), \
                    patch.object(r,'docker',side_effect=['',instance+'-foreign-home-alias']) as docker:
                with self.assertRaisesRegex(ValueError,'names already exist'):
                    r.foreign_writer_control(fixture,root/'policy.json',home)
            self.assertEqual([call.args[:2] for call in docker.call_args_list],[('ps','-aq'),('volume','ls')])

    def test_every_control_and_verified_cleanup_required_before_exclusive_private_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for check in r.CHECKS:
                value = report(); value['checks'].remove(check)
                with self.subTest(check=check), self.assertRaises(ValueError):
                    r.publish_pass(root, value, True)
            for cleanup in (False, None, 1):
                with self.assertRaises(ValueError): r.publish_pass(root, report(), cleanup)
            self.assertFalse((root / 'MULTI-COURSE-PASS.json').exists())
            r.publish_pass(root, report(), True)
            path = root / 'MULTI-COURSE-PASS.json'; original = path.read_bytes()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            value = json.loads(original)
            self.assertIs(value['closed_recovery_only'], True)
            self.assertEqual(value['scope'], 'same-worker-synthetic-multiple-courses')
            with self.assertRaises(FileExistsError): r.publish_pass(root, report(), True)
            self.assertEqual(path.read_bytes(), original)

    def test_private_data_bad_pins_and_inaccurate_lease_transition_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); cases = []
            for key in ('environment', 'roster', 'owner', 'policy_path', 'ledger', 'export_path'):
                value = report(); value[key] = 'private'; cases.append(value)
            value = report(); value['source_manifests_sha256'][1] = value['source_manifests_sha256'][0]; cases.append(value)
            value = report(); value['images']['course'] = 'course:latest'; cases.append(value)
            value = report(); value['allocation']['cpu_reserved'] = 4; cases.append(value)
            value = report(); value['retired_allocation']['active_courses'] = 0; cases.append(value)
            value = report(); value['retired_allocation']['memory_reserved_mb'] = 1535; cases.append(value)
            value = report(); value['allocation']['active_courses'] = True; cases.append(value)
            value = report(); value['allocation']['cpu_reserved'] = float('nan'); cases.append(value)
            value = report(); value['allocation']['policy_path'] = 'private'; cases.append(value)
            for value in cases:
                with self.subTest(value=value), self.assertRaises(ValueError): r.publish_pass(root, value, True)
            self.assertEqual(list(root.iterdir()), [])

    def test_cleanup_attempts_both_after_partial_start_or_cleanup_failure(self):
        for failure in ('first', 'second', 'unverified', None):
            first = Mock(); second = Mock(cleanup_verified=True)
            second.close.return_value = True
            cleanup = Mock(return_value=True)
            if failure == 'first': cleanup.side_effect = RuntimeError('first cleanup failed')
            if failure == 'second': second.close.side_effect = RuntimeError('second cleanup failed')
            if failure == 'unverified': second.cleanup_verified = False
            with patch.object(r, 'cleanup_rehearsal', cleanup), patch.object(r, 'cleanup_worker', return_value=True) as worker:
                if failure:
                    with self.assertRaises(RuntimeError): r.cleanup_courses(first, second, Path('/private/first'), 'source', None, Path('/private/policy.json'))
                    worker.assert_not_called()
                else:
                    self.assertIs(r.cleanup_courses(first, second, Path('/private/first'), 'source', None, Path('/private/policy.json')), True)
                    worker.assert_called_once()
            cleanup.assert_called_once_with(first, Path('/private/first'), 'source', None)
            second.close.assert_called_once()

    def test_partial_fixture_start_never_publishes_and_cleans_both(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            first = Mock(instance='rehearsal-one'); first.start.side_effect = RuntimeError('partial first start')
            second = Mock(instance='rehearsal-two')
            argv = ['verify_multiple_courses.py', '--output', str(root / 'run')]
            with patch.object(r.sys, 'argv', argv), patch.object(r.sys, 'platform', 'linux'), \
                    patch.object(r.os, 'geteuid', return_value=0), patch.object(r, 'AUTHORITY', root / 'authority'), \
                    patch.object(r, '_source', return_value={'revision': 'a' * 40, 'dirty': False}), \
                    patch.object(r, 'sha256_file', return_value='b' * 64), \
                    patch.object(r, 'Notebook', side_effect=[first, second]), \
                    patch.object(r, 'cleanup_courses', return_value=True) as cleanup, \
                    patch.object(r, 'publish_pass') as publish, \
                    patch.dict(sys.modules, {'allocation': types.ModuleType('allocation')}):
                with self.assertRaises(RuntimeError): r.main()
            cleanup.assert_called_once_with(first, second, root / 'run/course-one', 'rehearsal-one', None, root / 'run/worker-policy.json')
            second.start.assert_not_called(); publish.assert_not_called()
            self.assertFalse((root / 'run/MULTI-COURSE-PASS.json').exists())

    def test_disposal_is_bound_to_own_policy_daemon_claims_and_absent_resources(self):
        for failure in (None, 'foreign-claim', 'running-writer', 'unexpected-file'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve(); authority = root / 'authority'; authority.mkdir()
                state = root / 'worker-state'; state.mkdir(); policy_path = root / 'worker-policy.json'
                (authority / 'authority.json').write_text('{}'); (state / 'ledger.json').write_text('{}')
                if failure == 'unexpected-file': (state / 'unexpected').write_text('retain')
                deployment = root / 'source/deployment'
                claim = {'directory':str(root / 'foreign' if failure == 'foreign-claim' else deployment)}
                policy = {'state_directory':str(state), 'owner':'Synthetic rehearsal operator', 'worker_id':'synthetic-'+'a'*12}
                allocator = types.SimpleNamespace(
                    AUTHORITY=authority, worker_lock=lambda:nullcontext(),
                    policy=lambda path:(policy, 'b'*64), daemon=lambda:({}, 'c'*64),
                    binding=lambda *args:{'expected':'binding'},
                    ledger=lambda binding:{'claims':{'key':{'claim':claim}}},
                    private_directory=Mock(), sync=Mock(), stopped=Mock())
                if failure == 'running-writer': allocator.stopped.side_effect=ValueError('running volume writer')
                with patch.dict(sys.modules, {'allocation':allocator}), patch.object(r, 'AUTHORITY', authority):
                    if failure:
                        with self.assertRaises((RuntimeError, ValueError)): r.cleanup_worker(policy_path, (deployment,))
                        self.assertTrue((authority / 'authority.json').exists())
                        self.assertTrue((state / 'ledger.json').exists())
                    else:
                        self.assertIs(r.cleanup_worker(policy_path, (deployment,)), True)
                        self.assertFalse(authority.exists()); self.assertFalse(state.exists())
                        allocator.stopped.assert_called_once()

    def test_worker_disposal_failure_prevents_cleanup_success(self):
        first = Mock(); second = Mock(cleanup_verified=True); second.close.return_value=True
        with patch.object(r, 'cleanup_rehearsal', return_value=True), \
                patch.object(r, 'cleanup_worker', side_effect=RuntimeError('wrong worker binding')):
            with self.assertRaisesRegex(RuntimeError, 'worker-authority-cleanup-failed'):
                r.cleanup_courses(first, second, Path('/private/first'), 'source', None, Path('/private/policy.json'))

    def test_atomic_publication_failure_leaves_no_pass_or_scratch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch.object(r.os, 'link', side_effect=OSError('synthetic publication failure')):
                with self.assertRaises(OSError): r.publish_pass(root, report(), True)
            self.assertEqual(list(root.iterdir()), [])

    def test_optimized_python_refuses_before_output_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'run'
            result = subprocess.run([sys.executable, '-O', r.__file__, '--output', str(output)],
                                    capture_output=True, text=True, timeout=30,
                                    env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
            self.assertNotEqual(result.returncode, 0)
            self.assertRegex(result.stderr, r'optimized Python|Python assertions')
            self.assertFalse(output.exists())

    def test_preexisting_worker_authority_refused_without_resources_or_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); authority = root / 'authority'; authority.mkdir()
            argv = ['verify_multiple_courses.py', '--output', str(root / 'run')]
            with patch.object(r.sys, 'argv', argv), patch.object(r.sys, 'platform', 'linux'), \
                    patch.object(r.os, 'geteuid', return_value=0), patch.object(r, 'AUTHORITY', authority), \
                    patch.object(r, 'Notebook') as notebook:
                with self.assertRaises(SystemExit): r.main()
            notebook.assert_not_called(); self.assertFalse((root / 'run').exists())


class MultipleCourseFixtureTests(unittest.TestCase):
    def test_custom_identity_capacity_consumed_and_defaults_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            default = Notebook(root)
            self.assertEqual(default.domain, 'work.rehearsal.test')
            self.assertEqual(default.auth_domain, 'auth.rehearsal.test')
            self.assertEqual(default.client_id, 'course')
            self.assertEqual(default.capacity['active_limit'], 2)
            self.assertIsNone(default.allocation_policy)
            custom = Notebook(root, domain='one.rehearsal.test', auth_domain='authone.rehearsal.test',
                              client_id='course-one', capacity={'memory_mb':512, 'cpu':0.25, 'active_limit':1})
            custom.homes = root / 'homes'; custom.homes.mkdir()
            custom.env = {'WORKSPACE_PORT':'18000', 'WORKSPACE_OIDC_CLIENT_SECRET':'synthetic-secret'}
            with patch('notebook.run', side_effect=['sha256:' + 'a' * 64, 'sha256:' + 'b' * 64]): custom.render_setup()
            self.assertEqual(custom.env['WORKSPACE_CPU_LIMIT'], '0.25')
            self.assertEqual(custom.env['WORKSPACE_ACTIVE_LIMIT'], '1')
            self.assertEqual(custom.env['WORKSPACE_OIDC_CLIENT_ID'], 'course-one')
            self.assertEqual(custom.manifest['workspace']['origin'], custom.origin)
            with patch('notebook.run'): custom.configure_fixture_edges()
            self.assertIn('server_name one.rehearsal.test *.one.rehearsal.test', (root / 'nginx.conf').read_text())
            self.assertEqual(json.loads((root / 'realm.json').read_text())['clients'][0]['clientId'], 'course-one')
            for options in ({'domain':'outside.example'}, {'auth_domain':'work.rehearsal.test'},
                            {'client_id':'bad client'}, {'capacity':{'secret':'private'}}):
                with self.subTest(options=options), self.assertRaises(ValueError): Notebook(root, **options)

    def test_managed_fixture_holds_admission_during_compose_and_keeps_stop_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); events = []
            book = Notebook(root, allocation_policy=root / 'policy.json')
            @contextmanager
            def admission(*args):
                events.append('lock'); yield; events.append('unlock')
            allocator = types.SimpleNamespace(admission=admission)
            with patch.dict(sys.modules, {'allocation': allocator}), \
                    patch('notebook.read_environment', return_value={'synthetic':'env'}), \
                    patch('notebook.run', side_effect=lambda *args, **kwargs: events.append('compose')):
                book.compose('up', '-d', '--no-build', '--pull', 'never', 'hub')
                self.assertEqual(events, ['lock', 'compose', 'unlock'])
                events.clear(); book.compose('stop', 'hub')
                self.assertEqual(events, ['compose'])

    def test_two_fixtures_capture_same_original_resolver_before_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            first = Notebook(root / 'one'); second = Notebook(root / 'two')
            self.assertIs(first.original_dns, second.original_dns)
            self.assertNotEqual(first.instance, second.instance)


if __name__ == '__main__': unittest.main()
