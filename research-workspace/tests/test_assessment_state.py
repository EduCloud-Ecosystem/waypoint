"""AS1 scoped metadata and disabled HTTP controls; no provider or workload."""
import copy
import hashlib
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from research_workspace import assessment_peer as peer, assessment_state as a, finite_authority as f
from research_workspace import operation_guard as g, sqlite_ownership as o
from research_workspace import recovery, guarded_recovery, storage_inventory
from research_workspace import assessment_service as service_module
from research_workspace.assessment_service import DisabledAssessmentService
from research_workspace.guard_publication import Publication, SAFETY
from research_workspace.server import Application, Handler, Server
from research_workspace.assessment_service import AssessmentService
from research_workspace.store import Store
from assessment_fixtures import fresh_store, metadata_row, insert_row


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "state"
        self.store, self.tokens = fresh_store(self.root)

    def metadata(self):
        with self.store.read() as db:
            return [dict(zip(row.keys(), row)) for row in db.execute(a.ALL_META).fetchall()]

    def test_exact_profile_refusal_and_no_raw_factory(self):
        self.assertEqual(f.profile_digest(3), "432f95df152db6f1b16b1829f85d192021b6998fd265d7a4075ef2e29dde25af")
        self.assertEqual(f.profile_digest(4), "ed235fd6d6bb3c10632c0c5dc2bb20c8d3cb9076ebb157c71c1cc9c8072a656e")
        self.assertEqual(set(f.PROFILE_MODES), {1, 2, 3, 4})
        self.assertNotIn("assessment_sessions", f.ROW_LIMITS)
        status = self.store.operation_status()
        self.assertFalse(status["can_create_project"])
        self.assertFalse(status["can_submit_run"])
        before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        with self.assertRaises(ValueError):
            Store(self.root)
        with self.assertRaises(ValueError):
            self.store._connect()
        with self.assertRaises(ValueError):
            Store(Path(self.tmp.name) / "public-init", create=True, config={"mode": a.MODE})
        self.assertFalse((Path(self.tmp.name) / "public-init").exists())
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})

    def test_explicit_roles_and_same_snapshot_materialization(self):
        row = metadata_row(); insert_row(self.store, row)
        events = []
        with patch.object(o, "_event", side_effect=lambda root, role, stage: events.append((role, stage))):
            with self.store.read() as db:
                found = db.execute(a.BY_ID, (row["id"],)).fetchone()
                self.assertEqual(found["manifest_sha256"], row["manifest_sha256"])
                with self.assertRaises(ValueError):
                    db.execute("SELECT * FROM runs WHERE id=?", ("x",))
        self.assertIn(("assessment-read", "materialized"), events)
        self.assertIn(("assessment-read", "closed"), events)
        with self.assertRaises(ValueError):
            o.Owner(self.root, "mutation", guard=self.store.guard)
        with o.Owner(self.root, "guard-proof", guard=self.store.guard, readonly=True) as db:
            with self.assertRaises(ValueError):
                db.execute(a.ALL_META)
        with self.store.write(role="assessment-spawn") as db:
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(ValueError):
                db.execute(a.UPDATE, ("denied", "cancelled", 2, None, row["id"], 1, "pending"))

    def test_oversized_field_refuses_before_materialization(self):
        row = metadata_row()
        # Corrupt-field fixture via the real Owner, never an admitting manifest.
        row["manifest_json"] = "x" * 65537
        with self.store.transaction(request={"action": "assessment-observation"}) as db:
            db.execute(a.INSERT, tuple(row[k] for k in a.FIELDS))
        events = []
        with patch.object(o, "_event", side_effect=lambda root, role, stage: events.append((role, stage))):
            with self.store.read() as db:
                # Exclude schema/binding validation performed while opening the
                # owner; observe only the attempted business projection below.
                events.clear()
                with self.assertRaises(o.ReadBounds):
                    db.execute(a.BY_ID, (row["id"],))
        self.assertNotIn(("assessment-read", "materialized"), events)

    def test_persisted_upload_ceiling_is_bound_to_manifest_before_full_materialization(self):
        row = metadata_row()
        manifest = json.loads(row["manifest_json"])
        row["declared_upload_ceiling_bytes"] = manifest["ceilings"]["scratch_bytes"] + 1
        with self.assertRaises(ValueError):
            a.validate_row(row)
        # Model a same-shape corrupted persisted row. The metadata-only denial
        # projection remains available; the full row cannot be materialized.
        with self.store.transaction(request={"action": "assessment-observation"}) as db:
            db.execute(a.INSERT, tuple(row[key] for key in a.FIELDS))
        with self.store.read() as db:
            metadata = db.execute(a.ALL_META).fetchall()
        self.assertEqual(len(metadata), 1)
        self.assertEqual(metadata[0]["declared_upload_ceiling_bytes"], row["declared_upload_ceiling_bytes"])
        events = []
        with patch.object(o, "_event", side_effect=lambda root, role, stage: events.append((role, stage))):
            with self.store.read() as db:
                events.clear()
                with self.assertRaises(ValueError):
                    db.execute(a.BY_ID, (row["id"],))
        self.assertNotIn(("assessment-read", "materialized"), events)

    def test_per_subject_and_global_overflow_probe(self):
        for _ in range(5):
            insert_row(self.store, metadata_row())
        with self.store.read() as db:
            with self.assertRaises(o.ReadBounds):
                db.execute(a.SUBJECT_META, ("alice",))

    def test_seventeenth_global_row_refuses_before_materialization(self):
        counts = (("alice", 4), ("bob", 4), ("carol", 3), ("dave", 3), ("erin", 3))
        for subject, count in counts:
            for _ in range(count):
                insert_row(self.store, metadata_row(subject))
        events = []
        with patch.object(o, "_event", side_effect=lambda root, role, stage: events.append((role, stage))):
            with self.store.read() as db:
                events.clear()
                with self.assertRaises(o.ReadBounds):
                    db.execute(a.ALL_META)
        self.assertNotIn(("assessment-read", "materialized"), events)

    def test_wrong_type_and_nul_corruption_refuse_before_materialization(self):
        cases = (("wrong-type", lambda row: row.update(version=1.5)),
                 ("nul", lambda row: row.update(manifest_json=row["manifest_json"].replace("{", "{\x00", 1))))
        for label, corrupt in cases:
            with self.subTest(corruption=label):
                store, _ = fresh_store(self.root.parent / ("corrupt-" + label))
                row = metadata_row()
                corrupt(row)
                row["manifest_sha256"] = hashlib.sha256(row["manifest_json"].encode("ascii")).hexdigest()
                with store.transaction(request={"action": "assessment-observation"}) as db:
                    db.execute(a.INSERT, tuple(row[key] for key in a.FIELDS))
                events = []
                with patch.object(o, "_event", side_effect=lambda root, role, stage: events.append((role, stage))):
                    with store.read() as db:
                        events.clear()
                        with self.assertRaises(ValueError):
                            db.execute(a.BY_ID, (row["id"],))
                self.assertNotIn(("assessment-read", "materialized"), events)

    def test_all_sixteen_denied_atomically_and_final_version_is_absorbing(self):
        for subject in ("alice", "bob", "carol", "dave"):
            for _ in range(4):
                insert_row(self.store, metadata_row(subject, version=a.M - 1))
        stages = []
        original = Publication.save
        def save(publication, *args, **kwargs):
            stages.append(kwargs.get("stage"))
            return original(publication, *args, **kwargs)
        with patch.object(Publication, "save", save):
            self.store.close_project("2" * 32, "alice")
        rows = self.metadata()
        self.assertEqual(len(rows), 16)
        self.assertTrue(all(row["local_phase"] == "denied" and row["version"] == a.M for row in rows))
        self.assertLessEqual(len(stages), 4)
        self.assertEqual(stages, ["prepare", "effect"])
        self.assertFalse(self.store.guard.lease.active)
        self.store.revoke_user("bob")
        self.assertEqual(rows, self.metadata())

    def test_auth_controls_deny_only_matching_rows(self):
        alice, bob = metadata_row(), metadata_row("bob")
        insert_row(self.store, alice); insert_row(self.store, bob)
        self.store.membership("2" * 32, "alice", "bob", "viewer")
        values = {r["subject"]: r for r in self.metadata()}
        self.assertEqual(values["alice"]["local_phase"], "pending")
        self.assertEqual(values["bob"]["local_phase"], "denied")
        self.store.logout(self.tokens["alice"])
        self.assertTrue(all(r["local_phase"] == "denied" for r in self.metadata()))
        with self.store.read() as db:
            self.assertIsNone(db.execute(a.LOGOUT_SUBJECT, (hashlib.sha256(self.tokens["alice"].encode()).hexdigest(),)).fetchone())

    def test_auth_invalidation_and_matching_denials_rollback_as_one_unit(self):
        for _ in range(4):
            insert_row(self.store, metadata_row("alice"))
        before_rows = self.metadata()
        before_guard = self.store.guard.load()
        original = self.store._deny_assessments
        def fail_after_denials(db, affected, **kwargs):
            original(db, affected, **kwargs)
            raise OSError("injected denial transaction failure")
        with patch.object(self.store, "_deny_assessments", side_effect=fail_after_denials):
            with self.assertRaisesRegex(OSError, "injected denial transaction failure"):
                self.store.revoke_user("alice")
        self.assertEqual(self.metadata(), before_rows)
        with self.store.read() as db:
            self.assertIsNotNone(db.execute(a.LOGOUT_SUBJECT,
                (hashlib.sha256(self.tokens["alice"].encode()).hexdigest(),)).fetchone())
        self.assertFalse(self.store.guard.lease.active)
        self.assertEqual(sum(not item["essential"] for item in self.store.guard.load()["operations"].values()),
                         sum(not item["essential"] for item in before_guard["operations"].values()))
        self.assertEqual(self.store.operation_status()["state"], "paused")

    def test_all_sixteen_project_denials_rollback_on_publication_fault(self):
        for subject, count in (("alice", 4), ("bob", 4), ("carol", 4), ("dave", 4)):
            for _ in range(count):
                insert_row(self.store, metadata_row(subject))
        before_rows = self.metadata()
        original = self.store._deny_assessments
        stages = []
        original_save = Publication.save
        def record_save(publication, *args, **kwargs):
            stages.append(kwargs.get("stage"))
            return original_save(publication, *args, **kwargs)
        def fail_after_denials(db, affected, **kwargs):
            original(db, affected, **kwargs)
            raise OSError("injected all-row publication failure")
        with patch.object(self.store, "_deny_assessments", side_effect=fail_after_denials), \
                patch.object(Publication, "save", record_save):
            with self.assertRaisesRegex(OSError, "injected all-row publication failure"):
                self.store.close_project("2" * 32, "alice")
        self.assertEqual(self.metadata(), before_rows)
        with self.store.read() as db:
            project = json.loads(db.execute("SELECT payload FROM projects WHERE id=?", ("2" * 32,)).fetchone()[0])
        self.assertEqual(project["status"], "active")
        self.assertFalse(self.store.guard.lease.active)
        self.assertEqual(self.store.operation_status()["state"], "paused")
        self.assertLessEqual(len(stages), 4)
        self.assertLessEqual(stages.count("uncertainty-note"), 1)

    def test_rotate_removes_sessions_and_latches_assessment(self):
        insert_row(self.store, metadata_row())
        self.store.rotate_user("alice")
        self.assertEqual(self.metadata()[0]["local_phase"], "denied")
        with self.assertRaises(ValueError):
            # Raw factory remains unavailable even after a safety operation.
            self.store.connect()

    def test_ordinary_cap_retains_essential_slot_and_tombstones(self):
        insert_row(self.store, metadata_row())
        while len(self.store.guard.load()["operations"]) < 128:
            with self.store.transaction(request={"action": "assessment-observation"}):
                pass
        with self.assertRaises(g.GuardPaused):
            with self.store.transaction(request={"action": "assessment-observation"}):
                self.fail("intake reached effects")
        self.store.revoke_user("alice")
        self.assertEqual(self.metadata()[0]["local_phase"], "denied")
        data = self.store.guard.load()
        self.assertEqual(sum(not item["essential"] for item in data["operations"].values()), 128)
        self.assertEqual(sum(item["essential"] for item in data["operations"].values()), 1)
        self.assertFalse(self.store.guard.lease.active)

    def test_keep_true_and_outstanding_token_refuse(self):
        with self.assertRaises(ValueError):
            with self.store.transaction(keep=True, request={"action": "assessment-observation"}):
                self.fail("retained token")
        with self.store.guard.locked():
            token = self.store.guard.begin("mutation", request={"action": "assessment-observation"})
            try:
                with self.assertRaises(g.GuardPaused):
                    self.store.guard.begin("mutation", request={"action": "assessment-observation"})
            finally:
                token.release()

    def test_concurrent_short_guarded_transactions_release_every_token(self):
        start = threading.Barrier(4)
        outcomes = []
        def observe(index):
            try:
                start.wait(2)
                with self.store.transaction(request={"action": "assessment-observation", "scope_id": f"{index + 1:032x}"}):
                    outcomes.append(index)
            except BaseException as exc:
                outcomes.append(exc)
        threads = [threading.Thread(target=observe, args=(index,)) for index in range(3)]
        for thread in threads:
            thread.start()
        start.wait(2)
        for thread in threads:
            thread.join(3)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sorted(x for x in outcomes if type(x) is int), [0, 1, 2])
        self.assertFalse(any(isinstance(x, BaseException) for x in outcomes))
        self.assertFalse(self.store.guard.lease.active)

    def test_constructor_rejects_nonfinite_policy_and_clock_before_state_access(self):
        insert_row(self.store, metadata_row())
        invalid_values = (float("nan"), float("inf"), True, "1792414800")
        dummy_peer = type("Peer", (), {"authority_id": "5" * 32})()
        for value in invalid_values:
            with self.subTest(policy_expires=value), \
                    patch.object(self.store, "read", side_effect=AssertionError("read before policy validation")):
                with self.assertRaises(ValueError):
                    AssessmentService(self.store, dummy_peer, clock=lambda: 1792411200,
                                      max_upload_bytes=1024, policy_expires=value)
            self.assertIsNone(self.store._assessment_service)
        for value in invalid_values:
            with self.subTest(initial_clock=value), \
                    patch.object(self.store, "read", side_effect=AssertionError("read before clock validation")):
                with self.assertRaises(ValueError):
                    AssessmentService(self.store, dummy_peer, clock=lambda: value,
                                      max_upload_bytes=1024, policy_expires=1792414800.0)
            self.assertIsNone(self.store._assessment_service)
        self.assertEqual(self.metadata()[0]["local_phase"], "pending")

    def test_clock_rollback_holds_without_authority_or_row_effects(self):
        samples = iter((1792411200.0, 1792411199.0))
        dummy_peer = type("Peer", (), {"authority_id": "5" * 32})()
        service = AssessmentService(self.store, dummy_peer, clock=lambda: next(samples),
                                    max_upload_bytes=1024, policy_expires=1792414800.0)
        before_guard = self.store.guard.load()
        with self.assertRaises(peer.PeerFailure):
            service.now()
        self.assertTrue(service.held)
        self.assertEqual(self.store.guard.load(), before_guard)
        self.assertEqual(self.metadata(), [])

    def test_occupied_safety_slot_never_bypassed(self):
        insert_row(self.store, metadata_row())
        (self.root / SAFETY).write_bytes(b"retained failed publication")
        with self.assertRaises(g.GuardPaused):
            self.store.revoke_user("alice")
        self.assertEqual((self.root / SAFETY).read_bytes(), b"retained failed publication")

    def test_unsupported_recovery_export_and_inventory_before_effects(self):
        output = Path(self.tmp.name) / "output"
        for call in (lambda: recovery.checkpoint(self.root, output),
                     lambda: guarded_recovery.checkpoint(self.root, output),
                     lambda: self.store.snapshot("2" * 32, "alice"),
                     lambda: self.store.export_bundle("2" * 32, "alice", self.tokens["alice"])):
            with self.assertRaises(ValueError):
                call()
            self.assertFalse(output.exists())
        with patch.object(storage_inventory, "scan", side_effect=AssertionError("content scan")):
            self.assertFalse(storage_inventory.observe(self.root, retention=True)["complete"])

    def test_header_only_refusal_without_live_sql_or_content_scan(self):
        # Remove the easy config signal: the fixed database marker must still
        # refuse before a scan. This read cannot require SQLite deserialize.
        (self.root / "config.json").write_text('{"mode":"local-public-data"}')
        with patch("sqlite3.connect", side_effect=AssertionError("live SQLite open")), \
                patch.object(storage_inventory,"scan",side_effect=AssertionError("content scan")):
            self.assertFalse(storage_inventory.observe(self.root,retention=True)["complete"])
        # Unknown/malformed legacy bytes are not positive authority. The
        # discriminator leaves them to unchanged conservative inventory logic.
        (self.root / "workspace.sqlite3").write_bytes(b"broken")
        with patch("sqlite3.connect", side_effect=AssertionError("live SQLite open")):
            self.assertIsNone(a.refuse_state(self.root))
            destination = Path(self.tmp.name) / "not-yet-created"
            self.assertIsNone(a.refuse_state(destination))
            self.assertFalse(destination.exists())

    def test_header_refusal_rejects_file_replacement(self):
        (self.root / "config.json").write_text('{"mode":"local-public-data"}')
        path = self.root / "workspace.sqlite3"
        original_read = os.read
        replaced = []
        def read(fd, count):
            result = original_read(fd,count)
            if not replaced:
                replaced.append(True)
                path.rename(self.root / "retained-original.sqlite3")
                path.write_bytes(b"unrelated file")
            return result
        with patch.object(os,"read",read):
            with self.assertRaises(ValueError):
                a.refuse_state(self.root)
        self.assertEqual(path.read_bytes(),b"unrelated file")


