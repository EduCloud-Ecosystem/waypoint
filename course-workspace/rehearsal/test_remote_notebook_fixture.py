"""Fixture boundaries with fake Docker API; no image builds or daemon calls."""
import contextlib
import copy
import io
import json
import os
import stat
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

    def bootstrap(self, cpu=8):
        import worker_transport
        from runtime_authority import validate_config
        # Preserve the real identity/profile/config contract even though the
        # Docker I/O itself is deliberately replaced in these host unit tests.
        with patch.dict(sys.modules, docker=self.docker), patch.object(fixture, 'DockerBackend', side_effect=lambda config, *a, **kw: SimpleNamespace(config=validate_config(config))), \
                patch.object(worker_transport, 'worker_binding', side_effect=lambda backend, worker: 'b' * 64), \
                patch.object(worker_transport, 'linux_boot_id', return_value='c' * 32), patch.object(fixture.os, 'chown'), \
                patch.object(self.client, 'info', return_value={'NCPU': cpu, 'MemTotal': 8 * 1024**3, 'ID': 'synthetic-fixed-daemon'}):
            return fixture.initialize(self.root, request())

    def test_presets_reserve_all_control_services_and_refuse_inflated_inputs(self):
        for cpu in (4, 5, 6, 7, 8, 12):
            value = fixture.allocation_for_host(cpu, 8 * 1024**3)
            self.assertNotIn('controller_cpu', value)
            controls = value['central_cpu'] + 2 * value['worker_service_cpu'] + 2 * value['guardian_cpu']
            self.assertAlmostEqual(controls, value['host_reserve_cpu'])
            self.assertAlmostEqual(3 * (value['hub_cpu'] + value['learner_cpu']) + controls, value['reserved_cpu'])
            self.assertLessEqual(value['reserved_cpu'], cpu)
        for cpu, memory in [(True, 8 * 1024**3), (3, 8 * 1024**3), (4.0, 8 * 1024**3),
                (float('nan'), 8 * 1024**3), (float('inf'), 8 * 1024**3), (4097, 8 * 1024**3),
                (4, True), (4, 6656 * 1024**2 - 1)]:
            with self.assertRaises(ValueError): fixture.allocation_for_host(cpu, memory)

    def test_eight_cpu_remote_profile_remains_unchanged(self):
        expected = {'name': 'eight-cpu-remote', 'learner_cpu': .25, 'hub_cpu': 1.0,
            'reserved_cpu': 6.75, 'minimum_cpu': 8, 'host_reserve_cpu': 3.0,
            'central_cpu': 1.0, 'worker_service_cpu': .75, 'guardian_cpu': .25}
        for cpu in (8, 9, 16):
            self.assertEqual(fixture.allocation_for_host(cpu, 8 * 1024**3), expected)

    def test_lower_remote_profiles_keep_two_cpus_for_controls_without_changing_legacy(self):
        expected = {4: ('four-cpu-remote', 4, .5, .1, 3.8), 5: ('five-cpu-remote', 5, .75, .2, 4.85),
            6: ('six-cpu-remote', 6, 1.0, .25, 5.75), 7: ('six-cpu-remote', 6, 1.0, .25, 5.75)}
        for cpu, (name, minimum, hub, learner, total) in expected.items():
            with self.subTest(cpu=cpu):
                self.assertEqual(fixture.allocation_for_host(cpu, 8 * 1024**3), {
                    'name': name, 'minimum_cpu': minimum, 'hub_cpu': hub, 'learner_cpu': learner,
                    'reserved_cpu': total, 'host_reserve_cpu': 2.0, 'central_cpu': .5,
                    'worker_service_cpu': .5, 'guardian_cpu': .25})
        previous = [fixture.course_allocation(cpu) for cpu in (4, 5, 6)]
        self.assertEqual([value['name'] for value in previous], ['four-cpu-ci', 'five-cpu-local', 'six-cpu-local'])
        self.assertEqual([value['hub_cpu'] for value in previous], [1.0, 1.0, 1.0])
        self.assertEqual([value['host_reserve_cpu'] for value in previous], [.7, .75, 1.5])
        controls = [fixture.allocation_for_host(cpu, 8 * 1024**3)['host_reserve_cpu'] for cpu in range(4, 10)]
        self.assertEqual(controls, sorted(controls))

    def test_lower_profile_bootstrap_keeps_core_claims_and_applies_real_container_caps(self):
        from runtime_authority import DockerBackend
        parent = self.root
        for cpu in (4, 5, 6, 7):
            with self.subTest(cpu=cpu):
                self.root = parent / str(cpu); self.root.mkdir(mode=0o700)
                for path in (self.root / 'control', self.root / 'backend', *(self.root / 'backend' / worker for worker in fixture.WORKERS)):
                    path.mkdir(mode=0o700, exist_ok=True)
                self.client = FakeClient(); self.docker = SimpleNamespace(DockerClient=lambda **_: self.client)
                allocation = self.bootstrap(cpu)['allocation']; reserves = []
                with patch.object(self.client, 'info', return_value={'NCPU': cpu, 'MemTotal': 8 * 1024**3, 'ID': 'synthetic-fixed-daemon'}):
                    for worker in fixture.WORKERS:
                        config = json.loads((self.root / worker / 'credentials/runtime.json').read_bytes())
                        reserves.append(config['host_reserve_cpu'])
                        self.assertEqual(config['host_reserve_cpu'], 1.0)
                        for item in config['courses'][worker].values():
                            self.assertEqual(item['profile']['cpu'], 1 + allocation['learner_cpu'])
                        backend = DockerBackend.__new__(DockerBackend); backend.config = config; backend.client = self.client
                        backend.daemon()  # Actual conservative local proof must fit the verified daemon.
                self.assertEqual(sum(reserves), allocation['host_reserve_cpu'])
                with patch.dict(sys.modules, docker=self.docker): fixture.launch_workers(self.root); fixture.launch_users(self.root)
                for _, item in self.client.created:
                    cap = item.get('cpu_quota', 0) / item.get('cpu_period', 100000)
                    if item['name'].endswith('-service'): self.assertEqual(cap, allocation['worker_service_cpu'])
                    elif item['name'].endswith('-guardian'): self.assertEqual(cap, allocation['guardian_cpu'])
                    elif item['name'].endswith('-hub'): self.assertEqual(cap, allocation['hub_cpu'])
        self.root = parent

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
            self.assertEqual(item['cpu_quota'], 75000)
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

    def _actual_archive_copy(self, mixed_owners=False):
        import hashlib
        import volume_archive
        from runtime_authority import tree_hash
        configs = {}; homes = {}
        for worker, suffix in [('worker-one', 'source'), ('worker-two', 'receiver')]:
            parent = self.root / suffix; parent.mkdir()
            leaf = parent / (suffix + '-home-' + hashlib.sha256(b'alice').hexdigest()[:24])
            leaf.mkdir(mode=0o755); (leaf / 'work').mkdir(mode=0o755)
            homes[worker] = leaf
            configs[worker] = {'courses': {worker: {'course-main': {'home_root': str(parent), 'instance': suffix}}}}
        source, target = (homes[worker] for worker in fixture.WORKERS)
        runtime = source / '.runtime'; runtime.mkdir(mode=0o700); runtime.chmod(0o1700)
        saved = source / 'work/saved.txt'; saved.write_text('retained-synthetic-bytes'); saved.chmod(0o660)
        marker = runtime / 'metadata'; marker.write_text('root-owned-image-metadata'); marker.chmod(0o600)
        (source / 'saved-link').symlink_to('work/saved.txt')
        if mixed_owners:
            for path in (source, source / 'work', runtime, saved): os.chown(path, 1000, 1000)
            os.chown(source / 'saved-link', 1000, 1000, follow_symlinks=False)
            self.assertEqual((marker.stat().st_uid, marker.stat().st_gid), (0, 0))
        before = tree_hash(source)
        class ActualTreeBackend:
            def __init__(self, config): self.config = config
            def containers(self, *_): return []
            def inspect_course(self, _, worker): return {'retained_tree_sha256': tree_hash(homes[worker])}
        fixture.write(self.root / 'remote-fixture.json', {'workers': configs})
        original_pack, original_unpack = volume_archive.pack, volume_archive.unpack
        # Non-root host tests retain their own numeric owner. Linux/root uses
        # the exact helper arguments and proves real mixed0/1000 restoration.
        def pack(*args): return original_pack(*args[:2], os.geteuid(), os.getegid())
        def unpack(*args): return original_unpack(*args[:2], *(args[2:] if mixed_owners else (os.geteuid(), os.getegid())))
        with patch.object(fixture, 'DockerBackend', ActualTreeBackend), \
                patch.object(fixture, '_COPY_OWNERS', fixture._COPY_OWNERS | {(os.geteuid(), os.getegid())}), \
                patch.object(volume_archive, 'pack', side_effect=pack), patch.object(volume_archive, 'unpack', side_effect=unpack):
            self.assertEqual(fixture.copy_home(self.root), {'copied': True, 'local_recovery_staging': True, 'network_file_transfer': False})
        self.assertEqual(tree_hash(source), before); self.assertEqual(tree_hash(target), before)
        self.assertEqual((target / 'work/saved.txt').read_bytes(), saved.read_bytes())
        self.assertEqual(stat.S_IMODE((target / '.runtime').stat().st_mode), 0o1700)
        self.assertEqual(stat.S_IMODE((target / 'work/saved.txt').stat().st_mode), 0o660)
        self.assertEqual(os.readlink(target / 'saved-link'), 'work/saved.txt')
        if mixed_owners:
            self.assertEqual(((target / '.runtime/metadata').stat().st_uid, (target / '.runtime/metadata').stat().st_gid), (0, 0))
            self.assertEqual(((target / 'work/saved.txt').stat().st_uid, (target / 'work/saved.txt').stat().st_gid), (1000, 1000))

    def test_copy_real_archive_and_tree_hash_preserves_bytes_modes_and_literal_link(self):
        self._actual_archive_copy()

    @unittest.skipUnless(os.geteuid() == 0, 'mixed0/1000 ownership requires Linux/root fixture execution')
    def test_copy_real_archive_preserves_mixed_root_and_learner_ownership(self):
        self._actual_archive_copy(mixed_owners=True)

    def test_owner_restore_refuses_ancestor_symlink_without_touching_outside(self):
        home = self.root / 'home'; home.mkdir()
        outside = self.root / 'outside'; outside.mkdir(); (outside / 'sentinel').write_text('unchanged')
        (home / 'swapped').symlink_to(outside, target_is_directory=True)
        allowed = fixture._COPY_OWNERS | {(os.geteuid(), os.getegid())}
        owners = {(): (stat.S_IFDIR, os.geteuid(), os.getegid()), ('swapped',): (stat.S_IFDIR, 0, 0),
            ('swapped', 'sentinel'): (stat.S_IFREG, 0, 0)}
        before = (outside / 'sentinel').stat()
        with patch.object(fixture, '_COPY_OWNERS', allowed), patch.object(fixture.os, 'chown') as chown:
            with self.assertRaisesRegex(ValueError, 'remote_copy_membership_changed'):
                fixture._restore_home_owners(home, owners)
            chown.assert_not_called()
        after = (outside / 'sentinel').stat()
        self.assertEqual((after.st_ino, after.st_uid, after.st_gid), (before.st_ino, before.st_uid, before.st_gid))
        self.assertEqual((outside / 'sentinel').read_text(), 'unchanged')

    def test_owner_restore_refuses_outside_hardlink_before_any_ownership_effect(self):
        home = self.root / 'home'; home.mkdir()
        outside = self.root / 'outside'; outside.write_text('outside-hardlink-sentinel')
        os.link(outside, home / 'alias'); before = outside.stat()
        owners = {(): (stat.S_IFDIR, 0, 0), ('alias',): (stat.S_IFREG, 1000, 1000)}
        with patch.object(fixture, '_COPY_OWNERS', fixture._COPY_OWNERS | {(os.geteuid(), os.getegid())}), \
                patch.object(fixture.os, 'fchown') as fchown, patch.object(fixture.os, 'chown') as chown:
            with self.assertRaisesRegex(ValueError, 'remote_copy_hardlink_refused'):
                fixture._restore_home_owners(home, owners)
            fchown.assert_not_called(); chown.assert_not_called()
        after = outside.stat()
        self.assertEqual((after.st_ino, after.st_uid, after.st_gid), (before.st_ino, before.st_uid, before.st_gid))
        self.assertEqual(outside.read_text(), 'outside-hardlink-sentinel')

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

    def test_rpc_hub_operations_match_real_client_wait_and_preserve_other_defaults(self):
        from unittest.mock import Mock
        credentials = self.root / 'credentials'; credentials.mkdir()
        settings = {'url': 'https://authority.invalid:8443', 'authority_id': 'a' * 32,
            'ca_file': '/synthetic/ca.crt', 'certfile': '/synthetic/client.crt', 'keyfile': '/synthetic/client.key'}
        fixture.write(credentials / 'client.json', settings)
        api = Mock(); api.call.return_value = {'synthetic_result': True}
        with patch('hub.authority_client.AuthorityClient', return_value=api) as constructor:
            for operation in ('hub.authorize','hub.spawn','hub.poll','hub.stop','guardian.observe','ownership.inventory'):
                api.reset_mock(); constructor.reset_mock()
                payload = {'operation': operation, 'payload': {'unchanged': ['synthetic', 1]},
                    'worker': 'worker-one', 'course': 'course-main'}
                original = copy.deepcopy(payload)
                self.assertEqual(fixture.rpc(credentials, payload), {'ok': True, 'result': {'synthetic_result': True}})
                expected = dict(settings, worker_id='worker-one', course_id='course-main')
                if operation in ('hub.authorize','hub.spawn','hub.poll','hub.stop'): expected['timeout'] = 10
                constructor.assert_called_once_with(**expected)
                api.call.assert_called_once_with(operation, payload['payload'])
                self.assertIs(api.call.call_args.args[1], payload['payload'])
                self.assertEqual(payload, original)

    def test_hub_transport_failure_is_not_retried_or_converted_to_success(self):
        from hub.authority_client import AuthorityError
        from unittest.mock import Mock
        credentials = self.root / 'credentials'; credentials.mkdir()
        fixture.write(credentials / 'client.json', {'url': 'https://authority.invalid:8443'})
        api = Mock(); api.call.side_effect = AuthorityError('authority_transport_unavailable')
        payload = {'operation':'hub.spawn','payload':{'expected_epoch':1},'worker':'worker-one','course':'course-main'}
        original = copy.deepcopy(payload)
        with patch('hub.authority_client.AuthorityClient', return_value=api) as constructor:
            self.assertEqual(fixture.rpc(credentials,payload),
                {'ok':False,'refused':True,'code':'authority_transport_unavailable'})
        self.assertEqual(constructor.call_count,1); self.assertEqual(constructor.call_args.kwargs['timeout'],10)
        api.call.assert_called_once_with('hub.spawn',payload['payload']); self.assertEqual(payload,original)

    def test_operator_budget_is_explicit_only_in_its_config_and_never_retries_a_mutation(self):
        from hub.authority_client import AuthorityError
        from unittest.mock import Mock
        self.bootstrap()
        settings = json.loads((self.root/'operator/credentials/client.json').read_bytes())
        self.assertEqual(settings['timeout'],10)
        for identity in ('source','peer','receiver','receiver-rotated','guardian-one','guardian-two'):
            config = json.loads((self.root/identity/'credentials/client.json').read_bytes())
            self.assertNotIn('timeout',config)
        api=Mock(); api.call.return_value={'synthetic_result':True}
        credentials=self.root/'operator/credentials'
        operations={'ownership.preview':{'receiver_worker_id':'worker-two'},
            'ownership.reconcile_preview':{'selected_worker_id':'worker-two'},
            'ownership.apply':{'plan_id':'a'*32,'plan_sha256':'b'*64},
            'ownership.reconcile_apply':{'plan_id':'a'*32,'plan_sha256':'b'*64},'ownership.inventory':{}}
        with patch('hub.authority_client.AuthorityClient',return_value=api) as constructor:
            for operation,body in operations.items():
                api.reset_mock(); constructor.reset_mock()
                payload={'operation':operation,'payload':body,'worker':'worker-one','course':'course-main'}
                original=copy.deepcopy(payload)
                self.assertEqual(fixture.rpc(credentials,payload),{'ok':True,'result':{'synthetic_result':True}})
                constructor.assert_called_once_with(worker_id='worker-one',course_id='course-main',**settings)
                api.call.assert_called_once_with(operation,body); self.assertIs(api.call.call_args.args[1],body)
                self.assertEqual(payload,original)
            api.reset_mock(); constructor.reset_mock()
            api.call.side_effect=AuthorityError('authority_transport_unavailable')
            payload={'operation':'ownership.reconcile_apply','payload':operations['ownership.reconcile_apply'],
                'worker':'worker-one','course':'course-main'}
            self.assertEqual(fixture.rpc(credentials,payload),{'ok':False,'refused':True,'code':'authority_transport_unavailable'})
            constructor.assert_called_once_with(worker_id='worker-one',course_id='course-main',**settings)
            api.call.assert_called_once_with('ownership.reconcile_apply',payload['payload'])


