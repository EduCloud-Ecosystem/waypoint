"""Disposable production-connected export controls; no live provider."""
from contextlib import closing, contextmanager
import datetime as dt
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from research_workspace import bounded_export as be, evidence, finite_authority as f
from research_workspace import operation_guard as g, recovery as recovery
from research_workspace.identity import Identity
from research_workspace.server import Application, Handler, Server
from research_workspace.store import AccessError, Store, canonical
from test_operation_guard import GuardFixture, FakeBroker
import test_finite_authority as finite_tests
from test_workspace import IMAGES


class ExportFixture(GuardFixture):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "state"
        self.config = {"version": 1, "mode": f.EXPORT_MODE, "broker": sys.executable,
                       "images": IMAGES, "total_budget_cpu_seconds": 600,
                       "logical_authority": f.profile(2)}
        self.store = Store(self.root, create=True, guarded=True, config=self.config, subject="alice")
        self.key = self.store.initial_key
        self.token = self.store.session(key=self.key)[1]
        self.subject = "alice"
        self.broker = FakeBroker()
        self.data = {"name": "Synthetic scoped export", "question": "Are readers closed before artifact I/O?",
                     "success_criterion": "No mixed state or lost safety actions.",
                     "ends_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7)).isoformat(),
                     "budget_cpu_seconds": 120}
        self.project = self.store.create_project("alice", self.data, self.broker, 600, "project-request")
        self.pid = self.project["id"]

    def export(self):
        return self.store.export_bundle(self.pid, self.subject, self.token)

    def records(self):
        with closing(sqlite3.connect(self.root / "workspace.sqlite3")) as db:
            return recovery.database_digest(db), (self.root / g.FILE).read_bytes()

    @contextmanager
    def connections(self):
        opened = []
        original = self.store.connect
        class Probe:
            def __init__(self, db): self.db, self.closed = db, False
            def __getattr__(self, name): return getattr(self.db, name)
            def close(self): self.db.close(); self.closed = True
        def connect():
            probe = Probe(original()); opened.append(probe); return probe
        with patch.object(self.store, "connect", side_effect=connect):
            yield opened
        self.assertTrue(all(p.closed for p in opened))

    def assert_released(self, opened):
        self.assertTrue(opened)
        self.assertTrue(all(p.closed for p in opened), "export reader survives outside capture")
        self.assertEqual(self.store.guard.lease.depth, 0, "export guard survives artifact/ZIP/stream stage")

    def paused_stage(self, stage, action, *, refused=True):
        reached, release = threading.Event(), threading.Event()
        results = []
        module, name = (be, "_read_artifact") if stage == "artifact" else (evidence, "build_bundle")
        original = getattr(module, name)
        def block(*args, **kwargs):
            reached.set()
            if not release.wait(5): raise RuntimeError("fixture release missing")
            return original(*args, **kwargs)
        def export():
            try: results.append(self.export())
            except Exception as exc: results.append(exc)
        with self.connections() as opened, patch.object(module, name, side_effect=block):
            thread = threading.Thread(target=export)
            thread.start()
            try:
                self.assertTrue(reached.wait(3))
                self.assert_released(opened)
                writer_errors = []
                def writer():
                    try: action()
                    except Exception as exc: writer_errors.append(exc)
                writer_thread = threading.Thread(target=writer)
                writer_thread.start(); writer_thread.join(3)
                self.assertFalse(writer_thread.is_alive(), "safety action blocked by export")
                self.assertEqual(writer_errors, [])
            finally:
                release.set(); thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 1)
        if refused:
            self.assertIsInstance(results[0], (AccessError, be.ExportChanged))
        else:
            self.assertIsInstance(results[0], bytes)
        return results[0]


@contextmanager
def fresh():
    case = ExportFixture()
    case.setUp()
    try: yield case
    finally: case.tearDown()


