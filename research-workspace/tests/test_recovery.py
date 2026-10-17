"""Offline recovery controls; provider behavior here is explicitly simulated."""

from contextlib import closing
import datetime as dt
import fcntl
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from research_workspace import recovery as r
from research_workspace.recipes import expected_result
from research_workspace.server import Application
from research_workspace.store import AccessError, Store, canonical

IMAGES = json.loads((Path(__file__).resolve().parents[1] / "images.lock.json").read_text())
CSV = "group,value\n001,1\nNA,2\n"


class FakeBroker:
    """On-disk fake models the cutover contract, never provider execution."""
    fail_activation = False

    def __init__(self, executable, state):
        self.executable = str(executable)
        self.state = Path(state)

    def read(self):
        return json.loads((self.state / "authority.json").read_text())

    def write(self, value):
        self.state.mkdir(exist_ok=True, mode=0o700)
        r.atomic_json(self.state / "authority.json", value)

    def call(self, command, *args):
        options = dict(zip(args[::2], args[1::2]))
        if command == "restore":
            self.write(json.loads((Path(options["--checkpoint"]) / "authority.json").read_text()) | {"admission": "held"})
        if command == "activate":
            if self.fail_activation:
                raise ValueError("injected activation interruption")
            source = FakeBroker(self.executable, options["--source-state-dir"])
            source.write(source.read() | {"admission": "fenced"})
            self.write(self.read() | {"admission": "active"})
        value = self.read()
        report = {**value, "authority_sha256": r.sha(canonical(value).encode())}
        if command == "checkpoint":
            target = Path(options["--output"])
            target.mkdir(mode=0o700)
            (target / "authority.json").write_text(canonical(value))
            report["checkpoint_sha256"] = r.sha(canonical(value).encode())
        return report

    def init_project(self, pid, budget):
        value = self.read()
        value["projects"].append({"project_id": pid, "budget_cpu_seconds": budget, "charged_cpu_seconds": 30})
        self.write(value)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / "source"
        self.store = Store(self.state, create=True)
        r.atomic_json(self.state / "config.json", {"version": 1, "mode": "local-public-data", "broker": "/unused/test-broker", "images": IMAGES, "total_budget_cpu_seconds": 600})
        self.patch = patch.object(r, "Broker", FakeBroker)
        self.patch.start()
        FakeBroker.fail_activation = False
        self.broker = FakeBroker("unused", self.state / "broker")
        self.broker.write({"admission": "active", "unresolved": [], "projects": []})
        self.key = self.store.add_user("owner")
        self.revoked_key = self.store.add_user("revoked")
        _, self.token = self.store.session(key=self.key)
        self.project = self.store.create_project("owner", {"name": "Restore control", "question": "Are completed artifacts retained?", "success_criterion": "State and restrictions agree.", "ends_at": (dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=1)).isoformat(), "budget_cpu_seconds": 120}, self.broker, 600)
        self.pid = self.project["id"]
        self.store.membership(self.pid, "owner", "revoked", "viewer")
        self.store.revoke_user("revoked")
        self.run = self.submit()
        claimed = self.store.claim()
        claimed.update(status="succeeded", metrics=expected_result(CSV, "baseline"), cleanup_verified=True, compute={"reserved_cpu_seconds": 30, "charged_cpu_seconds": 30, "provenance": "estimated_upper_bound", "unresolved_liability": False})
        self.store.finish(claimed, claimed["metrics"])
        self.backup = self.root / "checkpoint"
        self.dest = self.root / "replacement"

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def submit(self):
        return self.store.submit(self.pid, "owner", {"recipe": "summary-python", "condition": "baseline", "csv": CSV}, IMAGES)

    def capture_restore(self):
        self.report = r.checkpoint(self.state, self.backup)
        return r.restore(self.backup, self.dest, trusted_sha256=self.report["checkpoint_sha256"], broker="unused")

    def activate(self):
        return r.activate(self.dest, self.state, self.backup, trusted_sha256=self.report["checkpoint_sha256"])

    def test_consistent_private_restore_retains_results_charges_and_revocations(self):
        self.store.close_project(self.pid, "owner")
        result = self.capture_restore()
        self.assertEqual(result["mode"], "recovery-held")
        self.assertEqual(result["run_counts"]["succeeded"], 1)
        self.assertEqual(result["broker"]["projects"][0]["charged_cpu_seconds"], 30)
        restored = Store(self.dest)
        self.assertEqual(restored.project(self.pid, "owner"), self.store.project(self.pid, "owner"))
        with self.assertRaises(AccessError):
            restored.project(self.pid, "revoked")
        self.assertEqual((self.dest / "runs" / self.run["id"] / "result.json").read_bytes(), (self.state / "runs" / self.run["id"] / "result.json").read_bytes())
        for path in self.dest.rglob("*"):
            if path.is_file():
                self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_cutover_clears_old_keys_sessions_and_never_revives_revoked_subjects(self):
        self.capture_restore()
        with self.assertRaises(ValueError):
            Application(self.dest)
        with self.assertRaises(ValueError):
            Store(self.dest).add_user("new")
        self.assertTrue(self.activate()["activated"])
        restored = Store(self.dest)
        for credential in ({"key": self.key}, {"token": self.token}, {"key": self.revoked_key}):
            with self.assertRaises(AccessError):
                restored.session(**credential)
        key = restored.rotate_user("owner")
        self.assertEqual(restored.session(key=key)[0], "owner")
        with self.assertRaises(ValueError):
            restored.rotate_user("revoked")
        with self.assertRaises(ValueError):
            self.store.add_user("cannot-reopen")
        with self.assertRaises(ValueError):
            Application(self.state)
        self.assertTrue(self.activate()["already_active"])

    def test_stale_membership_snapshot_cannot_reopen_access(self):
        self.capture_restore()
        self.store.revoke_user("owner")
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.activate()
        self.assertEqual(r.recovery_status(self.dest)["mode"], "recovery-held")

    def test_late_operator_revocation_cannot_write_retired_source(self):
        self.capture_restore()
        original = FakeBroker.call
        started = threading.Event()
        outcome = []
        thread = None

        def revoke():
            started.set()
            try:
                self.store.revoke_user("owner")
            except ValueError:
                outcome.append("explicitly_refused_on_retired_source")

        def concurrent(instance, command, *args):
            nonlocal thread
            if command == "activate":
                thread = threading.Thread(target=revoke)
                thread.start()
                self.assertTrue(started.wait(2))
            return original(instance, command, *args)

        with patch.object(FakeBroker, "call", concurrent):
            self.activate()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, ["explicitly_refused_on_retired_source"])
        # The operator must explicitly apply the refused action to the new
        # authority; it cannot silently mutate a stale original after cutover.
        Store(self.dest).revoke_user("owner")
        with self.assertRaises(AccessError):
            Store(self.dest).project(self.pid, "owner")

    def test_stale_compute_snapshot_cannot_refund_later_spending(self):
        self.capture_restore()
        value = self.broker.read()
        value["projects"][0]["charged_cpu_seconds"] = 60
        self.broker.write(value)
        with self.assertRaisesRegex(ValueError, "compute authority changed"):
            self.activate()

    def test_running_and_queued_work_never_replays_and_unknowns_remain_visible(self):
        running = self.submit()
        self.store.claim()
        queued = self.submit()
        value = self.broker.read()
        value["unresolved"] = [{"request_id": running["id"], "reserved_cpu_seconds": 30}]
        self.broker.write(value)
        report = self.capture_restore()
        self.assertEqual(report["run_counts"]["interrupted"], 1)
        self.assertEqual(report["run_counts"]["cancelled"], 1)
        self.assertTrue(report["unresolved_runs"][0]["compute"]["unresolved_liability"])
        self.assertFalse(report["automatic_replay"])
        self.assertEqual(Store(self.dest).run(self.pid, queued["id"], "owner")["compute"]["provenance"], "not_admitted")
        with self.assertRaisesRegex(ValueError, "liabilities"):
            self.activate()

    def test_changed_run_file_or_destination_is_refused(self):
        self.capture_restore()
        file = self.dest / "runs" / self.run["id"] / "result.json"
        file.write_text("{}")
        with self.assertRaisesRegex(ValueError, "replacement changed"):
            self.activate()

    def test_changed_source_artifact_refuses_stale_recovery(self):
        self.capture_restore()
        (self.state / "runs" / self.run["id"] / "result.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.activate()

    def test_crash_after_broker_cutover_before_application_admission_can_retry(self):
        self.capture_restore()
        original = r.atomic_json

        def interrupted(path, value):
            if Path(path) == self.dest / "recovery.json":
                raise OSError("injected after broker cutover")
            return original(path, value)

        with patch.object(r, "atomic_json", interrupted):
            with self.assertRaisesRegex(OSError, "injected"):
                self.activate()
        self.assertEqual(r.recovery_status(self.dest)["mode"], "recovery-held")
        self.assertEqual(r.recovery_status(self.state)["broker"]["admission"], "fenced")
        self.assertTrue(self.activate()["activated"])
        self.assertEqual(r.recovery_status(self.dest)["broker"]["projects"][0]["charged_cpu_seconds"], 30)

    def test_crash_between_active_marker_and_service_config_can_retry(self):
        self.capture_restore()
        original = r.atomic_json

        def interrupted(path, value):
            if Path(path) == self.dest / "config.json":
                raise OSError("injected before application admission")
            return original(path, value)

        with patch.object(r, "atomic_json", interrupted):
            with self.assertRaisesRegex(OSError, "injected"):
                self.activate()
        self.assertEqual(r.recovery_status(self.dest)["mode"], "recovery-held")
        self.assertEqual(r.recovery_status(self.dest)["recovery"]["phase"], "active")
        self.assertTrue(self.activate()["activated"])
        with self.assertRaises(AccessError):
            Store(self.dest).session(key=self.key)

    def test_checkpoint_and_restore_refuse_nested_authority_destinations(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            r.checkpoint(self.state, self.state / "nested")
        self.report = r.checkpoint(self.state, self.backup)
        for path in (self.state / "nested", self.backup / "nested"):
            with self.assertRaisesRegex(ValueError, "outside"):
                r.restore(self.backup, path, trusted_sha256=self.report["checkpoint_sha256"], broker="unused")

    def test_service_lock_prevents_snapshot_and_activation(self):
        with (self.state / "server.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "Stop this workspace"):
                r.checkpoint(self.state, self.backup)
        self.capture_restore()
        with (self.dest / "server.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "Stop this workspace"):
                self.activate()

    def test_failure_after_source_seal_leaves_both_stopped_and_same_pair_retries(self):
        self.capture_restore()
        FakeBroker.fail_activation = True
        with self.assertRaisesRegex(ValueError, "injected"):
            self.activate()
        self.assertEqual(r.recovery_status(self.state)["mode"], "retired")
        self.assertEqual(r.recovery_status(self.dest)["mode"], "recovery-held")
        FakeBroker.fail_activation = False
        self.assertTrue(self.activate()["activated"])

    def test_trusted_digest_replacement_tampering_and_existing_destination_refused(self):
        self.report = r.checkpoint(self.state, self.backup)
        with self.assertRaisesRegex(ValueError, "trusted digest"):
            r.restore(self.backup, self.dest, trusted_sha256="0"*64, broker="unused")
        (self.backup / "config.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "missing, replaced or unlisted"):
            r.restore(self.backup, self.dest, trusted_sha256=self.report["checkpoint_sha256"], broker="unused")
        self.assertFalse(self.dest.exists())

    def test_symlinks_hardlinks_special_files_and_bounds_fail_closed(self):
        (self.state / "runs" / "linked").symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, "linked"):
            r.checkpoint(self.state, self.backup)
        (self.state / "runs" / "linked").unlink()
        with patch.object(r, "MAX_FILE", 1):
            # read_file's default is frozen at definition, but inventory also
            # bounds aggregate bytes independently.
            with patch.object(r, "MAX_TOTAL", 1):
                with self.assertRaisesRegex(ValueError, "capacity"):
                    r.inventory(self.state / "runs")

    def test_no_restore_over_existing_authority(self):
        self.capture_restore()
        before = (self.dest / "config.json").read_bytes()
        with self.assertRaises(FileExistsError):
            r.restore(self.backup, self.dest, trusted_sha256=self.report["checkpoint_sha256"], broker="unused")
        self.assertEqual((self.dest / "config.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