class ReportTests(unittest.TestCase):
    def test_actual_hub_cpu_proof_observes_all_three_owned_hubs_and_rejects_changed_caps(self):
        name = request()['name']; images = {'hub': request()['hub_image']}
        records = {}; observed = []; allocation = fixture.allocation_for_host(4, 8 * 1024**3)
        for index, (_, _, suffix) in enumerate(fixture.ENDPOINTS):
            expected = name + '-' + suffix + '-hub'
            records[expected] = {'Name': '/' + expected, 'Id': format(index + 1, '064x'), 'Image': images['hub'],
                'Config': {'Labels': {'educloud.workspace.instance': name + '-' + suffix, 'educloud.workspace.kind': 'hub'}},
                'HostConfig': {'NanoCpus': 0, 'CpuPeriod': 100000, 'CpuQuota': 50000}}
        def inspect(expected): observed.append(expected); return records[expected]
        verifier.assert_hub_cpu_caps(name, images, 'b' * 32, allocation, inspect)
        self.assertEqual(observed, list(records))
        for expected in records:
            with self.subTest(expected=expected):
                records[expected]['HostConfig']['CpuQuota'] = 100000
                with self.assertRaisesRegex(ValueError, 'fixture_hub_cpu_unverified'):
                    verifier.assert_hub_cpu_caps(name, images, 'b' * 32, allocation, inspect)
                records[expected]['HostConfig']['CpuQuota'] = 50000
        with self.assertRaisesRegex(ValueError, 'fixture_hub_cpu_unverified'):
            verifier.assert_hub_cpu_caps(name, images, 'b' * 32, allocation, lambda expected: records[next(reversed(records))])
        records[next(iter(records))]['Image'] = 'sha256:' + 'f' * 64
        with self.assertRaisesRegex(ValueError, 'fixture_container_ownership_unverified'):
            verifier.assert_hub_cpu_caps(name, images, 'b' * 32, allocation, inspect)

    def test_hub_readiness_waits_for_http_startup_but_refuses_denial(self):
        from unittest.mock import Mock
        def response(status):
            value = verifier.requests.Response(); value.status_code = status; return value
        session = Mock(); session.get.side_effect = [verifier.requests.exceptions.ConnectionError(), response(503), response(200)]
        verifier.await_hub_readiness(session, 'http://127.0.0.1:1234', sleep=lambda _: None)
        self.assertEqual(session.get.call_count, 3)
        session.post.assert_not_called()
        session.get.assert_called_with('http://127.0.0.1:1234/hub/login', timeout=3, allow_redirects=False)
        session = Mock(); session.get.return_value = response(403)
        with self.assertRaises(verifier.requests.exceptions.HTTPError):
            verifier.await_hub_readiness(session, 'http://127.0.0.1:1234')
        self.assertEqual(session.get.call_count, 1)
        session.get.return_value = response(503)
        times = iter([0, 2])
        with self.assertRaisesRegex(RuntimeError, 'hub_not_ready'):
            verifier.await_hub_readiness(session, 'http://127.0.0.1:1234', timeout=1, monotonic=lambda: next(times))

    def test_hub_readiness_404_startup_is_bounded_and_never_accepts_redirect_or_denial(self):
        from unittest.mock import Mock
        def response(status):
            value = verifier.requests.Response(); value.status_code = status; return value
        session = Mock(); session.get.side_effect = [response(404), response(200)]
        verifier.await_hub_readiness(session, 'http://127.0.0.1:1234', sleep=lambda _: None)
        self.assertEqual(session.get.call_count, 2); session.post.assert_not_called()
        self.assertTrue(all(call.kwargs['allow_redirects'] is False for call in session.get.call_args_list))
        clock = [0.0]
        session = Mock(); session.get.return_value = response(404)
        def sleep(seconds): clock[0] += seconds
        with self.assertRaisesRegex(RuntimeError, 'hub_not_ready'):
            verifier.await_hub_readiness(session, 'http://127.0.0.1:1234', timeout=1,
                monotonic=lambda: clock[0], sleep=sleep)
        self.assertEqual(clock[0], 1); self.assertEqual(session.get.call_count, 4); session.post.assert_not_called()
        for status in (301, 302, 307, 308, 401, 403, 500):
            session = Mock(); session.get.return_value = response(status)
            with self.assertRaises((verifier.requests.exceptions.HTTPError, ValueError)):
                verifier.await_hub_readiness(session, 'http://127.0.0.1:1234')
            self.assertEqual(session.get.call_count, 1); session.post.assert_not_called()

    def test_failure_context_excludes_messages_urls_bodies_and_external_frames(self):
        response = verifier.requests.Response(); response.status_code = 403
        response.url = 'https://private.invalid/secret'; response._content = b'private-body-canary'
        try:
            raise verifier.requests.exceptions.HTTPError('private-message-canary', response=response)
        except verifier.requests.exceptions.HTTPError as error:
            value = verifier.failure_context(error, list(verifier.PHASES[:14]))
        self.assertEqual(value, {'phase': verifier.PHASES[14], 'exception': 'HTTPError', 'lines': [], 'http_status': 403})
        response._content = b'XSRF cookie does not match POST argument; private-cookie-canary'
        response.request = verifier.requests.Request('POST', 'https://private.invalid/secret').prepare()
        value = verifier.failure_context(verifier.requests.exceptions.HTTPError('private-message', response=response), [])
        self.assertEqual(value['request_verification'], 'xsrf'); self.assertEqual(value['http_method'], 'POST')
        self.assertNotIn('private', json.dumps(value))
        private_type = type('private-class-canary', (Exception,), {})
        self.assertEqual(verifier.failure_context(private_type('private-message'), [])['exception'], 'OtherFailure')

    def test_xsrf_diagnostic_requires_403_and_a_fixed_runtime_denial_phrase(self):
        cases = [(503, '<form><input name="_xsrf" value="private-cookie-canary"></form>', False),
            (403, '<form><input name="_xsrf" value="private-cookie-canary"></form>', False),
            (503, 'XSRF cookie does not match POST argument', False),
            (403, "'_xsrf' argument missing from POST", True),
            (403, "'_xsrf' argument invalid", True),
            (403, 'XSRF cookie does not match POST argument', True)]
        for status, body, classified in cases:
            response = verifier.requests.Response(); response.status_code = status; response._content = body.encode()
            value = verifier.failure_context(verifier.requests.exceptions.HTTPError('private-message', response=response), [])
            self.assertEqual(value.get('request_verification') == 'xsrf', classified)
            self.assertNotIn('private', json.dumps(value))

    def test_explicit_hub_stop_waits_for_stopped_owned_learner_removal(self):
        calls = []; remaining = ['stopped-owned-container', '']
        def command(*args):
            calls.append(args)
            self.assertEqual(args, ('ps', '-aq', '--filter',
                'label=educloud.workspace.instance=' + request()['name'] + '-source',
                '--filter', 'label=educloud.workspace.kind=learner'))
            return remaining.pop(0)
        def wait_for(predicate, timeout):
            self.assertEqual(timeout, 30)
            self.assertIs(predicate(), False)  # Running-only ps would incorrectly pass here.
            self.assertIs(predicate(), True)
        verifier.wait_for_learner_removal(request()['name'], 'source', command, wait_for)
        self.assertEqual(len(calls), 2); self.assertEqual(remaining, [])

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

    def test_v3_resource_reports_cannot_relabel_old_versions_or_lower_profiles(self):
        self.assertEqual(verifier.VERSION, 'waypoint-remote-notebook-rehearsal-v3')
        for cpu in (4, 5, 6, 7, 8):
            current = self.report(); current['host']['cpu'] = cpu
            current['allocation'] = fixture.allocation_for_host(cpu, current['host']['memory_bytes'])
            verifier.validate_report(current)
            historical = copy.deepcopy(current); historical['version'] = 'waypoint-remote-notebook-rehearsal-v2'
            with self.assertRaises(ValueError): verifier.validate_report(historical)
            if cpu < 8:
                legacy = fixture.course_allocation(cpu); legacy.pop('controller_cpu')
                central = .5 if cpu >= 6 else (.25 if cpu == 5 else .2)
                worker = round((legacy['host_reserve_cpu'] - central - 2 * legacy['guardian_cpu']) / 2, 6)
                current['allocation'] = dict(legacy, central_cpu=central, worker_service_cpu=worker)
                with self.assertRaises(ValueError): verifier.validate_report(current)

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

    def test_rpc_refusal_diagnostic_emits_only_protocol_enums(self):
        from hub.authority_client import OPERATIONS, ERRORS
        for operation in OPERATIONS:
            for code in ERRORS:
                reply = {'ok': False, 'refused': True, 'code': code, 'result': {'private': 'private-payload-canary'},
                    'message': 'private-token-path-cert-canary'}
                self.assertEqual(verifier.rpc_refusal_context(operation, reply), {'operation': operation, 'code': code})
        for operation in ('private-operation-canary', None, [], {}):
            for reply in ({'code': 'private-code-canary', 'result': 'private-result-canary'}, {'code': []}, None, []):
                observed = verifier.rpc_refusal_context(operation, reply)
                self.assertEqual(observed, {'operation': 'other', 'code': 'other'})
                self.assertNotIn('private', json.dumps(observed))

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

    def _cleanup_model(self, *, vanish=None, failed_remove=False, foreign=False, replace=False):
        name = request()['name']; images = {key: request()[key + '_image'] for key in ('controller', 'hub', 'course')}
        authority = 'b' * 32; records = {}; events = []; subject = __import__('hashlib').sha256(b'alice').hexdigest()[:24]
        names = [name + '-source-hub', name + '-authority', name + '-worker-one-guardian', name + '-worker-one-service',
            name + '-source-learner-' + subject]
        for index, actual in enumerate(names, 1):
            identifier = format(index, '064x'); kind = 'learner' if '-learner-' in actual else ('hub' if actual.endswith('-hub') else 'control')
            labels = {'educloud.fixture': name}
            if kind in ('learner', 'hub'):
                labels = {'educloud.workspace.instance': name + '-source', 'educloud.workspace.kind': kind}
            if kind == 'learner': labels.update({'educloud.runtime.authority': authority, 'educloud.runtime.worker': 'worker-one',
                'educloud.runtime.course': 'course-main', 'educloud.runtime.subject': subject, 'educloud.runtime.epoch': '1'})
            if actual.endswith('-guardian'): labels = {'educloud.runtime.authority': authority,
                'educloud.runtime.worker': 'worker-one', 'educloud.runtime.kind': 'guardian'}
            records[identifier] = {'Id': identifier, 'Name': '/' + actual,
                'Image': images['course' if kind == 'learner' else ('hub' if kind == 'hub' else 'controller')],
                'Config': {'Labels': labels}}
        victim = format(2, '064x')
        if foreign: records[victim]['Config']['Labels']['educloud.fixture'] = 'foreign'
        def invoke(*args):
            events.append(args)
            if args == ('ps', '-a', '--no-trunc', '--format', '{{.ID}} {{.Names}}'):
                return '\n'.join(identifier + ' ' + value['Name'].lstrip('/') for identifier, value in records.items())
            if args[:3] == ('ps', '-aq', '--no-trunc'):
                expected = args[-1].split('=', 2)[-1]
                return '\n'.join(identifier for identifier, value in records.items()
                    if value['Config']['Labels'].get('educloud.workspace.instance') == expected)
            if args[0] == 'inspect':
                if args[1] == victim and vanish == 'inspect':
                    records.pop(victim); raise RuntimeError('docker_fixture_command_failed:inspect')
                return json.dumps([records[args[1]]])
            if args[:2] == ('rm', '-f'):
                if args[2] == victim and failed_remove: raise RuntimeError('docker_fixture_command_failed:rm')
                value = records.pop(args[2])
                if args[2] == victim and replace:
                    replacement = 'f' * 64; value['Id'] = replacement; records[replacement] = value
                if args[2] == victim and vanish == 'rm': raise RuntimeError('docker_fixture_command_failed:rm')
                return ''
            raise AssertionError('unexpected cleanup operation')
        return name, images, authority, records, events, invoke

    def test_cleanup_controls_are_removed_before_learner_inspection(self):
        name, images, authority, records, events, invoke = self._cleanup_model()
        verifier.cleanup_participant_containers(name, images, authority, invoke)
        self.assertEqual(records, {})
        removed = [event[-1] for event in events if event[:2] == ('rm', '-f')]
        self.assertEqual(removed[-1], format(5, '064x'))
        first_learner_list = next(i for i, event in enumerate(events) if event[:3] == ('ps', '-aq', '--no-trunc'))
        self.assertTrue(all(events.index(('rm', '-f', format(index, '064x'))) < first_learner_list for index in range(1, 5)))

    def test_cleanup_tolerates_only_exact_id_disappearance(self):
        for operation in ('inspect', 'rm'):
            name, images, authority, records, _, invoke = self._cleanup_model(vanish=operation)
            verifier.cleanup_participant_containers(name, images, authority, invoke)
            self.assertEqual(records, {})
        for options, error in (({'failed_remove': True}, RuntimeError), ({'foreign': True}, ValueError), ({'replace': True}, ValueError)):
            name, images, authority, records, events, invoke = self._cleanup_model(**options)
            with self.assertRaises(error): verifier.cleanup_participant_containers(name, images, authority, invoke)
            self.assertTrue(records)
            if options.get('foreign'): self.assertNotIn(('rm', '-f', format(2, '064x')), events)
            if options.get('replace'): self.assertNotIn(('rm', '-f', 'f' * 64), events)

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


