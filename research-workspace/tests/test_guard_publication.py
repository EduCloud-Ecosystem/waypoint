"""Disposable v3 failure controls. No provider, cleanup, quota or migration."""
from contextlib import closing, contextmanager
import datetime as dt
import hashlib
import http.client
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from research_workspace import finite_authority as f, operation_guard as g
from research_workspace import guard_publication as p, recovery, runtime
from research_workspace.store import Store
from research_workspace.server import Application, Handler, Server
from test_operation_guard import FakeBroker, FaultDB
from test_workspace import IMAGES
import test_bounded_export as export_tests


class PublicationFixture(export_tests.ExportFixture):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "state"
        self.config = {"version": 1, "mode": f.PUBLICATION_MODE, "broker": sys.executable,
                       "images": IMAGES, "total_budget_cpu_seconds": 600,
                       "logical_authority": f.profile(3)}
        self.store = Store(self.root, create=True, guarded=True, config=self.config, subject="alice")
        self.key = self.store.initial_key
        self.token = self.store.session(key=self.key)[1]
        self.subject = "alice"
        self.broker = FakeBroker()
        self.data = {"name": "Synthetic guard publication", "question": "Does failed publication preserve debt?",
                     "success_criterion": "No scratch adoption or false safety success.",
                     "ends_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7)).isoformat(),
                     "budget_cpu_seconds": 120}
        self.project = self.store.create_project("alice", self.data, self.broker, 600, "project-request")
        self.pid = self.project["id"]

    def child(self, source):
        result = subprocess.run([sys.executable, "-c", source, str(self.root)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def database(self):
        with closing(sqlite3.connect(self.root / "workspace.sqlite3")) as db:
            return recovery.database_digest(db)

    def safety_actions(self):
        bob = self.store.add_user("bob")
        self.store.membership(self.pid, "alice", "bob", "editor")
        run = self.settled()
        return {"logout": lambda: self.store.logout(self.token),
                "revoke": lambda: self.store.revoke_user("bob"),
                "rotate": lambda: self.store.rotate_user("bob"),
                "demote": lambda: self.store.membership(self.pid, "alice", "bob", "viewer"),
                "remove": lambda: self.store.membership(self.pid, "alice", "bob", None),
                "cancel": lambda: self.store.cancel(self.pid, run["id"], "alice"),
                "close": lambda: self.store.close_project(self.pid, "alice"),
                "settle": lambda: self.store.finish(run, run["metrics"]),
                "recover": lambda: self.store.recover()}


@contextmanager
def fresh():
    case = PublicationFixture()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


class GuardPublicationTests(PublicationFixture):
    def test_exact_profile_and_policy_digest(self):
        self.assertEqual(f.profile_digest(3), "432f95df152db6f1b16b1829f85d192021b6998fd265d7a4075ef2e29dde25af")
        self.assertEqual(self.store.profile_version, 3)
        self.assertEqual(self.store.guard.binding["format"], f.PUBLICATION_GUARD_FORMAT)
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0x45464133)
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 2)
        self.assertFalse(self.store.operation_status()["quota_enforced"])
        self.assertEqual(self.reopened()["state"], "ready")
        self.assertFalse(list(self.root.glob(g.TEMP + "*")))

    def test_mutually_exclusive_cli_profiles(self):
        for flag in ("--finite-authority", "--bounded-export"):
            target = self.root.parent / "not-created"
            result = subprocess.run([sys.executable, "-m", "research_workspace", "init", "--state", str(target),
                                     "--broker", sys.executable, flag, "--bounded-guard-publication"],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(target.exists())

    def test_cached_current_rechecks_other_process_failed_scratch(self):
        for role, action in (("ordinary", "s.add_user('new')"), ("safety", "s.revoke_user('alice')")):
            with self.subTest(role=role), fresh() as c:
                current = (c.root / g.FILE).read_bytes()
                cached = c.store.guard.lease.durable_incarnation
                result = c.child("""
import json,sys
from unittest.mock import patch
from research_workspace.store import Store
from research_workspace import guard_publication as p
s=Store(sys.argv[1])
try:
    with patch.object(p.os,'write',side_effect=OSError('failed scratch')):
        """ + action + """
except OSError:
    print(json.dumps({'failed':True}))
""")
                self.assertTrue(result["failed"])
                self.assertEqual((c.root / g.FILE).read_bytes(), current)
                self.assertEqual(c.store.guard.lease.durable_incarnation, cached)
                slot = c.root / (p.ORDINARY if role == "ordinary" else p.SAFETY)
                self.assertEqual(slot.read_bytes(), b"")
                with self.assertRaises(g.GuardPaused): c.store.add_user("blocked")
                if role == "ordinary":
                    c.store.logout(c.token)
                else:
                    with self.assertRaises(g.GuardPaused): c.store.logout(c.token)
                self.assertEqual(slot.read_bytes(), b"")

    def test_safety_matrix_preserves_residue_and_refuses_exhaustion(self):
        for role in ("ordinary", "safety", "essential"):
            for action in ("logout", "revoke", "rotate", "demote", "remove", "cancel", "close", "settle", "recover"):
                with self.subTest(role=role, action=action), fresh() as c:
                    actions = c.safety_actions()
                    if role == "essential":
                        token = c.store.guard.begin("mutation", essential=True)
                        token.fail()
                    else:
                        (c.root / (p.ORDINARY if role == "ordinary" else p.SAFETY)).write_bytes(b"retained")
                    before = c.database()
                    if role == "ordinary":
                        actions[action]()
                        self.assertEqual((c.root / p.ORDINARY).read_bytes(), b"retained")
                    else:
                        with self.assertRaises(g.GuardPaused): actions[action]()
                        self.assertEqual(c.database(), before)
                    with self.assertRaises(g.GuardPaused): c.store.add_user("blocked")

    def test_no_borrow_after_unresolved_essential_with_empty_scratch(self):
        token = self.store.guard.begin("mutation", essential=True)
        token.effect()
        token.fail()
        self.assertFalse(list(self.root.glob(g.TEMP + "*")))
        before = self.database(), (self.root / g.FILE).read_bytes()
        for _ in range(3):
            with self.assertRaises(g.GuardPaused): self.store.logout(self.token)
        self.assertEqual((self.database(), (self.root / g.FILE).read_bytes()), before)

    def test_namespace_types_are_never_adopted_or_removed(self):
        for kind in ("symlink", "hardlink", "directory", "unknown", "oversize"):
            with self.subTest(kind=kind), fresh() as c:
                target = c.root / p.ORDINARY
                if kind == "symlink": target.symlink_to(c.root / g.FILE)
                if kind == "hardlink": os.link(c.root / g.FILE, target)
                if kind == "directory": target.mkdir()
                if kind == "unknown": target = c.root / ".operation-guard-foreign"; target.write_bytes(b"foreign")
                if kind == "oversize": target.write_bytes(b"x" * (g.MAX_BYTES + 1))
                before = target.lstat()
                with self.assertRaises(g.GuardPaused): c.store.logout(c.token)
                self.assertEqual((target.lstat().st_ino, target.lstat().st_size), (before.st_ino, before.st_size))

    def test_full_named_payload_family_and_exact_fit(self):
        encode, replace = g.encode, p.os.replace
        def padded(value):
            content = encode(value)
            return content + b" " * (g.MAX_BYTES - len(content)) if "publication" in value else content
        with patch.object(g, "encode", side_effect=padded):
            self.store.add_user("large")
            (self.root / p.ORDINARY).write_bytes(b"x" * g.MAX_BYTES)
            peaks = []
            def observed(*args, **kwargs):
                peaks.append(sum((self.root / name).stat().st_size for name in (g.FILE, p.ORDINARY, p.SAFETY)))
                return replace(*args, **kwargs)
            with patch.object(p.os, "replace", side_effect=observed): self.store.logout(self.token)
        self.assertTrue(peaks)
        self.assertEqual(set(peaks), {3 * 4194304})
        self.assertEqual((self.root / p.ORDINARY).stat().st_size, g.MAX_BYTES)

    def test_one_byte_overflow_precedes_scratch_and_business_effects(self):
        encode = g.encode
        before = self.database(), (self.root / g.FILE).read_bytes()
        def overflow(value):
            content = encode(value)
            return content + b" " * (g.MAX_BYTES + 1 - len(content)) if "publication" in value else content
        with patch.object(g, "encode", side_effect=overflow):
            with self.assertRaises(g.GuardPaused): self.store.add_user("oversize")
        self.assertFalse(list(self.root.glob(g.TEMP + "*")))
        self.assertEqual((self.database(), (self.root / g.FILE).read_bytes()), before)

    def test_short_writes_complete_one_candidate(self):
        write = p.os.write
        with patch.object(p.os, "write", side_effect=lambda fd, b: write(fd, b[:19])):
            self.store.add_user("short-writes")
        self.assertEqual(self.store.operation_status()["state"], "ready")
        self.assertFalse(list(self.root.glob(g.TEMP + "*")))

    def test_file_sync_write_close_and_rename_failures_preserve_exact_slot(self):
        for phase in ("write", "sync", "close", "rename"):
            with self.subTest(phase=phase), fresh() as c:
                original = getattr(p.os, {"write":"write", "sync":"fsync", "close":"close", "rename":"replace"}[phase])
                triggered = []
                def fault(*args, **kwargs):
                    pending = c.root / p.ORDINARY
                    owned = False
                    if phase == "rename": owned = True
                    elif pending.exists():
                        info = os.fstat(args[0]); owned = info.st_ino == pending.stat().st_ino
                    if owned and not triggered:
                        triggered.append(True)
                        if phase == "close": original(*args, **kwargs)
                        raise OSError("injected " + phase)
                    return original(*args, **kwargs)
                before = c.database(), (c.root / g.FILE).read_bytes()
                with patch.object(p.os, {"write":"write", "sync":"fsync", "close":"close", "rename":"replace"}[phase], side_effect=fault):
                    with self.assertRaises(OSError): c.store.add_user("failed")
                self.assertTrue(triggered)
                self.assertEqual((c.database(), (c.root / g.FILE).read_bytes()), before)
                residue = (c.root / p.ORDINARY).read_bytes()
                for _ in range(3):
                    with self.assertRaises(g.GuardPaused): c.store.logout(c.token)
                self.assertEqual((c.root / p.ORDINARY).read_bytes(), residue)
                self.assertEqual({x.name for x in c.root.glob(g.TEMP + "*")}, {p.ORDINARY})

    def test_postrename_failure_is_not_success_or_permission_from_absence(self):
        for phase in ("rename-ack", "parent-sync"):
            with self.subTest(phase=phase), fresh() as c:
                replace, fsync = p.os.replace, p.os.fsync
                replaced = []
                def rename(*args, **kwargs):
                    result = replace(*args, **kwargs); replaced.append(True)
                    if phase == "rename-ack": raise OSError("lost rename reply")
                    return result
                def sync(fd):
                    if replaced and stat.S_ISDIR(os.fstat(fd).st_mode): raise OSError("parent sync failed")
                    return fsync(fd)
                before = c.database()
                with patch.object(p.os, "replace", side_effect=rename), patch.object(p.os, "fsync", side_effect=sync):
                    with self.assertRaises(OSError): c.store.add_user("failed")
                self.assertFalse(list(c.root.glob(g.TEMP + "*")))
                self.assertEqual(c.database(), before)
                with self.assertRaises(g.GuardPaused): c.store.logout(c.token)
                result = c.child("""
import json,sys
from research_workspace.store import Store
from research_workspace.operation_guard import GuardPaused
s=Store(sys.argv[1])
try: s.logout('absent')
except GuardPaused: print(json.dumps({'refused':True}))
""")
                self.assertTrue(result["refused"])

    def test_receipt_outcomes_are_not_cached(self):
        self.store.logout(self.token)
        data = self.store.guard.load()
        essential = next(oid for oid, item in data["operations"].items() if item["essential"])
        with closing(sqlite3.connect(self.root / "workspace.sqlite3")) as db:
            db.execute("UPDATE operation_receipts SET outcome='started' WHERE id=?", (essential,)); db.commit()
        with self.assertRaises(g.GuardPaused): self.store.rotate_user("alice")

    def test_missing_receipt_refuses_safety_even_with_empty_slot(self):
        with patch.object(self.store.guard, "connect", side_effect=OSError("receipt unavailable")):
            with self.assertRaises(OSError): self.store.add_user("not-admitted")
        self.assertFalse((self.root / p.SAFETY).exists())
        with self.assertRaises(g.GuardPaused): self.store.logout(self.token)
        self.assertEqual(self.reopened()["state"], "paused")

    def test_required_failure_note_preserves_original_error_and_role(self):
        save = self.store.guard.publication.save
        def fail_note(data, **kwargs):
            if kwargs.get("stage") == "uncertainty-note":
                with patch.object(p.os, "write", side_effect=OSError("diagnostic scratch failed")):
                    return save(data, **kwargs)
            return save(data, **kwargs)
        with patch.object(self.store.guard.publication, "save", side_effect=fail_note):
            with self.assertRaisesRegex(ValueError, "original business failure") as captured:
                with self.store.transaction() as db:
                    db.execute("UPDATE users SET active=0 WHERE subject='alice'")
                    raise ValueError("original business failure")
        self.assertTrue(captured.exception.__notes__)
        self.assertTrue((self.root / p.ORDINARY).exists())
        self.assertFalse((self.root / p.SAFETY).exists())
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT active FROM users WHERE subject='alice'").fetchone()[0], 1)

    def test_new_worker_start_propagates_failed_recovery(self):
        worker = runtime.Worker(self.store, self.broker)
        (self.root / p.SAFETY).write_bytes(b"preserved")
        with self.assertRaises(g.GuardPaused): worker.start()
        self.assertFalse(worker.thread.is_alive())

    def test_takeover_precedes_business_transaction(self):
        self.child("import json,sys;from research_workspace.store import Store;s=Store(sys.argv[1]);s.logout('absent');print(json.dumps(True))")
        events = []
        barrier = self.store.guard.barrier
        original = self.store._connect
        def qualify(): events.append("barrier"); return barrier()
        class Observed:
            def __init__(self, db): self.db = db
            def __getattr__(self, name): return getattr(self.db, name)
            def execute(self, sql, *args):
                if sql == "BEGIN IMMEDIATE": events.append("business")
                return self.db.execute(sql, *args)
        def business(): return Observed(original())
        with patch.object(self.store.guard, "barrier", side_effect=qualify), patch.object(self.store, "_connect", side_effect=business):
            self.store.add_user("after-takeover")
        self.assertEqual(events, ["barrier", "business"])

    def test_released_tokens_cannot_authorize_another_effect(self):
        for essential in (False, True):
            with self.subTest(essential=essential):
                token = self.store.guard.begin("mutation", essential=essential)
                with self.store.transaction(operation=token, essential=essential) as db:
                    db.execute("UPDATE users SET active=active WHERE subject='alice'")
                before = self.database(), (self.root / g.FILE).read_bytes()
                with self.assertRaises(g.GuardPaused): token.effect()
                with self.assertRaises(g.GuardPaused):
                    with self.store.transaction(operation=token, essential=essential): pass
                self.assertEqual((self.database(), (self.root / g.FILE).read_bytes()), before)

    def test_full_ordinary_intent_allowance_retains_fresh_safety_path(self):
        for action in ("logout", "revoke", "rotate", "demote", "remove", "cancel", "close", "settle", "recover"):
            with self.subTest(action=action), fresh() as c:
                actions = c.safety_actions()
                count = sum(not item["essential"] for item in c.store.guard.load()["operations"].values())
                with patch.object(g, "MAX_OPERATIONS", count):
                    with self.assertRaises(g.GuardPaused): c.store.add_user("overflow")
                    actions[action]()

    def test_ordinary_worker_error_does_not_touch_an_active_safety_intent(self):
        token = self.store.guard.begin("mutation", essential=True)
        before = (self.root / g.FILE).read_bytes()
        error = OSError("claim failed")
        runtime.Worker(self.store, self.broker).guard_failure("ordinary", error)
        self.assertEqual((self.root / g.FILE).read_bytes(), before)
        self.assertIs(self.store.guard.lease.active[token.identity], token)
        self.assertFalse((self.root / p.SAFETY).exists())
        self.assertTrue(error.__notes__)

    def test_worker_run_and_settlement_errors_keep_their_origins(self):
        run = self.settled()
        worker = runtime.Worker(self.store, self.broker)
        def claim(**_): worker.stopped.set(); return run
        with patch.object(self.store, "claim", side_effect=claim), \
                patch.object(worker, "execute", side_effect=OSError("execution failed")), \
                patch.object(self.store, "finish", side_effect=OSError("settlement failed")), \
                patch.object(self.store.guard, "fail", wraps=self.store.guard.fail) as observed:
            worker.loop()
        self.assertEqual([call.kwargs["origin"] for call in observed.call_args_list], ["ordinary", "safety"])
        data = self.store.guard.load()
        self.assertTrue(any(item["kind"] == "run" and item["phase"] == "uncertain" for item in data["operations"].values()))
        self.assertTrue(any(item["essential"] and item["phase"] == "uncertain" for item in data["operations"].values()))

    def test_started_receipt_acknowledgement_failure_preserves_debt(self):
        for after in (False, True):
            with self.subTest(after=after), fresh() as c:
                connect = c.store.guard.connect
                with patch.object(c.store.guard, "connect", side_effect=lambda: FaultDB(connect(), commit=True, after_commit=after)):
                    with self.assertRaisesRegex(OSError, "commit failure"): c.store.add_user("not-admitted")
                with closing(c.store.connect()) as db:
                    self.assertIsNone(db.execute("SELECT subject FROM users WHERE subject='not-admitted'").fetchone())
                self.assertEqual(c.reopened()["state"], "paused")
                self.assertFalse((c.root / p.SAFETY).exists())

    def test_business_commit_and_close_failures_never_clear_uncertainty(self):
        for after, close in ((False, False), (True, False), (True, True)):
            with self.subTest(after=after, close=close), fresh() as c:
                connect = c.store._connect
                with patch.object(c.store, "_connect", side_effect=lambda: FaultDB(connect(), commit=True, after_commit=after, close=close)):
                    with self.assertRaisesRegex(OSError, "commit failure"):
                        # No preflight open, so faults apply to the business connection.
                        with c.store.transaction() as db:
                            db.execute("UPDATE users SET active=0 WHERE subject='alice'")
                self.assertEqual(c.reopened()["state"], "paused")
                data = c.store.guard.load()
                self.assertTrue(any(item["phase"] == "uncertain" for item in data["operations"].values()))

    def test_fresh_process_qualifies_effect_after_lost_parent_sync_without_forgiving_it(self):
        replace, fsync = p.os.replace, p.os.fsync
        count = []
        def rename(*args, **kwargs): count.append(True); return replace(*args, **kwargs)
        def sync(fd):
            if len(count) == 2 and stat.S_ISDIR(os.fstat(fd).st_mode): raise OSError("effect parent sync")
            return fsync(fd)
        with patch.object(p.os, "replace", side_effect=rename), patch.object(p.os, "fsync", side_effect=sync):
            with self.assertRaises(OSError): self.store.add_user("never-written")
        before = self.store.guard.load()
        unresolved = set(before["operations"])
        self.assertFalse(list(self.root.glob(g.TEMP + "*")))
        result = self.child("""
import json,sys
from research_workspace.store import Store
s=Store(sys.argv[1]);s.logout('absent')
print(json.dumps(s.operation_status()))
""")
        self.assertEqual(result["state"], "paused")
        after = self.store.guard.load()
        self.assertTrue(unresolved <= set(after["operations"]))
        with closing(self.store.connect()) as db:
            self.assertIsNone(db.execute("SELECT subject FROM users WHERE subject='never-written'").fetchone())

    def test_concurrent_processes_serialize_fixed_safety_path(self):
        source = """
import json,sys
from research_workspace.store import Store
s=Store(sys.argv[1])
for _ in range(8):s.rotate_user('alice')
print(json.dumps(s.operation_status()))
"""
        children = [subprocess.Popen([sys.executable, "-c", source, str(self.root)], stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True) for _ in range(2)]
        for child in children:
            stdout, stderr = child.communicate(timeout=20)
            self.assertEqual(child.returncode, 0, stderr)
            self.assertEqual(json.loads(stdout)["state"], "ready")
        self.assertFalse(list(self.root.glob(g.TEMP + "*")))
        self.assertEqual(self.reopened()["state"], "ready")

    def test_process_death_retains_candidate_without_adoption(self):
        current = (self.root / g.FILE).read_bytes()
        source = """
import os,sys
from unittest.mock import patch
from research_workspace.store import Store
from research_workspace import guard_publication as p
s=Store(sys.argv[1]);write=os.write
def die(fd,data):write(fd,data[:23]);os._exit(23)
with patch.object(p.os,'write',side_effect=die):s.add_user('dead')
"""
        result = subprocess.run([sys.executable, "-c", source, str(self.root)], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 23)
        self.assertEqual((self.root / g.FILE).read_bytes(), current)
        residue = (self.root / p.ORDINARY).read_bytes()
        self.assertEqual(len(residue), 23)
        self.store.logout(self.token)
        self.assertEqual((self.root / p.ORDINARY).read_bytes(), residue)

    def test_failed_bootstrap_cannot_be_adopted(self):
        target = self.root.parent / "failed-init"
        with patch.object(p.os, "write", side_effect=OSError("bootstrap failed")):
            with self.assertRaises(OSError): Store(target, create=True, guarded=True, config=self.config, subject="alice")
        self.assertEqual({x.name for x in target.iterdir()}, {g.LOCK, p.ORDINARY})
        with self.assertRaisesRegex(ValueError, "fresh empty"):
            Store(target, create=True, guarded=True, config=self.config, subject="alice")

    def test_lock_close_failure_preserves_original_and_holds_safety(self):
        original_close = g.os.close
        lock_inode = (self.root / g.LOCK).stat().st_ino
        def close(fd):
            is_lock = os.fstat(fd).st_ino == lock_inode
            original_close(fd)
            if is_lock: raise OSError("lost lock close")
        with patch.object(g.os, "close", side_effect=close):
            with self.assertRaisesRegex(ValueError, "original failure") as captured:
                with self.store.guard.locked(): raise ValueError("original failure")
        self.assertTrue(captured.exception.__notes__)
        self.assertTrue(self.store.guard.lease.publication_failed)
        with self.assertRaises(g.GuardPaused): self.store.logout(self.token)

    def test_http_logout_exhaustion_reports_failure_and_keeps_session(self):
        app = Application.__new__(Application)
        app.store, app.config, app.identity = self.store, self.config, None
        server = Server(("127.0.0.1", 0), Handler); server.app = app
        serving = threading.Thread(target=server.serve_forever); serving.start()
        (self.root / p.SAFETY).write_bytes(b"retained")
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request("DELETE", "/api/session", headers={"Cookie":"research_session=" + self.token,
                               "X-Research-Request":"1",
                               "Origin":"http://127.0.0.1:" + str(server.server_port)})
            response = connection.getresponse(); payload = json.loads(response.read())
            self.assertEqual(response.status, 503, payload)
            self.assertIsNone(response.getheader("Set-Cookie"))
            self.assertEqual(payload["operation_status"]["reason"], "safety_slot_exhausted")
        finally:
            connection.close(); server.shutdown(); serving.join(5); server.server_close()
        self.assertEqual(self.store.session(token=self.token), "alice")

    def test_descriptor_cleanup_attempts_every_close_once_and_preserves_original(self):
        opened, closed = [], []
        real_open, real_close = p.os.open, p.os.close
        def tracked(*args, **kwargs):
            fd = real_open(*args, **kwargs); opened.append(fd); return fd
        def failed_close(fd):
            closed.append(fd); real_close(fd); raise OSError("close acknowledgement")
        with patch.object(p.os, "open", side_effect=tracked), patch.object(p.os, "close", side_effect=failed_close):
            with self.assertRaisesRegex(ValueError, "original") as caught:
                with p.descriptor(self.root, os.O_RDONLY | os.O_DIRECTORY) as root:
                    with p.descriptor(g.FILE, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root):
                        raise ValueError("original")
        self.assertEqual(closed, list(reversed(opened)))
        self.assertEqual(len(caught.exception.__notes__), 2)
        for fd in opened:
            with self.assertRaises(OSError): os.fstat(fd)

    def test_export_authorization_and_reader_controls_on_new_profile(self):
        selected = [name for name in dir(export_tests.BoundedExportTests) if name.startswith("test_")
                    and name not in {"test_exact_fresh_profile_and_old_profile_compatibility",
                                     "test_cli_profiles_are_mutually_exclusive_before_state_creation"}]
        with patch.object(export_tests, "fresh", fresh):
            for name in selected:
                with self.subTest(control=name), fresh() as c:
                    getattr(export_tests.BoundedExportTests, name)(c)


if __name__ == "__main__":
    unittest.main()
