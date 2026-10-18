"""Fresh disposable logical-limit controls; no disk quota or real provider.

Large boundary states are seeded as synthetic historical rows in the existing
tables. The final admission/safety transition always uses production methods.
"""
from contextlib import closing
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from research_workspace import finite_authority as f, operation_guard as g
from research_workspace import recovery as r, guarded_recovery as gr, runtime
from research_workspace.bounded_process import STDOUT_LIMIT
from research_workspace.store import Store, AccessError, canonical
from test_operation_guard import GuardFixture, FakeBroker
from test_guarded_recovery import Broker as RecoveryBroker
from test_workspace import IMAGES


class FiniteAuthorityTests(GuardFixture):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "state"
        self.config = {"version": 1, "mode": f.MODE, "broker": sys.executable,
                       "images": IMAGES, "total_budget_cpu_seconds": 600,
                       "logical_authority": f.profile()}
        self.store = Store(self.root, create=True, guarded=True, config=self.config, subject="alice")
        self.key = self.store.initial_key
        self.broker = FakeBroker()
        self.data = {"name": "Synthetic logical limits", "question": "Are logical caps enforced before effects?",
                     "success_criterion": "History, safety actions and uncertainty are preserved.",
                     "ends_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7)).isoformat(),
                     "budget_cpu_seconds": 30}
        self.project = self.store.create_project("alice", self.data, self.broker, 600, "project-request")
        self.pid = self.project["id"]

    def count(self, table):
        with closing(self.store.connect()) as db:
            return db.execute("SELECT count(*) FROM " + table).fetchone()[0]

    def sequence(self):
        return self.store.guard.load()["sequence"]

    def seed_users(self, target, *, active=0):
        with closing(self.store._connect()) as db:
            for i in range(self.count("users"), target):
                subject = "seeduser" + str(i)
                db.execute("INSERT INTO users VALUES(?,?,?)", (subject, hashlib.sha256(subject.encode()).hexdigest(), active))
            db.commit()

    def seed_projects(self, target):
        with closing(self.store._connect()) as db:
            for i in range(self.count("projects"), target):
                pid = f"{i:032x}"
                project = {**self.project, "id": pid, "status": "closed"}
                db.execute("INSERT INTO projects VALUES(?,?)", (pid, canonical(project)))
                db.execute("INSERT INTO members VALUES(?,?,?)", (pid, "alice", "owner"))
            db.commit()

    def seed_members(self, target, *, exclude=None):
        self.seed_users(128, active=1)
        self.seed_projects(9)
        with closing(self.store._connect()) as db:
            users = [row[0] for row in db.execute("SELECT subject FROM users")]
            projects = [row[0] for row in db.execute("SELECT id FROM projects")]
            count = self.count("members")
            for pid in projects:
                for subject in users:
                    if count == target: break
                    if (pid, subject) == exclude: continue
                    if db.execute("SELECT 1 FROM members WHERE project_id=? AND subject=?", (pid, subject)).fetchone(): continue
                    db.execute("INSERT INTO members VALUES(?,?,?)", (pid, subject, "viewer"))
                    count += 1
            db.commit()
        self.assertEqual(self.count("members"), target)

    def compete(self, callback):
        barrier = threading.Barrier(2)
        results = []
        def run(index):
            barrier.wait(timeout=3)
            try:
                callback(index)
                results.append("committed")
            except f.LogicalLimit:
                results.append("refused")
        threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=5); self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(results), ["committed", "refused"])

    def test_binding_and_profile_are_explicit_and_not_a_quota(self):
        config = json.loads((self.root / "config.json").read_bytes())
        self.assertEqual(config["logical_authority"], f.profile())
        self.assertEqual(config["operation_guard"]["format"], f.GUARD_FORMAT)
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], f.USER_VERSION)
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertTrue(Store(self.root).finite)
        self.assertFalse(self.store.operation_status()["quota_enforced"])

    def test_missing_or_changed_binding_refuses_open_and_stale_writer(self):
        path = self.root / "config.json"
        original = path.read_bytes()
        for change in (lambda c: c.pop("logical_authority"),
                       lambda c: c.pop("operation_guard"),
                       lambda c: c["logical_authority"]["row_limits"].update(users=129),
                       lambda c: c.update(mode="local-public-data")):
            with self.subTest(change=change):
                value = json.loads(original); change(value); path.write_text(canonical(value))
                with self.assertRaises((f.InvalidProfile, g.GuardPaused)): Store(self.root)
                with self.assertRaises((f.InvalidProfile, g.GuardPaused)): self.store.revoke_user("alice")
                path.write_bytes(original)
        with closing(self.store._connect()) as db:
            db.execute("PRAGMA user_version=0"); db.commit()
        with self.assertRaises(f.InvalidProfile): Store(self.root)
        with self.assertRaises(f.InvalidProfile): self.store.add_user("bob")

    def test_guard_profile_digest_cannot_be_stripped(self):
        path = self.root / g.FILE
        value = json.loads(path.read_bytes()); value.pop("logical_authority_sha256")
        path.write_text(canonical(value))
        with self.assertRaises(g.GuardPaused): Store(self.root)
        with self.assertRaises(g.GuardPaused): self.store.logout("anything")

    def test_user_history_cap_and_competing_final_slot(self):
        self.seed_users(127)  # Revoked rows still count.
        self.compete(lambda index: self.store.add_user("competitor" + str(index)))
        self.assertEqual(self.count("users"), 128)
        sequence = self.sequence()
        with self.assertRaises(f.LogicalLimit): self.store.add_user("overflow")
        self.assertEqual(self.sequence(), sequence)
        self.store.revoke_user("competitor0")
        with self.assertRaises(f.LogicalLimit): self.store.add_user("still-overflow")
        self.store.rotate_user("alice")
        self.assertEqual(self.count("users"), 128)

    def test_signin_cap_invalid_keys_and_last_slot_compete(self):
        tokens = [self.store.session(key=self.key)[1] for _ in range(7)]
        self.compete(lambda _: self.store.session(key=self.key))
        self.assertEqual(self.count("sessions"), 8)
        sequence = self.sequence()
        with self.assertRaises(f.LogicalLimit): self.store.session(key=self.key)
        for _ in range(10):
            with self.assertRaises(AccessError): self.store.session(key="invalid")
        self.assertEqual(self.sequence(), sequence)
        self.assertEqual(self.store.session(token=tokens[0]), "alice")
        self.store.logout(tokens[0])
        self.store.session(key=self.key)
        self.assertEqual(self.count("sessions"), 8)
        rotated = self.store.rotate_user("alice")
        self.assertEqual(self.count("sessions"), 0)
        self.store.session(key=rotated)
        self.store.revoke_user("alice")
        self.assertEqual(self.count("sessions"), 0)

    def test_total_live_session_cap_and_expiry(self):
        bob = self.store.add_user("bob")
        self.seed_users(33, active=1)
        with closing(self.store._connect()) as db:
            subjects = [row[0] for row in db.execute("SELECT subject FROM users WHERE subject!='bob'")]
            for i in range(256):
                db.execute("INSERT INTO sessions VALUES(?,?,?)", (hashlib.sha256(f"token-{i}".encode()).hexdigest(), subjects[i // 8], time.time()+100))
            db.commit()
        with self.assertRaises(f.LogicalLimit): self.store.session(key=bob)
        self.assertEqual(self.store.session(token="token-0"), subjects[0])
        with closing(self.store._connect()) as db:
            db.execute("UPDATE sessions SET expires=0 WHERE token_hash=?", (hashlib.sha256(b"token-0").hexdigest(),)); db.commit()
        self.store.session(key=bob)
        self.assertEqual(self.count("sessions"), 256)

    def test_closed_project_history_and_idempotency_at_cap(self):
        self.seed_projects(20)
        self.assertEqual(self.store.create_project("alice", self.data, self.broker, 600, "project-request")["id"], self.pid)
        before = dict(self.broker.projects)
        sequence = self.sequence()
        with self.assertRaises(f.LogicalLimit): self.store.create_project("alice", self.data, self.broker, 600, "new-project")
        self.assertEqual(self.broker.projects, before)
        self.assertEqual(self.sequence(), sequence)
        self.store.close_project(self.pid, "alice")
        with self.assertRaises(f.LogicalLimit): self.store.create_project("alice", self.data, self.broker, 600, "after-close")

    def test_viewer_insertion_is_admission_demotion_and_removal_are_safety(self):
        self.store.add_user("bob"); self.store.add_user("charlie")
        self.store.membership(self.pid, "alice", "bob", "editor")
        self.seed_members(1024, exclude=(self.pid, "charlie"))
        sequence = self.sequence()
        with self.assertRaises(f.LogicalLimit): self.store.membership(self.pid, "alice", "charlie", "viewer")
        self.assertEqual(self.sequence(), sequence)
        self.store.membership(self.pid, "alice", "bob", "viewer")
        self.store.membership(self.pid, "alice", "bob")
        self.store.membership(self.pid, "alice", "charlie", "viewer")
        self.assertEqual(self.count("members"), 1024)
        item = max(self.store.guard.load()["operations"].values(), key=lambda i: i["sequence"])
        self.assertFalse(item["essential"])

    def test_project_initialization_keeps_owner_membership_slot(self):
        self.store.add_user("charlie")
        self.seed_members(1023, exclude=(self.pid, "charlie"))
        self.broker.launched.clear(); self.broker.resume = threading.Event()
        results = []
        def create():
            try: results.append(self.store.create_project("alice", self.data, self.broker, 600, "pending-project"))
            except BaseException as exc: results.append(exc)
        thread = threading.Thread(target=create); thread.start()
        try:
            self.assertTrue(self.broker.launched.wait(2))
            with self.assertRaises(g.GuardBusy): self.store.membership(self.pid, "alice", "charlie", "viewer")
        finally:
            self.broker.resume.set(); thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(results[0], dict)
        self.assertEqual(self.count("members"), 1024)

    def seed_runs_to_cap(self, run, target=1000):
        self.seed_projects(11)
        with closing(self.store._connect()) as db:
            pids = [self.pid] + [row[0] for row in db.execute("SELECT id FROM projects WHERE id!=?", (self.pid,))]
            for i in range(target-1):
                rid = f"{i:032x}"
                pid = pids[(i+1) % 10]
                value = {**run, "id": rid, "project_id": pid, "status": "failed" if i % 2 else "interrupted"}
                db.execute("INSERT INTO runs(id,project_id,subject,status,payload) VALUES(?,?,?,?,?)", (rid, pid, "alice", value["status"], canonical(value)))
            db.execute("UPDATE projects SET payload=? WHERE id=?",
                       (canonical({**self.project, "id": pids[-1], "status": "active"}), pids[-1]))
            db.commit()
        return pids[-1]

    def test_run_history_cap_idempotency_and_settlement(self):
        run = self.submit(); claimed = self.store.claim()
        empty_pid = self.seed_runs_to_cap(run)
        before = set((self.root / "runs").iterdir())
        self.assertEqual(self.submit()["id"], run["id"])
        original_pid = self.pid
        self.pid = empty_pid
        with self.assertRaises(f.LogicalLimit): self.submit("overflow")
        self.pid = original_pid
        self.assertEqual(set((self.root / "runs").iterdir()), before)
        self.store.cancel(self.pid, run["id"], "alice")
        claimed.update(status="failed", cleanup_verified=True,
                       compute={"reserved_cpu_seconds": 30, "charged_cpu_seconds": 30,
                                "provenance": "estimated_upper_bound", "unresolved_liability": False})
        self.store.finish(claimed, claimed=True)
        self.assertEqual(self.count("runs"), 1000)
        self.assertEqual(self.store.run(self.pid, run["id"], "alice")["compute"]["charged_cpu_seconds"], 30)

    def test_oversize_terminal_keeps_prior_record_and_guard_hold(self):
        self.submit(); run = self.store.claim()
        before = self.store.run(self.pid, run["id"], "alice")
        run.update(status="failed", error="x" * 262144)
        with self.assertRaises(f.LogicalLimit): self.store.finish(run, claimed=True)
        self.assertEqual(self.store.run(self.pid, run["id"], "alice"), before)
        self.assertEqual(self.store.operation_status()["state"], "paused")
        self.assertFalse((self.root / "runs" / run["id"] / "result.json").exists())
        self.store.cancel(self.pid, run["id"], "alice")
        self.assertEqual(self.store.operation_status()["state"], "paused")

    def test_encoded_limits_use_escaped_bytes_and_precede_result_write(self):
        self.assertEqual(len(f.bounded("x" * (65536-2), "result")), 65536)
        with self.assertRaises(f.LogicalLimit): f.bounded("x" * (65536-1), "result")
        with self.assertRaises(f.LogicalLimit): f.bounded("\x00" * 11000, "result")
        self.submit(); run = self.store.claim()
        with self.assertRaises(f.LogicalLimit): self.store.finish(run, "\x00" * 11000, claimed=True)
        self.assertFalse((self.root / "runs" / run["id"] / "result.json").exists())
        self.assertEqual(self.store.operation_status()["state"], "paused")

    def test_unqualified_profiles_and_oversize_paths_refuse_before_creation(self):
        for changes in ({"mode": "local-guarded-synthetic-oidc-v1", "identity": {}},
                        {"broker": "/" + "🧪" * 256}):
            path = self.root.parent / "uncreated"
            with self.assertRaises((f.InvalidProfile, f.LogicalLimit)):
                Store(path, create=True, guarded=True, config={**self.config, **changes}, subject="alice")
            self.assertFalse(path.exists())

    def test_checkpoint_preserves_profile_restore_refuses_before_effects(self):
        with patch.object(r, "Broker", RecoveryBroker), patch.object(gr, "Broker", RecoveryBroker):
            provider = RecoveryBroker("unused", self.root / "broker")
            provider.write({"admission": "active", "unresolved": [], "projects": [{"project_id": self.pid, "charged_cpu_seconds": 30}]})
            backup = self.root.parent / "backup"
            report = r.checkpoint(self.root, backup)
            self.assertEqual(json.loads((backup / "config.json").read_bytes())["logical_authority"], f.profile())
            destination = self.root.parent / "replacement"
            before = (self.root / "config.json").read_bytes()
            with patch.object(gr, "Broker") as broker:
                with self.assertRaisesRegex(ValueError, "not qualified"):
                    r.restore(backup, destination, trusted_sha256=report["checkpoint_sha256"], broker="unused")
                broker.assert_not_called()
            self.assertFalse(destination.exists())
            self.assertEqual((self.root / "config.json").read_bytes(), before)
            with closing(self.store.connect()) as db:
                digest = r.database_digest(db)
            with closing(self.store._connect()) as db:
                db.execute("PRAGMA user_version=0"); db.commit()
                self.assertNotEqual(r.database_digest(db), digest)

    def test_database_authority_identity_is_required_before_safety(self):
        mutations = ("DELETE FROM operation_authority",
                     "UPDATE operation_authority SET authority_id='wrong'",
                     "INSERT INTO operation_authority VALUES('extra',0,'barrier')")
        for mutation in mutations:
            for action in ("open", "connect", "revoke", "logout", "cancel"):
                with self.subTest(mutation=mutation, action=action):
                    case = FiniteAuthorityTests("test_binding_and_profile_are_explicit_and_not_a_quota")
                    try:
                        case.setUp()
                        run = case.submit()
                        token = case.store.session(key=case.key)[1]
                        operations = {"open": lambda: Store(case.root), "connect": case.store.connect,
                                      "revoke": lambda: case.store.revoke_user("alice"),
                                      "logout": lambda: case.store.logout(token),
                                      "cancel": lambda: case.store.cancel(case.pid, run["id"], "alice")}
                        with closing(sqlite3.connect(case.root / "workspace.sqlite3")) as db:
                            db.execute(mutation); db.commit()
                            before = r.database_digest(db)
                        guard_before = (case.root / g.FILE).read_bytes()
                        config_before = (case.root / "config.json").read_bytes()
                        with self.assertRaises(f.InvalidProfile):
                            value = operations[action]()
                            if hasattr(value, "close"): value.close()
                        with closing(sqlite3.connect(case.root / "workspace.sqlite3")) as db:
                            self.assertEqual(r.database_digest(db), before)
                        self.assertEqual((case.root / g.FILE).read_bytes(), guard_before)
                        self.assertEqual((case.root / "config.json").read_bytes(), config_before)
                    finally:
                        case.doCleanups()

    def test_all_recovery_entrypoints_reject_db_marker_before_effects(self):
        with patch.object(r, "Broker", RecoveryBroker), patch.object(gr, "Broker", RecoveryBroker):
            RecoveryBroker("unused", self.root / "broker").write({"admission": "active", "unresolved": [], "projects": []})
            backup = self.root.parent / "marker-backup"
            r.checkpoint(self.root, backup)
        config_path = backup / "config.json"
        config = json.loads(config_path.read_bytes())
        config.pop("logical_authority"); config["mode"] = "local-guarded-public-data-v1"
        config["operation_guard"].pop("logical_authority_sha256")
        config["operation_guard"]["format"] = "educloud-operation-guard-v1"
        config_path.write_text(canonical(config))
        manifest_path = backup / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["files"] = r.inventory(backup); manifest["files"].pop("manifest.json")
        manifest_path.write_text(canonical(manifest))
        trusted = r.sha(manifest_path.read_bytes())
        # This is an internally hash-consistent synthetic checkpoint with a
        # trusted fixture digest, not a failed integrity check.
        r.verified_checkpoint(backup, trusted)
        destination = self.root.parent / "marker-destination"
        before = (self.root / "config.json").read_bytes()
        for function in (r.restore, gr.restore, r.activate, gr.activate):
            with self.subTest(function=function.__module__ + "." + function.__name__):
                with patch.object(gr, "Broker") as broker, patch.object(r, "fresh_directory") as create, patch.object(r, "offline") as offline:
                    create.side_effect = AssertionError("destination creation reached")
                    offline.side_effect = AssertionError("source/replacement lock reached")
                    with self.assertRaisesRegex(ValueError, "not qualified"):
                        if function in (r.restore, gr.restore):
                            function(backup, destination, trusted_sha256=trusted, broker="unused")
                        else:
                            function(destination, self.root, backup, trusted_sha256=trusted)
                    broker.assert_not_called(); create.assert_not_called(); offline.assert_not_called()
                self.assertFalse(destination.exists())
                self.assertEqual((self.root / "config.json").read_bytes(), before)

    def test_all_caps_preserve_safety_and_conservative_settlement(self):
        self.store.add_user("bob")
        self.store.membership(self.pid, "alice", "bob", "editor")
        run = self.submit(); claimed = self.store.claim()
        self.seed_runs_to_cap(run); self.seed_projects(20); self.seed_members(1024)
        with closing(self.store._connect()) as db:
            subjects = [row[0] for row in db.execute("SELECT subject FROM users ORDER BY subject LIMIT 32")]
            for i in range(256):
                db.execute("INSERT INTO sessions VALUES(?,?,?)", (hashlib.sha256(f"full-token-{i}".encode()).hexdigest(), subjects[i // 8], time.time()+100))
            db.commit()
        self.assertEqual([self.count(t) for t in ("users", "projects", "members", "runs", "sessions")], [128,20,1024,1000,256])
        self.assertEqual(self.store.session(token="full-token-0"), subjects[0])
        self.store.logout("full-token-0")
        self.store.membership(self.pid, "alice", "bob", "viewer")
        self.store.membership(self.pid, "alice", "bob")
        self.store.rotate_user("bob"); self.store.revoke_user("bob")
        self.store.cancel(self.pid, run["id"], "alice")
        claimed.update(status="failed", cleanup_verified=True,
                       compute={"reserved_cpu_seconds":30,"charged_cpu_seconds":30,
                                "provenance":"estimated_upper_bound","unresolved_liability":False})
        self.store.finish(claimed, claimed=True)
        self.assertEqual(self.store.run(self.pid, run["id"], "alice")["compute"]["charged_cpu_seconds"], 30)
        self.assertEqual([self.count(t) for t in ("users", "projects", "runs")], [128,20,1000])
        self.assertEqual(self.reopened()["state"], "ready")

    def test_valid_interruption_keeps_sessions_and_safety_available(self):
        run = self.submit(); self.store.claim()
        token = self.store.session(key=self.key)[1]
        self.store.guard.fail()
        reopened = Store(self.root)
        self.assertEqual(reopened.session(token=token), "alice")
        reopened.cancel(self.pid, run["id"], "alice")
        reopened.logout(token)
        reopened.rotate_user("alice")
        self.assertEqual(reopened.operation_status()["state"], "paused")
        self.assertTrue(reopened.run(self.pid, run["id"], "alice")["compute"]["unresolved_liability"])

    def test_every_encoded_ceiling_and_project_refusal_before_provider(self):
        for kind, ceiling in f.ENCODED_LIMITS.items():
            with self.subTest(kind=kind):
                self.assertEqual(len(f.bounded("x" * (ceiling-2), kind)), ceiling)
                with self.assertRaises(f.LogicalLimit): f.bounded("x" * (ceiling-1), kind)
        # These values satisfy the older character limits but exceed the new
        # encoded project ceiling because astral characters use JSON escapes.
        data = {**self.data, "question":"🧪"*2000, "success_criterion":"🧪"*2000}
        before = self.sequence()
        with patch.object(self.broker, "init_project") as provider:
            with self.assertRaises(f.LogicalLimit):
                self.store.create_project("alice", data, self.broker, 600, "encoded-overflow")
            provider.assert_not_called()
        self.assertEqual(self.sequence(), before)
        self.assertEqual(self.count("projects"), 1)

    def test_escaped_receipt_overflow_retains_unknown_without_file(self):
        queued = self.submit()
        worker = runtime.Worker(self.store, self.broker)
        receipt = {"schema_version":1,"request_id":queued["id"],"project_id":self.pid,
                   "image":queued["environment"]["image"],"admitted":True,"execution_started":True,
                   "cleanup_verified":True,"unresolved_liability":False,"status":"failed",
                   "exit_code":1,"stdout":"","stderr":"","error":"",
                   "compute":{"reserved_cpu_seconds":30,"charged_cpu_seconds":30,"provenance":"estimated_upper_bound"},
                   "extra_diagnostics":"🧪"*180000}
        raw = json.dumps(receipt, ensure_ascii=False).encode()
        # Deliberately bypass collect's stricter 256 KiB input contract to
        # exercise serializer defense in depth; real collect rejects earlier.
        self.assertGreater(len(raw), STDOUT_LIMIT)
        self.assertGreater(len(f.encode(receipt)), f.ENCODED_LIMITS["receipt"])
        def collect(*args, **kwargs):
            worker.stopped.set()
            return raw, b""
        with patch.object(runtime.subprocess, "Popen") as spawn, patch.object(runtime, "collect", side_effect=collect):
            spawn.return_value.returncode=0
            worker.loop()
            self.assertEqual(spawn.call_count, 1)
        directory = self.root / "runs" / queued["id"]
        self.assertTrue((directory / "request.private.json").exists())
        self.assertFalse((directory / "receipt.private.json").exists())
        self.assertFalse((directory / "result.json").exists())
        saved = self.store.run(self.pid, queued["id"], "alice")
        self.assertEqual(saved["status"], "interrupted")
        self.assertTrue(saved["compute"]["unresolved_liability"])
        self.assertEqual(self.store.operation_status()["state"], "paused")

    def test_competing_final_project_membership_and_run_slots(self):
        for kind in ("projects", "members", "runs"):
            with self.subTest(kind=kind):
                case = FiniteAuthorityTests("test_binding_and_profile_are_explicit_and_not_a_quota")
                try:
                    case.setUp()
                    if kind == "projects":
                        case.seed_projects(19)
                        def admit(index):
                            for attempt in range(100):
                                try:
                                    return case.store.create_project("alice", case.data, case.broker, 600, "last-project-"+str(index))
                                except g.GuardBusy:
                                    threading.Event().wait(0.01)
                            self.fail("project admission remained busy")
                    elif kind == "members":
                        case.seed_members(1023)
                        with closing(case.store.connect()) as db:
                            missing = [tuple(row) for row in db.execute("SELECT p.id,u.subject FROM projects p CROSS JOIN users u LEFT JOIN members m ON m.project_id=p.id AND m.subject=u.subject WHERE m.subject IS NULL LIMIT 2")]
                        admit = lambda index: case.store.membership(missing[index][0], "alice", missing[index][1], "viewer")
                    else:
                        run = case.submit()
                        case.pid = case.seed_runs_to_cap(run, target=999)
                        admit = lambda index: case.submit("last-run-"+str(index))
                    case.compete(admit)
                    self.assertEqual(case.count(kind), f.ROW_LIMITS[kind])
                finally:
                    case.doCleanups()
