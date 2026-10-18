"""Deterministic denial/race controls; real provider acceptance is separate."""
import copy
from email.message import Message
from http.client import IncompleteRead
import json
import subprocess
import unittest
from unittest.mock import patch
from research_workspace.identity import Identity, IdentityUnavailable
from research_workspace.recipes import expected_result
from research_workspace.runtime import Worker
from research_workspace.store import AccessError, canonical
import test_workspace as workspace
import test_recovery as recovery


def config():
    return {'issuer': 'http://127.0.0.1:19001/idp/realms/test', 'audience': 'research', 'client_id': 'research',
            'client_secret': 'c' * 40, 'boundary_secret': 'b' * 40, 'public_origin': 'http://127.0.0.1:19001',
            'proxy_auth_url': 'http://oauth2-proxy:4180/oauth2/auth',
            'introspection_url': 'http://keycloak:8080/idp/realms/test/protocol/openid-connect/token/introspect',
            'subjects': {'external-alice': 'alice', 'external-bob': 'bob'}, 'cookie_name': 'app_test', 'proxy_cookie_name': 'proxy_test'}


class IdentityTests(unittest.TestCase):
    tearDown = workspace.WorkspaceTests.tearDown

    def setUp(self):
        workspace.WorkspaceTests.setUp(self)
        self.clock = 1000
        self.identity = Identity(config(), self.store, clock=lambda: self.clock)
        self.clock = 1002
        self.claims = {'active': True, 'iss': config()['issuer'], 'aud': ['research'], 'sub': 'external-alice',
                       'sid': 'session-one', 'iat': 1002, 'auth_time': 1002, 'exp': 1600}
        self.token = 'private-synthetic-token'
        self.identity._browser_token = lambda cookie: self.token
        self.identity._request = lambda request: copy.deepcopy(self.claims)
        _, self.cookie = self.identity.begin('private-cookie')
        self.context = self.identity.authenticate(self.cookie, 'private-cookie')

    def submit(self, key=None):
        return self.store.submit(self.pid, 'alice', {'recipe': 'summary-python', 'condition': 'baseline', 'csv': workspace.CSV}, workspace.IMAGES,
                                 key, lambda rid: self.identity.bind(rid, self.context))

    def test_introspection_requires_every_current_authority_claim(self):
        for key, value in [('active', False), ('active', 1), ('iss', 'http://other'), ('aud', ['wrong']), ('exp', 1002),
                           ('exp', True), ('iat', 1000), ('iat', 2000), ('auth_time', 1000), ('sub', 'unknown'), ('sid', '')]:
            with self.subTest(key=key, value=value):
                original = self.claims[key]; self.claims[key] = value
                with self.assertRaises(AccessError): self.identity.inspect_token(self.token)
                self.claims[key] = original

    def test_restart_rejects_original_even_if_auth_time_changes(self):
        self.identity.epoch = 1003; self.clock = 1004; self.claims['auth_time'] = 1004
        with self.assertRaises(AccessError): self.identity.inspect_token(self.token)

    def test_opaque_mapping_and_database_activation_are_authoritative(self):
        self.claims.update(email='owner@example.test', roles=['owner'])
        self.assertEqual(self.identity.inspect_token(self.token)[0], 'alice')
        self.store.revoke_user('alice')
        with self.assertRaises(AccessError): self.identity.inspect_token(self.token)

    def test_boundary_requires_one_exact_operator_secret(self):
        headers = Message(); headers['X-Auth-Request-User'] = 'alice'
        self.assertFalse(self.identity.boundary(headers)); headers['X-Research-Proxy'] = 'b' * 40
        self.assertTrue(self.identity.boundary(headers)); headers['X-Research-Proxy'] = 'b' * 40
        self.assertFalse(self.identity.boundary(headers))

    def test_only_owned_proxy_cookies_cross_private_boundary(self):
        adapter = Identity(config(), self.store); captured = []
        adapter._request = lambda req, **kw: captured.append(req) or self.token
        adapter._browser_token('proxy_test=a; proxy_test_0=b; proxy_test_csrf=c; app_test=d; unrelated=e')
        self.assertEqual(captured[0].get_header('Cookie'), 'proxy_test=a; proxy_test_0=b')

    def test_protocol_failure_is_generic_identity_unavailable(self):
        adapter = Identity(config(), self.store)
        with patch.object(adapter, '_fetch', side_effect=IncompleteRead(b'sensitive-prefix')):
            with self.assertRaises(IdentityUnavailable) as error: adapter._request(None)
        self.assertNotIn('sensitive', str(error.exception))

    def test_logout_is_effective_during_inflight_check(self):
        run = self.submit()
        def respond(request):
            self.identity.logout(self.cookie)
            return self.claims
        self.identity._request = respond
        with self.assertRaises(AccessError): self.identity.check_run(run['id'])
        with self.assertRaises(AccessError): self.identity.begin('replayed-cookie')

    def test_publication_cannot_pass_logout_after_final_online_check(self):
        self.submit(); run = self.store.claim(); self.identity.check_run(run['id'])
        self.identity.logout(self.cookie)
        run.update(status='succeeded', metrics=expected_result(workspace.CSV, 'baseline'))
        self.store.finish(run, run['metrics'], publication=self.identity.publication(run['id']))
        saved = self.store.run(self.pid, run['id'], 'alice')
        self.assertEqual(saved['status'], 'cancelled'); self.assertEqual(saved['metrics'], {})
        self.assertFalse((self.root / 'runs' / run['id'] / 'result.json').exists())

    def test_twenty_preclaim_cancellations_release_finite_contexts(self):
        for _ in range(20):
            run = self.submit(); self.store.cancel(self.pid, run['id'], 'alice')
        self.assertIsNone(self.store.claim(on_cancel=self.identity.release)); self.assertEqual(self.identity.runs, {})
        self.submit()

    def test_restart_cancels_queue_and_idempotency_never_rebinds(self):
        run = self.submit('same-request'); self.store.recover(cancel_queued=True); self.identity.runs.clear()
        repeated = self.submit('same-request')
        self.assertEqual(repeated['id'], run['id']); self.assertEqual(repeated['status'], 'cancelled')
        self.assertEqual(self.identity.runs, {}); self.assertIsNone(self.store.claim())

    def test_pre_dispatch_identity_loss_admits_no_compute(self):
        self.submit(); run = self.store.claim()
        self.identity._request = lambda req: (_ for _ in ()).throw(IdentityUnavailable())
        with patch('research_workspace.runtime.subprocess.Popen') as process:
            Worker(self.store, self.broker, self.identity).execute(run); process.assert_not_called()
        saved = self.store.run(self.pid, run['id'], 'alice')
        self.assertEqual(saved['status'], 'cancelled'); self.assertEqual(saved['compute']['charged_cpu_seconds'], 0)
        self.assertTrue(saved['cleanup_verified'])

    def test_logout_immediately_before_launch_admits_no_compute(self):
        self.submit(); run = self.store.claim()
        from research_workspace import runtime
        original = runtime.private_write
        def write(path, content):
            original(path, content)
            if path.name == 'request.private.json': self.identity.logout(self.cookie)
        with patch.object(runtime, 'private_write', side_effect=write), patch.object(runtime.subprocess, 'Popen') as process:
            Worker(self.store, self.broker, self.identity).execute(run); process.assert_not_called()
        self.assertEqual(self.store.run(self.pid, run['id'], 'alice')['compute']['charged_cpu_seconds'], 0)

    def test_execution_outage_latches_denial_even_after_recovery(self):
        self.submit(); run = self.store.claim(); result = expected_result(workspace.CSV, 'baseline')
        receipt = {'schema_version': 1, 'request_id': run['id'], 'project_id': self.pid, 'image': run['environment']['image'],
            'status': 'succeeded', 'admitted': True, 'execution_started': True, 'cleanup_verified': True,
            'unresolved_liability': False, 'stdout': canonical(result), 'stderr': '', 'error': '', 'exit_code': 0,
            'compute': {'reserved_cpu_seconds': 30, 'charged_cpu_seconds': 30, 'provenance': 'estimated_upper_bound'}}
        process = unittest.mock.Mock(returncode=0)
        def collect(process, *, timeout, tick):
            tick()
            return canonical(receipt).encode(), b''
        worker = Worker(self.store, self.broker, self.identity)
        self.broker.command = lambda *args: ['synthetic-broker']; self.broker.call = lambda *args: {}
        with patch('research_workspace.runtime.subprocess.Popen', return_value=process), patch('research_workspace.runtime.collect', side_effect=collect), patch('research_workspace.runtime.time.monotonic', side_effect=[0, 2, 2]), patch.object(worker, 'identity_allowed', side_effect=[True, False, True]):
            worker.execute(run)
        saved = self.store.run(self.pid, run['id'], 'alice')
        self.assertEqual(saved['status'], 'cancelled'); self.assertEqual(saved['metrics'], {})
        self.assertEqual(saved['compute']['charged_cpu_seconds'], 30); self.assertTrue(saved['cleanup_verified'])
        self.assertFalse((self.root / 'runs' / run['id'] / 'result.json').exists())
        for path in (self.root / 'runs' / run['id']).rglob('*'):
            if path.is_file(): self.assertNotIn(self.token.encode(), path.read_bytes())


