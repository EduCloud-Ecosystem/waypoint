"""Runtime controls use real private SQLite authority/barriers and injected Docker."""
import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ownership_contract import OwnershipError, digest
from runtime_authority import (DockerBackend, RuntimeDispatcher, RuntimeRefusal, VERSION, bootstrap,
                               tree_hash, file_hash, ADMISSION_UNUSED_SNAPSHOT, ADMISSION_UNUSED_RETAINED,
                               _GuardianAcquisitionBusy)


class Container:
    def __init__(self, owner, name, image, labels, **options):
        self.owner, self.name = owner, name
        self.id = uuid.uuid4().hex + uuid.uuid4().hex
        self.actions = []
        env = options.get('environment', {})
        networks = {options['network']: {'IPAddress': '172.20.0.2'}} if 'network' in options else {}
        self.attrs = {'Name': '/' + name, 'Image': image, 'Config': {'Labels': labels,
            'User': options.get('user', '0'), 'Env': [key + '=' + value for key, value in env.items()]},
            'State': {'Running': False, 'ExitCode': 0, 'StartedAt': '2026-10-14T00:00:00Z'},
            'NetworkSettings': {'Networks': networks}, 'Mounts': [], 'HostConfig': {
                'Privileged': False, 'ReadonlyRootfs': options.get('read_only', False),
                'CapDrop': options.get('cap_drop'), 'SecurityOpt': options.get('security_opt'),
                'PidsLimit': options.get('pids_limit'), 'Memory': options.get('mem_limit'),
                'MemorySwap': options.get('memswap_limit'), 'CpuPeriod': options.get('cpu_period'),
                'CpuQuota': options.get('cpu_quota'), 'RestartPolicy': options.get('restart_policy', {'Name': 'no'})}}
        for name, item in options.get('volumes', {}).items():
            self.attrs['Mounts'].append({'Type': 'volume', 'Name': name, 'Destination': item['bind'], 'RW': item['mode'] == 'rw'})

    def reload(self):
        if self.id not in self.owner.items: raise RuntimeError('removed fixture container')
    def start(self): self.actions.append('start'); self.attrs['State']['Running'] = True
    def update(self, **kwargs): self.actions.append(('update', kwargs)); self.attrs['HostConfig']['RestartPolicy'] = kwargs['restart_policy']
    def stop(self, timeout): self.actions.append(('stop', timeout)); self.attrs['State']['Running'] = False
    def remove(self, v, force):
        self.actions.append(('remove', v, force)); del self.owner.items[self.id]


class Containers:
    def __init__(self): self.items = {}; self.created = []
    def get(self, identity):
        for value in self.items.values():
            if identity in (value.id, value.name): return value
        raise KeyError(identity)
    def list(self, all, filters):
        labels = filters.get('label', [])
        return [value for value in self.items.values() if all_label(value, labels)]
    def create(self, image, name, labels, **kwargs):
        if any(item.name == name for item in self.items.values()): raise ValueError('name collision')
        value = Container(self, name, image, labels, **kwargs)
        self.items[value.id] = value; self.created.append((value, kwargs))
        return value


def all_label(value, labels):
    return all(value.attrs['Config']['Labels'].get(key) == expected for key, expected in (entry.split('=', 1) for entry in labels))


class Network:
    def __init__(self, name, labels): self.name = name; self.attrs = {'Labels': labels, 'Internal': True}
    def reload(self): pass
    def connect(self, container, aliases): container.attrs['NetworkSettings']['Networks'][self.name] = {'IPAddress': '172.20.0.3'}


class Networks:
    def __init__(self): self.items = {}
    def list(self, names): return [self.items[name] for name in names if name in self.items]
    def create(self, name, internal, labels):
        value = Network(name, labels); value.attrs['Internal'] = internal; self.items[name] = value; return value


class Docker:
    def __init__(self):
        self.containers = Containers(); self.networks = Networks(); self.volumes = type('Volumes', (), {})()
        self.volume_items = {}; self.volumes.get = self.volume_items.__getitem__
        self.image_labels = {}
        self.images = type('Images', (), {})()
        self.images.get = lambda image_id: type('Image', (), {'attrs': {'Id': image_id, 'Config': {'Labels': self.image_labels.get(image_id, {})}}})()
        self.info_value = {'ID': 'owned-test-daemon', 'MemTotal': 16 * 1024**3, 'NCPU': 8}
    def info(self): return self.info_value.copy()


