"""Focused AS1 review regressions; local synthetic sockets/metadata only."""
import copy
import hashlib
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from research_workspace import assessment_peer as peer, assessment_service as service, assessment_state as a
from research_workspace import guard_publication as publication, sqlite_ownership as ownership
from research_workspace.identity import Identity, Context
from research_workspace.store import AccessError
from assessment_fixtures import fresh_store, metadata_row


class BodyReaderTests(unittest.TestCase):
    def test_buffered_read_ahead_and_would_block_are_not_eof(self):
        receiving, sending = socket.socketpair()
        self.addCleanup(receiving.close)
        self.addCleanup(sending.close)
        reader = receiving.makefile("rb")
        self.addCleanup(reader.close)
        sending.sendall(b"body")
        self.assertEqual(reader.peek(4), b"body")  # socket is idle; bytes are buffered
        class InterruptedReader:
            attempts = 0
            def peek(self, size):
                return reader.peek(size)
            def read1(self, size):
                self.attempts += 1
                if self.attempts == 1:
                    raise BlockingIOError("synthetic readiness race")
                return reader.read1(size)
        interrupted = InterruptedReader()
        body = service.BoundedRequestBody(SimpleNamespace(connection=receiving, rfile=interrupted))
        body.bind(time.monotonic() + 1, lambda: None)
        self.assertEqual(body.read(4), b"body")
        self.assertEqual(interrupted.attempts, 2)

    def test_quiet_poll_then_body_is_not_poisoned_or_false_eof(self):
        receiving, sending = socket.socketpair()
        self.addCleanup(receiving.close)
        self.addCleanup(sending.close)
        reader = receiving.makefile("rb")
        self.addCleanup(reader.close)
        body = service.BoundedRequestBody(SimpleNamespace(connection=receiving, rfile=reader))
        checks = []
        body.bind(time.monotonic() + 2, lambda: checks.append(time.monotonic()))
        def send():
            time.sleep(.25)
            sending.sendall(b"ab")
            time.sleep(.25)
            sending.sendall(b"cd")
        thread = threading.Thread(target=send)
        thread.start()
        try:
            self.assertEqual(body.read(4), b"abcd")
        finally:
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(len(checks), 3)


class PublicationIdentityTests(unittest.TestCase):
    def test_real_identity_logout_is_ordered_after_bounded_response_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, _ = fresh_store(Path(temporary) / "state")
            identity = object.__new__(Identity)  # no OIDC connection/finite-profile enablement
            identity.lock, identity.revoked_sids = threading.RLock(), set()
            identity.clock = lambda: 1792411200.0
            cookie = "synthetic-cookie"
            key = hashlib.sha256(cookie.encode()).hexdigest()
            context = Context("alice", "synthetic-session", "synthetic-token", 1792414800, key)
            identity.sessions = {key: context}
            identity._key = lambda value: hashlib.sha256(value.encode()).hexdigest()
            subject = service.AssessmentService(store, SimpleNamespace(authority_id="5" * 32),
                clock=identity.clock, max_upload_bytes=1024, policy_expires=1792414800, identity=identity)
            auth = service.Authorization("alice", cookie, context.expires, context)
            requested, completed = threading.Event(), threading.Event()
            events = []
            def logout():
                requested.wait(2)
                identity.logout(cookie)
                events.append("logout")
                completed.set()
            thread = threading.Thread(target=logout)
            thread.start()
            original = service.strict_document
            def copy_response(*args, **kwargs):
                requested.set()
                # A logout concurrent with the exact bounded copy must wait;
                # no identity or database lock extends into HTTP delivery.
                self.assertFalse(completed.wait(.15), "logout passed the final response fence")
                value = original(*args, **kwargs)
                events.append("copy")
                return value
            try:
                with patch.object(service, "strict_document", copy_response):
                    result = subject._publication_response("2" * 32, auth, {"sessions": []},
                        active=False, limit=a.LIMITS["summary"])
            finally:
                requested.set()
                thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(events, ["copy", "logout"])
            self.assertEqual(hashlib.sha256(result.payload).hexdigest(), result.sha256)
            self.assertFalse(store.guard.lease.active)
            self.assertEqual(store.guard.lease.depth, 0)
            with self.assertRaises(AccessError):
                subject._publication_response("2" * 32, auth, {"sessions": []},
                    active=False, limit=a.LIMITS["summary"])


