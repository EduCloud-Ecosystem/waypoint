"""Fixture allocation selection and bounded private-log diagnostic contracts."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
import authenticated_fixture as fixture

NAME = 'wa1234567890'
IMAGE = 'sha256:' + 'a' * 64
HUB_IMAGE = 'sha256:' + 'b' * 64
CONTROLLER_IMAGE = 'sha256:' + 'c' * 64
CANARY = 'PRIVATE_TOKEN_AND_PATH_/private/forbidden'


class Stream:
    def __init__(self, raw): self.raw, self.closed = raw, False
    def __iter__(self): yield self.raw
    def close(self): self.closed = True


class Container:
    def __init__(self, name, image, labels, raw=b'', state=None):
        self.attrs = {'Name': '/' + name, 'Image': image, 'Config': {'Labels': labels},
                      'State': state or {'Running': True, 'OOMKilled': False, 'ExitCode': 0}}
        self.raw = raw; self.reads = 0; self.streams = []
    def reload(self): pass
    def logs(self, **options):
        assert options == {'stream': True, 'follow': False, 'tail': 2000}
        self.reads += 1; stream = Stream(self.raw); self.streams.append(stream); return stream
    def start(self): pass


class Docker:
    def __init__(self, cpu=8):
        self.cpu = cpu; self.items = {}; self.hubs = []; self.guards = []; self.volume_calls = []
        self.containers = self
        self.volumes = types.SimpleNamespace(create=self.volume)
        self.networks = types.SimpleNamespace(get=lambda name: types.SimpleNamespace(connect=lambda container: None))
    def info(self): return {'ID': 'synthetic-daemon', 'NCPU': self.cpu, 'MemTotal': 8 * 1024**3}
    def volume(self, name, **options): self.volume_calls.append((name, options)); return types.SimpleNamespace(name=name)
    def get(self, name): return self.items[name]
    def list(self, all, filters):
        labels = filters['label']
        return [container for container in self.items.values() if all_match(container, labels)]
    def create(self, image, name, **options):
        self.hubs.append(options)
        return Container(name, image, options['labels'])
    def run(self, image, name, **options): self.guards.append(options)


def all_match(container, labels):
    return all(container.attrs['Config']['Labels'].get(key) == value for key, value in (label.split('=', 1) for label in labels))


class PKI:
    def __init__(self, root):
        self.root = root; root.mkdir(mode=0o700); self.ca_cert = root / 'ca'; self.ca_cert.write_bytes(b'synthetic CA')
    def issue(self, identity, **options):
        cert, key = self.root / (identity + '.crt'), self.root / (identity + '.key')
        cert.write_bytes(b'certificate'); key.write_bytes(b'private synthetic key'); return cert, key
    def fingerprint(self, cert): return hashlib.sha256(cert.name.encode()).hexdigest()


class PresetDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.root.chmod(0o700)
        for name in ('control', 'backend'): (self.root / name).mkdir(mode=0o700)
        self.request = {'name': NAME, 'course_image': IMAGE, 'hub_image': HUB_IMAGE, 'controller_image': CONTROLLER_IMAGE,
            'ports': {'source': 10001, 'peer': 10002, 'receiver': 10003}, 'password': CANARY, 'network': 'owned-network', 'edge_network': 'owned-edge'}
    def initialize(self, cpu):
        client = Docker(cpu); captured = []
        def bootstrap(config, directory, actual):
            captured.append(config); config['authority_id'] = 'd' * 32
            authority = types.SimpleNamespace(authority_id=config['authority_id'], register_worker=lambda worker: None,
                                             register_course=lambda course, worker: None)
            return authority, None, config
        sdk = types.SimpleNamespace(DockerClient=lambda **options: client)
        with patch.dict(sys.modules, {'docker': sdk}), patch.object(fixture, 'bootstrap', side_effect=bootstrap), patch.object(fixture, 'TestPKI', PKI), patch.object(fixture.ReplayStore, 'create'), patch.object(fixture.os, 'chown'):
            result = fixture.initialize(self.root, self.request)
        return client, captured[0], result

    def test_deterministic_verified_cpu_presets_and_malformed_capacity_refusal(self):
        self.assertEqual(fixture.course_allocation(4), {'name': 'four-cpu-ci', 'learner_cpu': .1, 'hub_cpu': 1.0, 'reserved_cpu': 4.0, 'minimum_cpu': 4})
        for cpu in (5, 8, 64):
            self.assertEqual(fixture.course_allocation(cpu), {'name': 'five-cpu-local', 'learner_cpu': .25, 'hub_cpu': 1.0, 'reserved_cpu': 4.5, 'minimum_cpu': 5})
        for cpu in (True, False, 4.0, 3, -1, '8', None):
            with self.subTest(cpu=cpu), self.assertRaises(ValueError): fixture.course_allocation(cpu)

    def test_initialization_and_launch_use_selected_actual_profile(self):
        for cpu in (4, 8):
            with self.subTest(cpu=cpu), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory).resolve(); self.root.chmod(0o700)
                for name in ('control', 'backend'): (self.root / name).mkdir(mode=0o700)
                client, config, result = self.initialize(cpu)
                allocation = fixture.course_allocation(cpu)
                self.assertEqual(result['allocation'], allocation)
                self.assertEqual(fixture.read_record(self.root / 'fixture.json')['allocation'], allocation)
                self.assertEqual(sum(worker['cpu_budget'] for worker in config['workers'].values()) + config['host_reserve_cpu'], allocation['reserved_cpu'])
                self.assertEqual(sum(worker['memory_budget_mb'] for worker in config['workers'].values()) + config['host_reserve_mb'], 6656)
                sdk = types.SimpleNamespace(DockerClient=lambda **options: client)
                with patch.dict(sys.modules, {'docker': sdk}): fixture.launch(self.root)
                self.assertEqual(len(client.hubs), 3); self.assertEqual(len(client.guards), 2)
                for options in client.hubs:
                    self.assertEqual(options['environment']['WORKSPACE_CPU_LIMIT'], str(allocation['learner_cpu']))
                    self.assertEqual(options['environment']['WORKSPACE_MEMORY_MB'], '512')
                    self.assertEqual(options['cpu_quota'] / options['cpu_period'], 1)
                    self.assertEqual(options['mem_limit'], 1024 * 1024**2)
                for courses in config['courses'].values():
                    for item in courses.values():
                        self.assertEqual(item['profile']['learner_cpu'], allocation['learner_cpu'])
                        self.assertEqual(item['profile']['cpu'], 1 + allocation['learner_cpu'])

    def populate(self):
        client, config, _ = self.initialize(8)
        stages = '\n'.join('Authority notebook startup: ' + value for value in fixture.STAGES.values())
        hubraw = (stages + '\nAuthorityError: authority_operation_refused\nAuthorityError: ' + CANARY
                  + "\nalice's server never showed up at http://" + CANARY + ' after 120 seconds. Giving up.\n').encode()
        for worker, courses in config['courses'].items():
            for course, item in courses.items():
                client.items[item['hub_container']] = Container(item['hub_container'], HUB_IMAGE,
                    {'educloud.workspace.instance': item['instance'], 'educloud.workspace.kind': 'hub'}, hubraw)
                key = hashlib.sha256(b'alice').hexdigest()[:24]; name = item['instance'] + '-learner-' + key
                client.items[name] = Container(name, IMAGE, {'educloud.workspace.instance': item['instance'], 'educloud.workspace.kind': 'learner',
                    'educloud.runtime.authority': config['authority_id'], 'educloud.runtime.worker': worker,
                    'educloud.runtime.course': course, 'educloud.runtime.subject': key, 'educloud.runtime.epoch': '1'})
            guardian = config['guardians'][worker]
            raw = ('Guardian observation refused: authority_transport_unavailable\nGuardian snapshot deferred: endpoint_busy\nGuardian stop deferred: endpoint_busy\n' + CANARY).encode()
            client.items[guardian['container_name']] = Container(guardian['container_name'], CONTROLLER_IMAGE,
                {'educloud.runtime.authority': config['authority_id'], 'educloud.runtime.worker': worker, 'educloud.runtime.kind': 'guardian'}, raw)
        path = self.root / 'supervision.log'
        path.write_text('Runtime supervision stopping owned learners: guardian_stale_or_changed\n' + CANARY); path.chmod(0o600)
        fixture.write(self.root / 'service-failure.json', {'failure': CANARY})
        return client

    def test_fixed_diagnostics_counts_and_no_private_values_or_side_effects(self):
        client = self.populate(); result = fixture.failure_status(self.root, client)
        self.assertEqual(result['authority_failure'], 'unknown')
        self.assertEqual(result['supervisor']['reason_counts']['guardian_stale_or_changed'], 1)
        self.assertEqual(set(result['hubs']), {'source', 'peer', 'receiver'})
        for hub in result['hubs'].values():
            self.assertTrue(hub['available']); self.assertEqual(set(hub['startup_stage_counts'].values()), {1})
            self.assertEqual(hub['authority_error_counts']['authority_operation_refused'], 1)
            self.assertEqual(hub['http_startup_timeout_count'], 1)
        for guard in result['guardians'].values():
            self.assertTrue(guard['available']); self.assertEqual(guard['snapshot_busy_count'], 1)
            self.assertEqual(guard['stop_busy_count'], 1)
        for learner in result['learners'].values():
            self.assertEqual(learner, {'available': True, 'states': [{'Running': True, 'OOMKilled': False, 'ExitCode': 0}]})
        serialized = json.dumps(result)
        for private in (CANARY, NAME, str(self.root), IMAGE, 'educloud.', 'alice'): self.assertNotIn(private, serialized)
        self.assertEqual(client.hubs, []); self.assertEqual(client.guards, [])
        self.assertTrue(all(stream.closed for container in client.items.values() for stream in container.streams))

    def test_foreign_identity_never_read_and_malformed_state_never_published(self):
        client = self.populate()
        source = client.items[NAME + '-source-hub']; source.attrs['Image'] = CONTROLLER_IMAGE
        guardian = client.items[NAME + '-worker-one-guardian']; guardian.attrs['Config']['Labels']['educloud.runtime.worker'] = CANARY
        key = hashlib.sha256(b'alice').hexdigest()[:24]
        learner = client.items[NAME + '-source-learner-' + key]; learner.attrs['State']['ExitCode'] = CANARY
        result = fixture.failure_status(self.root, client)
        self.assertFalse(result['hubs']['source']['available']); self.assertEqual(source.reads, 0)
        self.assertFalse(result['guardians']['worker-one']['available']); self.assertEqual(guardian.reads, 0)
        self.assertEqual(result['learners']['source'], {'available': False, 'states': []})
        self.assertNotIn(CANARY, json.dumps(result))

    def test_stream_byte_bound_truncation_and_unknown_topology_refusal(self):
        client = self.populate(); source = client.items[NAME + '-source-hub']
        source.raw = b'x' * (fixture.DIAGNOSTIC_LIMIT + 10000) + CANARY.encode()
        result = fixture.failure_status(self.root, client)
        self.assertTrue(result['hubs']['source']['truncated']); self.assertTrue(source.streams[0].closed)
        self.assertNotIn(CANARY, json.dumps(result))
        config = fixture.read_record(self.root / 'runtime.json')
        config['courses']['worker-one']['course-main']['instance'] = CANARY
        fixture.write(self.root / 'runtime.json', config)
        with self.assertRaisesRegex(ValueError, '^fixture_diagnostic_scope_invalid$'): fixture.failure_status(self.root, client)

    def test_empty_chunk_transport_is_bounded_closed_and_marked_incomplete(self):
        class Empty:
            closed = False
            count = 0
            def __iter__(self):
                while True: self.count += 1; yield b''
            def close(self): self.closed = True
        stream = Empty()
        container = types.SimpleNamespace(logs=lambda **options: stream)
        self.assertEqual(fixture._diagnostic_logs(container), ('', True))
        self.assertTrue(stream.closed); self.assertEqual(stream.count, 16385)

    def test_failure_status_never_reads_supervisor_symlink_target(self):
        client = self.populate(); path = self.root / 'supervision.log'; path.unlink()
        outside = self.root / 'sentinel'; outside.write_text(CANARY)
        path.symlink_to(outside)
        before = outside.stat()
        result = fixture.failure_status(self.root, client)
        self.assertFalse(result['supervisor']['available']); self.assertEqual(outside.read_text(), CANARY)
        self.assertEqual(outside.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertNotIn(CANARY, json.dumps(result))


if __name__ == '__main__': unittest.main()