class RuntimeFixture:
    def __init__(self, directory):
        self.directory = Path(directory).resolve(); self.client = Docker(); self.now = 100.0
        self.image = 'sha256:' + 'c' * 64
        self.profile = {'active_limit': 1, 'learner_memory_mb': 512, 'learner_cpu': 0.25, 'memory_mb': 1536, 'cpu': 1.25}
        workers = {}; courses = {}; guardians = {}
        for worker in ('worker-one', 'worker-two'):
            workers[worker] = {'worker_id': worker, 'provider_id': worker + '-provider', 'boot_id': ('1' if worker == 'worker-one' else '2') * 32,
                'policy_sha256': 'a' * 64, 'memory_budget_mb': 3072 if worker == 'worker-one' else 1536,
                'cpu_budget': 2.5 if worker == 'worker-one' else 1.25, 'capacity_memory_mb': 16384, 'capacity_cpu': 8}
            names = ('course-main', 'course-peer') if worker == 'worker-one' else ('course-main',)
            courses[worker] = {}
            guardians[worker] = {'container_name': worker + '-guardian', 'image_id': self.image, 'heartbeat_seconds': 3}
            for course in names:
                instance = worker + '-' + course
                home = self.directory / instance; home.mkdir(mode=0o700)
                for subject in ('alice', 'bob'):
                    key = hashlib.sha256(subject.encode()).hexdigest()[:24]
                    volume = instance + '-home-' + key
                    path = home / volume; path.mkdir(mode=0o700); (path / 'work').mkdir(mode=0o700)
                    (path / 'work' / 'saved.txt').write_text('retained learner bytes\n')
                    self.client.volume_items[volume] = type('Volume', (), {'attrs': {
                        'Labels': {'educloud.workspace.instance': instance, 'educloud.workspace.kind': 'home'},
                        'Options': {'type': 'none', 'o': 'bind', 'device': str(path)}}})()
                snapshot = self.directory / (instance + '.snapshot'); snapshot.write_bytes(b'opaque fixture snapshot')
                runtime = self.directory / (instance + '.runtime'); runtime.write_bytes(b'fixed image runtime')
                courses[worker][course] = {'instance': instance, 'logical_identity': {
                    'origin': 'https://' + course + '.test', 'dns_subtree': course + '.test', 'issuer': 'https://identity.test/realms/' + course,
                    'client_id': course}, 'profile': self.profile.copy(), 'image_id': self.image, 'hub_container': instance + '-hub',
                    'subjects': ['alice', 'bob'], 'home_root': str(home), 'snapshot_path': str(snapshot), 'runtime_path': str(runtime)}
                hub = self.client.containers.create(self.image, instance + '-hub',
                    {'educloud.workspace.instance': instance, 'educloud.workspace.kind': 'hub'}); hub.start()
        config = {'version': VERSION, 'authority_id': '0' * 32, 'daemon_id': 'owned-test-daemon',
                  'backend_directory': str(self.directory / 'backend'), 'workers': workers, 'courses': courses,
                  'guardians': guardians, 'host_reserve_mb': 2048, 'host_reserve_cpu': 1}
        self.authority, self.backend, self.config = bootstrap(config, self.directory / 'authority', self.client)
        self.backend.clock = lambda: self.now
        for worker in workers:
            guardian = self.client.containers.create(self.image, worker + '-guardian', {
                'educloud.runtime.authority': self.authority.authority_id, 'educloud.runtime.worker': worker, 'educloud.runtime.kind': 'guardian'})
            guardian.start()
            self.authority.register_worker(worker)
        for course in ('course-main', 'course-peer'):
            self.authority.register_course(course, 'worker-one')
        self.revoked = set()
        self.dispatcher = RuntimeDispatcher(self.authority, self.backend, self.guardian_authorize)
        self.heartbeat('worker-one', 'course-main'); self.heartbeat('worker-one', 'course-peer')

    def guardian_authorize(self, fingerprint, worker, course):
        if fingerprint in self.revoked: raise RuntimeRefusal('fixture_revoked')

    def principal(self, worker='worker-one', course='course-main', role='hub', authorize=lambda: None):
        return {'role': role, 'worker_id': worker, 'course_id': course, 'cert_sha256': 'e' * 64, 'authorize': authorize}

    def heartbeat(self, worker, course):
        return self.dispatcher.dispatch(self.principal(worker, course, 'guardian'), 'guardian.observe', course,
                                        self.backend.guardian_identity(worker))

    def env(self, subject='alice', worker='worker-one', course='course-main'):
        hub = self.config['courses'][worker][course]['hub_container']
        return {'JUPYTERHUB_USER': subject, 'JUPYTERHUB_API_TOKEN': 'scoped-test-token', 'JUPYTERHUB_API_URL': 'http://' + hub + ':8081/hub/api'}

    def spawn(self, subject='alice', worker='worker-one', course='course-main', epoch=1, authorize=lambda: None):
        return self.dispatcher.dispatch(self.principal(worker, course, authorize=authorize), 'hub.spawn', course,
            {'subject': subject, 'environment': self.env(subject, worker, course), 'expected_epoch': epoch})


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); os.chmod(self.temp.name, 0o700)
        self.fixture = RuntimeFixture(self.temp.name)
    def tearDown(self): self.temp.cleanup()
    def refuses(self, code, callback):
        with self.assertRaises(RuntimeRefusal) as caught: callback()
        self.assertEqual(caught.exception.code, code)

    def test_spawn_has_fixed_security_resources_home_and_no_control_mount(self):
        f = self.fixture; result = f.spawn(); container, options = f.client.containers.created[-1]
        self.assertEqual(result['server_id'], container.id)
        self.assertEqual(options['mem_limit'], 512 * 1024**2); self.assertEqual(options['cpu_quota'], 25000)
        self.assertEqual(options['cap_drop'], ['ALL']); self.assertTrue(options['read_only'])
        self.assertEqual(options['restart_policy'], {'Name': 'no'}); self.assertEqual(len(options['volumes']), 1)
        self.assertEqual(options['environment']['JUPYTERHUB_DISABLE_USER_CONFIG'], '1')
        self.assertFalse(any('AUTHORITY' in key or 'SSL' in key for key in options['environment']))
        f.backend.verified_container(container, 'worker-one', 'course-main', check_policy=True)

    def test_roster_scope_arbitrary_environment_and_stale_epoch_refused(self):
        f = self.fixture
        self.refuses('runtime_subject_not_in_roster', lambda: f.spawn('mallory'))
        self.refuses('runtime_cached_epoch_invalid', lambda: f.spawn(epoch=2))
        payload = {'subject': 'alice', 'environment': dict(f.env(), DOCKER_HOST='tcp://controller'), 'expected_epoch': 1}
        self.refuses('runtime_environment_invalid', lambda: f.dispatcher.dispatch(f.principal(), 'hub.spawn', 'course-main', payload))
        self.refuses('runtime_principal_scope_invalid', lambda: f.dispatcher.dispatch(f.principal(course='course-peer'), 'hub.spawn', 'course-main', payload))
        self.assertEqual(len(f.backend.containers('worker-one', 'course-main')), 0)

    def test_exact_server_and_epoch_stop_cannot_target_later_container(self):
        f = self.fixture; first = f.spawn(); first_container = f.client.containers.get(first['server_id'])
        f.backend.remove(first_container, 'worker-one', 'course-main'); second = f.spawn()
        result = f.dispatcher.dispatch(f.principal(), 'hub.stop', 'course-main', {'subject': 'alice', 'server_id': first['server_id'], 'epoch': 1})
        self.assertEqual(result, {'stopped': True}); self.assertTrue(f.client.containers.get(second['server_id']).attrs['State']['Running'])
        self.refuses('runtime_cached_epoch_invalid', lambda: f.dispatcher.dispatch(f.principal(), 'hub.stop', 'course-main', {'subject': 'alice', 'server_id': second['server_id'], 'epoch': 2}))

    def test_capacity_and_resource_drift_refuse_adoption(self):
        f = self.fixture; first = f.spawn()
        self.refuses('runtime_course_capacity_exceeded', lambda: f.spawn('bob'))
        f.client.containers.get(first['server_id']).attrs['HostConfig']['Memory'] = 1024**3
        self.refuses('runtime_container_policy_changed', lambda: f.spawn())

    def test_changed_token_recreates_exact_server_and_preserves_home(self):
        f = self.fixture; first = f.spawn()
        repeated = f.spawn(); self.assertEqual(first['server_id'], repeated['server_id'])
        environment = dict(f.env(), JUPYTERHUB_API_TOKEN='new-per-server-token')
        second = f.dispatcher.dispatch(f.principal(), 'hub.spawn', 'course-main', {'subject': 'alice', 'environment': environment, 'expected_epoch': 1})
        self.assertNotEqual(first['server_id'], second['server_id'])
        self.assertNotIn(first['server_id'], f.client.containers.items)
        self.assertEqual(len(f.client.volume_items), 6)

    def test_revocation_during_existing_server_inspection_has_no_runtime_effects(self):
        f = self.fixture; first = f.spawn(); container = f.client.containers.get(first['server_id'])
        actions = list(container.actions); created = len(f.client.containers.created); revoked = []
        original = f.backend.verified_container
        def inspect(value, *args, **kwargs):
            result = original(value, *args, **kwargs)
            if value.id == first['server_id'] and kwargs.get('check_policy'): revoked.append(True)
            return result
        def authorize():
            if revoked: raise RuntimeRefusal('revoked_before_existing_removal')
        environment = dict(f.env(), JUPYTERHUB_API_TOKEN='new-per-server-token')
        with patch.object(f.backend, 'verified_container', side_effect=inspect):
            self.refuses('revoked_before_existing_removal', lambda: f.dispatcher.dispatch(
                f.principal(authorize=authorize), 'hub.spawn', 'course-main',
                {'subject': 'alice', 'environment': environment, 'expected_epoch': 1}))
        self.assertTrue(revoked)
        self.assertIn(first['server_id'], f.client.containers.items)
        self.assertIs(f.client.containers.get(first['server_id']), container)
        self.assertTrue(container.attrs['State']['Running'])
        self.assertEqual(container.actions, actions)
        self.assertEqual(len(f.client.containers.created), created)

    def test_revocation_after_network_list_refuses_network_create(self):
        f = self.fixture; revoked = []; created = len(f.client.containers.created)
        original = f.client.networks.list
        def inspect(*args, **kwargs):
            result = original(*args, **kwargs); revoked.append(True); return result
        def authorize():
            if revoked: raise RuntimeRefusal('revoked_before_network_create')
        with patch.object(f.client.networks, 'list', side_effect=inspect):
            self.refuses('revoked_before_network_create', lambda: f.spawn(authorize=authorize))
        self.assertEqual(f.client.networks.items, {})
        self.assertEqual(len(f.client.containers.created), created)

    def test_revocation_after_network_inspection_refuses_hub_connection(self):
        f = self.fixture; revoked = []; created = len(f.client.containers.created)
        hub = f.client.containers.get(f.config['courses']['worker-one']['course-main']['hub_container'])
        networks = dict(hub.attrs['NetworkSettings']['Networks'])
        original = Network.reload
        def inspect(network):
            original(network); revoked.append(True)
        def authorize():
            if revoked: raise RuntimeRefusal('revoked_before_network_connect')
        with patch.object(Network, 'reload', new=inspect):
            self.refuses('revoked_before_network_connect', lambda: f.spawn(authorize=authorize))
        self.assertEqual(len(f.client.networks.items), 1)
        self.assertEqual(hub.attrs['NetworkSettings']['Networks'], networks)
        self.assertEqual(len(f.client.containers.created), created)

    def test_network_preparation_requires_the_matching_held_admission_scope(self):
        f = self.fixture; checked = []
        self.refuses('runtime_admission_scope_invalid', lambda: f.backend.prepare_home_network(
            'worker-one', 'course-main', 'alice', authorize=lambda: checked.append(True)))
        self.assertEqual(checked, [])
        self.assertEqual(f.client.networks.items, {})

    def test_revocation_during_stop_inspection_preserves_existing_runtime(self):
        f = self.fixture; first = f.spawn(); container = f.client.containers.get(first['server_id'])
        actions = list(container.actions); created = len(f.client.containers.created); armed = []; revoked = []
        original = f.backend.verified_container
        def authorize():
            if revoked: raise RuntimeRefusal('revoked_before_stop_removal')
            armed.append(True)
        def inspect(value, *args, **kwargs):
            result = original(value, *args, **kwargs)
            if armed and value.id == first['server_id']: revoked.append(True)
            return result
        with patch.object(f.backend, 'verified_container', side_effect=inspect):
            self.refuses('revoked_before_stop_removal', lambda: f.dispatcher.dispatch(
                f.principal(authorize=authorize), 'hub.stop', 'course-main',
                {'subject': 'alice', 'server_id': first['server_id'], 'epoch': 1}))
        self.assertTrue(revoked)
        self.assertIn(first['server_id'], f.client.containers.items)
        self.assertIs(f.client.containers.get(first['server_id']), container)
        self.assertTrue(container.attrs['State']['Running'])
        self.assertEqual(container.actions, actions)
        self.assertEqual(len(f.client.containers.created), created)

    def test_revocation_inside_remove_inspection_preserves_running_server(self):
        f = self.fixture; first = f.spawn(); container = f.client.containers.get(first['server_id'])
        actions = list(container.actions); revoked = []; inside = []
        remove = f.backend.remove; inspect = f.backend.verified_container
        def authorize():
            if revoked: raise RuntimeRefusal('revoked_inside_remove')
        def verify(value, *args, **kwargs):
            result = inspect(value, *args, **kwargs)
            if inside and value.id == first['server_id']: revoked.append(True)
            return result
        def remove_owned(*args, **kwargs):
            inside.append(True)
            try: return remove(*args, **kwargs)
            finally: inside.pop()
        with patch.object(f.backend, 'verified_container', side_effect=verify), patch.object(f.backend, 'remove', side_effect=remove_owned):
            self.refuses('revoked_inside_remove', lambda: f.dispatcher.dispatch(
                f.principal(authorize=authorize), 'hub.stop', 'course-main',
                {'subject': 'alice', 'server_id': first['server_id'], 'epoch': 1}))
        self.assertIn(first['server_id'], f.client.containers.items)
        self.assertEqual(container.actions, actions)
        self.assertTrue(container.attrs['State']['Running'])

    def test_revocation_after_restart_policy_update_refuses_stop_and_remove(self):
        f = self.fixture; first = f.spawn(); container = f.client.containers.get(first['server_id'])
        actions = list(container.actions); revoked = []; update = container.update
        def authorize():
            if revoked: raise RuntimeRefusal('revoked_after_update')
        def change(**kwargs):
            update(**kwargs); revoked.append(True)
        with patch.object(container, 'update', side_effect=change):
            self.refuses('revoked_after_update', lambda: f.dispatcher.dispatch(
                f.principal(authorize=authorize), 'hub.stop', 'course-main',
                {'subject': 'alice', 'server_id': first['server_id'], 'epoch': 1}))
        self.assertIn(first['server_id'], f.client.containers.items)
        self.assertEqual(container.actions, actions + [('update', {'restart_policy': {'Name': 'no'}})])
        self.assertTrue(container.attrs['State']['Running'])

    def test_revocation_between_create_start_removes_only_created_container(self):
        f = self.fixture; revoked = []; original = f.client.containers.create
        def create(*args, **kwargs):
            container = original(*args, **kwargs); revoked.append(True); return container
        def authorize():
            if revoked: raise RuntimeRefusal('revoked_before_start')
        with patch.object(f.client.containers, 'create', side_effect=create):
            self.refuses('revoked_before_start', lambda: f.spawn(authorize=authorize))
        container = f.client.containers.created[-1][0]
        self.assertNotIn('start', container.actions)
        self.assertEqual(container.actions[-1], ('remove', False, False))
        self.assertEqual(f.backend.containers('worker-one', 'course-main'), [])
        self.assertEqual(len(f.client.volume_items), 6)

    def test_heartbeat_expiry_during_final_hub_authorization_prevents_start(self):
        f = self.fixture; calls = []
        def authorize():
            if any(item.attrs['Config']['Labels'].get('educloud.workspace.kind') == 'learner'
                   for item in f.client.containers.items.values()):
                calls.append(True)
                if len(calls) == 2: f.now = 104
        self.refuses('guardian_stale_or_changed', lambda: f.spawn(authorize=authorize))
        created = f.client.containers.created[-1][0]
        self.assertNotIn('start', created.actions)
        self.assertNotIn(created.id, f.client.containers.items)
        self.assertEqual(created.actions[-1], ('remove', False, False))

    def test_heartbeat_expiry_during_final_authorization_prevents_network_creation(self):
        f = self.fixture; calls = []; created = len(f.client.containers.created)
        def authorize():
            calls.append(True)
            if len(calls) == 2: f.now = 104
        self.refuses('guardian_stale_or_changed', lambda: f.spawn(authorize=authorize))
        self.assertEqual(f.client.networks.items, {})
        self.assertEqual(len(f.client.containers.created), created)

    def test_existing_server_adoption_refuses_heartbeat_expired_by_slow_guardian_policy(self):
        f = self.fixture; first = f.spawn(); container = f.client.containers.get(first['server_id'])
        actions = list(container.actions); created = len(f.client.containers.created); calls = []
        original = f.dispatcher.guardian_authorize
        def authorize(*args):
            original(*args); calls.append(True)
            if len(calls) == 2: f.now = 104
        with patch.object(f.dispatcher, 'guardian_authorize', side_effect=authorize):
            self.refuses('guardian_stale_or_changed', f.spawn)
        self.assertEqual(container.actions, actions)
        self.assertEqual(len(f.client.containers.created), created)
        self.assertTrue(container.attrs['State']['Running'])

    def test_adoption_return_checks_lease_after_slow_server_inspection(self):
        f = self.fixture; first = f.spawn(); container = f.client.containers.get(first['server_id'])
        actions = list(container.actions); original = f.dispatcher.server
        def observe(*args):
            result = original(*args); f.now = 104; return result
        with patch.object(f.dispatcher, 'server', side_effect=observe):
            self.refuses('guardian_stale_or_changed', f.spawn)
        self.assertEqual(container.actions, actions)

    def test_new_server_return_expiry_cleans_only_its_newly_started_container(self):
        f = self.fixture; peer = f.spawn(course='course-peer'); original = f.dispatcher.server
        def observe(*args):
            result = original(*args); f.now = 104; return result
        with patch.object(f.dispatcher, 'server', side_effect=observe):
            self.refuses('guardian_stale_or_changed', f.spawn)
        created = f.client.containers.created[-1][0]
        self.assertIn('start', created.actions)
        self.assertNotIn(created.id, f.client.containers.items)
        self.assertTrue(f.client.containers.get(peer['server_id']).attrs['State']['Running'])

    def test_guardian_revocation_during_create_refuses_start_and_cleans_owned_container(self):
        f = self.fixture; original = f.client.containers.create
        def create(*args, **kwargs):
            value = original(*args, **kwargs)
            f.revoked.add('e' * 64)
            return value
        with patch.object(f.client.containers, 'create', side_effect=create):
            self.refuses('fixture_revoked', f.spawn)
        container = f.client.containers.created[-1][0]
        self.assertNotIn('start', container.actions)
        self.assertEqual(container.actions[-1], ('remove', False, False))
        self.assertEqual(len(f.client.volume_items), 6)

    def test_restart_drift_is_disabled_before_guardian_stop_and_peer_survives(self):
        f = self.fixture; main = f.spawn(); peer = f.spawn(course='course-peer')
        container = f.client.containers.get(main['server_id']); container.attrs['HostConfig']['RestartPolicy'] = {'Name': 'always'}
        f.dispatcher.supervision_tick()
        self.assertEqual(container.actions[-3:], [('update', {'restart_policy': {'Name': 'no'}}), ('stop', 5), ('remove', False, False)])
        self.assertTrue(f.client.containers.get(peer['server_id']).attrs['State']['Running']); self.assertEqual(len(f.client.volume_items), 6)

    def test_pinned_inherited_provenance_labels_allow_exact_owned_rollback(self):
        f = self.fixture
        inherited = {'com.docker.compose.project': 'old-fixture', 'com.docker.compose.service': 'course-image', 'org.opencontainers.image.title': 'course'}
        f.client.image_labels[f.image] = inherited
        for container in f.client.containers.items.values(): container.attrs['Config']['Labels'].update(inherited)
        # A content ID's metadata cannot change after bootstrap in real Docker.
        # Reopen after setting fixture provenance so it is validated on first read.
        f.backend = DockerBackend(f.config, f.client, clock=lambda: f.now)
        f.authority.backend = f.backend; f.dispatcher.backend = f.backend
        original = f.client.containers.create
        def create(image, name, labels, **kwargs): return original(image, name, dict(inherited, **labels), **kwargs)
        with patch.object(f.client.containers, 'create', side_effect=create):
            first = f.spawn()
        value = f.client.containers.get(first['server_id'])
        f.backend.verified_container(value, 'worker-one', 'course-main', check_policy=True)
        value.attrs['Config']['Labels']['educloud.runtime.foreign'] = 'forbidden'
        self.refuses('runtime_container_scope_invalid', lambda: f.backend.remove(value, 'worker-one', 'course-main'))
        del value.attrs['Config']['Labels']['educloud.runtime.foreign']
        f.backend.remove(value, 'worker-one', 'course-main')
        self.assertNotIn(first['server_id'], f.client.containers.items)
        self.assertEqual(len(f.client.volume_items), 6)

    def test_reserved_ownership_labels_on_image_refused_before_create(self):
        f = self.fixture; count = len(f.client.containers.created)
        f.client.image_labels[f.image] = {'educloud.runtime.worker': 'forged-worker'}
        self.refuses('runtime_image_labels_unsupported', lambda: DockerBackend(f.config, f.client))
        self.assertEqual(len(f.client.containers.created), count)

    def test_configured_image_metadata_is_cached_without_live_authorization(self):
        f = self.fixture
        with patch.object(f.client.images, 'get', side_effect=AssertionError('metadata reread')), \
                patch.object(f.client, 'info', side_effect=AssertionError('redundant daemon read')):
            labels = f.backend.image_labels(f.image)
            labels['untrusted-copy'] = 'changed'
            self.assertEqual(f.backend.image_labels(f.image), {})
        f.client.info_value['ID'] = 'changed-daemon'
        self.refuses('runtime_daemon_changed', f.spawn)
        self.refuses('runtime_daemon_changed', lambda: f.backend.guardian_identity('worker-one'))

    def test_unconfigured_image_metadata_stays_live_and_rejects_tags(self):
        f = self.fixture; other = 'sha256:' + 'd' * 64
        f.client.image_labels[other] = {'public': 'first'}
        with patch.object(f.client.images, 'get', wraps=f.client.images.get) as inspect, \
                patch.object(f.client, 'info', wraps=f.client.info) as daemon:
            self.assertEqual(f.backend.image_labels(other), {'public': 'first'})
            f.client.image_labels[other] = {'public': 'second'}
            self.assertEqual(f.backend.image_labels(other), {'public': 'second'})
            self.assertEqual(inspect.call_count, 2); self.assertEqual(daemon.call_count, 2)
            self.refuses('runtime_image_labels_unsupported', lambda: f.backend.image_labels('course:latest'))
            self.assertEqual(inspect.call_count, 2)

    def test_image_cache_first_read_rejects_wrong_actual_id_and_bad_labels(self):
        f = self.fixture
        cases = [({'Id': 'sha256:' + 'd' * 64, 'Config': {'Labels': {}}}),
                 ({'Id': f.image, 'Config': {'Labels': {'educloud.runtime.worker': 'forged'}}}),
                 ({'Id': f.image, 'Config': {'Labels': {'public': 1}}})]
        for attrs in cases:
            with self.subTest(attrs=attrs), patch.object(f.client.images, 'get',
                    return_value=type('Image', (), {'attrs': attrs})()):
                self.refuses('runtime_image_labels_unsupported', lambda: DockerBackend(f.config, f.client))

    def test_image_cache_warms_each_exact_configured_id_once(self):
        f = self.fixture
        with patch.object(f.client.images, 'get', wraps=f.client.images.get) as inspect, \
                patch.object(f.client, 'info', wraps=f.client.info) as daemon:
            backend = DockerBackend(f.config, f.client)
            self.assertEqual(inspect.call_args_list, [unittest.mock.call(f.image)])
            self.assertEqual(daemon.call_count, 1)
            for _ in range(3): self.assertEqual(backend.image_labels(f.image), {})
            self.assertEqual(inspect.call_count, 1); self.assertEqual(daemon.call_count, 1)

    def test_dead_guardian_and_revoked_certificate_stop_running_and_refuse_spawn(self):
        f = self.fixture; result = f.spawn()
        f.revoked.add('e' * 64); f.dispatcher.supervision_tick()
        self.assertNotIn(result['server_id'], f.client.containers.items)
        self.refuses('fixture_revoked', lambda: f.spawn())
        f.revoked.clear(); f.heartbeat('worker-one', 'course-main')
        f.client.containers.get('worker-one-guardian').attrs['State']['Running'] = False
        self.refuses('guardian_container_unavailable', lambda: f.spawn())

    def test_missing_barrier_and_database_replacement_not_recreated(self):
        f = self.fixture
        with f.backend.connection() as conn: conn.execute("DELETE FROM barriers WHERE endpoint='worker-one/course-main'")
        self.refuses('runtime_barrier_history_missing', lambda: f.backend.barrier('worker-one', 'course-main'))
        replacement = f.backend.database.with_suffix('.replacement'); shutil.copyfile(f.backend.database, replacement); replacement.chmod(0o600)
        os.replace(replacement, f.backend.database)
        self.refuses('runtime_database_binding_changed', lambda: f.backend.barrier('worker-one', 'course-main'))

    def test_dual_fence_atomic_history_and_pre_effect_authorization(self):
        f = self.fixture; first = f.spawn(); before = f.backend.barrier('worker-one', 'course-main')
        plan = f.directory / 'move.json'
        # Stop first because its fresh Jupyter-like work is the same fixed fixture bytes.
        f.authority.preview('course-main', 'worker-two', plan)
        from ownership_contract import read_json
        request = f.authority._request(read_json(plan))
        def reject(): raise RuntimeRefusal('stale_authorization')
        with self.assertRaises(RuntimeRefusal):
            with f.backend.fence_context(request, authorize=reject): pass
        self.assertEqual(f.backend.barrier('worker-one', 'course-main'), before)
        self.assertTrue(f.client.containers.get(first['server_id']).attrs['State']['Running'])
        with patch('ownership_contract.fault_hook', side_effect=lambda stage, tx: (_ for _ in ()).throw(RuntimeError()) if stage == 'fenced' else None):
            with self.assertRaises(OwnershipError): f.authority.apply(plan)
        self.assertEqual(f.backend.containers('worker-one', 'course-main'), [])
        self.assertEqual(f.backend.barrier('worker-one', 'course-main')['denied_through_epoch'], 1)
        self.assertEqual(f.authority.apply(plan)['epoch'], 2)
        with self.assertRaises(OwnershipError): f.spawn()

    def test_failed_guardian_observation_preserves_new_server_identity(self):
        f = self.fixture; first = f.spawn()
        identity = f.backend.guardian_identity('worker-one'); observed = f.backend.guardian_snapshot('worker-one', 'course-main')
        f.backend.remove(f.client.containers.get(first['server_id']), 'worker-one', 'course-main')
        newer = f.spawn()
        f.backend.guardian_failclosed('worker-one', 'course-main', identity, 1, observed)
        self.assertTrue(f.client.containers.get(newer['server_id']).attrs['State']['Running'])

    def test_atomic_dual_floor_transaction_has_no_barrier_index_gap(self):
        f = self.fixture; plan = f.directory / 'move.json'
        f.authority.preview('course-main', 'worker-two', plan)
        from ownership_contract import read_json
        request = f.authority._request(read_json(plan))
        before = {worker: f.backend.barrier(worker, 'course-main') for worker in ('worker-one', 'worker-two')}
        original = f.backend._store; calls = []
        def interrupt(conn, endpoint, record):
            calls.append(endpoint)
            if len(calls) == 2: raise OSError('injected second row failure')
            original(conn, endpoint, record)
        with patch.object(f.backend, '_store', side_effect=interrupt):
            with self.assertRaises(OSError):
                with f.backend.reconciliation_context(request, authorize=lambda: None): pass
        self.assertEqual({worker: f.backend.barrier(worker, 'course-main') for worker in before}, before)
        reopened = DockerBackend(f.config, f.client)
        with reopened.reconciliation_context(request, authorize=lambda: None) as context:
            proof = context.inspect()
            self.assertEqual(set(proof['endpoints']), set(before))
            self.assertTrue(all(record['denied_through_epoch'] == 1 for record in proof['endpoints'].values()))
        self.refuses('runtime_fence_context_expired', context.inspect)

    def test_wrong_labels_not_removed_and_daemon_budget_checked(self):
        f = self.fixture; value = f.spawn(); container = f.client.containers.get(value['server_id'])
        container.attrs['Config']['Labels']['educloud.runtime.authority'] = 'b' * 32
        self.refuses('runtime_container_scope_invalid', lambda: f.backend.stop_endpoint('worker-one', 'course-main'))
        self.assertTrue(container.attrs['State']['Running'])
        f.client.info_value['MemTotal'] = 4096 * 1024**2
        self.refuses('runtime_shared_daemon_budget_exceeded', lambda: f.backend.inspect_worker('worker-one'))

    def test_healthy_watchdog_refuses_owned_container_from_wrong_epoch(self):
        f = self.fixture; first = f.spawn(); peer = f.spawn(course='course-peer')
        value = f.client.containers.get(first['server_id'])
        value.attrs['Config']['Labels']['educloud.runtime.epoch'] = '2'
        before = f.backend.inspect_course('course-main', 'worker-one')['retained_tree_sha256']
        f.dispatcher.supervision_tick()
        self.assertNotIn(first['server_id'], f.client.containers.items)
        self.assertTrue(f.client.containers.get(peer['server_id']).attrs['State']['Running'])
        self.assertEqual(f.backend.inspect_course('course-main', 'worker-one')['retained_tree_sha256'], before)

    def test_descriptor_tree_race_never_reads_outside_symlink_replacement(self):
        root = self.fixture.directory / 'race-home'; root.mkdir()
        nested = root / 'nested'; nested.mkdir(); (nested / 'inside.txt').write_text('owned bytes')
        outside = self.fixture.directory / 'race-outside'; outside.mkdir(); secret = outside / 'synthetic.txt'; secret.write_text('synthetic outside bytes')
        original_open = os.open; original_read = os.read; changed = []; outside_reads = []
        secret_inode = secret.stat().st_ino
        def opening(name, flags, *args, **kwargs):
            if name == 'nested' and kwargs.get('dir_fd') is not None and flags & os.O_DIRECTORY and not changed:
                nested.rename(root / 'moved'); nested.symlink_to(outside, target_is_directory=True); changed.append(True)
            return original_open(name, flags, *args, **kwargs)
        def reading(fd, size):
            if os.fstat(fd).st_ino == secret_inode: outside_reads.append(True)
            return original_read(fd, size)
        with patch('runtime_authority.os.open', side_effect=opening), patch('runtime_authority.os.read', side_effect=reading):
            with self.assertRaises((OSError, RuntimeRefusal)): tree_hash(root)
        self.assertTrue(changed); self.assertEqual(outside_reads, [])

    def test_file_path_replacement_during_read_refuses_unstable_evidence(self):
        path = self.fixture.directory / 'race-file'; path.write_bytes(b'original')
        replacement = self.fixture.directory / 'replacement-file'; replacement.write_bytes(b'replaced')
        original_read = os.read; changed = []
        def reading(fd, size):
            if not changed:
                os.replace(replacement, path); changed.append(True)
            return original_read(fd, size)
        with patch('runtime_authority.os.read', side_effect=reading):
            self.refuses('runtime_evidence_changed', lambda: file_hash(path))

    def test_root_directory_identity_persists_across_backend_restart(self):
        f = self.fixture; item = f.config['courses']['worker-one']['course-main']
        root = Path(item['home_root']); root.rename(root.with_name(root.name + '-retained'))
        root.mkdir()
        self.refuses('runtime_home_root_changed', lambda: f.backend.inspect_course('course-main', 'worker-one'))
        self.refuses('runtime_database_binding_changed', lambda: DockerBackend(f.config, f.client))

    def test_stopper_only_guardian_open_does_not_attest_unmounted_home_roots(self):
        f = self.fixture
        with patch('runtime_authority.open_directory', side_effect=FileNotFoundError('homes unmounted')):
            guardian_backend = DockerBackend(f.config, f.client, inspect_roots=False)
            self.assertEqual(guardian_backend.guardian_identity('worker-one'), f.backend.guardian_identity('worker-one'))
            with self.assertRaises(FileNotFoundError): guardian_backend.inspect_course('course-main', 'worker-one')

    def test_admission_never_reads_oversize_special_or_changing_student_tree(self):
        f = self.fixture; item = f.config['courses']['worker-one']['course-main']
        home = next(Path(item['home_root']).iterdir())
        with (home / 'oversize').open('wb') as stream: stream.truncate(65 * 1024**2)
        os.mkfifo(home / 'special')
        original = file_hash; paths = []
        def trusted_only(path):
            paths.append(str(path))
            self.assertEqual(str(path), item['runtime_path'])
            return original(path)
        with patch('runtime_authority.tree_hash', side_effect=AssertionError('student tree inspected')), patch('runtime_authority.file_hash', side_effect=trusted_only):
            with f.authority.admission('course-main', 'worker-one') as grant:
                self.assertEqual(grant['epoch'], 1)
                (home / 'changed-while-live').write_bytes(b'live student mutation')
        self.assertTrue(paths)
        with self.assertRaises(RuntimeRefusal): f.backend.inspect_course('course-main', 'worker-one')
        with self.assertRaises(OwnershipError): f.authority.preview('course-main', 'worker-two', f.directory / 'oversize-plan')

    def test_admission_scope_is_exact_endpoint_thread_local_and_always_cleared(self):
        f = self.fixture
        with f.authority.admission('course-main', 'worker-one'):
            scoped = f.backend.inspect_course('course-main', 'worker-one')
            self.assertEqual(scoped['snapshot_sha256'], ADMISSION_UNUSED_SNAPSHOT)
            self.assertEqual(scoped['retained_tree_sha256'], ADMISSION_UNUSED_RETAINED)
            peer = f.backend.inspect_course('course-peer', 'worker-one')
            self.assertNotEqual(peer['retained_tree_sha256'], ADMISSION_UNUSED_RETAINED)
            with ThreadPoolExecutor(max_workers=1) as pool:
                other_thread = pool.submit(f.backend.inspect_course, 'course-main', 'worker-one').result(timeout=5)
            self.assertNotEqual(other_thread['retained_tree_sha256'], ADMISSION_UNUSED_RETAINED)
        self.assertNotEqual(f.backend.inspect_course('course-main', 'worker-one')['snapshot_sha256'], ADMISSION_UNUSED_SNAPSHOT)
        with self.assertRaises(RuntimeRefusal):
            with f.authority.admission('course-main', 'worker-one'): raise RuntimeRefusal('fixture_failure')
        self.assertIsNone(getattr(f.backend.admission_scope, 'endpoint', None))
        plan = f.directory / 'full-proof-plan'; f.authority.preview('course-main', 'worker-two', plan)
        from ownership_contract import read_json
        self.assertNotEqual(read_json(plan)['evidence']['worker-one']['retained_tree_sha256'], ADMISSION_UNUSED_RETAINED)

    def test_admission_still_refuses_runtime_profile_and_root_replacement(self):
        f = self.fixture; item = f.config['courses']['worker-one']['course-main']
        Path(item['runtime_path']).write_bytes(b'changed trusted runtime')
        with self.assertRaises(OwnershipError):
            with f.authority.admission('course-main', 'worker-one'): pass
        Path(item['runtime_path']).write_bytes(b'fixed image runtime')
        item['profile']['learner_memory_mb'] = 1024
        with self.assertRaises(OwnershipError):
            with f.authority.admission('course-main', 'worker-one'): pass
        item['profile']['learner_memory_mb'] = 512
        root = Path(item['home_root']); root.rename(root.with_name(root.name + '-old')); root.mkdir()
        with self.assertRaises(OwnershipError):
            with f.authority.admission('course-main', 'worker-one'): pass

    def test_watchdog_busy_defers_without_authorizing_and_checks_peer_before_escalation(self):
        f = self.fixture; first = f.spawn(); peer = f.spawn(course='course-peer')
        f.dispatcher.monotonic = lambda: f.now
        held = threading.Event(); release = threading.Event()
        def holder():
            with f.backend.locks([('worker-one', 'course-main')]):
                held.set(); release.wait(10)
        thread = threading.Thread(target=holder); thread.start()
        try:
            self.assertTrue(held.wait(3))
            f.dispatcher.supervision_tick()
            self.assertIn(('worker-one', 'course-main'), f.dispatcher.busy_since)
            f.now += 11; f.heartbeat('worker-one', 'course-peer')
            self.refuses('guardian_stale_or_changed', lambda: f.backend.require_guardian('worker-one', 'course-main', 1))
            self.refuses('runtime_endpoint_enforcement_unavailable', f.dispatcher.supervision_tick)
            self.assertTrue(f.client.containers.get(peer['server_id']).attrs['State']['Running'])
        finally:
            release.set(); thread.join(5)
        self.assertFalse(thread.is_alive())
        f.dispatcher.supervision_tick()
        self.assertNotIn(first['server_id'], f.client.containers.items)
        self.assertNotIn(('worker-one', 'course-main'), f.dispatcher.busy_since)

    def test_watchdog_bounded_fairness_acquires_actual_periodic_flock_gap(self):
        f = self.fixture; source = f.spawn(); peer = f.spawn(course='course-peer')
        clock = [0.0]; f.dispatcher.monotonic = lambda: clock[0]
        stop = threading.Event(); held = threading.Event(); windows = []
        def holder():
            while not stop.is_set():
                with f.backend.locks([('worker-one', 'course-main')]):
                    held.set(); stop.wait(0.3)
                stop.wait(0.15)
        thread = threading.Thread(target=holder); thread.start()
        real = f.dispatcher._supervise_endpoint
        def supervise(worker, course, *, wait_seconds=0.1):
            if (worker, course) == ('worker-one', 'course-main'): windows.append(wait_seconds)
            return real(worker, course, wait_seconds=wait_seconds)
        try:
            self.assertTrue(held.wait(2))
            with patch.object(f.dispatcher, '_supervise_endpoint', side_effect=supervise):
                f.dispatcher.supervision_tick()
                self.assertIn(('worker-one', 'course-main'), f.dispatcher.busy_since)
                clock[0] = 0.6; f.dispatcher.supervision_tick()
            self.assertEqual(windows, [0.1, 1])
            self.assertNotIn(('worker-one', 'course-main'), f.dispatcher.busy_since)
            self.assertTrue(f.client.containers.get(source['server_id']).attrs['State']['Running'])
            self.assertTrue(f.client.containers.get(peer['server_id']).attrs['State']['Running'])
            self.assertEqual(f.backend.require_guardian('worker-one', 'course-main', 1)['checked_at'], 100)
        finally: stop.set(); thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_watchdog_acquisition_window_never_extends_remaining_inability_limit(self):
        f = self.fixture; f.dispatcher.monotonic = lambda: 9.75
        f.dispatcher.busy_since[('worker-one', 'course-main')] = 0
        windows = []
        def supervise(worker, course, *, wait_seconds=0.1):
            if (worker, course) == ('worker-one', 'course-main'):
                windows.append(wait_seconds); raise RuntimeRefusal('runtime_endpoint_lock_busy')
        with patch.object(f.dispatcher, '_supervise_endpoint', side_effect=supervise):
            f.dispatcher.supervision_tick()
            self.assertEqual(windows, [0.25])
            self.assertEqual(f.dispatcher.busy_since[('worker-one', 'course-main')], 0)
            f.dispatcher.monotonic = lambda: 10
            self.refuses('runtime_endpoint_enforcement_unavailable', f.dispatcher.supervision_tick)
            self.assertEqual(windows, [0.25, 0.1])

    def test_guardian_actual_held_flock_defers_without_epoch_heartbeat_or_effect(self):
        f = self.fixture; source = f.spawn(); peer = f.spawn(course='course-peer')
        before = {item.id: list(item.actions) for item in f.client.containers.items.values()}
        identity = f.backend.guardian_identity('worker-one'); held = threading.Event(); release = threading.Event(); authorized = []
        with f.backend.connection() as conn:
            history = [tuple(row) for row in conn.execute('SELECT * FROM guardians ORDER BY endpoint')]
        def holder():
            with f.backend.locks([('worker-one', 'course-main')]): held.set(); release.wait(5)
        thread = threading.Thread(target=holder); thread.start()
        try:
            self.assertTrue(held.wait(2)); f.now += 1
            result = f.dispatcher.dispatch(f.principal(role='guardian', authorize=lambda: authorized.append(True)),
                'guardian.observe', 'course-main', identity)
            self.assertEqual(result, {'authorized': False, 'deferred': True, 'worker_id': 'worker-one', 'course_id': 'course-main'})
            self.assertEqual(authorized, [])
            with f.backend.connection() as conn:
                self.assertEqual([tuple(row) for row in conn.execute('SELECT * FROM guardians ORDER BY endpoint')], history)
            self.assertEqual({item.id: item.actions for item in f.client.containers.items.values()}, before)
            self.assertEqual(f.dispatcher.busy_since, {})
        finally: release.set(); thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(getattr(f.backend.observation_scope, 'token', None))
        self.assertTrue(f.client.containers.get(source['server_id']).attrs['State']['Running'])
        self.assertTrue(f.client.containers.get(peer['server_id']).attrs['State']['Running'])
        self.assertTrue(f.heartbeat('worker-one', 'course-main')['authorized'])
        self.assertEqual(f.backend.require_guardian('worker-one', 'course-main', 1)['checked_at'], 101)

    def test_guardian_budget_is_thread_local_restored_and_hub_default_unchanged(self):
        f = self.fixture; waits = []; entered = threading.Event(); release = threading.Event()
        identity = f.backend.guardian_identity('worker-one')
        @contextmanager
        def unavailable(endpoints, *, wait_seconds=3):
            waits.append((threading.current_thread().name, wait_seconds))
            raise RuntimeRefusal('runtime_endpoint_lock_busy')
            yield
        def scoped():
            with f.backend.guardian_observation_budget():
                entered.set(); release.wait(3)
            self.assertIsNone(getattr(f.backend.observation_scope, 'token', None))
        thread = threading.Thread(target=scoped); thread.start()
        try:
            self.assertTrue(entered.wait(2))
            with patch.object(f.backend, 'locks', side_effect=unavailable):
                self.refuses('runtime_endpoint_lock_busy', lambda: f.dispatcher.dispatch(f.principal(), 'hub.authorize', 'course-main', {}))
                self.assertTrue(f.dispatcher.dispatch(f.principal(role='guardian'), 'guardian.observe', 'course-main', identity)['deferred'])
                self.refuses('runtime_endpoint_lock_busy', lambda: f.dispatcher.dispatch(f.principal(), 'hub.authorize', 'course-main', {}))
            self.assertEqual([wait for _, wait in waits], [3, .1, 3])
        finally: release.set(); thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_nested_observation_context_is_refused_and_outer_scope_survives(self):
        f = self.fixture
        with f.backend.guardian_observation_budget() as token:
            with self.assertRaisesRegex(RuntimeRefusal, '^runtime_observation_scope_nested$'):
                with f.backend.guardian_observation_budget(): pass
            self.assertIs(f.backend.observation_scope.token, token)
        self.assertIsNone(f.backend.observation_scope.token)

    def test_post_acquisition_busy_errors_never_become_deferred(self):
        f = self.fixture; identity = f.backend.guardian_identity('worker-one')
        for target in ('validate_request', 'guardian_heartbeat'):
            with self.subTest(target=target), patch.object(f.backend, target, side_effect=RuntimeRefusal('runtime_endpoint_lock_busy')):
                self.refuses('runtime_endpoint_lock_busy', lambda: f.dispatcher.dispatch(f.principal(role='guardian'), 'guardian.observe', 'course-main', identity))
            self.assertIsNone(getattr(f.backend.observation_scope, 'token', None))
            self.assertIsNone(getattr(f.backend.admission_scope, 'endpoint', None))
        def deny(): raise RuntimeRefusal('runtime_endpoint_lock_busy')
        self.refuses('runtime_endpoint_lock_busy', lambda: f.dispatcher.dispatch(f.principal(role='guardian', authorize=deny), 'guardian.observe', 'course-main', identity))
        with patch.object(f.backend, 'inspect_worker', side_effect=RuntimeRefusal('runtime_endpoint_lock_busy')):
            self.refuses('runtime_endpoint_lock_busy', lambda: f.dispatcher.dispatch(f.principal(role='guardian'), 'guardian.observe', 'course-main', identity))
        self.assertIsNone(f.backend.observation_scope.token)

    def test_deferred_marker_must_match_current_token_and_exact_endpoint(self):
        f = self.fixture; identity = f.backend.guardian_identity('worker-one')
        def wrong_token(*args): raise _GuardianAcquisitionBusy(object(), ('worker-one', 'course-main'))
        def wrong_endpoint(*args): raise _GuardianAcquisitionBusy(f.backend.observation_scope.token, ('worker-one', 'course-peer'))
        for callback in (wrong_token, wrong_endpoint):
            with patch.object(f.dispatcher, '_observe', side_effect=callback):
                with self.assertRaises(_GuardianAcquisitionBusy):
                    f.dispatcher.dispatch(f.principal(role='guardian'), 'guardian.observe', 'course-main', identity)
            self.assertIsNone(f.backend.observation_scope.token)

    def test_nonowner_or_pending_sql_refusals_are_not_busy_deferrals(self):
        f = self.fixture
        identity = f.backend.guardian_identity('worker-two')
        with self.assertRaisesRegex(OwnershipError, '^current_owner_required$'):
            f.dispatcher.dispatch(f.principal('worker-two', role='guardian'), 'guardian.observe', 'course-main', identity)
        with f.authority._connection() as conn:
            conn.execute("UPDATE courses SET pending=? WHERE course_id='course-main'", ('f' * 32,))
        with self.assertRaises(OwnershipError):
            f.dispatcher.dispatch(f.principal(role='guardian'), 'guardian.observe', 'course-main', f.backend.guardian_identity('worker-one'))
        self.assertIsNone(f.backend.observation_scope.token)

    def test_deferred_observations_do_not_reset_prolonged_watchdog_inability(self):
        f = self.fixture; held = threading.Event(); release = threading.Event(); f.dispatcher.monotonic = lambda: 10
        f.dispatcher.busy_since[('worker-one', 'course-main')] = 0
        identity = f.backend.guardian_identity('worker-one')
        def holder():
            with f.backend.locks([('worker-one', 'course-main')]): held.set(); release.wait(5)
        thread = threading.Thread(target=holder); thread.start()
        try:
            self.assertTrue(held.wait(2))
            self.assertTrue(f.dispatcher.dispatch(f.principal(role='guardian'), 'guardian.observe', 'course-main', identity)['deferred'])
            self.assertEqual(f.dispatcher.busy_since[('worker-one', 'course-main')], 0)
            self.refuses('runtime_endpoint_enforcement_unavailable', f.dispatcher.supervision_tick)
        finally: release.set(); thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_real_tls_busy_response_requires_guardian_scope_and_valid_body(self):
        from authority_test_pki import TestPKI
        from authority_access import AuthorityServer, ReplayStore
        from hub.authority_client import AuthorityClient, AuthorityError, VERSION as ACCESS_VERSION
        f = self.fixture; pki = TestPKI(f.directory / 'tls-pki')
        guardian_cert, guardian_key = pki.issue('guardian'); hub_cert, hub_key = pki.issue('hub')
        acl = f.directory / 'tls-acl.json'
        acl.write_text(json.dumps({'version': ACCESS_VERSION, 'authority_id': f.authority.authority_id,
            'clients': {pki.fingerprint(guardian_cert): {'role': 'guardian', 'workers': ['worker-one'], 'courses': ['course-main']},
                        pki.fingerprint(hub_cert): {'role': 'hub', 'workers': ['worker-one'], 'courses': ['course-main']}}})); acl.chmod(0o600)
        replay = ReplayStore.create(f.directory / 'tls-replay', f.authority.authority_id)
        server = AuthorityServer(('127.0.0.1', 0), authority_id=f.authority.authority_id,
            certfile=pki.server_cert, keyfile=pki.server_key, ca_file=pki.ca_cert, acl_file=acl,
            replay_store=replay, dispatch=f.dispatcher.dispatch, timeout=.5)
        serving = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}); serving.start()
        held = threading.Event(); release = threading.Event()
        def holder():
            with f.backend.locks([('worker-one', 'course-main')]): held.set(); release.wait(5)
        holding = threading.Thread(target=holder); holding.start()
        def client(cert, key, course='course-main'):
            return AuthorityClient('https://localhost:' + str(server.server_port), f.authority.authority_id,
                pki.ca_cert, cert, key, 'worker-one', course, timeout=1)
        try:
            self.assertTrue(held.wait(2)); identity = f.backend.guardian_identity('worker-one')
            for api, payload, code in [(client(hub_cert, hub_key), identity, 'authority_access_refused'),
                                        (client(guardian_cert, guardian_key, 'course-peer'), identity, 'authority_access_refused'),
                                        (client(guardian_cert, guardian_key), {}, 'authority_frame_invalid')]:
                with self.assertRaises(AuthorityError) as caught: api.call('guardian.observe', payload)
                self.assertEqual(caught.exception.code, code)
            result = client(guardian_cert, guardian_key).call('guardian.observe', identity)
            self.assertEqual(result, {'authorized': False, 'deferred': True, 'worker_id': 'worker-one', 'course_id': 'course-main'})
            # The normal replay audit remains mandatory even for a no-grant reply.
            with replay._connection() as conn: self.assertEqual(conn.execute('SELECT count(*) FROM requests').fetchone()[0], 1)
            self.assertEqual(f.backend.require_guardian('worker-one', 'course-main', 1)['checked_at'], 100)
        finally:
            release.set(); holding.join(5); server.shutdown(); server.server_close(); serving.join(2)
        self.assertFalse(holding.is_alive()); self.assertFalse(serving.is_alive())

    def test_supervisor_warning_is_fixed_only_when_owned_learners_exist(self):
        f = self.fixture
        with self.assertNoLogs('runtime_authority', level='WARNING'): f.dispatcher.supervision_tick()
        f.spawn()
        def refused(*args): raise RuntimeError('PRIVATE_TEST_CREDENTIAL')
        f.dispatcher.guardian_authorize = refused
        with self.assertLogs('runtime_authority', level='WARNING') as captured: f.dispatcher.supervision_tick()
        self.assertEqual(captured.output, ['WARNING:runtime_authority:Runtime supervision stopping owned learners: runtime_supervision_refused'])
        self.assertNotIn('PRIVATE_TEST_CREDENTIAL', ''.join(captured.output))
        self.assertEqual(f.backend.containers('worker-one', 'course-main'), [])

    def test_watchdog_reuses_owned_inventory_but_rescans_after_removal(self):
        f = self.fixture; result = f.spawn(); peer = f.spawn(course='course-peer')
        container = f.client.containers.get(result['server_id'])
        f.revoked.add('e' * 64)
        with patch.object(f.client.containers, 'list', wraps=f.client.containers.list) as inventory:
            f.dispatcher._supervise_endpoint('worker-one', 'course-main')
        self.assertEqual(inventory.call_count, 2)  # Observation, then post-effect proof.
        self.assertNotIn(container.id, f.client.containers.items)
        self.assertEqual(container.actions[-3:], [('update', {'restart_policy': {'Name': 'no'}}),
                                                 ('stop', 5), ('remove', False, False)])
        self.assertTrue(f.client.containers.get(peer['server_id']).attrs['State']['Running'])

    def test_watchdog_post_effect_scan_refuses_remaining_owned_container(self):
        f = self.fixture; result = f.spawn(); f.revoked.add('e' * 64)
        container = f.client.containers.get(result['server_id']); remove = f.backend.remove
        def remaining(*args, **kwargs):
            remove(*args, **kwargs)
            # An external Docker actor defeats removal: the final proof must
            # observe remaining state rather than trust the pre-effect list.
            f.client.containers.items[container.id] = container
        with patch.object(f.backend, 'remove', side_effect=remaining):
            self.refuses('runtime_quiescence_unverified',
                lambda: f.dispatcher._supervise_endpoint('worker-one', 'course-main'))
        self.assertIn(container.id, f.client.containers.items)

    def test_watchdog_inventory_failure_cannot_become_empty_success(self):
        f = self.fixture; result = f.spawn()
        container = f.client.containers.get(result['server_id'])
        container.attrs['Config']['Labels']['educloud.runtime.foreign'] = 'forged'
        self.refuses('runtime_container_scope_invalid',
            lambda: f.dispatcher._supervise_endpoint('worker-one', 'course-main'))
        self.assertEqual(container.actions, ['start'])
        self.assertTrue(container.attrs['State']['Running'])

    def test_guardian_failure_subtypes_are_fixed_and_public_code_unchanged(self):
        f = self.fixture
        for mutate, expected in ((lambda: setattr(f, 'now', 104), 'heartbeat_expired'),
                                 (lambda: setattr(f, 'now', 99), 'heartbeat_future'),
                                 (lambda: f.client.containers.get('worker-one-guardian').attrs['State'].update(StartedAt='PRIVATE_CANARY'), 'container_identity_changed')):
            f.now = 100; f.client.containers.get('worker-one-guardian').attrs['State']['StartedAt'] = '2026-10-14T00:00:00Z'
            f.heartbeat('worker-one', 'course-main'); mutate()
            with self.assertLogs('runtime_authority', level='WARNING') as logs:
                self.refuses('guardian_stale_or_changed', lambda: f.backend.require_guardian('worker-one', 'course-main', 1))
            self.assertEqual(logs.output, ['WARNING:runtime_authority:Guardian heartbeat refused: ' + expected])
            self.assertNotIn('PRIVATE_CANARY', ''.join(logs.output))
        f.now = 100; f.heartbeat('worker-one', 'course-main')
        with self.assertLogs('runtime_authority', level='WARNING') as logs:
            self.refuses('guardian_stale_or_changed', lambda: f.backend.require_guardian('worker-one', 'course-main', 2))
        self.assertEqual(logs.output, ['WARNING:runtime_authority:Guardian heartbeat refused: epoch_changed'])


if __name__ == '__main__': unittest.main()
