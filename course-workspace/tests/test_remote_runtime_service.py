"""Real authority/credential SQLite with synthetic worker channels; no Docker claim."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from authority_access import AccessPolicy, publish
from authority_test_pki import TestPKI
from hub.authority_client import VERSION as ACL_VERSION
from ownership_contract import OwnershipError, digest
from remote_backend import RemoteBackend, RemoteError, RemoteObservationBusy
import remote_runtime_service as service
import remote_credentials
from test_remote_backend import Channel, Client
import test_remote_backend as backend_fixture
from worker_enrollment import EnrollmentStore
from worker_transport import WorkerError


class RuntimeChannel(Channel):
    def call(self, operation, payload=None, *, authorize=None):
        if operation == 'binding':
            return {'binding_sha256': self.client.binding}
        if operation != 'runtime_v2':
            return super().call(operation, payload)
        if self.client.before_effect:
            self.client.before_effect()
        authorize({'kind': 'principal'})
        if payload['operation'] == 'hub.spawn':
            authorize({'kind': 'guardian', 'cert_sha256': '2' * 64})
        self.client.effects.append(payload['operation'])
        if self.client.result is not None:
            return self.client.result
        return {'authorized': True, 'epoch': self.request['epoch'],
                'worker_id': self.client.worker_id, 'course_id': self.course}


class RuntimeClient(Client):
    binding = 'a' * 64
    busy = False
    before_effect = None
    result = None
    @contextmanager
    def session(self, course, mode, request=None):
        if mode == 'observe' and self.busy:
            raise WorkerError('worker_observation_busy')
        if mode in ('activate', 'observe') and self.quarantined:
            raise WorkerError('worker_quarantined')
        channel = RuntimeChannel(self, course, mode, request)
        self.channels.append(channel)
        try:
            yield channel
        finally:
            channel.live = False


class RemoteRuntimeTests(unittest.TestCase):
    def setUp(self):
        backend_fixture.RemoteBackendTests.setUp(self)
        self.addCleanup(self.temp.cleanup)
        self.clients = {w: RuntimeClient(w, self.authority.authority_id) for w in self.clients}
        for client in self.clients.values():
            client.effects = []
        self.backend = RemoteBackend(self.authority.authority_id,
            {w: c.evidence for w, c in self.clients.items()}, self.clients, {w: ['main'] for w in self.clients})
        self.authority.backend = self.backend
        self.now = 100
        self.binding = {'runtime_config_sha256': 'a' * 64, 'ca_sha256': 'b' * 64,
            'home_root_bindings_sha256': 'c' * 64, 'workers': {
                w: {'provider_id': c.evidence['provider_id'], 'boot_id': c.evidence['boot_id'],
                    'evidence_sha256': digest(c.evidence), 'courses': ['main']} for w, c in self.clients.items()}}
        self.store = EnrollmentStore.create(self.root / 'enrollment', self.authority.authority_id,
                                            self.binding, clock=lambda: self.now)
        self.metadata = {slot: {'fingerprint': digit * 64, 'spki_sha256': other * 64,
            'not_before': 0, 'not_after': 1000} for slot, digit, other in [('hub:main', '1', '3'), ('guardian', '2', '4')]}
        self.apply('enroll', credentials=self.metadata)
        self.acl = self.root / 'acl.json'
        publish(self.acl, {'version': ACL_VERSION, 'authority_id': self.authority.authority_id, 'clients': {}})
        self.policy = AccessPolicy(self.acl, self.authority.authority_id, enrollment=self.store)
        self.dispatcher = service.RemoteRuntimeDispatcher(self.authority, self.backend,
            lambda fp, worker, course: self.policy.authorize_fingerprint(fp, 'guardian', worker, course),
            {worker: 'a' * 64 for worker in self.clients})

    def apply(self, action, **kwargs):
        plan = self.store.preview(action, 'worker-one', **kwargs)
        return self.store.apply(plan['plan_id'], plan['plan_sha256'], lambda: self.binding)

    def principal(self, operation='hub.authorize'):
        guardian = operation == 'guardian.observe'
        payload = {'guardian_container_id': '5' * 64, 'start_identity': '6' * 64} if guardian else {}
        return self.policy.principal(('2' if guardian else '1') * 64,
            {'operation': operation, 'worker_id': 'worker-one', 'course_id': 'main', 'payload': payload}), payload

    def test_real_authority_and_registry_forward_only_under_held_worker_channel(self):
        for operation in ('hub.authorize', 'guardian.observe'):
            principal, payload = self.principal(operation)
            reply = self.dispatcher.dispatch(principal, operation, 'main', payload)
            self.assertIs(reply['authorized'], True)
            self.assertEqual(reply['epoch'], 1)
        self.assertEqual(self.clients['worker-one'].effects, ['hub.authorize', 'guardian.observe'])
        self.assertEqual(self.clients['worker-one'].channels[-1].mode, 'observe')
        self.assertFalse(self.backend._sessions())

    def test_live_worker_binding_is_checked_under_admission_before_effect(self):
        principal, payload = self.principal()
        self.clients['worker-one'].binding = 'b' * 64
        with self.assertRaises(OwnershipError):
            self.dispatcher.dispatch(principal, 'hub.authorize', 'main', payload)
        self.assertEqual(self.clients['worker-one'].effects, [])
        self.assertFalse(self.backend._sessions())

    def test_revocation_between_admission_and_worker_effect_is_rechecked(self):
        principal, payload = self.principal()
        self.clients['worker-one'].before_effect = lambda: self.apply('revoke', target_fingerprint='1' * 64)
        with self.assertRaises(Exception): self.dispatcher.dispatch(principal, 'hub.authorize', 'main', payload)
        self.assertEqual(self.clients['worker-one'].effects, [])

    def test_current_guardian_revocation_is_checked_for_spawn_effect(self):
        principal, _ = self.principal()
        self.clients['worker-one'].before_effect = lambda: self.apply('revoke', target_fingerprint='2' * 64)
        with self.assertRaises(Exception): self.dispatcher.dispatch(principal, 'hub.spawn', 'main', {})
        self.assertEqual(self.clients['worker-one'].effects, [])

    def test_registry_loss_has_no_legacy_fallback_or_effect(self):
        principal, payload = self.principal()
        self.clients['worker-one'].before_effect = lambda: self.store.database.unlink()
        with self.assertRaises(Exception): self.dispatcher.dispatch(principal, 'hub.authorize', 'main', payload)
        self.assertEqual(self.clients['worker-one'].effects, [])

    def test_credential_expiry_during_remote_call_has_no_effect(self):
        principal, payload = self.principal()
        self.clients['worker-one'].before_effect = lambda: setattr(self, 'now', 1000)
        with self.assertRaises(Exception): self.dispatcher.dispatch(principal, 'hub.authorize', 'main', payload)
        self.assertEqual(self.clients['worker-one'].effects, [])

    def test_authority_state_rechecked_without_reciprocal_rpc(self):
        principal, payload = self.principal()
        def before():
            # Every reverse callback must remain local; reads before the call
            # have already completed on the held worker channel.
            self.backend.inspect_worker = lambda *args: self.fail('reciprocal worker read')
            self.backend.inspect_course = lambda *args: self.fail('reciprocal course read')
        self.clients['worker-one'].before_effect = before
        self.assertTrue(self.dispatcher.dispatch(principal, 'hub.authorize', 'main', payload)['authorized'])

    def test_corrupt_central_ownership_during_remote_call_has_no_effect(self):
        principal, payload = self.principal()
        def corrupt():
            with self.authority._connection() as conn:
                conn.execute("UPDATE courses SET epoch=epoch+1 WHERE course_id='main'")
                conn.commit()
        self.clients['worker-one'].before_effect = corrupt
        with self.assertRaises(Exception): self.dispatcher.dispatch(principal, 'hub.authorize', 'main', payload)
        self.assertEqual(self.clients['worker-one'].effects, [])

    def test_only_guardian_acquisition_busy_defers_without_heartbeat(self):
        principal, payload = self.principal('guardian.observe')
        self.clients['worker-one'].busy = True
        self.assertEqual(self.dispatcher.dispatch(principal, 'guardian.observe', 'main', payload),
            {'authorized': False, 'deferred': True, 'worker_id': 'worker-one', 'course_id': 'main'})
        self.assertEqual(self.clients['worker-one'].effects, [])
        self.clients['worker-one'].busy = False
        self.clients['worker-one'].before_effect = lambda: (_ for _ in ()).throw(WorkerError('worker_observation_busy'))
        with self.assertRaises(Exception): self.dispatcher.dispatch(principal, 'guardian.observe', 'main', payload)

    def test_quarantine_never_masquerades_as_guardian_contention(self):
        principal, payload = self.principal('guardian.observe')
        self.clients['worker-one'].quarantined = True
        with self.assertRaises(Exception): self.dispatcher.dispatch(principal, 'guardian.observe', 'main', payload)
        self.assertEqual(self.clients['worker-one'].effects, [])

    def test_runtime_channel_cannot_be_used_after_admission_or_with_wrong_mode(self):
        with self.assertRaises(RemoteError):
            self.backend.runtime_v2('worker-one', 'main', 'hub.spawn', {}, {}, lambda c: None)
        with self.authority.admission('main', 'worker-one'):
            with self.assertRaises(RemoteError):
                self.backend.runtime_v2('worker-one', 'main', 'guardian.observe', {}, {}, lambda c: None)

    def test_wrong_principal_roles_scope_and_malformed_results_refuse(self):
        principal, payload = self.principal()
        for changed in ({'role': 'operator'}, {'course_id': 'other'}, {'cert_sha256': 'invalid'}):
            with self.subTest(changed=changed), self.assertRaises(OwnershipError):
                self.dispatcher.dispatch(dict(principal, **changed), 'hub.authorize', 'main', payload)
        for result in ({}, {'authorized': 1, 'epoch': 1, 'worker_id': 'worker-one', 'course_id': 'main'},
                       {'authorized': True, 'epoch': True, 'worker_id': 'worker-one', 'course_id': 'main'}):
            self.clients['worker-one'].result = result
            with self.subTest(result=result), self.assertRaises(OwnershipError):
                self.dispatcher.dispatch(principal, 'hub.authorize', 'main', payload)


class RemoteCredentialTests(unittest.TestCase):
    def setUp(self):
        backend_fixture.RemoteBackendTests.setUp(self)
        self.addCleanup(self.temp.cleanup)
        self.pki = TestPKI(self.root / 'pki')
        self.plans = self.root / 'plans'; self.plans.mkdir(mode=0o700)
        self.acl = self.root / 'acl.json'
        publish(self.acl, {'version': ACL_VERSION, 'authority_id': self.authority.authority_id, 'clients': {}})
        self.config = {'version': service.VERSION, 'control': {'version': 'educloud-independent-workers-v1',
            'authority_id': self.authority.authority_id, 'authority_directory': str(self.root / 'authority'),
            'plan_directory': str(self.plans), 'workers': {}}, 'runtime_bindings': {w: 'a' * 64 for w in self.clients},
            'service': {'bind': '127.0.0.1', 'port': 8443, 'certfile': str(self.pki.server_cert),
                'keyfile': str(self.pki.server_key), 'ca_file': str(self.pki.ca_cert), 'acl_file': str(self.acl),
                'replay_directory': str(self.root / 'replay'), 'enrollment_directory': str(self.root / 'managed')}}
        for w, client in self.clients.items():
            self.config['control']['workers'][w] = {'evidence': client.evidence, 'courses': ['main'], 'channel': {
                'host': 'localhost', 'port': 8444, 'server_sha256': self.pki.fingerprint(self.pki.server_cert),
                'ca_file': str(self.pki.ca_cert), 'certfile': str(self.pki.server_cert), 'keyfile': str(self.pki.server_key)}}
        self.path = self.root / 'remote.json'; publish(self.path, self.config)
        self.backend.binding = lambda worker, course: 'a' * 64

    def test_remote_binding_requires_registered_worker_and_exact_runtime_pin(self):
        binding = service.verify_binding(self.config, backend=self.backend, authority=self.authority)
        self.assertEqual(binding['runtime_config_sha256'], digest(self.config))
        self.assertEqual(binding['workers']['worker-one']['courses'], ['main'])
        self.backend.binding = lambda worker, course: 'b' * 64
        with self.assertRaises(OwnershipError): service.verify_binding(self.config, backend=self.backend, authority=self.authority)

    def test_per_effect_binding_callback_is_local_and_rejects_configuration_or_ca_drift(self):
        binding = service.verify_binding(self.config, backend=self.backend, authority=self.authority)
        verify = service.local_binding_verifier(self.path, self.config, binding)
        with patch.object(self.backend, 'inspect_worker', side_effect=AssertionError('remote read')):
            verify('worker-one', 'main')
        changed = deepcopy(self.config); changed['service']['port'] += 1
        self.path.write_text(json.dumps(changed))
        with self.assertRaises(Exception): verify('worker-one', 'main')
        self.path.write_text(json.dumps(self.config))
        self.pki.ca_cert.write_bytes(self.pki.ca_cert.read_bytes() + b'\n')
        with self.assertRaises(Exception): verify('worker-one', 'main')

    def test_credentials_cli_creates_reviews_enrolls_and_rejects_changed_review(self):
        args = SimpleNamespace(config=str(self.path), command='create')
        with patch.object(service, 'validate_config', return_value=self.backend):
            self.assertEqual(remote_credentials.execute(args)['status'], 'CREATED')
            sources = {}
            for slot in ('guardian', 'hub:main'):
                certificate, _ = self.pki.issue(slot.replace(':', '-'))
                sources[slot] = str(certificate)
            source_file = self.root / 'leaves.json'; publish(source_file, sources)
            review_file = self.root / 'review.json'
            preview = SimpleNamespace(config=str(self.path), command='preview', worker='worker-one', action='enroll',
                credentials=str(source_file), slot=None, target_fingerprint=None, overlap_seconds=None, output=str(review_file))
            summary = remote_credentials.execute(preview)
            apply = SimpleNamespace(config=str(self.path), command='apply', plan_file=str(review_file), review_sha256=summary['review_sha256'])
            self.assertEqual(remote_credentials.execute(apply)['status'], 'COMPLETE')
            self.assertEqual(remote_credentials.execute(SimpleNamespace(config=str(self.path), command='status'))['live_credentials'], 2)
            apply.review_sha256 = '0' * 64
            with self.assertRaises(Exception): remote_credentials.execute(apply)
            # Removal of authority must remain possible while any worker is down.
            with patch.object(self.backend, 'inspect_worker', side_effect=OSError('offline')), patch.object(self.backend, 'binding', side_effect=OSError('offline')):
                self.assertEqual(remote_credentials.execute(SimpleNamespace(config=str(self.path), command='status'))['live_credentials'], 2)
                local = service.verify_binding(self.config, backend=self.backend, authority=self.authority, observe=False)
                self.assertEqual(local['workers']['worker-one']['courses'], ['main'])
                preview.action = 'revoke_worker'; preview.credentials = None
                preview.output = str(self.root / 'revoke.json')
                summary = remote_credentials.execute(preview)
                apply.plan_file = preview.output; apply.review_sha256 = summary['review_sha256']
                self.assertEqual(remote_credentials.execute(apply)['status'], 'COMPLETE')
                self.assertEqual(remote_credentials.execute(SimpleNamespace(config=str(self.path), command='status'))['live_credentials'], 0)


    def test_runtime_config_rejects_extra_modes_missing_managed_registry_and_boolean_port(self):
        # Client objects are synthetic here; no network or CA fallback is used.
        with patch.object(service, 'load_backend', return_value=self.backend):
            self.assertIs(service.validate_config(self.config), self.backend)
            for bad in (dict(self.config, fallback=True), dict(self.config, runtime_bindings={})):
                with self.assertRaises(OwnershipError): service.validate_config(bad)
            for change in ({'port': True}, {'enrollment_directory': ''}):
                bad = deepcopy(self.config); bad['service'].update(change)
                with self.assertRaises(OwnershipError): service.validate_config(bad)


if __name__ == '__main__':
    unittest.main()