class IndependentEnvelopeTests(unittest.TestCase):
    def test_complete_database_and_control_forms_fit_declared_limits(self):
        row = metadata_row()
        # The maximum escaped manifest field is a size envelope only, not a
        # valid manifest or an admission fixture.
        row["manifest_json"] = '"' * a.LIMITS["manifest"]
        row["manifest_sha256"] = hashlib.sha256(row["manifest_json"].encode("ascii")).hexdigest()
        encoded_row = a.encode(row)
        other = {key: value for key, value in row.items() if key != "manifest_json"}
        self.assertLessEqual(len(a.encode(other)), a.LIMITS["row_other"])
        self.assertLessEqual(len(encoded_row), a.LIMITS["row_json"])
        summary = {key: row[key] for key in a.META_FIELDS}
        summary.update(cleanup_trusted=False, upload_authorized=False,
                       execution_authorized=False, actual_resource_accepted=False)
        self.assertLessEqual(len(a.encode(summary)), a.LIMITS["metadata_row"])
        control_path = Path(__file__).resolve().parents[1] / "assessment/fixtures/owned-data/prepare-request.json"
        control = json.loads(control_path.read_bytes())
        self.assertLessEqual(len(a.encode(control)), a.LIMITS["control"])

    def test_maximum_json_forms_include_escaping_decimal_keys_and_failure(self):
        # Construct bounds independently of policy helpers. The manifest string
        # is a worst-case field envelope, not an accepted benchmark manifest.
        raw = '"' * 65536
        encoded = json.dumps(raw, ensure_ascii=True, separators=(",", ":")).encode()
        self.assertEqual(len(encoded), 131074)
        self.assertLessEqual(len(encoded) + 2048, 135168)
        self.assertLessEqual(16 * 135168 + 1024, 2359296)
        maximum = 9223372036854775807
        item = {"kind": "initialize", "phase": "uncertain", "resource_id": "r" * 80,
                "subject": "s" * 80, "request_key": "k" * 80, "request_hash": "a" * 64,
                "owner": "b" * 32, "reason": "unresolved_compute", "sequence": maximum,
                "essential": False}
        serialize = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        self.assertLessEqual(len(serialize({"f" * 32: item})), 1024)
        operations = {f"{i:032x}": dict(item, sequence=maximum - i) for i in range(129)}
        operations[f"{128:032x}"]["essential"] = True
        publication = {"id": "a" * 32, "owner": "b" * 32, "role": "ordinary", "operation_id": "c" * 32,
                       "stage": "uncertainty-note", "previous_generation": maximum - 1,
                       "previous_sha256": "d" * 64, "generation": maximum}
        document = {"format": "educloud-assessment-operation-guard-AS1", "authority_id": "e" * 32,
                    "logical_authority_sha256": "f" * 64, "generation": maximum, "sequence": maximum,
                    "operations": operations, "publication": publication}
        self.assertLessEqual(len(serialize(document)), 262144)
        self.assertLessEqual(len(serialize({**document, "operations": {}})), 2048)

    def test_counter_reserve_edges_and_overflow(self):
        at_boundary = {"sequence": 2**63 - 66, "generation": 2**63 - 261, "operations": {}}
        a.admission(at_boundary, False)
        for field in ("sequence", "generation"):
            invalid = dict(at_boundary); invalid[field] += 1
            with self.assertRaises(g.GuardPaused):
                a.admission(invalid, False)
        a.admission({"sequence": 2**63 - 2, "generation": 2**63 - 5, "operations": {}}, True)
        with self.assertRaises(g.GuardPaused):
            a.admission({"sequence": 2**63 - 1, "generation": 2**63 - 5, "operations": {}}, True)