class IdentityRecoveryTests(unittest.TestCase):
    setUp = recovery.RecoveryTests.setUp
    tearDown = recovery.RecoveryTests.tearDown
    submit = recovery.RecoveryTests.submit
    capture_restore = recovery.RecoveryTests.capture_restore
    activate = recovery.RecoveryTests.activate

    def test_restore_keeps_oidc_mode_and_revocations_without_key_fallback(self):
        path = self.state / 'config.json'; value = json.loads(path.read_text())
        value.update(mode='local-synthetic-oidc', identity=config()); path.write_text(canonical(value))
        queued = self.submit(); self.capture_restore(); self.activate()
        restored = json.loads((self.dest / 'config.json').read_text())
        self.assertEqual(restored['mode'], 'local-synthetic-oidc'); self.assertEqual(restored['identity'], config())
        from research_workspace.store import Store
        store = Store(self.dest)
        self.assertEqual(store.run(self.pid, queued['id'], 'owner')['status'], 'cancelled')
        with self.assertRaises(AccessError): store.session(token=self.token)
        with self.assertRaises(AccessError): store.session(key=self.revoked_key)

class IdentityFailureTests(unittest.TestCase):
    setUp = IdentityTests.setUp
    tearDown = IdentityTests.tearDown
    def test_submission_rollback_releases_bound_context(self):
        from contextlib import contextmanager
        from research_workspace.server import Application
        app = Application.__new__(Application); app.store = self.store; app.identity = self.identity
        app.config = {'images': workspace.IMAGES}
        transaction = self.store.transaction
        @contextmanager
        def fail_commit():
            with transaction() as db:
                yield db
                raise OSError('injected commit failure')
        with patch.object(self.store, 'transaction', fail_commit):
            with self.assertRaises(OSError):
                app.dispatch('POST', '/api/projects/' + self.pid + '/runs', 'alice',
                             {'recipe': 'summary-python', 'condition': 'baseline', 'csv': workspace.CSV}, None, self.context)
        self.assertEqual(self.identity.runs, {})
        self.assertEqual(self.store.project(self.pid, 'alice')['runs'], [])

    def test_deadline_and_saturation_fail_closed_with_bounded_tasks(self):
        import threading
        from queue import Empty
        adapter = Identity(config(), self.store)
        done = threading.Event()
        with patch.object(adapter, '_fetch', side_effect=lambda *a, **k: done.wait(2)), patch('research_workspace.identity.Queue.get', side_effect=Empty) as wait:
            try:
                with self.assertRaises(IdentityUnavailable): adapter._request(None)
                wait.assert_called_once_with(timeout=6)
            finally: done.set()
        other = Identity(config(), self.store)
        for _ in range(4): other.inflight.acquire()
        with self.assertRaises(IdentityUnavailable): other._request(None)
        for _ in range(4): other.inflight.release()