class BoundedExportTests(ExportFixture):
    def test_exact_fresh_profile_and_old_profile_compatibility(self):
        self.assertEqual(Store(self.root).profile_version, 2)
        self.assertEqual(self.store.guard.binding["format"], f.EXPORT_GUARD_FORMAT)
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], f.EXPORT_USER_VERSION)
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 2)
        self.assertFalse(self.store.operation_status()["quota_enforced"])
        with self.assertRaises(ValueError): self.store.snapshot(self.pid, "alice")
        with self.assertRaises(ValueError): Store(self.root, create=True, guarded=True, config=self.config, subject="alice")
        legacy = Store(self.root.parent / "legacy", create=True, guarded=True,
                       config={**self.config, "mode": f.MODE, "logical_authority": f.profile()}, subject="alice")
        self.assertEqual(Store(legacy.root).profile_version, 1)
        with self.assertRaises(ValueError): legacy.export_bundle(self.pid, "alice", self.token)

    def test_cli_profiles_are_mutually_exclusive_before_state_creation(self):
        target = self.root.parent / "uncreated"
        result = subprocess.run([sys.executable, "-m", "research_workspace", "init", "--state", str(target),
                                 "--broker", sys.executable, "--finite-authority", "--bounded-export"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(target.exists())

    def test_unchanged_running_and_successful_exports_preserve_private_boundary(self):
        succeeded = self.settled(); self.store.finish(succeeded, succeeded["metrics"])
        self.submit("second"); running = self.store.claim()
        before = self.records()
        payload = self.export()
        self.assertEqual(payload, self.export())
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            runs = json.loads(archive.read("runs.json"))
            self.assertEqual({r["status"] for r in runs}, {"running", "succeeded"})
            observed = next(r for r in runs if r["id"] == running["id"])
            self.assertEqual(observed["compute"]["provenance"], "unknown")
            self.assertTrue(observed["compute"]["unresolved_liability"])
            self.assertNotIn(f"runs/{running['id']}/result.json", archive.namelist())
            content = b"".join(archive.read(n) for n in archive.namelist())
            for private in (self.token, self.key, "cancel_requested", "request_key", "token_hash", "key_hash"):
                self.assertNotIn(private.encode(), content)
        self.assertEqual(self.records(), before)
        self.assertTrue(evidence.verify_bundle(payload)["valid"])

    def test_blocked_artifacts_and_zip_release_readers_for_safety(self):
        for stage in ("artifact", "zip"):
            for action in ("revoke", "rotate", "cancel", "settle"):
                with self.subTest(stage=stage, action=action), fresh() as c:
                    run = c.settled()  # Captured DB state is still running.
                    actions = {"revoke": lambda: c.store.revoke_user("alice"),
                               "rotate": lambda: c.store.rotate_user("alice"),
                               "cancel": lambda: c.store.cancel(c.pid, run["id"], "alice"),
                               "settle": lambda: c.store.finish(run, run["metrics"])}
                    c.paused_stage(stage, actions[action])
                    with closing(c.store.connect()) as db:
                        if action == "revoke": self.assertEqual(db.execute("SELECT active FROM users WHERE subject='alice'").fetchone()[0], 0)
                        if action == "rotate": self.assertEqual(db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
                        if action == "cancel":
                            row = db.execute("SELECT status,cancel_requested FROM runs WHERE id=?", (run["id"],)).fetchone()
                            self.assertEqual(tuple(row), ("running", 1))
                        if action == "settle":
                            row = json.loads(db.execute("SELECT payload FROM runs WHERE id=?", (run["id"],)).fetchone()[0])
                            self.assertEqual((row["status"], row["compute"]["charged_cpu_seconds"]), ("succeeded", 30))

    def test_final_check_rejects_membership_logout_expiry_close_and_new_run(self):
        for action in ("remove", "demote", "logout", "expire", "close", "new-run"):
            with self.subTest(action=action), fresh() as c:
                bob = c.store.add_user("bob"); c.store.membership(c.pid, "alice", "bob", "editor")
                c.subject, c.token = c.store.session(key=bob)
                c.submit()
                def expire():
                    with closing(c.store._connect()) as db:
                        db.execute("UPDATE sessions SET expires=0"); db.commit()
                actions = {"remove": lambda: c.store.membership(c.pid, "alice", "bob"),
                           "demote": lambda: c.store.membership(c.pid, "alice", "bob", "viewer"),
                           "logout": lambda: c.store.logout(c.token), "expire": expire,
                           "close": lambda: c.store.close_project(c.pid, "alice"),
                           "new-run": lambda: c.submit("new-run")}
                c.paused_stage("zip", actions[action])

    def test_membership_change_reverted_before_final_check_is_current_equality(self):
        bob = self.store.add_user("bob"); self.store.membership(self.pid, "alice", "bob", "viewer")
        self.subject, self.token = self.store.session(key=bob)
        def revert():
            self.store.membership(self.pid, "alice", "bob")
            self.store.membership(self.pid, "alice", "bob", "viewer")
        self.paused_stage("zip", revert, refused=False)

    def test_missing_or_changed_artifacts_refuse_without_mutation(self):
        run = self.settled(); self.store.finish(run, run["metrics"])
        for name in ("input/data.csv", "recipe/recipe.py", "result.json"):
            path = self.root / "runs" / run["id"] / name
            original = path.read_bytes()
            for mode in ("missing", "changed"):
                with self.subTest(name=name, mode=mode):
                    path.unlink()
                    if mode == "changed": path.write_bytes(b"{}")
                    before = self.records()
                    with self.assertRaisesRegex(ValueError, "could not be verified") as caught:
                        self.export()
                    self.assertNotIn(str(self.root), str(caught.exception))
                    self.assertEqual(self.records(), before)
                    path.write_bytes(original)

    def test_after_copy_file_removal_keeps_verified_bytes(self):
        run = self.settled(); self.store.finish(run, run["metrics"])
        expected = self.export()
        def remove(): (self.root / "runs" / run["id"] / "result.json").unlink()
        self.assertEqual(self.paused_stage("zip", remove, refused=False), expected)

    def test_session_required_and_subject_cannot_substitute(self):
        for subject, token in (("alice", None), ("alice", "bad"), ("bob", self.token)):
            with self.subTest(subject=subject, token_valid=token == self.token):
                with self.assertRaises(AccessError): self.store.export_bundle(self.pid, subject, token)

    def test_manifest_bounds_before_parse_or_artifacts(self):
        self.submit()
        for table, column, value in (("runs", "payload", "x" * 262145),
                                     ("projects", "payload", "x" * 32769),
                                     ("runs", "request_key", "x" * 81)):
            with self.subTest(table=table, column=column):
                with closing(sqlite3.connect(self.root / "workspace.sqlite3")) as db:
                    prior = db.execute(f"SELECT {column} FROM {table}").fetchone()[0]
                    db.execute(f"UPDATE {table} SET {column}=?", (value,)); db.commit()
                with patch.object(be, "_build") as build:
                    with self.assertRaises(ValueError): self.export()
                    build.assert_not_called()
                with closing(sqlite3.connect(self.root / "workspace.sqlite3")) as db:
                    db.execute(f"UPDATE {table} SET {column}=?", (prior,)); db.commit()
        with closing(sqlite3.connect(self.root / "workspace.sqlite3")) as db:
            original = db.execute("SELECT payload FROM runs").fetchone()[0]
            for i in range(100):
                db.execute("INSERT INTO runs(id,project_id,subject,status,payload) VALUES(?,?,'alice','failed',?)", (f"seed{i}", self.pid, original))
            db.commit()
        with patch.object(be, "_build") as build:
            with self.assertRaises(ValueError): self.export()
            build.assert_not_called()

    def test_safe_artifact_traversal_and_descriptor_cleanup(self):
        self.submit()
        directory = self.root / "scratch"; directory.mkdir()
        file = directory / "data"; file.write_bytes(b"abcd")
        (directory / "link").symlink_to(file)
        (self.root / "linked").symlink_to(directory, target_is_directory=True)
        os.mkfifo(directory / "fifo")
        for relative, maximum in (("scratch/data", 3), ("scratch/link", 10), ("linked/data", 10), ("scratch/fifo", 10), ("scratch/missing", 10)):
            opened = set(); real_open, real_close = os.open, os.close
            def tracked_open(*a, **kw):
                fd = real_open(*a, **kw); opened.add(fd); return fd
            def tracked_close(fd): real_close(fd); opened.remove(fd)
            with self.subTest(relative=relative), patch.object(os, "open", side_effect=tracked_open), patch.object(os, "close", side_effect=tracked_close):
                with self.assertRaises((ValueError, OSError)): be._read_artifact(self.root, relative, maximum)
            self.assertEqual(opened, set())

    def test_read_zip_and_final_failures_release_resources_without_intents(self):
        self.submit(); before = self.records()
        for location in ("read", "zip", "final"):
            count = 0; capture = be._capture
            def fail_final(*args):
                nonlocal count
                count += 1
                if count == 2: raise AccessError("final authorization refused")
                return capture(*args)
            target, name, effect = {"read": (be, "_read_artifact", OSError("private path")),
                                    "zip": (evidence, "build_bundle", OSError("private zip error")),
                                    "final": (be, "_capture", fail_final)}[location]
            with self.subTest(location=location), self.connections(), patch.object(target, name, side_effect=effect):
                with self.assertRaises((ValueError, AccessError)): self.export()
            self.assertEqual(self.records(), before)
            self.assertEqual(self.store.guard.lease.depth, 0)

    def test_mixed_stripped_and_stale_bindings_refuse(self):
        for change in ("mode", "policy", "guard-format", "database", "stripped", "authority"):
            with self.subTest(change=change), fresh() as c:
                config_path = c.root / "config.json"; config = json.loads(config_path.read_bytes())
                if change == "mode": config["mode"] = f.MODE
                if change == "policy": config["logical_authority"] = f.profile()
                if change == "guard-format": config["operation_guard"]["format"] = f.GUARD_FORMAT
                if change == "stripped": config.pop("logical_authority"); config.pop("operation_guard"); config["mode"] = "local-public-data"
                config_path.write_text(canonical(config))
                with closing(sqlite3.connect(c.root / "workspace.sqlite3")) as db:
                    if change == "database": db.execute(f"PRAGMA user_version={f.USER_VERSION}")
                    if change == "authority": db.execute("DELETE FROM operation_authority")
                    db.commit()
                before = c.records()
                for operation in (lambda: Store(c.root), c.export, lambda: c.store.revoke_user("alice")):
                    with self.assertRaises((f.InvalidProfile, ValueError, g.GuardPaused)): operation()
                self.assertEqual(c.records(), before)

    def test_oidc_and_direct_stripped_checkpoint_recovery_refuse(self):
        with self.assertRaisesRegex(ValueError, "OIDC.*not qualified"): Identity({}, self.store)
        finite_tests.FiniteAuthorityTests.test_all_recovery_entrypoints_reject_db_marker_before_effects(self)

    def test_http_stream_has_no_readers_or_locks_and_no_late_retraction(self):
        self.submit()
        app = Application.__new__(Application); app.store = self.store; app.config = self.config; app.identity = None
        server = Server(("127.0.0.1", 0), Handler); server.app = app
        reached, release = threading.Event(), threading.Event(); result = []
        original = Handler.respond
        def respond(handler, status, value, **kw):
            if isinstance(value, bytes):
                reached.set()
                if not release.wait(5): raise RuntimeError("stream fixture timeout")
            return original(handler, status, value, **kw)
        def request():
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=8)
            try:
                conn.request("GET", f"/api/projects/{self.pid}/export", headers={"Cookie": "research_session=" + self.token})
                response = conn.getresponse(); result.append((response.status, response.read()))
            finally: conn.close()
        with self.connections() as opened, patch.object(Handler, "respond", respond):
            serving = threading.Thread(target=server.serve_forever); serving.start()
            caller = threading.Thread(target=request); caller.start()
            try:
                self.assertTrue(reached.wait(3)); self.assert_released(opened)
                self.store.revoke_user("alice")
            finally:
                release.set(); caller.join(8); server.shutdown(); serving.join(3); server.server_close()
        self.assertEqual(result[0][0], 200)
        self.assertTrue(evidence.verify_bundle(result[0][1])["valid"])

    def test_uncertain_descriptor_close_attempts_all_others_once(self):
        directory = self.root / "scratch"; directory.mkdir()
        (directory / "data").write_bytes(b"abcd")
        opened = []; attempted = []; real_open, real_close = os.open, os.close
        def tracked_open(*a, **kw):
            fd = real_open(*a, **kw); opened.append(fd); return fd
        def uncertain_close(fd):
            attempted.append(fd); real_close(fd)
            if len(attempted) == 1: raise OSError("injected close acknowledgement loss")
        with patch.object(os, "open", side_effect=tracked_open), patch.object(os, "close", side_effect=uncertain_close):
            with self.assertRaises(OSError): be._read_artifact(self.root, "scratch/data", 10)
        self.assertEqual(attempted, list(reversed(opened)))
        for fd in opened:
            with self.assertRaises(OSError): os.fstat(fd)
        with self.assertRaises(AccessError): self.store.session(token=self.token)