class PeerReplyTests(unittest.TestCase):
    def status(self):
        path = Path(__file__).resolve().parents[1] / "assessment/fixtures/owned-data/status-reply.json"
        return json.loads(path.read_bytes())

    def observation(self, index):
        return {"observation_id": f"{index:032x}", "scope_id": "4" * 32,
                "scope_generation": index, "observed_at": "2026-10-19T12:00:00.000000Z",
                "method": "synthetic_local_scope_v1", "assurance": "synthetic",
                "new_admission": "denied", "writers_drained": "observed",
                "scope_access_revocation": "observed", "owned_removal": "observed_absent",
                "reason": "none", "cleanup_trusted": False,
                "actual_resource_accepted": False, "schema": "outfitter.owned-data-observation/v1"}

    def test_exact_protocol_cross_fields_and_eight_observation_edge(self):
        valid = self.status()
        valid.update(state="cleanup_unresolved", deny_latched=True, code="UNRESOLVED",
                     generation=a.M, declared_upload_ceiling_bytes=a.M, observations=[self.observation(i) for i in range(1, 9)])
        raw = a.encode(valid) + b"\n"
        self.assertLessEqual(len(raw), a.LIMITS["status"])
        self.assertEqual(peer.reply(raw, 0), valid)
        malformed = []
        for field, value in (("liveness", "unknown"), ("code", "DENIED"), ("deny_latched", True)):
            candidate = self.status(); candidate[field] = value; malformed.append(candidate)
        candidate = self.status(); candidate["test_permission"] = "synthetic_ingest_only"; malformed.append(candidate)
        candidate = self.status(); candidate["test_permission"] = "synthetic_ingest_only"; candidate["liveness"] = "observed_current"; candidate["watch_epoch"] = None; malformed.append(candidate)
        candidate = self.status(); candidate["observations"] = [self.observation(1)]; malformed.append(candidate)
        for candidate in malformed:
            with self.subTest(candidate={key: candidate[key] for key in ("state", "deny_latched", "code", "test_permission", "liveness")}):
                with self.assertRaises(peer.PeerFailure):
                    peer.reply(a.encode(candidate) + b"\n", 0)