class ReplyIdentityTests(unittest.TestCase):
    def test_control_identity_is_hex32_not_pure_manifest_opaque80(self):
        fixture = Path(__file__).resolve().parents[1] / "assessment/fixtures/owned-data/status-reply.json"
        valid = json.loads(fixture.read_bytes())
        self.assertEqual(peer.reply(a.encode(valid) + b"\n", 0), valid)
        for field in ("scope_id", "authority_id", "waypoint_authority_id", "project_id",
                      "admitting_owner_epoch", "writer_epoch", "watch_epoch"):
            for value in ("a" * 31, "A" * 32, "x" * 32, "a" * 80):
                with self.subTest(field=field, value=value):
                    bad = copy.deepcopy(valid)
                    bad[field] = value
                    with self.assertRaises(peer.PeerFailure):
                        peer.reply(a.encode(bad) + b"\n", 0)

    def test_observed_absence_requires_denial_drain_and_revocation(self):
        fixture = Path(__file__).resolve().parents[1] / "assessment/fixtures/owned-data/status-reply.json"
        valid = json.loads(fixture.read_bytes())
        observation = {"schema":"outfitter.owned-data-observation/v1", "observation_id":"a"*32,
            "scope_id":valid["scope_id"], "scope_generation":1, "observed_at":"2026-10-19T12:00:00.000000Z",
            "method":"synthetic_local_scope_v1", "assurance":"synthetic", "new_admission":"denied",
            "writers_drained":"observed", "scope_access_revocation":"observed", "owned_removal":"observed_absent",
            "reason":"none", "cleanup_trusted":False, "actual_resource_accepted":False}
        valid.update(state="cleanup_unresolved",deny_latched=True,code="UNRESOLVED",observations=[observation])
        self.assertEqual(peer.reply(a.encode(valid)+b"\n",0),valid)
        for field, value in (("new_admission","unknown"),("writers_drained","failed"),
                             ("scope_access_revocation","unknown")):
            with self.subTest(field=field):
                candidate = copy.deepcopy(valid)
                candidate["observations"][0][field] = value
                with self.assertRaises(peer.PeerFailure):
                    peer.reply(a.encode(candidate)+b"\n",0)


