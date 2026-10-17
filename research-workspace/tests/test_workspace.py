import contextlib
import datetime as dt
import json
from pathlib import Path
import tempfile
import threading
import unittest

from research_workspace.recipes import expected_result, parse_csv, validate_result
from research_workspace.store import AccessError, Store, canonical

CSV = "group,value\na,1\na,2\na,3\na,4\na,100\n"
IMAGES = {"python": "sha256:" + "1" * 64, "r": "sha256:" + "2" * 64}


class Broker:
    def __init__(self):
        self.projects = {}

    def init_project(self, project, budget):
        self.projects[project] = budget

    def usage(self, project):
        return {"budget_cpu_seconds": self.projects[project], "remaining_cpu_seconds": self.projects[project]}


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "state"
        self.store = Store(self.root, create=True)
        (self.root / "config.json").write_text('{}')
        self.alice = self.store.add_user("alice")
        self.bob = self.store.add_user("bob")
        self.broker = Broker()
        self.data = {"name": "A public-data comparison", "question": "How does trimming change the descriptive mean?",
                     "success_criterion": "Both methods return independently verified results.",
                     "ends_at": (dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=7)).isoformat(), "budget_cpu_seconds": 120}
        self.project = self.store.create_project("alice", self.data, self.broker, 600)
        self.pid = self.project["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def submit(self, subject="alice", **changes):
        data = {"recipe": "summary-python", "condition": "baseline", "csv": CSV, "parameters": {}}
        data.update(changes)
        return self.store.submit(self.pid, subject, data, IMAGES)

    def test_credentials_are_hashed_and_sessions_revoked(self):
        subject, token = self.store.session(key=self.alice)
        self.assertEqual(subject, "alice")
        self.assertEqual(self.store.session(token=token), "alice")
        with contextlib.closing(self.store.connect()) as db:
            self.assertNotIn(self.alice, str([tuple(row) for row in db.execute("SELECT * FROM users")]))
        self.store.revoke_user("alice")
        with self.assertRaises(AccessError):
            self.store.session(token=token)
        with self.assertRaises(AccessError):
            self.store.session(key=self.alice)

    def test_cross_project_read_submit_export_refused(self):
        run = self.submit()
        for action in (lambda: self.store.project(self.pid, "bob"), lambda: self.store.run(self.pid, run["id"], "bob"),
                       lambda: self.store.snapshot(self.pid, "bob"), lambda: self.submit("bob")):
            with self.assertRaises(AccessError):
                action()

    def test_viewer_cannot_submit_or_manage(self):
        self.store.membership(self.pid, "alice", "bob", "viewer")
        self.assertEqual(self.store.project(self.pid, "bob")["role"], "viewer")
        with self.assertRaises(AccessError):
            self.submit("bob")
        with self.assertRaises(AccessError):
            self.store.close_project(self.pid, "bob")

    def test_removal_cancels_queued_work_and_export_access(self):
        self.store.membership(self.pid, "alice", "bob", "editor")
        run = self.submit("bob")
        self.store.membership(self.pid, "alice", "bob")
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.run(self.pid, run["id"], "alice")["status"], "cancelled")
        with self.assertRaises(AccessError):
            self.store.snapshot(self.pid, "bob")

    def test_demoted_editor_cannot_publish_running_result(self):
        self.store.membership(self.pid, "alice", "bob", "editor")
        self.submit("bob")
        run = self.store.claim()
        self.store.membership(self.pid, "alice", "bob", "viewer")
        run.update(status="succeeded", metrics=expected_result(CSV, "baseline"), cleanup_verified=True)
        self.store.finish(run, run["metrics"])
        saved = self.store.run(self.pid, run["id"], "alice")
        self.assertEqual(saved["status"], "cancelled")
        self.assertEqual(saved["metrics"], {})
        self.assertFalse((self.root / "runs" / run["id"] / "result.json").exists())

    def test_close_prevents_dispatch_and_new_runs(self):
        self.submit()
        self.store.close_project(self.pid, "alice")
        self.assertIsNone(self.store.claim())
        with self.assertRaises(ValueError):
            self.submit()

    def test_owner_and_unprovisioned_members_are_protected(self):
        with self.assertRaises(ValueError):
            self.store.membership(self.pid, "alice", "alice")
        with self.assertRaises(ValueError):
            self.store.membership(self.pid, "alice", "unprovisioned", "editor")

    def test_idempotency_collision_and_repeat(self):
        data = {"recipe": "summary-python", "condition": "baseline", "csv": CSV}
        first = self.store.submit(self.pid, "alice", data, IMAGES, "request-1")
        again = self.store.submit(self.pid, "alice", data, IMAGES, "request-1")
        self.assertEqual(first["id"], again["id"])
        with self.assertRaises(ValueError):
            self.store.submit(self.pid, "alice", dict(data, condition="alternative"), IMAGES, "request-1")

    def test_restart_retains_inputs_and_does_not_retry_running_job(self):
        self.submit()
        run = self.store.claim()
        restarted = Store(self.root)
        restarted.recover()
        saved = restarted.run(self.pid, run["id"], "alice")
        self.assertEqual(saved["status"], "interrupted")
        self.assertFalse(saved["cleanup_verified"])
        self.assertIsNone(restarted.claim())
        self.assertEqual((self.root / "runs" / run["id"] / "input/data.csv").read_text(), CSV)

    def test_global_project_budget_is_atomic(self):
        errors = []
        barrier = threading.Barrier(2)
        def create():
            barrier.wait()
            try:
                self.store.create_project("alice", self.data, self.broker, 240)
            except ValueError as exc:
                errors.append(str(exc))
        threads = [threading.Thread(target=create) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(len(errors), 1)
        self.assertEqual(len(self.store.projects("alice")), 2)

    def test_missing_authority_cannot_be_silently_recreated(self):
        (self.root / "config.json").unlink()
        with self.assertRaises(ValueError):
            Store(self.root)
        with self.assertRaises(ValueError):
            Store(self.root, create=True)

    def test_snapshot_contains_no_auth_or_private_receipts(self):
        run = self.submit()
        (self.root / "runs" / run["id"] / "receipt.private.json").write_text('{"secret":"private"}')
        project, runs, files = self.store.snapshot(self.pid, "alice")
        self.assertEqual(len(files), 2)
        self.assertNotIn("private", " ".join(files))
        self.assertNotIn("subject", runs[0])

    def test_only_reviewed_recipes_and_finite_data(self):
        for changes in ({"recipe": "shell"}, {"parameters": {"command": "evil"}}, {"condition": "arbitrary"},
                        {"csv": "group,value\na,nan\n"}, {"csv": "group,value\n../../x,1\n"},
                        {"csv": "group,value\na,1,2\n"}):
            with self.assertRaises(ValueError):
                self.submit(**changes)

    def test_independent_result_checker_rejects_plausible_wrong_output(self):
        expected = expected_result(CSV, "baseline")
        validate_result(expected, CSV, "baseline")
        self.assertEqual(expected_result(CSV, "alternative")["mean"], 3)
        for wrong in (dict(expected, mean=3), dict(expected, count=True), dict(expected, mean=float("nan"))):
            with self.assertRaises(ValueError):
                validate_result(wrong, CSV, "baseline")


if __name__ == "__main__":
    unittest.main()