class DisabledHTTPTests(unittest.TestCase):
    def test_actual_production_application_refuses_before_body(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            config = {"version": 1, "mode": f.SCOPED_MODE, "broker": sys.executable,
                      "images": {}, "total_budget_cpu_seconds": 600, "logical_authority": f.profile(4)}
            Store(root, create=True, guarded=True, config=config, subject="alice")
            app = Application(root)
            self.assertIs(type(app.assessment), DisabledAssessmentService)
            server = Server(("127.0.0.1", 0), Handler); server.app = app
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            thread.start()
            try:
                before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
                for suffix, headers in (("", "Content-Length: 999999999\r\n"),
                                         ("/" + "a" * 32 + "/upload", "Transfer-Encoding: chunked\r\n"),
                                         ("/" + "a" * 32 + "/seal", "Content-Length: 2\r\nContent-Length: 3\r\n")):
                    with socket.create_connection(server.server_address, timeout=2) as connection:
                        request = (f"POST /api/projects/{'2'*32}/assessments{suffix} HTTP/1.1\r\n"
                                   f"Host: 127.0.0.1:{server.server_port}\r\n{headers}\r\n")
                        connection.sendall(request.encode())  # deliberately no body
                        response = bytearray()
                        while True:
                            chunk = connection.recv(65536)
                            if not chunk:
                                break
                            response.extend(chunk)
                        self.assertIn(b"503", response.split(b"\r\n", 1)[0])
                        self.assertIn(b"ASSESSMENT_UPLOAD_DISABLED", response)
                self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()})
            finally:
                server.shutdown(); thread.join(2); server.server_close()


