"""Fresh disposable guard controls; provider effects are explicitly simulated."""

from contextlib import closing
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from research_workspace import operation_guard as g
from research_workspace import store as storage
from research_workspace import runtime
from research_workspace.recipes import expected_result
from research_workspace.server import Application
from research_workspace.store import Store, canonical
from test_workspace import CSV, IMAGES


class FakeBroker:
    def __init__(self):
        self.projects = {}
        self.launched = threading.Event()
        self.resume = None

    def init_project(self, pid, budget, *, launch=None):
        def spawn():
            self.projects[pid] = budget
            self.launched.set()
        launch(spawn) if launch else spawn()
        if self.resume is not None and not self.resume.wait(3):
            raise RuntimeError("test initialization did not resume")

    def command(self, *args):
        return ["unexecuted-test-broker", *args]


class FaultDB:
    def __init__(self, db, *, rollback=False, close=False, commit=False, after_commit=False):
        self.db, self.rollback_error, self.close_error = db, rollback, close
        self.commit_error, self.after_commit = commit, after_commit

    def __getattr__(self, name):
        return getattr(self.db, name)

    def rollback(self):
        if self.rollback_error:
            raise OSError("injected rollback failure")
        return self.db.rollback()

    def close(self):
        self.db.close()
        if self.close_error:
            raise OSError("injected close failure")

    def commit(self):
        if self.after_commit:
            self.db.commit()
        if self.commit_error:
            raise OSError("injected commit failure")
        if not self.after_commit:
            self.db.commit()


class GuardFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "state"
        self.config = {"version": 1, "mode": "local-guarded-public-data-v1", "broker": sys.executable,
                       "images": IMAGES, "total_budget_cpu_seconds": 600}
        self.store = Store(self.root, create=True, guarded=True, config=self.config, subject="alice")
        self.key = self.store.initial_key
        self.broker = FakeBroker()
        self.data = {"name": "Synthetic guard control", "question": "Does uncertainty stop new work?",
                     "success_criterion": "Failures remain visible without replay.",
                     "ends_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7)).isoformat(),
                     "budget_cpu_seconds": 120}
        self.project = self.store.create_project("alice", self.data, self.broker, 600, "project-request")
        self.pid = self.project["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def submit(self, key="run-request"):
        return self.store.submit(self.pid, "alice", {"recipe": "summary-python", "condition": "baseline", "csv": CSV}, IMAGES, key)

    def reopened(self):
        # A separate process cannot inherit an active in-memory lease or latch.
        code = "import json,sys; from research_workspace.store import Store; print(json.dumps(Store(sys.argv[1]).operation_status()))"
        result = subprocess.run([sys.executable, "-c", code, str(self.root)], capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def assertPaused(self):
        self.assertEqual(self.store.operation_status()["state"], "paused")
        with self.assertRaises((g.GuardPaused, storage.AccessError)):
            self.submit("another-request")
        self.assertEqual(self.reopened()["state"], "paused")

    def settled(self):
        self.submit()
        run = self.store.claim()
        run.update(status="succeeded", metrics=expected_result(CSV, "baseline"), cleanup_verified=True,
                   compute={"reserved_cpu_seconds": 30, "charged_cpu_seconds": 30,
                            "provenance": "estimated_upper_bound", "unresolved_liability": False})
        return run


class GuardTests(GuardFixture):
    def test_success_joint_receipts_and_restart_barrier(self):
        run = self.settled()
        self.store.finish(run, run["metrics"])
        self.assertEqual(self.reopened()["state"], "ready")
        self.assertFalse(self.store.guard.lease.active)
        data = self.store.guard.load()
        self.assertTrue(all(row[2] == "committed" for row in self.store.guard.proof(data).values()))
        self.assertTrue(all(item["phase"] in {"prepared", "effect"} for item in data["operations"].values()))

    def test_completed_project_key_reuses_same_allocation(self):
        project = self.store.create_project("alice", self.data, self.broker, 600, "project-request")
        self.assertEqual(project, self.project)
        self.assertEqual(len(self.broker.projects), 1)
        with self.assertRaises(ValueError):
            self.store.create_project("alice", {**self.data, "name": "different"}, self.broker, 600, "project-request")

    def test_uncertain_initialization_cannot_allocate_again(self):
        with patch.object(self.broker, "init_project", side_effect=OSError("lost provider reply")):
            with self.assertRaises(OSError):
                self.store.create_project("alice", self.data, self.broker, 600, "uncertain-project")
        with self.assertRaises(g.GuardPaused):
            self.store.create_project("alice", self.data, self.broker, 600, "uncertain-project")
        self.assertEqual(len(self.broker.projects), 1)
        self.assertPaused()

    def test_initial_intent_failure_has_no_business_effect(self):
        before = self.store.project(self.pid, "alice")
        with patch.object(self.store.guard, "save", side_effect=OSError("intent write failed")):
            with self.assertRaises(OSError):
                self.submit()
        self.assertEqual(self.store.project(self.pid, "alice"), before)
        self.assertEqual(list((self.root / "runs").iterdir()), [])
        self.assertFalse(self.store.guard.lease.active)

    def test_prepared_intent_without_receipt_holds_on_restart(self):
        with patch.object(self.store.guard, "connect", side_effect=OSError("registration failed")), patch.object(g.Operation, "fail", autospec=True, side_effect=lambda token, *args: token.release()):
            with self.assertRaises(OSError):
                self.submit()
        self.assertPaused()

    def test_first_witness_claim_hold_race_cannot_change_queue(self):
        queued = self.submit()
        original = self.store.guard.begin
        def begin(*args, **kwargs):
            self.store.guard.lease.failure = "worker_failed"
            return original(*args, **kwargs)
        with patch.object(self.store.guard, "begin", side_effect=begin):
            with self.assertRaises(g.GuardPaused):
                self.store.claim()
        self.assertEqual(self.store.run(self.pid, queued["id"], "alice")["status"], "queued")

    def test_hold_after_preparation_fences_actual_spawn(self):
        self.submit()
        run = self.store.claim()
        original = runtime.private_write
        def write(path, content):
            original(path, content)
            self.store.guard.fail()
        with patch.object(runtime, "private_write", side_effect=write), patch.object(runtime.subprocess, "Popen") as spawn:
            with self.assertRaises(g.GuardPaused):
                runtime.Worker(self.store, self.broker).execute(run)
            spawn.assert_not_called()
        self.assertPaused()

    def test_launch_and_hold_creation_serialize(self):
        self.submit()
        run = self.store.claim()
        token = self.store.guard.running(run["id"])
        entered, leave, held = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def launch():
            try:
                token.launch(lambda: (entered.set(), leave.wait(3)))
            except BaseException as exc:
                errors.append(exc)
        first = threading.Thread(target=launch)
        first.start(); self.assertTrue(entered.wait(2))
        second = threading.Thread(target=lambda: (self.store.guard.fail(), held.set()))
        second.start()
        self.assertFalse(held.wait(.05))
        leave.set(); first.join(3); second.join(3)
        self.assertFalse(errors)
        self.assertTrue(held.is_set())
        self.assertPaused()

    def test_second_witness_rollback_and_failure_note_failure_release_lease(self):
        real = self.store._connect
        save = self.store.guard.save
        def fail_note(data):
            if any(item["phase"] == "uncertain" for item in data["operations"].values()):
                raise OSError("cannot write failure note")
            return save(data)
        with patch.object(self.store, "_connect", side_effect=lambda: FaultDB(real(), rollback=True)), patch.object(self.store.guard, "save", side_effect=fail_note):
            with self.assertRaises(ValueError):
                with self.store.transaction() as db:
                    db.execute("UPDATE users SET active=active WHERE subject='alice'")
                    raise ValueError("after effect")
        self.assertFalse(self.store.guard.lease.active)
        self.assertPaused()

    def test_close_failure_releases_lease_and_holds(self):
        real = self.store._connect
        with patch.object(self.store, "_connect", side_effect=lambda: FaultDB(real(), close=True)):
            with self.assertRaises(OSError):
                self.store.add_user("bob")
        self.assertFalse(self.store.guard.lease.active)
        self.assertPaused()

    def test_commit_before_and_after_faults_retain_uncertainty(self):
        for after in (False, True):
            with self.subTest(after=after):
                real = self.store._connect
                with patch.object(self.store, "_connect", side_effect=lambda: FaultDB(real(), commit=True, after_commit=after)):
                    with self.assertRaises(OSError):
                        self.store.logout("unused")
                self.assertFalse(self.store.guard.lease.active)
                self.assertPaused()

    def test_third_witness_payload_is_bounded_before_result_write(self):
        run = self.settled()
        run["error"] = "x" * 262144
        with self.assertRaises(ValueError):
            self.store.finish(run, run["metrics"])
        self.assertFalse((self.root / "runs" / run["id"] / "result.json").exists())
        self.assertPaused()

    def test_value_error_after_result_write_retains_file_and_hold(self):
        run = self.settled()
        original = storage.private_write
        def write(path, content):
            original(path, content)
            raise ValueError("injected after result publication")
        with patch.object(storage, "private_write", side_effect=write):
            with self.assertRaises(ValueError):
                self.store.finish(run, run["metrics"])
        self.assertTrue((self.root / "runs" / run["id"] / "result.json").exists())
        run.update(status="interrupted", metrics={}, error="retained uncertainty")
        self.store.finish(run)
        self.assertPaused()

    def test_fourth_witness_normal_init_overlap_is_temporary(self):
        self.broker.launched.clear()
        self.broker.resume = threading.Event()
        errors = []
        def initialize():
            try:
                self.store.create_project("alice", self.data, self.broker, 600, "overlap")
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=initialize)
        thread.start(); self.assertTrue(self.broker.launched.wait(2))
        with self.assertRaises(g.GuardBusy):
            self.store.claim()
        self.assertEqual(self.store.operation_status()["state"], "ready")
        # Revocation proves the provider wait does not hold the guard/DB locks.
        self.store.revoke_user("absent")
        self.broker.resume.set(); thread.join(3)
        self.assertFalse(errors)
        self.assertEqual(self.store.operation_status()["state"], "ready")
        self.assertEqual(self.reopened()["state"], "ready")

    def test_missing_or_old_sidecar_is_never_legacy(self):
        old = (self.root / g.FILE).read_bytes()
        self.store.add_user("bob")
        (self.root / g.FILE).write_bytes(old)
        self.assertPaused()
        (self.root / g.FILE).unlink()
        self.assertEqual(Store(self.root).operation_status()["state"], "paused")

    def test_missing_binding_is_refused(self):
        config = json.loads((self.root / "config.json").read_bytes())
        config.pop("operation_guard")
        config["mode"] = "local-public-data"
        (self.root / "config.json").write_text(canonical(config))
        with self.assertRaises(g.GuardPaused):
            Store(self.root)

    def test_ordinary_limit_preserves_essential_slot(self):
        with patch.object(g, "MAX_OPERATIONS", len(self.store.guard.load()["operations"])):
            with self.assertRaises(g.GuardPaused):
                self.submit()
            self.store.revoke_user("alice")
            self.store.logout("unused")
            self.assertLessEqual(sum(item["essential"] for item in self.store.guard.load()["operations"].values()), 1)
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT active FROM users WHERE subject='alice'").fetchone()[0], 0)

    def test_uncertain_essential_slot_is_not_cleared_by_later_safety_write(self):
        with self.assertRaises(ValueError):
            with self.store.transaction(essential=True) as db:
                db.execute("UPDATE users SET active=active")
                raise ValueError("uncertain safety write")
        before = [(oid, item) for oid, item in self.store.guard.load()["operations"].items() if item["essential"]]
        self.store.revoke_user("alice")
        self.store.logout("unused")
        self.assertEqual(before, [(oid, item) for oid, item in self.store.guard.load()["operations"].items() if item["essential"]])
        self.assertPaused()

    def test_essential_slot_reuse_stale_pair_holds(self):
        self.store.logout("one")
        old = (self.root / g.FILE).read_bytes()
        self.store.logout("two")
        (self.root / g.FILE).write_bytes(old)
        self.assertPaused()

    def test_pending_run_survives_fresh_process_without_replay(self):
        self.submit()
        self.store.claim()
        self.assertEqual(self.reopened()["state"], "paused")

    def test_unresolved_compute_is_not_a_terminal_receipt(self):
        self.submit()
        run = self.store.claim()
        run.update(status="interrupted", cleanup_verified=False)
        self.store.finish(run)
        data = self.store.guard.load()
        pending = [(oid, item) for oid, item in data["operations"].items() if item["kind"] == "run"]
        self.assertEqual(self.store.guard.proof(data)[pending[0][0]][2], "started")
        self.assertPaused()

    def test_rename_failure_preserves_temp_and_holds(self):
        with patch.object(g.os, "replace", side_effect=OSError("rename fault")):
            with self.assertRaises(OSError):
                self.submit()
        self.assertTrue(list(self.root.glob(g.TEMP + "*")))
        self.assertPaused()

    def test_postrename_parent_fsync_failure_is_pending_on_restart(self):
        with patch.object(g, "sync_directory", side_effect=OSError("parent sync fault")):
            with self.assertRaises(OSError):
                self.submit()
        self.assertFalse(self.store.guard.lease.active)
        self.assertPaused()

    def test_file_fsync_failure_has_no_business_effect(self):
        with patch.object(g.os, "fsync", side_effect=OSError("file sync fault")):
            with self.assertRaises(OSError):
                self.submit()
        self.assertEqual(list((self.root / "runs").iterdir()), [])
        self.assertPaused()

    def test_status_is_pure_and_does_not_call_broker(self):
        app = Application(self.root)
        before = (self.root / g.FILE).read_bytes()
        with patch.object(app.broker, "call", side_effect=AssertionError("status called broker")):
            status, value = app.dispatch("GET", "/api/operation-status", "alice", {}, None)
        self.assertEqual(status, 200)
        self.assertFalse(value["quota_enforced"])
        self.assertEqual(before, (self.root / g.FILE).read_bytes())

    def test_valid_held_store_serves_reads_and_logout(self):
        _, session = self.store.session(key=self.key)
        token = self.store.guard.begin("mutation")
        token.fail()
        app = Application(self.root)
        self.assertEqual(app.dispatch("GET", "/api/operation-status", "alice", {}, None)[1]["state"], "paused")
        self.assertEqual(app.store.session(token=session), "alice")
        app.store.logout(session)
        self.assertEqual(len(app.store.projects("alice")), 1)
        self.assertPaused()

    def test_worker_claim_and_double_settlement_failures_latch(self):
        worker = runtime.Worker(self.store, self.broker)
        def claim(*args, **kwargs):
            worker.stopped.set()
            raise OSError("claim failure")
        with patch.object(self.store, "claim", side_effect=claim):
            worker.loop()
        self.assertPaused()

    def test_proven_prelaunch_cancellation_and_integrity_refusal_are_settled(self):
        for cancelled in (True, False):
            with self.subTest(cancelled=cancelled):
                self.submit("prelaunch-" + str(cancelled))
                run = self.store.claim()
                if cancelled:
                    self.store.cancel(self.pid, run["id"], "alice")
                else:
                    path = self.root / "runs" / run["id"] / "input/data.csv"
                    path.chmod(0o600)
                    path.write_text("changed")
                with patch.object(runtime.subprocess, "Popen") as spawn:
                    runtime.Worker(self.store, self.broker).execute(run)
                    spawn.assert_not_called()
                saved = self.store.run(self.pid, run["id"], "alice")
                self.assertEqual(saved["compute"]["provenance"], "not_admitted")
                self.assertEqual(self.store.operation_status()["state"], "ready")
                self.assertEqual(self.reopened()["state"], "ready")

    def test_unknown_receipt_with_cleanup_and_charge_cannot_certify_completion(self):
        self.submit()
        run = self.store.claim()
        receipt = {"schema_version": 1, "request_id": run["id"], "project_id": self.pid,
                   "image": run["environment"]["image"], "admitted": True, "execution_started": True,
                   "cleanup_verified": True, "unresolved_liability": False, "status": "unknown",
                   "exit_code": 0, "stdout": "", "stderr": "", "error": "",
                   "compute": {"reserved_cpu_seconds": 30, "charged_cpu_seconds": 30, "provenance": "estimated_upper_bound"}}
        with patch.object(runtime.subprocess, "Popen") as spawn, patch.object(runtime, "collect", return_value=(canonical(receipt).encode(), b"")):
            spawn.return_value.returncode = 0
            runtime.Worker(self.store, self.broker).execute(run)
        self.assertEqual(self.store.run(self.pid, run["id"], "alice")["status"], "interrupted")
        self.assertPaused()

    def test_successful_submit_retry_is_read_only_even_at_intent_limit(self):
        run = self.submit()
        before = (self.root / g.FILE).read_bytes()
        count = sum(not item["essential"] for item in self.store.guard.load()["operations"].values())
        with patch.object(g, "MAX_OPERATIONS", count):
            self.assertEqual(self.store.operation_status()["reason"], "intent_limit")
            self.assertEqual(self.submit(), run)
        self.assertEqual(before, (self.root / g.FILE).read_bytes())

    def test_unsupported_receipt_schema_cannot_serve_as_ready(self):
        with closing(self.store._connect()) as db:
            db.executescript("ALTER TABLE operation_receipts RENAME TO old_receipts; CREATE TABLE operation_receipts AS SELECT * FROM old_receipts; DROP TABLE old_receipts;")
        self.assertPaused()

    def test_terminal_commit_close_failure_with_lost_note_needs_new_barrier(self):
        real = self.store._connect
        save = self.store.guard.save
        def no_failure_note(data):
            if any(item["phase"] == "uncertain" for item in data["operations"].values()):
                raise OSError("lost failure note")
            return save(data)
        with patch.object(self.store, "_connect", side_effect=lambda: FaultDB(real(), close=True)), patch.object(self.store.guard, "save", side_effect=no_failure_note):
            with self.assertRaises(OSError):
                self.store.add_user("durably-published")
        self.assertEqual(self.store.operation_status()["state"], "paused")
        self.assertFalse(self.store.guard.lease.active)
        # All business effects and their proof committed; a new successful real
        # authority transaction can recover that proof without replaying work.
        self.assertEqual(self.reopened()["state"], "ready")
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM users WHERE subject='durably-published'").fetchone()[0], 1)

    def test_essential_reuse_interruption_between_json_and_receipt_holds(self):
        self.store.logout("first-incarnation")
        with patch.object(self.store.guard, "connect", side_effect=OSError("receipt replacement failed")):
            with self.assertRaises(OSError):
                self.store.logout("second-incarnation")
        self.assertPaused()

    def test_last_admitted_slot_can_launch_project_and_run(self):
        count = lambda: sum(not item["essential"] for item in self.store.guard.load()["operations"].values())
        with patch.object(g, "MAX_OPERATIONS", count() + 1):
            project = self.store.create_project("alice", self.data, self.broker, 600, "last-project-slot")
            self.assertIn(project["id"], self.broker.projects)
            self.assertEqual(self.store.operation_status()["reason"], "intent_limit")
        self.submit()
        with patch.object(g, "MAX_OPERATIONS", count() + 1):
            run = self.store.claim()
            token = self.store.guard.running(run["id"])
            launched = []
            token.launch(lambda: launched.append("simulated launch boundary"))
            self.assertEqual(len(launched), 1)
            run.update(status="cancelled", cleanup_verified=True, compute={"reserved_cpu_seconds": 0,
                       "charged_cpu_seconds": 0, "provenance": "not_admitted", "unresolved_liability": False})
            self.store.finish(run)
            self.assertEqual(self.store.operation_status()["reason"], "intent_limit")
            self.assertEqual(self.store.guard.proof(self.store.guard.load())[token.identity][2], "committed")

    def test_essential_controls_refuse_unsupported_schema_without_business_write(self):
        with closing(self.store._connect()) as db:
            db.executescript("ALTER TABLE operation_receipts RENAME TO old_receipts; CREATE TABLE operation_receipts AS SELECT * FROM old_receipts; DROP TABLE old_receipts;")
        with self.assertRaises(g.GuardPaused):
            self.store.revoke_user("alice")
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT active FROM users WHERE subject='alice'").fetchone()[0], 1)
        self.assertFalse(self.store.guard.lease.active)

    def test_unexpected_trigger_refuses_essential_writes(self):
        with closing(self.store._connect()) as db:
            db.execute("CREATE TRIGGER unsupported AFTER UPDATE ON users BEGIN UPDATE users SET active=1; END")
        with self.assertRaises(g.GuardPaused):
            self.store.revoke_user("alice")
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT active FROM users WHERE subject='alice'").fetchone()[0], 1)

    def test_queued_claim_slot_cannot_be_consumed_by_other_work(self):
        count = sum(not item["essential"] for item in self.store.guard.load()["operations"].values())
        with patch.object(g, "MAX_OPERATIONS", count + 1):
            state = self.store.operation_status()
            self.assertTrue(state["can_create_project"])
            self.assertFalse(state["can_submit_run"])
            with self.assertRaises(g.GuardPaused):
                self.submit()
            self.assertEqual(self.store.project(self.pid, "alice")["runs"], [])
        with patch.object(g, "MAX_OPERATIONS", count + 2):
            queued = self.submit()
            with self.assertRaises(g.GuardPaused):
                self.store.add_user("cannot-consume-claim-slot")
            with self.assertRaises(g.GuardPaused):
                self.store.create_project("alice", self.data, self.broker, 600, "cannot-consume-slot")
            claimed = self.store.claim()
            self.assertEqual(claimed["id"], queued["id"])
            launches = []
            self.store.guard.running(claimed["id"]).launch(lambda: launches.append("simulated"))
            self.assertEqual(launches, ["simulated"])
            self.assertEqual(self.store.operation_status()["reason"], "intent_limit")

    def test_fresh_authority_parent_sync_failure_precedes_business_state(self):
        root = self.root.parent / "failed-bootstrap"
        with patch.object(storage, "sync_directory", side_effect=OSError("parent sync failed")):
            with self.assertRaises(OSError):
                Store(root, create=True, guarded=True, config=self.config, subject="alice")
        self.assertFalse((root / "workspace.sqlite3").exists())
        self.assertFalse((root / "config.json").exists())

    def test_zero_sql_changes_does_not_erase_attempted_effect(self):
        with self.assertRaises(ValueError):
            with self.store.transaction() as db:
                db.execute("UPDATE users SET active=0 WHERE subject='missing'")
                self.assertEqual(db.raw.total_changes, 0)
                raise ValueError("after attempted write")
        self.assertPaused()

    def test_failed_observation_after_spawn_is_never_not_admitted(self):
        queued = self.submit()
        worker = runtime.Worker(self.store, self.broker)
        def failed_collect(*args, **kwargs):
            worker.stopped.set()
            raise OSError("observer failed after spawn")
        with patch.object(runtime.subprocess, "Popen") as spawn, patch.object(runtime, "collect", side_effect=failed_collect):
            worker.loop()
            self.assertEqual(spawn.call_count, 1)
        saved = self.store.run(self.pid, queued["id"], "alice")
        self.assertEqual(saved["status"], "interrupted")
        self.assertTrue(saved["compute"]["unresolved_liability"])
        self.assertEqual(saved["compute"]["provenance"], "unknown")
        self.assertPaused()

    def test_double_settlement_failure_retains_run_intent(self):
        self.submit()
        worker = runtime.Worker(self.store, self.broker)
        def execute(run):
            worker.stopped.set()
            raise ValueError("first settlement failure")
        with patch.object(worker, "execute", side_effect=execute), patch.object(self.store, "finish", side_effect=OSError("fallback failed")):
            worker.loop()
        self.assertFalse(self.store.guard.lease.active)
        self.assertPaused()


if __name__ == "__main__":
    unittest.main()
