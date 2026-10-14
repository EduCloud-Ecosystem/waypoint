"""Regression for the observed missing runtime import in the native fixture."""
import ast
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime_authority
import verify_authenticated_runtime as verifier


class CpuLimitTests(unittest.TestCase):
    def test_cli_nanocpu_and_sdk_period_quota_caps(self):
        self.assertEqual(verifier.configured_cpu_limit({'NanoCpus': 1000000000, 'CpuQuota': 0, 'CpuPeriod': 0}), 1)
        self.assertEqual(verifier.configured_cpu_limit({'NanoCpus': 400000000, 'CpuQuota': 0, 'CpuPeriod': 0}), .4)
        self.assertEqual(verifier.configured_cpu_limit({'NanoCpus': 0, 'CpuQuota': 10000, 'CpuPeriod': 100000}), .1)

    def test_unbounded_ambiguous_partial_or_boolean_caps_refuse(self):
        for limits in ({}, {'CpuQuota': -1, 'CpuPeriod': 100000}, {'CpuQuota': 10000},
                       {'NanoCpus': 1000000000, 'CpuQuota': 100000, 'CpuPeriod': 100000},
                       {'NanoCpus': True}, {'CpuQuota': True, 'CpuPeriod': 100000},
                       {'NanoCpus': -1}, {'NanoCpus': 1.0}):
            with self.subTest(limits=limits), self.assertRaisesRegex(ValueError, '^fixture_cpu_limit_invalid$'):
                verifier.configured_cpu_limit(limits)


class LoopbackHub:
    """HTTP lifecycle model: readiness reads cannot create a replacement."""
    def __init__(self, *, healthy=False, api_mode='normal'):
        self.state = 'healthy' if healthy else 'stale-running'
        self.api_mode = api_mode
        self.trace = []
        self.spawn_requests = self.creates = 0
        self.lock = threading.Lock()
        model = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup(); self.connection.settimeout(1)
            def log_message(self, *args): pass
            def send(self, status, value, *, login=False, redirect=False):
                body = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Connection', 'close')
                if login:
                    self.send_header('Set-Cookie', '_xsrf=fixture-xsrf; Path=/')
                    self.send_header('Set-Cookie', 'hub-session=fixture-session; Path=/')
                if redirect: self.send_header('Location', '/user/alice/lab')
                self.end_headers(); self.wfile.write(body)

            def do_GET(self):
                with model.lock:
                    if self.path == '/hub/login':
                        self.send(200, {}, login=True); return
                    cookies = self.headers.get('Cookie', '')
                    if 'hub-session=fixture-session' not in cookies:
                        self.send(403, {}); return
                    if self.path == '/hub/api/user':
                        model.trace.append(('self-api', model.state))
                        if self.headers.get('X-XSRFToken') != 'fixture-xsrf' or model.api_mode == 'denied':
                            self.send(403, {}); return
                        if model.api_mode == 'malformed':
                            self.send(200, []); return
                        if model.api_mode == 'pending':
                            self.send(200, {'server': None, 'servers': {'': {'pending': 'stop'}}}); return
                        if model.state == 'stale-running':
                            self.send(200, {'server': '/user/alice/', 'servers': {'': {'pending': None}}})
                            model.state = 'pending-stop'
                        elif model.state == 'pending-stop':
                            self.send(200, {'server': None, 'servers': {'': {'pending': 'stop'}}})
                            model.state = 'absent'
                        else:
                            self.send(200, {'server': None, 'servers': {}})
                        return
                    if self.path == '/hub/spawn/alice':
                        model.trace.append(('spawn', model.state)); model.spawn_requests += 1
                        if model.state == 'absent':
                            model.creates += 1; model.state = 'healthy'
                        self.send(302, {}, redirect=True); return
                    if self.path.startswith('/user/alice/'):
                        model.trace.append(('readiness', model.state))
                        self.send(200 if model.state == 'healthy' else 503, {}); return
                    self.send(404, {})

        self.server = HTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
            kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def session(self):
        session = requests.Session()
        session.get(self.base + '/hub/login', timeout=1).raise_for_status()
        return session

    def poll(self):
        with self.lock:
            self.state = {'stale-running': 'pending-stop', 'pending-stop': 'absent'}.get(self.state, self.state)

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(1)
        if self.thread.is_alive(): raise AssertionError('loopback Hub did not stop')


