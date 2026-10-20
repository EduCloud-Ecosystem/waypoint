"""Actual HTTP/Store/Go lifecycle controls. Helpers exist only in Go test binaries.

Set WAYPOINT_OWNED_PEER_ASSETS to the reviewed build's directory. No engine,
provider, compute grant, production upload or trusted cleanup is exercised.
"""
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from research_workspace import assessment_service as service_module, assessment_state as a, operation_guard as g
from research_workspace import sqlite_ownership as o
from research_workspace.assessment_peer import Child, OwnedDataPeer, PeerFailure
from research_workspace.assessment_service import AssessmentService
from research_workspace.server import Application, Handler, Server
from assessment_fixtures import fresh_store

FIXTURES = Path(__file__).resolve().parents[1] / "assessment/fixtures/owned-data"
ASSETS = os.environ.get("WAYPOINT_OWNED_PEER_ASSETS")
WALL = 1792411200.0  # checked against the literal timestamp below by fixture


class TestPeer(OwnedDataPeer):
    """Only argv/environment differ; transport and lifecycle are the real ones."""
    __test__ = False

    def __init__(self, directory, state, root, clock):
        super().__init__(Path(directory) / "broker.test", state, "5" * 32)
        self.root, self.clock_file = root, clock
        self.watch_exits = []
        self.test_env = {}

    def command(self, operation):
        return [self.executable, "-test.run=^TestOwnedPeer$", "--", operation]

    def environment(self):
        result = super().environment()
        result.update(OUTFITTER_OWNED_TEST_HELPER="1", OUTFITTER_OWNED_TEST_STATE=self.state,
                      OUTFITTER_OWNED_TEST_ROOT=str(self.root), OUTFITTER_OWNED_TEST_CLOCK=str(self.clock_file),
                      GOMAXPROCS="2")
        result.update(self.test_env)
        return result

    def start(self, operation, lifeline=None):
        if operation == "prepare":
            target = self.clock_file.parent / f"watch-{len(self.watch_exits)}.exit"
            self.watch_exits.append(target)
            self.test_env["OUTFITTER_OWNED_TEST_WATCH_EXIT"] = str(target)
        return super().start(operation, lifeline)


