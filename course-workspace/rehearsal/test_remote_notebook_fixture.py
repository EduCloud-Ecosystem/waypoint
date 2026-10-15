"""Fixture boundaries with fake Docker API; no image builds or daemon calls."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests'), str(Path(__file__).resolve().parent)]
import remote_notebook_fixture as fixture
import verify_remote_notebooks as verifier


def request():
    name = 'wa' + 'a' * 10
    return {'name': name, 'network': name + '-authority-control', 'edge_network': name + '-edge',
        'worker_networks': {worker: name + '-' + worker + '-channel' for worker in fixture.WORKERS},
        'ports': {'authority': 20001, 'source': 20002, 'peer': 20003, 'receiver': 20004},
        'password': 'synthetic-private-password-canary',
        **{key + '_image': 'sha256:' + str(index) * 64 for index, key in enumerate(('course', 'hub', 'controller'), 1)}}


class FakeClient:
    def __init__(self):
        self.created = []; self.connected = []; self.volume_records = []; self.items = {}
        self.volumes = SimpleNamespace(create=self.volume)
        self.containers = SimpleNamespace(create=self.create, run=self.run, get=lambda name: self.items[name])
        self.networks = SimpleNamespace(get=lambda name: SimpleNamespace(connect=lambda hub: self.connected.append((name, hub.name))))
        self.api = SimpleNamespace(create_networking_config=lambda value: {'EndpointsConfig': value},
            create_endpoint_config=lambda aliases: {'Aliases': aliases})
    def info(self): return {'NCPU': 8, 'MemTotal': 8 * 1024**3, 'ID': 'synthetic-fixed-daemon'}
    def volume(self, **kwargs): self.volume_records.append(kwargs); return SimpleNamespace(name=kwargs['name'])
    def create(self, image, **kwargs):
        self.created.append((image, kwargs)); result = SimpleNamespace(name=kwargs['name'], start=lambda: None)
        self.items[result.name] = result; return result
    def run(self, image, **kwargs): return self.create(image, **kwargs)


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / 'private'; self.root.mkdir(mode=0o700)
        for path in (self.root / 'control', self.root / 'backend', *(self.root / 'backend' / worker for worker in fixture.WORKERS)):
            path.mkdir(mode=0o700, exist_ok=True)
        self.client = FakeClient(); self.docker = SimpleNamespace(DockerClient=lambda **_: self.client)

    def bootstrap(self):
        import worker_transport
        from runtime_authority import validate_config
        # Preserve the real identity/profile/config contract even though the
        # Docker I/O itself is deliberately replaced in these host unit tests.
        with patch.dict(sys.modules, docker=self.docker), patch.object(fixture, 'DockerBackend', side_effect=lambda config, *a, **kw: SimpleNamespace(config=validate_config(config))), \
                patch.object(worker_transport, 'worker_binding', side_effect=lambda backend, worker: 'b' * 64), \
                patch.object(worker_transport, 'linux_boot_id', return_value='c' * 32), patch.object(fixture.os, 'chown'):
            return fixture.initialize(self.root, request())

    def test_presets_reserve_all_control_services_and_refuse_inflated_inputs(self):
        for cpu in (4, 5, 6, 8):
            value = fixture.allocation_for_host(cpu, 8 * 1024**3)
            self.assertNotIn('controller_cpu', value)
            controls = value['central_cpu'] + 2 * value['worker_service_cpu'] + 2 * value['guardian_cpu']
            self.assertAlmostEqual(controls, value['host_reserve_cpu'])
            self.assertAlmostEqual(3 * (value['hub_cpu'] + value['learner_cpu']) + controls, value['reserved_cpu'])
            self.assertLessEqual(value['reserved_cpu'], cpu)
        for cpu, memory in [(True, 8 * 1024**3), (3, 8 * 1024**3), (4, True), (4, 6656 * 1024**2 - 1)]:
            with self.assertRaises(ValueError): fixture.allocation_for_host(cpu, memory)

    def test_mount_plan_excludes_central_socket_homes_and_other_worker_state(self):
        plan = fixture.mount_plan(self.root, request())
        self.assertEqual(plan['central'], {request()['name'] + '-control': {'bind': str(self.root / 'control'), 'mode': 'rw'}})
        for worker, wanted in [('worker-one', {'source', 'peer'}), ('worker-two', {'receiver'})]:
            mounts = plan[worker]; destinations = {item['bind'] for item in mounts.values()}
            self.assertEqual({suffix for _, _, suffix in fixture.ENDPOINTS if str(self.root / suffix / 'homes') in destinations}, wanted)
            other = 'worker-two' if worker == 'worker-one' else 'worker-one'
            self.assertNotIn(str(self.root / 'backend' / other), destinations)
            self.assertNotIn(str(self.root / 'control'), destinations)
            self.assertIn('/var/run/docker.sock', destinations)
        broken = request(); broken['ports']['receiver'] = broken['ports']['source']
        with self.assertRaises(ValueError): fixture.mount_plan(self.root, broken)

    def test_bootstrap_private_material_and_binding_namespaces(self):
        observed = self.bootstrap(); control = json.loads((self.root / 'control/remote.json').read_bytes())
        self.assertEqual(control['control']['authority_id'], observed['authority_id'])
        self.assertEqual(set(control['runtime_bindings']), set(fixture.WORKERS))
        self.assertNotIn('/homes', json.dumps(control)); self.assertNotIn('/backend', json.dumps(control))
        keys = {path.relative_to(self.root / 'control').as_posix() for path in (self.root / 'control').rglob('*.key')}
        self.assertEqual(keys, {'credentials/server.key', 'worker-one-channel/client.key', 'worker-two-channel/client.key'})
        for worker in fixture.WORKERS:
            config = json.loads((self.root / worker / 'credentials/runtime.json').read_bytes())
            self.assertEqual(set(config['workers']), {worker}); self.assertEqual(set(config['courses']), {worker})
            self.assertEqual(config['workers'][worker]['boot_id'], 'c' * 32)
            for item in config['courses'][worker].values():
                from ownership_contract import logical_identity
                logical_identity(item['logical_identity'])
                self.assertEqual(item['logical_identity']['issuer'], 'https://identity.remote-notebook.invalid/realms/fixture')
            self.assertTrue((self.root / 'backend' / worker / 'journal/journal.sqlite').is_file())
        for identity, worker in [('guardian-one', 'worker-one'), ('guardian-two', 'worker-two')]:
            config = json.loads((self.root / identity / 'credentials/runtime.json').read_bytes())
            self.assertEqual(set(config['workers']), {worker})
            self.assertFalse((self.root / identity / 'credentials/server.key').exists())
        self.assertEqual(len(self.client.volume_records), 3)
        self.assertTrue(all(record['driver_opts']['o'] == 'bind' for record in self.client.volume_records))
        # Managed runtime identities do not get static fallback entries.
        acl = json.loads((self.root / 'control/acl.json').read_bytes())
        self.assertEqual({entry['role'] for entry in acl['clients'].values()}, {'operator'})
        with self.assertRaises(FileExistsError): self.bootstrap()

    def test_launchers_preserve_disjoint_mounts_and_socketless_hubs(self):
        self.bootstrap()
        with patch.dict(sys.modules, docker=self.docker):
            fixture.launch_workers(self.root); fixture.launch_users(self.root)
        service = [(image, item) for image, item in self.client.created if item['name'].endswith('-service')]
        self.assertEqual(len(service), 2)
        for _, item in service:
            self.assertEqual(item['command'][:2], ['python', 'worker_runtime.py'])
            worker = item['labels']['educloud.remote.worker']
            self.assertEqual(item['volumes'], fixture.mount_plan(self.root, request())[worker])
            self.assertEqual(item['networking_config'], {request()['worker_networks'][worker]: {'Aliases': [worker + '.local.test']}})
            self.assertEqual(item['cpu_quota'], 40000)
        hubs = [item for _, item in self.client.created if item['name'].endswith('-hub')]
        self.assertEqual(len(hubs), 3)
        for item in hubs:
            destinations = {mount['bind'] for mount in item['volumes'].values()}
            self.assertNotIn('/var/run/docker.sock', destinations)
            self.assertFalse(any('/homes' in value for value in destinations))
            self.assertEqual(item['environment']['WORKSPACE_RUNTIME_MODE'], 'authority-local')
            self.assertEqual(item['environment']['WORKSPACE_CPU_LIMIT'], '0.25')
            self.assertEqual(set(item['labels']), {'educloud.workspace.instance', 'educloud.workspace.kind'})
        guardians = [item for _, item in self.client.created if item['name'].endswith('-guardian')]
        for item in guardians:
            worker = item['labels']['educloud.runtime.worker']; destinations = {m['bind'] for m in item['volumes'].values()}
            self.assertIn(str(self.root / 'backend' / worker), destinations)
            self.assertFalse(any('/homes' in value or '/control' in value for value in destinations))

    def test_actual_docker_sdk_preserves_worker_dns_aliases_when_available(self):
        try:
            from docker.models.containers import _create_container_args
        except ImportError:
            self.skipTest('Docker SDK optional in host test requirements; exact fixture shape is always tested')
        self.bootstrap()
        with patch.dict(sys.modules, docker=self.docker): fixture.launch_workers(self.root)
        for image, item in self.client.created:
            worker = item['labels']['educloud.remote.worker']; network = request()['worker_networks'][worker]
            converted = _create_container_args({'image': image, 'version': '1.47', 'network': network,
                'networking_config': item['networking_config']})
            self.assertEqual(converted['networking_config']['EndpointsConfig'][network]['Aliases'], [worker + '.local.test'])
            # Reproduce the prior accidental second wrapper: the real SDK
            # drops its endpoint aliases because the outer key is not network.
            broken = _create_container_args({'image': image, 'version': '1.47', 'network': network,
                'networking_config': self.client.api.create_networking_config(item['networking_config'])})
            self.assertEqual(broken['networking_config'], {network: None})

    def test_copy_refuses_live_or_nonfresh_receiver_before_archive_or_deletion(self):
        self.bootstrap()
        home = self.root / 'receiver/homes' / (request()['name'] + '-receiver-home-' + __import__('hashlib').sha256(b'alice').hexdigest()[:24])
        work = home / 'work/saved.txt'; work.write_text('receiver-only-private-canary')
        backend = SimpleNamespace(containers=lambda *args: [], inspect_course=lambda *args: {'retained_tree_sha256': 'a' * 64})
        with patch.object(fixture, 'DockerBackend', return_value=backend), patch('volume_archive.pack') as pack:
            with self.assertRaisesRegex(ValueError, 'remote_copy_receiver_not_fresh'): fixture.copy_home(self.root)
            pack.assert_not_called(); self.assertEqual(work.read_text(), 'receiver-only-private-canary')
        backend.containers = lambda *args: [object()]
        with patch.object(fixture, 'DockerBackend', return_value=backend), patch('volume_archive.pack') as pack:
            with self.assertRaisesRegex(ValueError, 'remote_copy_participants_not_stopped'): fixture.copy_home(self.root)
            pack.assert_not_called()

    def test_one_shot_fault_does_not_modify_default_service(self):
        import ownership_contract
        previous = ownership_contract.fault_hook; stages = []
        def service(_):
            for _ in range(2):
                try: ownership_contract.fault_hook('fenced', 'a' * 32)
                except RuntimeError: stages.append('interrupted')
                else: stages.append('normal')
        with patch('remote_runtime_service.serve', side_effect=service): fixture.serve(self.root, interrupt=True)
        self.assertEqual(stages, ['interrupted', 'normal']); self.assertIs(ownership_contract.fault_hook, previous)

    def test_worker_ready_is_authenticated_readonly_and_only_transport_can_defer(self):
        from worker_transport import WorkerError
        from unittest.mock import Mock
        config = {'control': {}, 'runtime_bindings': {worker: 'a' * 64 for worker in fixture.WORKERS}}
        fixture.write(self.root / 'control/remote.json', config)
        backend = SimpleNamespace(workers={worker: {'worker_id': worker} for worker in fixture.WORKERS},
            courses={worker: ['course-main'] for worker in fixture.WORKERS},
            inspect_worker=Mock(side_effect=lambda worker: {'worker_id': worker}), binding=Mock(return_value='a' * 64))
        with patch('remote_control.load_backend', return_value=backend), patch.object(fixture.Authority, 'open') as authority:
            self.assertEqual(fixture.worker_ready(self.root), {'ready': True, 'verified_workers': 2})
            self.assertEqual(backend.inspect_worker.call_count, 2); self.assertEqual(backend.binding.call_count, 2)
            authority.assert_not_called()
            backend.inspect_worker.side_effect = lambda worker: (_ for _ in ()).throw(WorkerError('worker_transport_unavailable'))
            self.assertEqual(fixture.worker_ready(self.root), {'ready': False, 'verified_workers': 0})
            for code in ('worker_server_pin_changed', 'worker_channel_refused'):
                backend.inspect_worker.side_effect = WorkerError(code)
                with self.assertRaises(WorkerError): fixture.worker_ready(self.root)
            backend.inspect_worker.side_effect = lambda worker: {'worker_id': worker}
            backend.binding.return_value = 'b' * 64
            with self.assertRaisesRegex(ValueError, 'remote_fixture_worker_binding_changed'): fixture.worker_ready(self.root)


class ReportTests(unittest.TestCase):
    def report(self, status='PASS', phases=None):
        phases = list(verifier.PHASES) if phases is None else phases
        value = {'version': verifier.VERSION, 'status': status, 'scope': verifier.SCOPE, 'phases': phases,
            'cleanup_verified': True, 'acceptance_eligible': status == 'PASS', 'flags': verifier.observed_flags(phases),
            'source': 'a' * 40, 'code_sha256': {key: 'b' * 64 for key in verifier.CODE_FILES},
            'images': {key: 'sha256:' + 'c' * 64 for key in ('course', 'hub', 'controller')},
            'host': {'cpu': 8, 'memory_bytes': 8 * 1024**3}, 'allocation': fixture.allocation_for_host(8, 8 * 1024**3)}
        if status == 'FAIL': value['failure'] = 'RuntimeError'
        return value

    def test_strict_phase_schema_observations_and_no_false_acceptance(self):
        good = self.report(); self.assertEqual(verifier.validate_report(good), good)
        early = self.report('FAIL', list(verifier.PHASES[:2])); verifier.validate_report(early)
        self.assertIs(early['flags']['actual_python_r'], False); self.assertIs(early['flags']['actual_mtls_worker_channels'], False)
        changes = [lambda v: v.update(cleanup_verified=False), lambda v: v['phases'].pop(),
            lambda v: v['phases'].append(v['phases'][0]), lambda v: v.update(private_payload='private-canary'),
            lambda v: v['flags'].update(actual_two_host_isolation=True), lambda v: v['flags'].update(actual_python_r=1),
            lambda v: v['code_sha256'].pop('worker_runtime.py'), lambda v: v['allocation'].update(central_cpu=True),
            lambda v: v['host'].update(cpu=True), lambda v: v.update(failure='private-canary')]
        for change in changes:
            value = copy.deepcopy(good); change(value)
            with self.assertRaises(ValueError): verifier.validate_report(value)
        early['flags']['actual_python_r'] = True
        with self.assertRaises(ValueError): verifier.validate_report(early)

    def test_namespace_collision_checks_are_readonly_and_fail_before_creation(self):
        calls = []
        def command(*args):
            calls.append(args)
            return request()['name'] + '-worker-two-service' if args == ('ps', '-a', '--format', '{{.Names}}') else ''
        with self.assertRaisesRegex(ValueError, 'fixture_namespace_exists'):
            verifier.assert_fresh_namespace(request()['name'], request(), command)
        self.assertTrue(all('create' not in args and 'rm' not in args for args in calls))

    def test_dirty_source_never_contacts_docker(self):
        with patch.object(verifier.evidence, '_source', return_value={'revision': 'a' * 40, 'dirty': True}), \
                patch.object(verifier, 'command') as command:
            with self.assertRaisesRegex(ValueError, 'clean_source_required'): verifier.run(SimpleNamespace())
            command.assert_not_called()

    def test_strict_denial_refuses_transport_errors_and_malformed_replies(self):
        denied = {'ok': False, 'refused': True, 'code': 'authority_access_refused'}
        verifier.require_refusal(denied, 'authority_access_refused')
        invalid = [dict(denied, code='authority_transport_unavailable'), dict(denied, refused=1),
            dict(denied, ok=0), dict(denied, private='private-canary'), {'ok': False}, {'ok': True, 'result': {}}]
        for reply in invalid:
            with self.assertRaises(ValueError): verifier.require_refusal(reply, 'authority_access_refused')

    def test_kernel_calls_preserve_actual_smoke_execution_sentinel(self):
        executed = []
        def actual_contract(session, base, user, language, code):
            self.assertIn('CONTINUITY_OK', code)
            self.assertEqual(user, 'alice'); executed.append(language)
        verifier.exercise_kernels(object(), 'http://fixture.invalid', actual_contract)
        self.assertEqual(executed, ['python3', 'ir'])

    def test_restarted_source_requires_actual_login_403_and_same_origin_login_redirect(self):
        base = 'http://127.0.0.1:20002'
        def session(code=403, destination=base + '/hub/login?next=spawn'):
            def get(url, **_):
                return SimpleNamespace(url=destination if '/hub/spawn/' in url else url, raise_for_status=lambda: None)
            return SimpleNamespace(get=get, post=lambda *a, **kw: SimpleNamespace(status_code=code))
        with patch.object(verifier, 'xsrf', return_value='synthetic-xsrf'):
            verifier.verify_source_login_refused(session(), base, 'synthetic-password')
            for value in (session(200), session(500), session(destination='http://foreign.invalid/hub/login'),
                          session(destination=base + '/user/alice/lab')):
                with self.assertRaises(ValueError): verifier.verify_source_login_refused(value, base, 'synthetic-password')

    def test_cleanup_and_loss_controls_require_exact_owned_identity(self):
        name = request()['name']; images = {key: request()[key + '_image'] for key in ('controller', 'hub', 'course')}
        value = {'Id': 'a' * 64, 'Name': '/' + name + '-worker-two-service', 'Image': images['controller'],
            'Config': {'Labels': {'educloud.fixture': name}}, 'State': {'Running': True, 'StartedAt': 'fixed-start'}}
        self.assertEqual(verifier.require_owned_container(value, name, images, 'b' * 32), value['Id'])
        self.assertTrue(verifier.same_running_enforcer(value, copy.deepcopy(value)))
        for mutate in (lambda v: v.update(Image='sha256:' + 'f' * 64),
                       lambda v: v['Config']['Labels'].update({'educloud.fixture': 'foreign'}),
                       lambda v: v.update(Name='/foreign-service')):
            changed = copy.deepcopy(value); mutate(changed)
            with self.assertRaises(ValueError): verifier.require_owned_container(changed, name, images, 'b' * 32)
        for mutate in (lambda v: v.update(Id='c' * 64), lambda v: v['State'].update(StartedAt='replaced'), lambda v: v['State'].update(Running=False)):
            changed = copy.deepcopy(value); mutate(changed); self.assertFalse(verifier.same_running_enforcer(value, changed))

    def test_readiness_wait_has_no_mutation_retries_or_malformed_success(self):
        calls = []; clock = [0.0]
        def sleep(seconds): clock[0] += seconds
        def probe():
            calls.append('readonly')
            return {'ready': len(calls) == 3, 'verified_workers': 2 if len(calls) == 3 else 1}
        verifier.await_worker_readiness(probe, monotonic=lambda: clock[0], sleep=sleep)
        self.assertEqual(calls, ['readonly'] * 3)
        for value in ({'ready': True, 'verified_workers': True}, {'ready': True, 'verified_workers': 1},
                      {'ready': False, 'verified_workers': 2}, {'ready': True, 'verified_workers': 2, 'extra': 'private-canary'}):
            with self.assertRaises(ValueError): verifier.await_worker_readiness(lambda: value)
        with self.assertRaisesRegex(RuntimeError, 'authenticated_workers_not_ready'):
            verifier.await_worker_readiness(lambda: {'ready': False, 'verified_workers': 0}, timeout=.5,
                monotonic=lambda: clock[0], sleep=sleep)
        failures = []
        def invalid(): failures.append(True); raise RuntimeError('fixed-helper-failure')
        with self.assertRaisesRegex(RuntimeError, 'fixed-helper-failure'): verifier.await_worker_readiness(invalid)
        self.assertEqual(failures, [True])


if __name__ == '__main__': unittest.main()
