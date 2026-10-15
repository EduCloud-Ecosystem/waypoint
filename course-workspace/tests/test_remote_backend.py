import copy
from contextlib import contextmanager
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ownership_contract import Authority, OwnershipError, digest
from remote_backend import RemoteBackend, RemoteError
from remote_control import load_backend


class Channel:
    def __init__(self, client, course, mode, request):
        self.client, self.course, self.mode, self.request = client, course, mode, request
        self.live = True

    def call(self, operation, payload=None):
        if not self.live: raise RuntimeError('closed test channel')
        client = self.client
        client.calls.append((operation, self.mode))
        if operation == 'worker': return copy.deepcopy(client.evidence)
        if operation == 'course':
            return dict(client.course_evidence, admission_floor=client.barrier['denied_through_epoch'] + 1)
        if operation == 'check': return {'locked': self.live}
        if operation == 'state': return {'quarantined': client.quarantined}
        if operation == 'proof':
            return {'barrier': dict(client.barrier), 'course': self.call('course')}
        if operation == 'fence':
            if self.mode != 'exclusive': raise AssertionError('unlocked fence')
            client.quarantined = True
            client.barrier.update(request_sha256=digest(payload['request']),
                                  denied_through_epoch=payload['request']['old_epoch'])
            client.course_evidence['running_count'] = 0
            return {'barrier': dict(client.barrier), 'course': self.call('course')}
        if operation == 'runtime': return {'synthetic': True}
        raise AssertionError(operation)

    def finish(self):
        if self.client.finish_failure: raise RuntimeError('test completion reply lost')
        self.client.quarantined = False
        self.client.finishes += 1
        return {'finished': True}


class Client:
    def __init__(self, worker, authority):
        self.worker_id, self.authority_id = worker, authority
        self.evidence = {'worker_id': worker, 'provider_id': 'provider-' + worker,
            'boot_id': ('b' if worker == 'worker-one' else 'c') * 32, 'policy_sha256': 'd' * 64,
            'memory_budget_mb': 8192, 'cpu_budget': 8, 'capacity_memory_mb': 16384, 'capacity_cpu': 16}
        self.course_evidence = {'logical_identity': {'origin': 'https://main.test', 'dns_subtree': 'main.test',
            'issuer': 'https://identity.test/realms/main', 'client_id': 'main'},
            'profile': {'active_limit': 1, 'learner_memory_mb': 512, 'learner_cpu': .25, 'memory_mb': 1536, 'cpu': 1.25},
            'snapshot_sha256': 'e' * 64, 'runtime_sha256': 'f' * 64, 'retained_tree_sha256': 'a' * 64,
            'admission_floor': 1, 'running_count': 0}
        self.barrier = {'authority_id': authority, 'worker_id': worker, 'course_id': 'main',
                        'barrier_id': ('1' if worker == 'worker-one' else '2') * 32,
                        'denied_through_epoch': 0, 'request_sha256': '0' * 64}
        self.calls, self.channels = [], []
        self.finishes = 0
        self.quarantined, self.finish_failure = False, False

    @contextmanager
    def session(self, course, mode, request=None):
        if mode == 'activate' and self.quarantined: raise RuntimeError('quarantined test worker')
        channel = Channel(self, course, mode, request)
        self.channels.append(channel)
        try: yield channel
        finally: channel.live = False


class RemoteBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        os.chmod(self.temp.name, 0o700)
        class Empty:
            fixture_only = True
            scope = 'course'
        self.root = Path(self.temp.name).resolve()
        self.authority = Authority.create(self.root / 'authority', Empty())
        self.clients = {w: Client(w, self.authority.authority_id) for w in ('worker-one', 'worker-two')}
        self.backend = RemoteBackend(self.authority.authority_id,
            {w: c.evidence for w, c in self.clients.items()}, self.clients,
            {w: ['main'] for w in self.clients})
        self.authority.backend = self.backend
        for worker in self.clients: self.authority.register_worker(worker)
        self.authority.register_course('main', 'worker-one')

    def tearDown(self): self.temp.cleanup()

    def plan(self, name='plan'):
        path = self.root / (name + '.json')
        self.authority.preview('main', 'worker-two', path)
        return path

    def test_remote_transfer_uses_exact_held_channels_through_complete(self):
        self.assertEqual(self.authority.apply(self.plan())['status'], 'COMPLETE')
        for client in self.clients.values():
            self.assertEqual(client.finishes, 1)
            self.assertFalse(client.quarantined)
            self.assertTrue(all(mode == 'exclusive' for op, mode in client.calls if op in ('fence', 'proof', 'check')))
        with self.authority.admission('main', 'worker-two') as grant:
            self.assertEqual(grant['epoch'], 2)
            self.assertEqual(self.backend.runtime('worker-two', 'main', 'hub.spawn', {}), {'synthetic': True})
        with self.assertRaises(OwnershipError):
            with self.authority.admission('main', 'worker-one'): pass

    def test_runtime_requires_current_admission_and_same_endpoint(self):
        with self.assertRaises(RemoteError): self.backend.runtime('worker-one', 'main', 'hub.spawn', {})
        with self.authority.admission('main', 'worker-one'):
            with self.assertRaises(RemoteError): self.backend.runtime('worker-two', 'main', 'hub.spawn', {})
            with self.assertRaises(RemoteError): self.backend.runtime('worker-one', 'main', 'shell', {})

    def test_lost_post_commit_finish_is_recoverable_by_exact_completed_retry(self):
        path = self.plan()
        self.clients['worker-two'].finish_failure = True
        with self.assertRaises(OwnershipError): self.authority.apply(path)
        self.assertTrue(self.clients['worker-two'].quarantined)
        self.assertTrue(self.backend.quarantine('worker-two', 'main'))
        self.assertEqual(self.authority.inventory()['pending_transfers'], 0)
        with self.assertRaises(OwnershipError):
            with self.authority.admission('main', 'worker-two'): pass
        self.clients['worker-two'].finish_failure = False
        self.assertEqual(self.authority.resume(path)['status'], 'COMPLETE')
        self.assertFalse(self.clients['worker-two'].quarantined)
        with self.authority.admission('main', 'worker-two') as grant:
            self.assertEqual(grant['epoch'], 2)

    def test_stale_authorizer_has_no_fence_or_quarantine_effect(self):
        plan = self.plan()
        import json
        request = self.authority._request(json.loads(plan.read_text()))
        def refused(): raise OwnershipError('review changed')
        with self.assertRaises(OwnershipError):
            with self.backend.fence_context(request, authorize=refused): pass
        self.assertFalse(any(c.quarantined for c in self.clients.values()))
        self.assertFalse(any(op == 'fence' for c in self.clients.values() for op, _ in c.calls))

    def test_missing_completion_acknowledgment_is_not_success(self):
        path = self.plan()
        for value in ({}, {'finished': 1}, {'finished': False}, {'finished': True, 'extra': True}):
            with self.subTest(ack=value), patch.object(Channel, 'finish', return_value=value):
                with self.assertRaises(OwnershipError): self.authority.apply(path)
        self.assertTrue(all(c.quarantined for c in self.clients.values()))
        self.assertEqual(self.authority.resume(path)['status'], 'COMPLETE')

    def test_malformed_lock_acknowledgment_never_commits_ownership(self):
        path = self.plan()
        original = Channel.call
        for value in ({}, {'locked': 1}, {'locked': False}, {'locked': True, 'extra': True}):
            def call(channel, operation, payload=None):
                return value if operation == 'check' else original(channel, operation, payload)
            with self.subTest(ack=value), patch.object(Channel, 'call', new=call):
                with self.assertRaises(OwnershipError): self.authority.apply(path)
            self.assertEqual(self.authority.inventory()['pending_transfers'], 1)
            self.assertTrue(all(c.quarantined for c in self.clients.values()))
        self.assertEqual(self.authority.resume(path)['status'], 'COMPLETE')

    def test_failure_in_core_body_retains_quarantine_and_never_finishes(self):
        import json
        request = self.authority._request(json.loads(self.plan().read_text()))
        with self.assertRaises(RuntimeError):
            with self.backend.fence_context(request, authorize=lambda: None) as view:
                self.assertEqual(view.inspect()['observed_running_count'], 0)
                raise RuntimeError('injected core crash')
        self.assertTrue(all(c.quarantined for c in self.clients.values()))
        self.assertEqual(sum(c.finishes for c in self.clients.values()), 0)
        with self.assertRaises(RemoteError): view.inspect()

    def test_changed_worker_identity_is_not_a_stopped_observation(self):
        self.clients['worker-one'].evidence['boot_id'] = '9' * 32
        with self.assertRaises(RemoteError): self.backend.inspect_worker('worker-one')
        with self.assertRaises(OwnershipError): self.authority.preview('main', 'worker-two', self.root / 'bad')

    def test_lost_channel_never_supplies_fence_proof(self):
        import json
        request = self.authority._request(json.loads(self.plan().read_text()))
        with self.assertRaises(RuntimeError):
            with self.backend.fence_context(request, authorize=lambda: None) as view:
                self.backend._sessions()[('worker-two', 'main')].live = False
                view.inspect()
        self.assertTrue(all(c.quarantined for c in self.clients.values()))
        self.assertEqual(sum(c.finishes for c in self.clients.values()), 0)

    def test_operator_configuration_requires_explicit_exact_schema(self):
        for value in ({}, {'version': 'unknown'}, {'version': 'educloud-independent-workers-v1', 'workers': []}):
            with self.assertRaises(OwnershipError): load_backend(value)


if __name__ == '__main__': unittest.main()