class OIDCLogoutTests(unittest.TestCase):
    def test_http_logout_denies_subject_assessments_after_identity_invalidation(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, _ = fresh_store(Path(temporary) / "state")
            class NoopPeer:
                authority_id = "5" * 32
                calls = []
                def call(self, operation, control):
                    self.calls.append(operation)
                    with store.read() as db:
                        row = db.execute(a.BY_ID, (control["scope_id"],)).fetchone()
                    return {"scope_id": row["id"], "authority_id": row["outfitter_authority_id"],
                            "waypoint_authority_id": store.guard.binding["authority_id"],
                            "project_id": row["project_id"], "manifest_sha256": row["manifest_sha256"],
                            "declared_upload_ceiling_bytes": row["declared_upload_ceiling_bytes"],
                            "expires_at": row["expires_at"], "admitting_owner_epoch": row["admitting_owner_epoch"]}
            synthetic_peer = NoopPeer()
            service = AssessmentService(store, synthetic_peer, clock=lambda: 1792411200.0,
                                       max_upload_bytes=1024, policy_expires=1792414800.0)
            insert_row(store, metadata_row("alice"))
            insert_row(store, metadata_row("bob"))
            identity = type("TestIdentity", (), {})()
            identity.config = {"cookie_name": "oidc_session", "proxy_cookie_name": "proxy_session",
                               "issuer": "https://identity.invalid", "client_id": "synthetic",
                               "public_origin": ""}
            identity.boundary = lambda headers: True
            identity.logout = lambda token: "alice" if token == "opaque-synthetic-cookie" else None
            app = object.__new__(Application)
            app.store, app.identity, app.assessment = store, identity, service
            app.config, app.broker = {}, None
            server = Server(("127.0.0.1", 0), Handler)
            identity.config["public_origin"] = f"http://127.0.0.1:{server.server_port}"
            server.app = app
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection(*server.server_address, timeout=5)
                connection.request("DELETE", "/api/session", body=b"", headers={
                    "Cookie": "oidc_session=opaque-synthetic-cookie",
                    "X-Research-Request": "1",
                    "Origin": identity.config["public_origin"],
                })
                response = connection.getresponse()
                payload = json.loads(response.read())
                connection.close()
                self.assertEqual(response.status, 200, payload)
                self.assertIs(payload["identity_provider_session_ended"], False)
                with store.read() as db:
                    rows = {row["subject"]: row for row in db.execute(a.ALL_META).fetchall()}
                self.assertEqual(rows["alice"]["local_phase"], "denied")
                self.assertEqual(rows["bob"]["local_phase"], "pending")
                self.assertEqual(synthetic_peer.calls, ["cancel"])
            finally:
                server.shutdown(); thread.join(2); server.server_close()


class PeerCloseTests(unittest.TestCase):
    def test_failed_child_stdin_close_is_sticky_uncertainty(self):
        child = object.__new__(peer.Child)
        child.pid = child.status = None
        child.input, child.output, child.error = 17, None, None
        child.raw, child.has_error = bytearray(), False
        child.close_uncertain = False
        with patch.object(peer.os, "close", side_effect=OSError("synthetic close failure")):
            with self.assertRaises(peer.PeerFailure):
                child.finish()
        self.assertTrue(child.close_uncertain)
        self.assertFalse(child.abort())

    def test_child_parent_endpoint_close_fault_is_sticky_and_transfers_child(self):
        real_pipe, real_close = peer.os.pipe, peer.os.close
        for target_index in range(3):
            for after_close in (False, True):
                with self.subTest(parent_endpoint=target_index, after_close=after_close):
                    pairs, injected = [], False
                    def capture_pipe():
                        pair = real_pipe()
                        pairs.append(pair)
                        return pair
                    def close(fd):
                        nonlocal injected
                        parent_ends = (pairs[0][0], pairs[1][1], pairs[2][1]) if len(pairs) == 3 else ()
                        if len(parent_ends) == 3 and fd == parent_ends[target_index] and not injected:
                            injected = True
                            if after_close:
                                real_close(fd)
                            raise OSError("injected ambiguous parent-end close")
                        return real_close(fd)
                    with patch.object(peer.os, "pipe", capture_pipe), patch.object(peer.os, "close", close):
                        with self.assertRaises(peer.PeerFailure) as caught:
                            peer.Child([sys.executable, "-c", "import time;time.sleep(30)"], os.environ)
                    failure = caught.exception
                    self.assertTrue(injected)
                    self.assertTrue(getattr(failure, "ownership_uncertain", False))
                    child = failure.child
                    self.assertIsInstance(child, peer.Child)
                    self.assertIsNotNone(child.pid)
                    self.assertFalse(child.abort())
                    self.assertTrue(child.close_uncertain)
                    if not after_close:
                        try:
                            os.fstat(pairs[0][0] if target_index == 0 else pairs[1][1] if target_index == 1 else pairs[2][1])
                        except OSError:
                            pass
                        else:
                            real_close(pairs[0][0] if target_index == 0 else pairs[1][1] if target_index == 1 else pairs[2][1])

    def test_parent_close_uncertainty_holds_service_and_local_admission(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            store, tokens = fresh_store(root)
            class SyntheticPeer:
                authority_id = "5" * 32
                def start(self, operation, lifeline):
                    return peer.Child([sys.executable, "-c", "import time;time.sleep(30)"], os.environ,
                                      lifeline.duplicate_descriptors(operation))
            service = AssessmentService(store, SyntheticPeer(), clock=lambda: 1792411200.0,
                                        max_upload_bytes=1024, policy_expires=1792414800.0)
            row = metadata_row()
            insert_row(store, row)
            live = service_module.LiveScope(peer.Lifeline(), tokens["alice"])
            service.scopes[row["id"]] = live
            auth = service.authenticate(tokens["alice"])
            real_pipe, real_close = peer.os.pipe, peer.os.close
            pairs, injected = [], False
            def capture_pipe():
                pair = real_pipe(); pairs.append(pair); return pair
            def close(fd):
                nonlocal injected
                if len(pairs) == 3 and fd == pairs[0][0] and not injected:
                    injected = True
                    raise OSError("injected original parent-end close failure")
                return real_close(fd)
            with patch.object(peer.os, "pipe", capture_pipe), patch.object(peer.os, "close", close):
                with self.assertRaises(peer.PeerFailure) as caught:
                    service._spawn("prepare", row, auth, live)
            self.assertTrue(injected)
            self.assertTrue(getattr(caught.exception, "ownership_uncertain", False))
            self.assertTrue(service.held)
            self.assertTrue(o.held(root))
            self.assertFalse(store.guard.lease.active)
            self.assertEqual(store.operation_status()["reason"], "sqlite_ownership_unconfirmed")
            try:
                os.fstat(pairs[0][0])
            except OSError:
                pass
            else:
                real_close(pairs[0][0])


class LifelineOwnershipTests(unittest.TestCase):
    def test_closed_lifeline_refuses_without_child_spawn(self):
        life = peer.Lifeline()
        life.close()
        transport = peer.OwnedDataPeer("/synthetic/broker", "/synthetic/state", "5" * 32)
        with patch.object(peer, "Child") as child:
            with self.assertRaises(peer.PeerFailure):
                transport.start("ingest", life)
        child.assert_not_called()

    def test_ambiguous_lifeline_close_is_sticky_and_not_retried(self):
        life = peer.Lifeline()
        target, attempts = life.owner_write, []
        real_close = os.close
        def fail_once(fd):
            if fd == target:
                attempts.append(fd)
                raise OSError("injected ambiguous lifeline close")
            real_close(fd)
        with patch.object(peer.os, "close", fail_once):
            with self.assertRaises(peer.PeerFailure):
                life.close()
        self.assertEqual(attempts, [target])
        self.assertTrue(life.uncertain)
        with self.assertRaises(peer.PeerFailure):
            life.current()
        try:
            os.fstat(target)
        except OSError:
            pass
        else:
            real_close(target)

    def test_duplicate_set_survives_concurrent_close(self):
        life = peer.Lifeline()
        entered, release = threading.Event(), threading.Event()
        duplicates, failures = [], []
        target = life.owner_read
        real_fcntl = peer.fcntl.fcntl
        def blocked(fd, command, argument):
            if fd == target:
                entered.set()
                if not release.wait(2):
                    raise TimeoutError("descriptor duplication did not resume")
            return real_fcntl(fd, command, argument)
        def duplicate():
            try:
                duplicates.extend(life.duplicate_descriptors("prepare"))
            except BaseException as exc:
                failures.append(exc)
        def close():
            try:
                life.close()
            except BaseException as exc:
                failures.append(exc)
        with patch.object(peer.fcntl, "fcntl", blocked):
            first = threading.Thread(target=duplicate)
            first.start()
            self.assertTrue(entered.wait(2))
            second = threading.Thread(target=close)
            second.start()
            release.set()
            first.join(3); second.join(3)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(duplicates), 3)
        for fd in duplicates:
            os.fstat(fd)
            os.close(fd)
        with self.assertRaises(peer.PeerFailure):
            life.current()

    def test_concurrent_close_cannot_close_reused_descriptor(self):
        barrier = threading.Barrier(2)
        class RacingLifeline(peer.Lifeline):
            def __init__(self):
                self.racing = False
                super().__init__()
                self.racing = True
            def __setattr__(self, name, value):
                if name == "owner_write" and value is None and getattr(self, "racing", False):
                    try:
                        barrier.wait(.2)
                    except threading.BrokenBarrierError:
                        pass
                object.__setattr__(self, name, value)
        life = RacingLifeline()
        target, calls, sentinel = life.owner_write, [], {}
        released = threading.Event()
        real_close = os.close
        failures = []
        def close(fd):
            if fd == target:
                calls.append(fd)
                if len(calls) == 1:
                    real_close(fd)
                    sentinel["fd"] = os.open(os.devnull, os.O_RDONLY)
                    released.set()
                    return
                if not released.wait(1):
                    raise TimeoutError("first ambiguous close did not finish")
            real_close(fd)
        def close_endpoint():
            try:
                life._close("owner_write")
            except BaseException as exc:
                failures.append(exc)
        with patch.object(peer.os, "close", close):
            first = threading.Thread(target=close_endpoint)
            second = threading.Thread(target=close_endpoint)
            first.start(); second.start()
            first.join(2); second.join(2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(calls, [target])
        self.assertEqual(sentinel.get("fd"), target)
        os.fstat(sentinel["fd"])
        real_close(sentinel["fd"])
        life.close()


if __name__ == "__main__":
    unittest.main()
