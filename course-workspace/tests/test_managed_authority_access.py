"""Real mTLS against managed runtime membership; no hosted-worker acceptance."""
from copy import deepcopy
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from authority_test_pki import TestPKI
from authority_access import AccessPolicy, AuthorityServer, ReplayStore, publish
from hub.authority_client import AuthorityClient, AuthorityError, VERSION
from worker_credentials import verify_certificate
from worker_enrollment import EnrollmentStore, MAX_CERTIFICATES
import controller_service


class ManagedAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(); root = Path(cls.temporary.name).resolve()
        cls.pki = TestPKI(root / 'pki')
        cls.leaves = {role: cls.pki.issue(role) for role in ('hub', 'guardian', 'operator', 'new', 'unknown', 'peer-hub', 'peer-guardian')}
        cls.metadata = {role: verify_certificate(pair[0], cls.pki.ca_cert) for role, pair in cls.leaves.items()}
    @classmethod
    def tearDownClass(cls): cls.temporary.cleanup()
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup); self.root = Path(temporary.name).resolve()
        self.authority = uuid.uuid4().hex; self.now = int(time.time())
        self.binding = {'runtime_config_sha256': 'a' * 64, 'ca_sha256': self.pki.fingerprint(self.pki.ca_cert), 'home_root_bindings_sha256': 'b' * 64,
            'workers': {worker: {'provider_id': 'provider-' + worker, 'boot_id': boot * 32, 'evidence_sha256': boot * 64, 'courses': [course]}
                        for worker, boot, course in (('one', 'c', 'course'), ('two', 'd', 'peer'))}}
        self.store = EnrollmentStore.create(self.root / 'enrollment', self.authority, self.binding, clock=lambda: self.now)
        self.acl = self.root / 'acl.json'
        # Deliberately retain a legacy runtime row; it must be inert in managed mode.
        publish(self.acl, {'version': VERSION, 'authority_id': self.authority, 'clients': {
            self.metadata['operator']['fingerprint']: {'role': 'operator', 'workers': ['one', 'two'], 'courses': ['course', 'peer']},
            self.metadata['hub']['fingerprint']: {'role': 'hub', 'workers': ['one'], 'courses': ['course']}}})
        self.policy = AccessPolicy(self.acl, self.authority, enrollment=self.store); self.effects = []; self.extra_effect = None
        def dispatch(principal, op, course, payload):
            if self.extra_effect: self.extra_effect(principal)
            principal['authorize'](); self.effects.append((principal['role'], course)); return {'observed': True}
        replay = ReplayStore.create(self.root / 'replay', self.authority)
        self.server = AuthorityServer(('127.0.0.1', 0), authority_id=self.authority, certfile=self.pki.server_cert, keyfile=self.pki.server_key,
            ca_file=self.pki.ca_cert, acl_file=self.acl, replay_store=replay, dispatch=dispatch, access_policy=self.policy, timeout=.5)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True); self.thread.start()
        self.addCleanup(self.close)
    def close(self): self.server.shutdown(); self.server.server_close(); self.thread.join(2)
    def client(self, role='hub', worker='one', course='course'):
        cert, key = self.leaves[role]
        return AuthorityClient('https://localhost:' + str(self.server.server_port), self.authority, self.pki.ca_cert, cert, key, worker, course, timeout=1)
    def call(self, role='hub', worker='one', course='course'):
        op = 'guardian.observe' if 'guardian' in role else 'ownership.inventory' if role == 'operator' else 'hub.authorize'
        payload = {'guardian_container_id': 'a' * 64, 'start_identity': 'b' * 64} if op == 'guardian.observe' else {}
        return self.client(role, worker, course).call(op, payload)
    def refused(self, callback, code='authority_access_refused'):
        with self.assertRaises(AuthorityError) as caught: callback()
        self.assertEqual(caught.exception.code, code)
    def apply(self, action, worker='one', **kwargs):
        plan = self.store.preview(action, worker, **kwargs); return self.store.apply(plan['plan_id'], plan['plan_sha256'], lambda: self.binding)
    def enroll(self, worker='one'):
        self.apply('enroll', worker, credentials={'guardian': deepcopy(self.metadata['guardian' if worker == 'one' else 'peer-guardian']),
            'hub:' + ('course' if worker == 'one' else 'peer'): deepcopy(self.metadata['hub' if worker == 'one' else 'peer-hub'])})
    def test_actual_tls_unknown_and_legacy_static_runtime_inert_until_enrolled(self):
        self.refused(self.call); self.refused(lambda: self.call('unknown'))
        self.assertEqual(self.call('operator'), {'observed': True})
        self.enroll(); self.assertEqual(self.call(), {'observed': True}); self.assertEqual(self.call('guardian'), {'observed': True})
        self.refused(lambda: self.call('hub', 'two', 'peer'))
        self.assertIs(self.server.policy, self.policy)
    def test_staging_inert_overlap_deadline_and_same_existing_client_fresh_refusal(self):
        self.enroll(); old = self.client(); old.call('hub.authorize', {})
        self.refused(lambda: self.call('new'))
        self.apply('stage', slot='hub:course', credentials=deepcopy(self.metadata['new']))
        self.refused(lambda: self.call('new'))
        self.apply('activate', slot='hub:course', overlap_seconds=2)
        self.assertEqual(self.call('new'), {'observed': True}); self.assertEqual(old.call('hub.authorize', {}), {'observed': True})
        self.now += 2
        self.refused(lambda: old.call('hub.authorize', {})); self.assertEqual(self.call('new'), {'observed': True})
    def test_live_expiry_revoke_and_captured_principal_refuse_before_effect(self):
        self.enroll(); self.enroll('two')
        def revoke(principal): self.apply('revoke', target_fingerprint=principal['cert_sha256'])
        self.extra_effect = revoke
        self.refused(self.call, 'authority_operation_refused'); self.assertEqual(self.effects, [])
        self.extra_effect = None; self.assertEqual(self.call('peer-hub', 'two', 'peer'), {'observed': True})
        guardian_fp = self.metadata['guardian']['fingerprint']
        self.policy.authorize_fingerprint(guardian_fp, 'guardian', 'one', 'course')
        self.now = self.metadata['guardian']['not_after']
        self.refused(lambda: self.policy.authorize_fingerprint(guardian_fp, 'guardian', 'one', 'course'))
    def test_revoke_worker_denies_own_roles_and_preserves_peer_and_static_operator(self):
        self.enroll(); self.enroll('two'); self.apply('revoke_worker')
        self.refused(self.call); self.refused(lambda: self.call('guardian'))
        self.assertEqual(self.call('peer-hub', 'two', 'peer'), {'observed': True})
        self.assertEqual(self.call('peer-guardian', 'two', 'peer'), {'observed': True})
        self.assertEqual(self.call('operator'), {'observed': True})
    def test_fresh_binding_callback_denies_captured_principal_and_guardian_without_affecting_static_operator(self):
        self.enroll(); checked = []; changed = [False]
        def binding(worker, course):
            checked.append((worker, course))
            if changed[0]: raise RuntimeError('PRIVATE_BINDING_CANARY')
        policy = AccessPolicy(self.acl, self.authority, enrollment=self.store, binding_verifier=binding)
        frame = {'operation': 'hub.authorize', 'worker_id': 'one', 'course_id': 'course', 'payload': {}}
        principal = policy.principal(self.metadata['hub']['fingerprint'], frame)
        changed[0] = True
        self.refused(principal['authorize'])
        self.refused(lambda: policy.authorize_fingerprint(self.metadata['guardian']['fingerprint'], 'guardian', 'one', 'course'))
        count = len(checked); policy.authorize_fingerprint(self.metadata['operator']['fingerprint'], 'operator', 'one', 'course')
        self.assertEqual(len(checked), count)

    def test_registry_loss_cannot_fall_back_to_existing_static_runtime_entry(self):
        self.enroll(); self.store.database.unlink()
        self.refused(self.call); self.refused(lambda: self.call('guardian'))
        self.assertEqual(self.call('operator'), {'observed': True})
    def test_operator_leaf_collision_cannot_obtain_managed_runtime_role(self):
        self.apply('enroll', credentials={'guardian': deepcopy(self.metadata['operator']), 'hub:course': deepcopy(self.metadata['new'])})
        self.refused(lambda: self.client('operator').call('guardian.observe', {'guardian_container_id': 'a' * 64, 'start_identity': 'b' * 64}))
        self.assertEqual(self.call('operator'), {'observed': True})
    def test_controller_injects_identical_managed_policy_into_server_and_guardian_callback(self):
        self.enroll(); captured = {}
        config = {'authority_id': self.authority}
        service = {'authority_directory': 'private', 'enrollment_directory': str(self.store.directory), 'acl_file': str(self.acl),
            'replay_directory': 'private', 'plan_directory': 'private', 'bind': '127.0.0.1', 'port': 0,
            'certfile': str(self.pki.server_cert), 'keyfile': str(self.pki.server_key), 'ca_file': str(self.pki.ca_cert)}
        class Runtime:
            def __init__(self, authority, backend, guardian): captured['guardian'] = guardian
            def supervision_tick(self): pass
            def dispatch(self, *args): pass
        class Server:
            def __init__(self, address, **options): captured['policy'] = options['access_policy']
            def serve_forever(self, **options): pass
            def server_close(self): pass
            def shutdown(self): pass
        with patch.object(controller_service, 'DockerBackend'), patch.object(controller_service.Authority, 'open'), patch('worker_credentials.verify_binding', return_value=self.binding), patch('worker_credentials.make_runtime_binding_verifier') as factory, patch.object(controller_service, 'RuntimeDispatcher', Runtime), patch.object(controller_service, 'OperatorDispatcher'), patch.object(controller_service, 'AuthorityServer', Server), patch.object(controller_service.ReplayStore, 'open'):
            controller_service.serve(config, service)
        binding_check = factory.return_value
        policy = captured['policy']; self.assertIsNotNone(policy.enrollment)
        fp = self.metadata['guardian']['fingerprint']; captured['guardian'](fp, 'one', 'course')
        self.assertEqual(binding_check.call_args.args[-2:], ('one', 'course'))
        self.assertEqual(factory.call_count, 1)
        self.apply('revoke', target_fingerprint=fp)
        self.refused(lambda: captured['guardian'](fp, 'one', 'course'))
    def test_managed_live_map_bound_matches_registry_instead_of_legacy_128_cap(self):
        approved = {format(index, '064x'): {'role': 'hub', 'workers': ['one'], 'courses': ['course']} for index in range(129)}
        store = types.SimpleNamespace(authority_id=self.authority, entries=lambda: approved)
        policy = AccessPolicy(self.acl, self.authority, enrollment=store)
        policy.authorize_fingerprint(format(128, '064x'), 'hub', 'one', 'course')
        oversized = {format(index, '064x'): {'role': 'hub', 'workers': ['one'], 'courses': ['course']} for index in range(MAX_CERTIFICATES + 1)}
        store.entries = lambda: oversized
        self.refused(lambda: policy.authorize_fingerprint(format(128, '064x'), 'hub', 'one', 'course'))

    def test_configured_missing_or_empty_registry_refuses_service_no_legacy_fallback(self):
        for value in ('', str(self.root / 'missing')):
            service = {'authority_directory': 'private', 'enrollment_directory': value, 'acl_file': str(self.acl)}
            with patch.object(controller_service, 'DockerBackend'), patch.object(controller_service.Authority, 'open'), patch('worker_credentials.verify_binding', return_value=self.binding), patch.object(controller_service, 'AuthorityServer') as server:
                with self.assertRaises(Exception): controller_service.serve({'authority_id': self.authority}, service)
                server.assert_not_called()


if __name__ == '__main__': unittest.main()