class LifelineConstructorTests(unittest.TestCase):
    def test_denial_closes_all_lifelines_before_uncertain_metadata_stops_launches(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, _ = fresh_store(Path(temporary)/"state")
            rows = [metadata_row(denied=True), metadata_row(denied=True)]
            pipes = [peer.Lifeline(), peer.Lifeline()]
            calls = []
            def ambiguous(operation, control):
                self.assertTrue(all(item.owner_write is None for item in pipes))
                calls.append((operation,control["scope_id"]))
                failure = peer.PeerFailure()
                failure.ownership_uncertain = True
                raise failure
            subject = service.AssessmentService(store, SimpleNamespace(authority_id="5"*32,call=ambiguous),
                clock=lambda:1792411200.0, max_upload_bytes=1024, policy_expires=1792414800.0)
            for row, capability in zip(rows,pipes):
                self.addCleanup(capability.close)
                subject.scopes[row["id"]] = SimpleNamespace(pipes=capability)
            subject.denied(rows)
            self.assertTrue(subject.held)
            self.assertTrue(ownership.held(store.root))
            self.assertTrue(all(item.owner_read is None and item.owner_write is None
                                and item.watch_read is None and item.watch_write is None for item in pipes))
            subject.denied(rows)
            self.assertEqual(calls,[("cancel",rows[0]["id"])])

    def test_constructor_cleanup_uncertainty_holds_service_before_spawn(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, tokens = fresh_store(Path(temporary)/"state")
            def refuse_call(*args, **kwargs):
                raise peer.PeerFailure("ASSESSMENT_MISSING")
            transport = SimpleNamespace(authority_id="5"*32, call=refuse_call)
            subject = service.AssessmentService(store, transport, clock=lambda:1792411200.0,
                max_upload_bytes=1024, policy_expires=1792414800.0)
            auth = subject.authenticate(tokens["alice"])
            manifest = json.loads(metadata_row()["manifest_json"])
            real_pipe, real_close = peer.os.pipe, peer.os.close
            pairs, attempts = [], []
            def pipe():
                if pairs:
                    raise OSError("synthetic second-pipe construction failure")
                result = real_pipe();pairs.append(result);return result
            def close(fd):
                if pairs and fd == pairs[0][1]:
                    attempts.append(fd)
                    raise OSError("synthetic original capability close uncertainty")
                return real_close(fd)
            try:
                with patch.object(peer.os,"pipe",pipe),patch.object(peer.os,"close",close):
                    with self.assertRaises(peer.PeerFailure) as raised:
                        subject.prepare("2"*32,auth,"a"*32,manifest,1024)
                self.assertTrue(getattr(raised.exception,"ownership_uncertain",False))
                self.assertEqual(attempts,[pairs[0][1]])
                self.assertTrue(subject.held)
                self.assertTrue(ownership.held(store.root))
                self.assertFalse(store.guard.lease.active)
                self.assertEqual(subject.scopes,{})
            finally:
                # Test-owned failing descriptor is known not to have closed;
                # production never retries an ambiguously closed integer FD.
                if pairs:
                    real_close(pairs[0][1])

    def test_metadata_child_abort_uncertainty_holds_before_another_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, tokens = fresh_store(Path(temporary)/"state")
            row = metadata_row()
            from assessment_fixtures import insert_row
            insert_row(store,row)
            fixture = Path(__file__).resolve().parents[1] / "assessment/fixtures/owned-data/status-reply.json"
            value = json.loads(fixture.read_bytes())
            value.update(scope_id=row["id"],authority_id=row["outfitter_authority_id"],
                waypoint_authority_id=store.guard.binding["authority_id"],project_id=row["project_id"],
                manifest_sha256=row["manifest_sha256"],expires_at=row["expires_at"],
                declared_upload_ceiling_bytes=row["declared_upload_ceiling_bytes"],
                admitting_owner_epoch=row["admitting_owner_epoch"])
            calls = []
            class MetadataPeer(peer.OwnedDataPeer):
                def command(self,operation):
                    calls.append(operation)
                    return [sys.executable,"-c","import sys;sys.stdin.buffer.read();sys.stdout.buffer.write("+repr(a.encode(value)+b"\n")+")"]
            transport = MetadataPeer(sys.executable,temporary,"5"*32)
            subject = service.AssessmentService(store,transport,clock=lambda:1792411200.0,
                max_upload_bytes=1024,policy_expires=1792414800.0)
            auth = subject.authenticate(tokens["alice"])
            real_abort = peer.Child.abort
            def ambiguous(child):
                real_abort(child)  # actual child is drained; simulate lost close acknowledgement
                return False
            with patch.object(peer.Child,"abort",ambiguous):
                with self.assertRaises(peer.PeerFailure):
                    subject.status("2"*32,row["id"],auth)
            self.assertTrue(subject.held)
            self.assertTrue(ownership.held(store.root))
            self.assertEqual(calls,["status"])
            with self.assertRaises(ownership.OwnershipFailure):
                subject.status("2"*32,row["id"],auth)
            self.assertEqual(calls,["status"])


class CompleteEnvelopeTests(unittest.TestCase):
    def test_independent_complete_maximum_forms(self):
        # Size envelopes, not valid workload manifests/admission claims. Every
        # actual metadata key and longest declared scalar is included.
        encode = lambda x: json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        maximum = 9007199254740991
        row = {"id": "f"*32, "project_id": "e"*32, "subject": "s"*80,
               "request_key": "d"*32, "manifest_json": '"'*65536,
               "manifest_sha256": "c"*64, "outfitter_authority_id": "b"*32,
               "created_at": "9999-12-31T23:59:59.999999Z", "expires_at": "9999-12-31T23:59:59Z",
               "local_phase": "pending", "deny_reason": "storage_uncertain", "version": maximum,
               "last_scope_generation": maximum, "declared_upload_ceiling_bytes": maximum,
               "admitting_owner_epoch": "a"*32}
        fields = sum(len(str(value)) for value in row.values())
        self.assertLessEqual(fields, 67584)
        self.assertLessEqual(16*fields, 1081344)
        self.assertLessEqual(len(encode(row)), 135168)
        self.assertLessEqual(len(encode([row]*16)), 2359296)
        meta = {k:v for k,v in row.items() if k != "manifest_json"}
        self.assertLessEqual(len(encode(meta)), 1024)
        summary = dict(meta, cleanup_trusted=False, upload_authorized=False,
                       execution_authorized=False, actual_resource_accepted=False)
        self.assertLessEqual(len(encode({"sessions":[summary]*16})), 32768)
        observation = {"schema":"outfitter.owned-data-observation/v1", "observation_id":"a"*32,
            "scope_id":"f"*32, "scope_generation":maximum, "observed_at":"9999-12-31T23:59:59.999999Z",
            "method":"synthetic_local_scope_v1", "assurance":"synthetic", "new_admission":"denied",
            "writers_drained":"observed", "scope_access_revocation":"observed", "owned_removal":"observed_absent",
            "reason":"record_uncertain", "cleanup_trusted":False, "actual_resource_accepted":False}
        self.assertLessEqual(len(encode(observation)), 1024)
        status = {"schema":"outfitter.owned-data-status/v1", "scope_id":"f"*32,
            "authority_id":"b"*32, "waypoint_authority_id":"6"*32, "project_id":"e"*32,
            "manifest_sha256":"c"*64, "declared_upload_ceiling_bytes":maximum, "admitting_owner_epoch":"a"*32,
            "generation":maximum, "state":"cleanup_unresolved", "deny_latched":True,
            "expires_at":"9999-12-31T23:59:59Z", "writer_epoch":"7"*32, "watch_epoch":"8"*32,
            "code":"UNRESOLVED", "observations":[dict(observation,observation_id=f"{i:032x}") for i in range(8)],
            "test_permission":"none", "upload_authorized":False, "execution_authorized":False,
            "cleanup_trusted":False, "actual_resource_accepted":False, "liveness":"observed_current"}
        self.assertEqual(peer.reply(encode(status)+b"\n",0),status)
        self.assertLessEqual(len(encode({"session":summary,"broker":status,"cache_persisted":False,"duplicate":False})),16384)
        control = {"schema":"outfitter.owned-data-control/v1", "operation":"prepare", "scope_id":"f"*32,
            "waypoint_authority_id":"6"*32,"expected_broker_authority_id":"b"*32,"project_id":"e"*32,
            "admitting_owner_epoch":"a"*32,"declared_upload_ceiling_bytes":maximum,
            "expires_at":"9999-12-31T23:59:59Z","manifest":{"pad":"x"*(65536-10)}}
        self.assertEqual(len(encode(control["manifest"])),65536)
        self.assertLessEqual(len(encode(control))+1,131072)

    def test_failure_after_effect_has_one_uncertainty_note_and_releases_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, _ = fresh_store(Path(temporary)/"state")
            before = store.guard.load()
            stages = []
            original = publication.Publication.save
            def record(owner, *args, **kwargs):
                stages.append(kwargs.get("stage"))
                return original(owner,*args,**kwargs)
            row = metadata_row()
            with patch.object(publication.Publication,"save",record):
                with self.assertRaisesRegex(OSError,"synthetic business failure"):
                    with store.transaction(request={"action":"assessment-observation"}) as db:
                        db.execute(a.INSERT,tuple(row[k] for k in a.FIELDS))
                        raise OSError("synthetic business failure")
            after = store.guard.load()
            self.assertEqual(stages,["prepare","effect","uncertainty-note"])
            self.assertLessEqual(after["generation"]-before["generation"],4)
            self.assertFalse(store.guard.lease.active)
            with store.read() as db:
                self.assertEqual(db.execute(a.COUNT).fetchone()[0],0)