@unittest.skipUnless(ASSETS, "reviewed real Outfitter test helpers not supplied")
class ConnectedTests(unittest.TestCase):
    def setUp(self):
        from datetime import datetime
        # The accepted Go test peer exposes a fixed synthetic owner policy.
        # Keep this override inside the disposable test process only.
        self.boot_patch = patch.object(g, "BOOT", "7" * 32)
        self.boot_patch.start()
        self.addCleanup(self.boot_patch.stop)
        self.wall = datetime.fromisoformat("2026-10-19T12:00:00+00:00").timestamp()
        self.base = Path(tempfile.mkdtemp(prefix="waypoint-as1-real-")).resolve()
        self.content = self.base / "content"; self.content.mkdir(mode=0o700)
        self.clock_file = self.base / "clock.json"
        self.clock_file.write_bytes(a.encode({"Wall": "2026-10-19T12:00:00Z", "TickMS": 1000, "Epoch": "a" * 32}))
        self.clock_file.chmod(0o600)
        self.store, self.tokens = fresh_store(self.base / "waypoint")
        state = self.base / "broker"
        env = {k: v for k, v in os.environ.items() if not k.startswith("OUTFITTER_OWNED_TEST_")}
        env.update(OUTFITTER_OWNED_TEST_SEED=str(state), GOMAXPROCS="2")
        seed = subprocess.run([str(Path(ASSETS) / "ledger.test"), "-test.run=^TestOwnedFixtureSeedHelper$"],
                              env=env, capture_output=True, timeout=10)
        self.assertEqual((seed.returncode, seed.stdout, seed.stderr), (0, b"", b""))
        self.peer = TestPeer(ASSETS, state, self.content, self.clock_file)
        self.service = AssessmentService(self.store, self.peer, clock=lambda: self.wall,
                                         max_upload_bytes=1024, policy_expires=self.wall + 3600)
        app = object.__new__(Application)  # test-only positive construction
        app.store, app.identity, app.assessment = self.store, None, self.service
        app.config, app.broker = {}, self.peer
        self.server = Server(("127.0.0.1", 0), Handler); self.server.app = app
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.original = self.accounting()
        self.manifest = json.loads((FIXTURES / "prepare-request.json").read_bytes())["manifest"]

    def tearDown(self):
        self.server.shutdown(); self.thread.join(2); self.server.server_close()
        self.service.close_lifelines()
        deadline = time.monotonic() + 6
        while any(not path.exists() for path in self.peer.watch_exits) and time.monotonic() < deadline:
            time.sleep(.02)
        if any(not path.exists() for path in self.peer.watch_exits):
            self.fail(f"Synthetic watcher exit unconfirmed; fixture retained at {self.base}")
        self.assertEqual(self.original, self.accounting())
        # Only this disposable synthetic fixture is removed after child exits.
        shutil.rmtree(self.base)

    def accounting(self):
        value = json.loads((Path(self.peer.state) / "ledger.json").read_bytes())
        value.pop("owned_data_scopes")
        return a.encode(value)

    def request(self, method, path, body=None, *, subject="alice", extra=None, kind="application/json"):
        headers = {"X-Research-Request": "1", "Content-Type": kind,
                   "Cookie": "research_session=" + self.tokens[subject]}
        headers.update(extra or {})
        data = a.encode(body) if isinstance(body, dict) else body
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        try:
            connection.request(method, path, body=data, headers=headers)
            result = connection.getresponse()
            return result.status, json.loads(result.read())
        finally:
            connection.close()

    def path(self, scope=None, action=None, project="2" * 32):
        return f"/api/projects/{project}/assessments" + ("/" + scope if scope else "") + ("/" + action if action else "")

    def prepare(self, subject="alice"):
        status, result = self.request("POST", self.path(), {"manifest": self.manifest, "declared_upload_ceiling_bytes": 1024},
                                      subject=subject, extra={"Idempotency-Key": "a" * 32})
        self.assertEqual(status, 201, result)
        self.assertEqual(result["broker"]["liveness"], "observed_current")
        self.assertFalse(self.store.guard.lease.active)
        return result["session"]["id"]

    def test_actual_http_prepare_upload_seal_cancel_and_no_metadata_body(self):
        writes = []
        original_write = Child.write
        def write(child, payload, check=None):
            self.assertFalse(self.store.guard.lease.active)
            self.assertEqual(self.store.guard.lease.depth, 0)
            writes.append(len(payload))
            return original_write(child, payload, check)
        payload = b"SYNTHETIC_BODY_SENTINEL,no-source-file-name\n1,2\n"
        with patch.object(Child, "write", write):
            scope = self.prepare()
            code, result = self.request("POST", self.path(scope, "upload"), payload, kind="application/octet-stream")
            self.assertEqual(code, 200, result)
            code, result = self.request("POST", self.path(scope, "seal"), b"")
            self.assertEqual(code, 200, result)
            self.assertEqual(result["broker"]["state"], "sealed")
        self.assertTrue(writes)
        for path in (self.base / "waypoint").iterdir():
            if path.is_file():
                data = path.read_bytes()
                self.assertNotIn(payload, data)
                self.assertNotIn(hashlib.sha256(payload).hexdigest().encode(), data)
        self.assertNotIn(payload, (Path(self.peer.state) / "ledger.json").read_bytes())
        code, result = self.request("POST", self.path(scope, "cancel"), b"")
        self.assertEqual(code, 200, result)
        self.assertEqual(result["session"]["local_phase"], "denied")
        self.assertEqual(result["broker"]["state"], "cleanup_unresolved")
        for flag in ("cleanup_trusted", "execution_authorized", "actual_resource_accepted", "upload_authorized"):
            self.assertIs(result["broker"][flag], False)
        self.assertEqual(self.original, self.accounting())

    def test_duplicate_prepare_is_lookup_only_and_original_manifest_unchanged(self):
        scope = self.prepare()
        before = self.store.guard.load()
        code, result = self.request("POST", self.path(), {"manifest": self.manifest, "declared_upload_ceiling_bytes": 1024},
                                    extra={"Idempotency-Key": "a" * 32})
        self.assertEqual((code, result["duplicate"], result["session"]["id"]), (200, True, scope))
        self.assertEqual(before, self.store.guard.load())
        self.assertEqual(len(self.peer.watch_exits), 1)
        with self.store.read() as db:
            row = db.execute(a.BY_ID, (scope,)).fetchone()
        self.assertEqual(json.loads(row["manifest_json"]), self.manifest)
        self.assertNotEqual(scope, self.manifest["assessment_id"])

    def test_bad_http_framing_and_cross_project_refuse_without_body(self):
        scope = self.prepare()
        for project, lengths, expected in (("2" * 32, "Content-Length: 4\r\nContent-Length: 4\r\n", 400),
                                            ("2" * 32, "Transfer-Encoding: chunked\r\n", 400),
                                            ("3" * 32, "Content-Length: 4\r\n", 403)):
            with socket.create_connection(self.server.server_address, timeout=2) as connection:
                request = (f"POST {self.path(scope, 'upload', project)} HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
                           f"Cookie: research_session={self.tokens['alice']}\r\nX-Research-Request: 1\r\n"
                           f"Content-Type: application/octet-stream\r\n{lengths}\r\n")
                connection.sendall(request.encode())
                response = connection.recv(65536)
                self.assertIn(str(expected).encode(), response.split(b"\r\n", 1)[0])

    def test_late_logout_fence_prevents_real_ingest_spawn(self):
        scope = self.prepare()
        original = self.service._spawn
        calls = []
        def spawn(operation, row, auth, live):
            if operation == "ingest":
                self.store.logout(self.tokens["alice"])
                calls.append("logout-before-final-fence")
            return original(operation, row, auth, live)
        with patch.object(self.service, "_spawn", spawn):
            code, result = self.request("POST", self.path(scope, "upload"), b"never-forwarded", kind="application/octet-stream")
        self.assertNotEqual(code, 200)
        self.assertEqual(calls, ["logout-before-final-fence"])
        with self.store.read() as db:
            self.assertEqual(db.execute(a.BY_ID, (scope,)).fetchone()["local_phase"], "denied")
        ledger = json.loads((Path(self.peer.state) / "ledger.json").read_bytes())
        self.assertFalse(ledger["owned_data_scopes"][scope]["writer_started"])

    def test_stale_status_response_not_published_after_membership_revocation(self):
        scope = self.prepare(subject="bob")
        entered, release = threading.Event(), threading.Event()
        original_call = self.peer.call
        outcomes = []

        def blocked_call(operation, control, lifeline=None):
            if operation == "status":
                entered.set()
                if not release.wait(4):
                    raise TimeoutError("blocked status peer was not released")
            return original_call(operation, control, lifeline)

        def request_status():
            try:
                outcomes.append(self.request("GET", self.path(scope), subject="bob"))
            except BaseException as failure:
                outcomes.append(failure)

        with patch.object(self.peer, "call", blocked_call):
            thread = threading.Thread(target=request_status)
            thread.start()
            try:
                self.assertTrue(entered.wait(4), "status request did not reach peer")
                self.store.membership("2" * 32, "alice", "bob", None)
            finally:
                release.set()
                thread.join(6)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcomes), 1)
        status, body = outcomes[0]
        self.assertEqual(status, 403, body)
        self.assertEqual(body.get("code"), "ASSESSMENT_DENIED")
        self.assertNotIn(scope, json.dumps(body))

    def test_scope_denial_before_final_fence_blocks_status_success(self):
        scope = self.prepare(subject="alice")
        entered, release = threading.Event(), threading.Event()
        original_call = self.peer.call
        outcomes = []
        def blocked_call(operation, control, lifeline=None):
            if operation == "status":
                entered.set()
                if not release.wait(4):
                    raise TimeoutError("blocked status peer was not released")
            return original_call(operation, control, lifeline)
        def request_status():
            try:
                outcomes.append(self.request("GET", self.path(scope)))
            except BaseException as failure:
                outcomes.append(failure)
        with patch.object(self.peer, "call", blocked_call):
            thread = threading.Thread(target=request_status)
            thread.start()
            try:
                self.assertTrue(entered.wait(4), "status request did not reach peer")
                self.store.close_project("2" * 32, "alice")
            finally:
                release.set()
                thread.join(6)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcomes), 1)
        status, body = outcomes[0]
        self.assertNotEqual(status, 200, body)
        self.assertEqual(body.get("code"), "ASSESSMENT_DENIED")
        self.assertNotIn(scope, json.dumps(body))

    def test_final_response_owner_failure_never_publishes_success(self):
        scope = self.prepare()
        original = self.service._publication_response
        def fail_final_owner(*args, **kwargs):
            original_enter = o.Owner.__enter__
            def fail_assessment_read(owner):
                if owner.role == "assessment-read":
                    o.hold(owner.root)
                    raise o.OwnershipFailure()
                return original_enter(owner)
            with patch.object(o.Owner, "__enter__", fail_assessment_read):
                return original(*args, **kwargs)
        with patch.object(self.service, "_publication_response", fail_final_owner):
            code, result = self.request("GET", self.path(scope))
        self.assertEqual(code, 503, result)
        self.assertEqual(result.get("code"), "ASSESSMENT_UNRESOLVED")
        self.assertNotIn(scope, json.dumps(result))
        self.assertTrue(o.held(self.store.root))
        self.assertEqual(self.store.operation_status()["reason"], "sqlite_ownership_unconfirmed")

    def test_final_response_copy_digest_precedes_unlocked_send(self):
        hashes, sent = [], []
        original_publish, original_respond = self.service._publication_response, Handler.respond
        def record_publish(*args, **kwargs):
            value = original_publish(*args, **kwargs)
            hashes.append((value.payload, value.sha256))
            return value
        def record_respond(handler, status, value, **kwargs):
            if handler.path.startswith("/api/projects/") and status in {200, 201}:
                sent.append((status, value, self.store.guard.lease.depth,
                             bool(self.store.guard.lease.active)))
            return original_respond(handler, status, value, **kwargs)
        with patch.object(self.service, "_publication_response", record_publish), \
                patch.object(Handler, "respond", record_respond):
            self.prepare()
        self.assertEqual(len(hashes), 1)
        self.assertEqual(len(sent), 1)
        payload, digest = hashes[0]
        status, value, depth, active = sent[0]
        self.assertEqual((depth, active), (0, False))
        self.assertEqual(status, 201)
        self.assertEqual(value, payload)
        self.assertEqual(hashlib.sha256(value).hexdigest(), digest)
        self.assertEqual(json.loads(value)["session"]["local_phase"], "bound")

    def test_final_publication_orders_all_current_authority_invalidations(self):
        actions = (("revoke", "bob", lambda: self.store.revoke_user("bob")),
                   ("logout", "carol", lambda: self.store.logout(self.tokens["carol"])),
                   ("membership", "dave", lambda: self.store.membership("2" * 32, "alice", "dave", "viewer")),
                   ("project-close", "alice", lambda: self.store.close_project("2" * 32, "alice")))
        for label, subject, invalidate in actions:
            with self.subTest(action=label):
                scope = self.prepare(subject=subject)
                entered, release = threading.Event(), threading.Event()
                outcomes = []
                original = self.service._publication_response
                def before_final(*args, **kwargs):
                    entered.set()
                    if not release.wait(4):
                        raise TimeoutError("final publication control not released")
                    return original(*args, **kwargs)
                def request():
                    try:
                        outcomes.append(self.request("GET", self.path(scope), subject=subject))
                    except BaseException as error:
                        outcomes.append(error)
                with patch.object(self.service, "_publication_response", before_final):
                    thread = threading.Thread(target=request)
                    thread.start()
                    try:
                        self.assertTrue(entered.wait(4))
                        invalidate()  # committed before the response fence
                    finally:
                        release.set()
                        thread.join(6)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(outcomes), 1)
                self.assertIsInstance(outcomes[0], tuple)
                status, body = outcomes[0]
                self.assertNotIn(status, (200, 201), body)
                self.assertEqual(body.get("code"), "ASSESSMENT_DENIED")
                self.assertNotIn(scope, json.dumps(body))
                self.assertFalse(self.store.guard.lease.active)

    def test_real_http_delayed_first_body_and_quiet_intervals_succeed(self):
        body = a.encode({"manifest": self.manifest, "declared_upload_ceiling_bytes": 1024})
        with socket.create_connection(self.server.server_address, timeout=4) as connection:
            request = (f"POST {self.path()} HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
                       f"Cookie: research_session={self.tokens['alice']}\r\nX-Research-Request: 1\r\n"
                       "Content-Type: application/json\r\nIdempotency-Key: " + "a" * 32 + "\r\n"
                       f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n")
            with patch.object(service_module, "HTTP_BODY_SECONDS", 2):
                connection.sendall(request.encode())
                time.sleep(.25)  # greater than the live-check polling interval
                connection.sendall(body[:len(body)//2])
                time.sleep(.25)
                connection.sendall(body[len(body)//2:])
                response = http.client.HTTPResponse(connection)
                response.begin()
                result = json.loads(response.read())
                response.close()
            self.assertEqual(response.status, 201, result)
            self.assertEqual(result["session"]["local_phase"], "bound")
            self.assertFalse(self.store.guard.lease.active)

    def test_real_http_short_body_eof_refuses_before_prepare_effect(self):
        before = self.store.guard.load()
        with socket.create_connection(self.server.server_address, timeout=3) as connection:
            request = (f"POST {self.path()} HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
                       f"Cookie: research_session={self.tokens['alice']}\r\nX-Research-Request: 1\r\n"
                       "Content-Type: application/json\r\nIdempotency-Key: " + "a" * 32 + "\r\n"
                       "Content-Length: 4\r\nConnection: close\r\n\r\n{")
            connection.sendall(request.encode())
            connection.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(connection)
            response.begin()
            result = json.loads(response.read())
            response.close()
        self.assertEqual(response.status, 400, result)
        self.assertEqual(result["code"], "ASSESSMENT_INVALID")
        self.assertEqual(before, self.store.guard.load())
        self.assertEqual(self.peer.watch_exits, [])

    def test_slow_trickle_body_hits_absolute_prepare_deadline(self):
        body = a.encode({"manifest": self.manifest, "declared_upload_ceiling_bytes": 1024})
        before = self.store.guard.load()
        sent = []
        started = time.monotonic()
        connection = socket.create_connection(self.server.server_address, timeout=2)
        connection.settimeout(2)
        request = (f"POST {self.path()} HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
                   f"Cookie: research_session={self.tokens['alice']}\r\nX-Research-Request: 1\r\n"
                   "Content-Type: application/json\r\nIdempotency-Key: " + "a" * 32 + "\r\n"
                   f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n")
        def trickle():
            for byte in body:
                if time.monotonic() - started > 1.5:
                    return
                try:
                    connection.sendall(bytes((byte,)))
                    sent.append(byte)
                    time.sleep(.03)
                except OSError:
                    return
        response = bytearray()
        reset = False
        with patch.object(service_module, "HTTP_BODY_SECONDS", .45):
            connection.sendall(request.encode())
            writer = threading.Thread(target=trickle)
            writer.start()
            try:
                while True:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    response.extend(chunk)
            except socket.timeout:
                self.fail("absolute body deadline did not return a response")
            except ConnectionResetError:
                # Closing an HTTP request with unread, still-arriving body
                # bytes can reset the socket. This is bounded transport refusal,
                # not a confirmed JSON error response or successful operation.
                reset = True
            writer.join(2)
        connection.close()
        self.assertFalse(writer.is_alive())
        self.assertGreaterEqual(len(sent), 2)
        self.assertLess(time.monotonic() - started, 3)
        if not reset:
            self.assertIn(b"503", response.split(b"\r\n", 1)[0])
            self.assertIn(b"ASSESSMENT_UNRESOLVED", response)
        elif response:
            self.assertNotIn(b"200", response.split(b"\r\n", 1)[0])
            self.assertNotIn(b"201", response.split(b"\r\n", 1)[0])
        with self.store.read() as db:
            self.assertEqual(db.execute(a.COUNT).fetchone()[0], 0)
        self.assertEqual(before, self.store.guard.load())

    def test_stalled_upload_is_cancelled_by_current_membership_check(self):
        scope = self.prepare(subject="bob")
        auth = self.service.authenticate(self.tokens["bob"])
        payload = b"SYNTHETIC_BODY_SENTINEL"
        entered = threading.Event()
        forwarded = []
        original_read, original_write = service_module.BoundedRequestBody.read, Child.write
        def read(body, length):
            entered.set()
            return original_read(body, length)
        def write(child, value, check=None):
            if payload in value:
                forwarded.append(value)
            return original_write(child, value, check)
        connection = socket.create_connection(self.server.server_address, timeout=3)
        connection.settimeout(3)
        request = (f"POST {self.path(scope, 'upload')} HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
                   f"Cookie: research_session={self.tokens['bob']}\r\nX-Research-Request: 1\r\n"
                   "Content-Type: application/octet-stream\r\n"
                   f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n").encode()
        response = bytearray()
        with patch.object(service_module, "HTTP_BODY_SECONDS", 1.5), \
                patch.object(service_module.BoundedRequestBody, "read", read), \
                patch.object(Child, "write", write):
            connection.sendall(request + payload[:1])
            self.assertTrue(entered.wait(3), "upload body reader was not entered")
            self.store.membership("2" * 32, "alice", "bob", None)
            try:
                while True:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    response.extend(chunk)
            except socket.timeout:
                self.fail("membership revocation did not interrupt the stalled body")
        connection.close()
        self.assertIn(b"403", response.split(b"\r\n", 1)[0])
        self.assertIn(b"ASSESSMENT_DENIED", response)
        self.assertEqual(forwarded, [])
        live = self.service._live(scope, auth)
        self.assertTrue(live.lock.acquire(blocking=False))
        live.lock.release()
        self.assertFalse(self.store.guard.lease.active)

    def test_concurrent_writer_refused_before_second_source_read(self):
        scope = self.prepare()
        auth = self.service.authenticate(self.tokens["alice"])
        entered, release = threading.Event(), threading.Event()
        class Source:
            def read(self, length):
                entered.set()
                if not release.wait(4):
                    raise TimeoutError()
                return b"safe"[:length]
        outcomes = []
        def first():
            try:
                outcomes.append(self.service.upload("2" * 32, scope, auth, Source(), 4))
            except BaseException as failure:
                outcomes.append(failure)
        thread = threading.Thread(target=first); thread.start()
        self.assertTrue(entered.wait(4))
        class Unread:
            def read(self, _):
                raise AssertionError("second body was read")
        try:
            with self.assertRaises(PeerFailure) as caught:
                self.service.upload("2" * 32, scope, auth, Unread(), 4)
            self.assertEqual(caught.exception.code, "ASSESSMENT_WRITER_ACTIVE")
        finally:
            release.set(); thread.join(6)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], dict, outcomes)
        self.assertFalse(self.store.guard.lease.active)

    def test_short_source_and_owner_loss_never_reopen(self):
        scope = self.prepare()
        auth = self.service.authenticate(self.tokens["alice"])
        with self.assertRaises((PeerFailure, OSError)):
            self.service.upload("2" * 32, scope, auth, io.BytesIO(b"x"), 9)
        with self.store.read() as db:
            self.assertEqual(db.execute(a.BY_ID, (scope,)).fetchone()["local_phase"], "denied")
        with self.assertRaises(PeerFailure):
            self.service.upload("2" * 32, scope, auth, io.BytesIO(b"second"), 6)


if __name__ == "__main__":
    unittest.main()