class FixtureContract(unittest.TestCase):
    def test_one_shot_spawn_before_normal_poll_does_not_create_replacement(self):
        hub = LoopbackHub(); self.addCleanup(hub.close)
        session = hub.session(); self.addCleanup(session.close)
        response = session.get(hub.base + '/hub/spawn/alice', timeout=1, allow_redirects=False)
        self.assertEqual(response.status_code, 302)
        hub.poll(); hub.poll()  # Hub's ordinary poll clears its stale server.
        for endpoint in ('lab', 'api/contents'):
            self.assertEqual(session.get(hub.base + '/user/alice/' + endpoint, timeout=1).status_code, 503)
        self.assertEqual(hub.state, 'absent')
        self.assertEqual(hub.creates, 0)
        self.assertEqual(hub.spawn_requests, 1)

    def test_restart_waits_authenticated_self_api_absence_before_one_explicit_spawn(self):
        hub = LoopbackHub(); self.addCleanup(hub.close)
        session = hub.session(); self.addCleanup(session.close)
        verifier.request_notebook_start(session, hub.base, previous_runtime_stopped=True)
        self.assertEqual(hub.trace, [('self-api', 'stale-running'), ('self-api', 'pending-stop'),
                                    ('self-api', 'absent'), ('spawn', 'absent')])
        self.assertEqual(hub.spawn_requests, 1)
        self.assertEqual(hub.creates, 1)
        self.assertEqual(session.get(hub.base + '/user/alice/api/contents', timeout=1).status_code, 200)

    def test_healthy_existing_notebook_omits_poll_and_does_not_duplicate_create(self):
        hub = LoopbackHub(healthy=True); self.addCleanup(hub.close)
        session = hub.session(); self.addCleanup(session.close)
        verifier.request_notebook_start(session, hub.base, previous_runtime_stopped=False)
        self.assertEqual(hub.trace, [('spawn', 'healthy')])
        self.assertEqual(hub.spawn_requests, 1)
        self.assertEqual(hub.creates, 0)
        self.assertEqual(hub.state, 'healthy')

    def test_untrusted_malformed_or_pending_self_api_never_requests_spawn(self):
        real_wait = verifier.wait
        for mode in ('denied', 'malformed', 'pending', 'missing-auth', 'wrong-xsrf'):
            with self.subTest(mode=mode):
                hub = LoopbackHub(api_mode=mode)
                session = hub.session()
                try:
                    if mode == 'missing-auth': session.cookies.clear()
                    if mode == 'wrong-xsrf':
                        session.cookies.clear(); session.cookies.set('hub-session', 'fixture-session')
                        session.cookies.set('_xsrf', 'incorrect')
                    with patch.object(verifier, 'wait', side_effect=lambda check, timeout: real_wait(check, .03)):
                        with self.assertRaisesRegex(RuntimeError, '^fixture_condition_timeout$'):
                            verifier.request_notebook_start(session, hub.base, previous_runtime_stopped=True)
                    self.assertEqual(hub.spawn_requests, 0)
                    self.assertEqual(hub.creates, 0)
                finally:
                    session.close(); hub.close()

    def test_cli_failure_keeps_private_exception_details_out_of_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canary = 'fixture-private-error-canary'
            (root / 'sitecustomize.py').write_text(
                "import subprocess\ndef private_failure(*args, **kwargs):\n"
                "    raise RuntimeError('fixture-private-error-canary')\n"
                "subprocess.run = private_failure\n")
            report = root / 'report.json'
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('verify_authenticated_runtime.py')),
                '--output', str(report)], env=dict(os.environ, PYTHONPATH=str(root)),
                capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(canary, result.stdout + result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            evidence = json.loads(report.read_text())
            self.assertEqual(evidence['status'], 'FAIL')
            self.assertEqual(evidence['failure'], 'RuntimeError')
            self.assertTrue(evidence['cleanup_verified'])

    def test_every_lazy_runtime_import_in_fixture_is_available(self):
        tree = ast.parse(Path(__file__).with_name('authenticated_fixture.py').read_text())
        imported = [alias.name for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == 'runtime_authority' for alias in node.names]
        self.assertTrue(imported)
        for name in imported:
            with self.subTest(name=name): self.assertTrue(hasattr(runtime_authority, name))


if __name__ == '__main__': unittest.main()
