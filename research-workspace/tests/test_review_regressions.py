"""Independent cross-module regressions for authority and result publication."""

import datetime as dt
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from research_workspace.evidence import build_bundle, verify_bundle
from research_workspace.recipes import expected_result
from research_workspace.runtime import Worker
from research_workspace.store import AccessError, Store


CSV = "group,value\na,1\na,2\na,3\na,4\na,100\n"
IMAGE = json.loads((Path(__file__).resolve().parents[1] / "images.lock.json").read_text())["python"]


class StubBroker:
    def init_project(self, project_id, budget):
        return {"project_id": project_id, "budget_cpu_seconds": budget}

    def command(self, *args):
        return ["unused-test-broker", *args]


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "state"
        self.store = Store(self.root, create=True)
        (self.root / "config.json").write_text("{}")
        self.store.add_user("owner")
        self.store.add_user("collaborator")
        self.broker = StubBroker()
        self.project = self.store.create_project("owner", {
            "name": "Review controls", "question": "Can another researcher inspect the records?",
            "success_criterion": "Exact inputs remain traceable, including failed work.",
            "ends_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).isoformat(),
            "budget_cpu_seconds": 300,
        }, self.broker, 600)
        self.pid = self.project["id"]

    def tearDown(self):
        self.temporary.cleanup()

    def submit(self):
        return self.store.submit(self.pid, "owner", {
            "recipe": "summary-python", "condition": "baseline", "csv": CSV, "parameters": {},
        }, {"python": IMAGE})

    def test_candidate_blocks_admission_and_queued_dispatch(self):
        from research_workspace import catalog
        entry = next(e for e in catalog.list_environments() if e['id'] == 'summary-python-v1')
        entry.pop('available')
        entry['status'] = 'candidate'
        with patch.object(catalog, 'resolve_environment', return_value=entry):
            with self.assertRaises(ValueError):
                self.submit()
        self.assertEqual(self.store.project(self.pid, 'owner')['runs'], [])
        self.submit()
        run = self.store.claim()
        with patch.object(catalog, 'resolve_environment', return_value=entry), patch('subprocess.Popen') as dispatch:
            with self.assertRaises(ValueError):
                Worker(self.store, self.broker).execute(run)
            dispatch.assert_not_called()

    def receipt(self, run):
        return {
            "schema_version": 1, "request_id": run["id"], "project_id": self.pid,
            "status": "succeeded", "admitted": True, "execution_started": True,
            "exit_code": 0, "error": "", "stdout": json.dumps(expected_result(CSV, "baseline")),
            "stderr": "", "cleanup_verified": True, "unresolved_liability": False,
            "image": IMAGE, "created_at": run["created_at"], "started_at": run["started_at"],
            "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "deadline": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)).isoformat(),
            "cancel_requested": False, "teardown_risk": "supervised-reaper",
            "compute": {"reserved_cpu_seconds": 30, "charged_cpu_seconds": 30, "provenance": "estimated_upper_bound"},
        }

    def execute_receipt(self, run, receipt, *, before_return=None, returncode=0):
        def collect(process, **kwargs):
            if before_return:
                before_return()
            return json.dumps(receipt).encode(), b""
        process = SimpleNamespace(returncode=returncode)
        with patch("research_workspace.runtime.subprocess.Popen", return_value=process), patch("research_workspace.runtime.collect", side_effect=collect):
            Worker(self.store, self.broker).execute(run)

    def test_queued_and_cancelled_runs_export_with_explicit_accounting(self):
        run = self.submit()
        report = verify_bundle(build_bundle(*self.store.snapshot(self.pid, "owner")))
        self.assertEqual(report["run_count"], 1)
        self.store.cancel(self.pid, run["id"], "owner")
        self.assertIsNone(self.store.claim())
        report = verify_bundle(build_bundle(*self.store.snapshot(self.pid, "owner")))
        self.assertTrue(any(g["code"] == "run_not_succeeded" for g in report["gaps"]))

    def test_restart_keeps_unknown_accounting_unknown(self):
        self.submit()
        run = self.store.claim()
        self.store.recover()
        restored = self.store.run(self.pid, run["id"], "owner")
        self.assertEqual(restored["status"], "interrupted")
        self.assertEqual(restored["compute"]["provenance"], "unknown")
        self.assertIsNone(restored["compute"]["reserved_cpu_seconds"])
        self.assertIsNone(restored["compute"]["charged_cpu_seconds"])
        self.assertIs(restored["compute"]["unresolved_liability"], True)
        self.assertTrue(verify_bundle(build_bundle(*self.store.snapshot(self.pid, "owner")))["valid"])

    def test_full_success_receipt_publishes_independently_checked_output(self):
        self.submit()
        run = self.store.claim()
        self.execute_receipt(run, self.receipt(run))
        saved = self.store.run(self.pid, run["id"], "owner")
        self.assertEqual(saved["status"], "succeeded")
        self.assertEqual(saved["metrics"], expected_result(CSV, "baseline"))
        self.assertTrue(verify_bundle(build_bundle(*self.store.snapshot(self.pid, "owner")))["valid"])

    def test_malformed_success_receipts_cannot_publish(self):
        mutations = {
            "missing_compute": lambda r: r.pop("compute"),
            "negative_charge": lambda r: r["compute"].update(charged_cpu_seconds=-30),
            "wrong_image": lambda r: r.update(image="sha256:" + "9" * 64),
            "nonzero_exit": lambda r: r.update(exit_code=7),
            "wrong_schema": lambda r: r.update(schema_version=99),
            "not_admitted": lambda r: r.update(admitted=False),
            "not_started": lambda r: r.update(execution_started=False),
            "falsey_nonboolean_liability": lambda r: r.update(unresolved_liability=0),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                self.submit()
                run = self.store.claim()
                receipt = self.receipt(run)
                mutate(receipt)
                try:
                    self.execute_receipt(run, receipt)
                except ValueError:
                    pass
                saved = self.store.run(self.pid, run["id"], "owner")
                self.assertNotEqual(saved["status"], "succeeded")
                self.assertFalse((self.root / "runs" / run["id"] / "result.json").exists())

    def test_broker_process_failure_cannot_publish_success_receipt(self):
        self.submit()
        run = self.store.claim()
        try:
            self.execute_receipt(run, self.receipt(run), returncode=2)
        except ValueError:
            pass
        self.assertNotEqual(self.store.run(self.pid, run["id"], "owner")["status"], "succeeded")
        self.assertFalse((self.root / "runs" / run["id"] / "result.json").exists())

    def test_project_close_during_compute_prevents_result_publication(self):
        self.submit()
        run = self.store.claim()
        self.execute_receipt(run, self.receipt(run), before_return=lambda: self.store.close_project(self.pid, "owner"))
        saved = self.store.run(self.pid, run["id"], "owner")
        self.assertEqual(saved["status"], "cancelled")
        self.assertEqual(saved["metrics"], {})
        self.assertFalse((self.root / "runs" / run["id"] / "result.json").exists())

    def test_revoked_subject_cannot_read_project_index(self):
        self.store.revoke_user("owner")
        try:
            result = self.store.projects("owner")
        except AccessError:
            return
        self.assertEqual(result, [])

    def test_membership_removal_blocks_every_project_read_surface(self):
        self.store.membership(self.pid, "owner", "collaborator", "viewer")
        run = self.submit()
        self.store.membership(self.pid, "owner", "collaborator")
        for action in (
            lambda: self.store.project(self.pid, "collaborator"),
            lambda: self.store.run(self.pid, run["id"], "collaborator"),
            lambda: self.store.snapshot(self.pid, "collaborator"),
        ):
            with self.assertRaises(AccessError):
                action()

    def test_project_reads_close_database_connections(self):
        opened = []
        connect = self.store.connect
        def tracked_connect():
            connection = connect()
            opened.append(connection)
            return connection
        with patch.object(self.store, "connect", side_effect=tracked_connect):
            self.store.projects("owner")
            self.store.project(self.pid, "owner")
        for connection in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_broker_deadline_terminates_observation_without_refunding_unknown_work(self):
        self.submit()
        run = self.store.claim()
        import sys
        self.broker.command = lambda *args: [sys.executable, '-c', 'import time; time.sleep(30)']
        from research_workspace.bounded_process import collect
        def short_deadline(process, **kwargs):
            return collect(process, timeout=0.05)
        with patch("research_workspace.runtime.collect", side_effect=short_deadline):
            with self.assertRaisesRegex(ValueError, 'deadline'):
                Worker(self.store, self.broker).execute(run)
        saved = self.store.run(self.pid, run["id"], "owner")
        self.assertIs(saved["compute"]["unresolved_liability"], True)
        self.assertIsNone(saved["compute"]["charged_cpu_seconds"])
        self.assertFalse((self.root / "runs" / run["id"] / "result.json").exists())


if __name__ == "__main__":
    unittest.main()