class RuntimeDiagnosticsTests(unittest.TestCase):
    def model(self):
        name = request()['name']; authority = 'b' * 32
        images = {key: request()[key + '_image'] for key in ('controller', 'hub', 'course')}
        records = {}; events = []; subject = __import__('hashlib').sha256(b'alice').hexdigest()[:24]
        records[name + '-authority'] = {'Id': 'f' * 64, 'Name': '/' + name + '-authority', 'Image': images['controller'],
            'Config': {'Labels': {'educloud.fixture': name}}, 'State': {'Running': True, 'OOMKilled': False, 'ExitCode': 0}}
        for index, (worker, kind) in enumerate(((worker, kind) for worker in fixture.WORKERS for kind in ('service', 'guardian')), 1):
            actual = name + '-' + worker + '-' + kind
            labels = {'educloud.fixture': name} if kind == 'service' else {'educloud.runtime.authority': authority,
                'educloud.runtime.worker': worker, 'educloud.runtime.kind': 'guardian'}
            records[actual] = {'Id': format(index, '064x'), 'Name': '/' + actual, 'Image': images['controller'],
                'Config': {'Labels': labels}, 'State': {'Running': True, 'OOMKilled': False, 'ExitCode': 0}}
        for index, suffix in enumerate(('source', 'peer'), 10):
            actual = name + '-' + suffix + '-learner-' + subject
            labels = {'educloud.workspace.instance': name + '-' + suffix, 'educloud.workspace.kind': 'learner',
                'educloud.runtime.authority': authority, 'educloud.runtime.worker': 'worker-one',
                'educloud.runtime.course': 'course-main' if suffix == 'source' else 'course-peer',
                'educloud.runtime.subject': subject, 'educloud.runtime.epoch': '1'}
            records[actual] = {'Id': format(index, '064x'), 'Name': '/' + actual, 'Image': images['course'],
                'Config': {'Labels': labels}, 'State': {'Running': suffix == 'peer', 'OOMKilled': False, 'ExitCode': 137 if suffix == 'source' else 0}}
        def invoke(*args, **kwargs):
            events.append(('docker', args)); self.assertEqual(kwargs, {'timeout': 3})
            if args[0] == 'inspect':
                value = next(value for actual, value in records.items() if args[1] in (actual, value['Id']))
                return json.dumps([value])
            self.assertEqual(args[:3], ('ps', '-aq', '--no-trunc'))
            instance = args[4].split('=', 2)[-1]
            return '\n'.join(value['Id'] for value in records.values() if value['Config']['Labels'].get('educloud.workspace.instance') == instance)
        def logs(identifier):
            events.append(('logs', identifier))
            return ('private-token-cert-path-canary\nWorker runtime stopping owned learners: stale_or_changed\n'
                'Guardian observation refused: authority_transport_unavailable\n'
                'authority_transport_failure stage=response_headers category=timeout\n'
                'authority_replay_failure stage=begin category=sqlite_busy\n', False)
        return name, authority, images, records, events, invoke, logs

    def test_runtime_diagnostics_capture_owned_state_first_and_never_raw_material(self):
        name, authority, images, _, events, invoke, logs = self.model()
        result = verifier.runtime_diagnostics(RuntimeError('private-exception'), name, images, authority, invoke=invoke, read_logs=logs)
        self.assertEqual(result['learners']['source']['states'], [{'running': False, 'oom_killed': False, 'exit_code': 137}])
        self.assertEqual(result['learners']['source']['running_count'], 0)
        self.assertEqual(result['learners']['peer']['running_count'], 1)
        self.assertEqual(result['learners']['receiver']['container_count'], 0)
        self.assertEqual(len(result['enforcers']), 4)
        self.assertTrue(result['authority']['logs_available'])
        self.assertEqual(result['authority']['counts']['replay']['begin/sqlite_busy'], 1)
        self.assertTrue(all(value['available'] and value['logs_available'] for value in result['enforcers'].values()))
        first_log = next(index for index, event in enumerate(events) if event[0] == 'logs')
        self.assertEqual(sum(event[0] == 'docker' and event[1][0] == 'ps' for event in events[:first_log]), 3)
        raw = json.dumps(result)
        for secret in ('private-exception', 'private-token-cert-path-canary', name, authority, images['course'], format(10, '064x')): self.assertNotIn(secret, raw)
        self.assertTrue(all(event[0] == 'logs' or event[1][0] in ('ps', 'inspect') for event in events))

    def test_foreign_control_is_unavailable_without_log_read_and_peer_collection_continues(self):
        name, authority, images, records, events, invoke, logs = self.model()
        value = records[name + '-worker-one-service']; value['Config']['Labels']['educloud.fixture'] = 'foreign'
        result = verifier.runtime_diagnostics(ValueError('private'), name, images, authority, invoke=invoke, read_logs=logs)
        self.assertIs(result['enforcers']['worker-one-service']['available'], False)
        self.assertNotIn(('logs', value['Id']), events)
        self.assertIs(result['enforcers']['worker-two-service']['available'], True)

    def test_log_categories_reject_unknown_suffixes_and_private_messages(self):
        raw = ('Worker runtime supervisor failed: supervision/runtime_endpoint_enforcement_unavailable\n'
            'Guardian snapshot deferred: endpoint_busy\nGuardian stop deferred: endpoint_busy\n'
            'Guardian observation refused: private-token-canary\n'
            'Worker runtime stopping owned learners: private-path-canary\n'
            'authority_transport_failure stage=private_path category=timeout\n'
            'authority_transport_failure stage=response_headers category=private_key\n'
            'authority_replay_failure stage=private_stage category=sqlite_busy\n'
            'authority_replay_failure stage=begin category=private_reason\n'
            'authority_replay_failure stage=begin category=sqlite_busy\n'
            'authority_transport_failure stage=response_headers category=timeout\n')
        result = verifier.diagnostic_log_counts(raw)
        self.assertEqual(result['supervisor']['runtime_endpoint_enforcement_unavailable'], 1)
        self.assertEqual(result['snapshot_busy'], 1); self.assertEqual(result['stop_busy'], 1)
        self.assertEqual(sum(result['guardian'].values()), 0)
        self.assertEqual(sum(result['transport'].values()), 1)
        self.assertEqual(sum(result['replay'].values()), 1)
        for secret in ('private-token-canary', 'private-path-canary', 'private_path', 'private_key'):
            self.assertNotIn(secret, json.dumps(result))

    def test_http_diagnostics_use_only_path_content_and_json_message_categories(self):
        response = verifier.requests.Response(); response.status_code = 403
        response.headers['Content-Type'] = 'application/json; charset=utf-8'
        response._content = json.dumps({'message': 'Forbidden', 'private': 'private-token-canary'}).encode()
        for path, category in [('/user/alice/api/kernels', 'notebookkernel'), ('/hub/user/alice/api/kernels', 'hubfallback'),
                ('/hub/login', 'hublogin'), ('/user/alice/oauth_callback', 'oauth'), ('/private-secret', 'other')]:
            response.url = 'https://private.invalid' + path
            result = verifier.diagnostic_http_context(verifier.requests.exceptions.HTTPError('private-message', response=response))
            self.assertEqual(result['final_path_category'], category); self.assertEqual(result['content_type_category'], 'json')
            self.assertEqual(result['json403_message_class'], 'forbidden'); self.assertNotIn('private', json.dumps(result))
        response._content = b'{"message":"private-arbitrary-secret"}'
        self.assertEqual(verifier.diagnostic_http_context(verifier.requests.exceptions.HTTPError(response=response))['json403_message_class'], 'other')
        response.headers['Content-Type'] = 'text/html'; response._content = b'<input name="_xsrf" value="private">'
        self.assertEqual(verifier.diagnostic_http_context(verifier.requests.exceptions.HTTPError(response=response))['json403_message_class'], 'not_applicable')

    def test_actual_log_subprocess_is_byte_bounded_and_owned_process_is_reaped(self):
        processes = []
        def launch(args, **kwargs):
            self.assertEqual(args[:4], ['docker', 'logs', '--tail', '2000'])
            process = verifier.subprocess.Popen([sys.executable, '-c', 'import os;os.write(1,b"x"*262144)'], **kwargs)
            processes.append(process); return process
        raw, truncated = verifier.bounded_container_logs('a' * 64, popen=launch)
        self.assertEqual(len(raw.encode()), 131072); self.assertIs(truncated, True)
        self.assertIsNotNone(processes[0].poll()); self.assertTrue(processes[0].stdout.closed)
        def waiting(args, **kwargs):
            process = verifier.subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'], **kwargs)
            processes.append(process); return process
        clock = iter([0, 4])
        with self.assertRaisesRegex(RuntimeError, 'fixture_diagnostic_unavailable'):
            verifier.bounded_container_logs('a' * 64, popen=waiting, monotonic=lambda: next(clock))
        self.assertIsNotNone(processes[-1].poll()); self.assertTrue(processes[-1].stdout.closed)

    def test_capture_failures_publish_only_unavailable_and_never_escape_to_cleanup(self):
        from unittest.mock import Mock
        collect = Mock(side_effect=ValueError('private-diagnostic-canary')); publish = Mock()
        verifier.capture_runtime_diagnostics(Path('/unused'), RuntimeError('private-original'), 'unused', {}, None, collect=collect, publish=publish)
        self.assertEqual(publish.call_args.args[1], {'version': 'waypoint-remote-runtime-diagnostics-v2',
            'scope': 'post-failure-nonatomic-readonly', 'available': False})
        publish.side_effect = RuntimeError('private-publication-canary')
        verifier.capture_runtime_diagnostics(Path('/unused'), RuntimeError('private-original'), 'unused', {}, None, collect=collect, publish=publish)


if __name__ == '__main__': unittest.main()
