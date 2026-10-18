"""v2 checkpoint/cutover controls with an on-disk simulated provider."""

from contextlib import closing
import json
from pathlib import Path
from unittest.mock import patch

from research_workspace import recovery as r, guarded_recovery as gr, operation_guard as g
from research_workspace.store import Store, canonical
from test_operation_guard import GuardFixture
from test_recovery import FakeBroker


class Broker(FakeBroker):
    def call(self, command, *args, launch=None):
        if launch is not None:
            launch(lambda: None)
        return super().call(command, *args)


class GuardedRecoveryTests(GuardFixture):
    def setUp(self):
        GuardFixture.setUp(self)
        self.patch_r = patch.object(r, "Broker", Broker)
        self.patch_g = patch.object(gr, "Broker", Broker)
        self.patch_r.start(); self.patch_g.start()
        self.addCleanup(self.patch_r.stop); self.addCleanup(self.patch_g.stop)
        Broker.fail_activation = False
        self.provider = Broker("unused", self.root / "broker")
        self.provider.write({"admission": "active", "unresolved": [], "projects": [{"project_id": self.pid, "charged_cpu_seconds": 30}]})
        self.backup = self.root.parent / "checkpoint"
        self.destination = self.root.parent / "replacement"


    def capture(self):
        self.report = r.checkpoint(self.root, self.backup)
        return r.restore(self.backup, self.destination, trusted_sha256=self.report["checkpoint_sha256"], broker="unused")


    def activate(self):
        return r.activate(self.destination, self.root, self.backup, trusted_sha256=self.report["checkpoint_sha256"])


    def test_v2_preserves_guard_revocations_charges_and_no_replay(self):
        self.store.add_user("revoked")
        self.store.revoke_user("revoked")
        run = self.submit()
        result = self.capture()
        self.assertEqual(self.report["format"], r.GUARD_FORMAT)
        self.assertEqual(result["mode"], "recovery-held")
        restored = Store(self.destination, resume=False)
        self.assertEqual(restored.operation_status()["state"], "ready")
        self.assertEqual(restored.run(self.pid, run["id"], "alice")["status"], "cancelled")
        with closing(restored.connect()) as db:
            self.assertEqual(db.execute("SELECT active FROM users WHERE subject='revoked'").fetchone()[0], 0)
        self.assertEqual(result["broker"]["projects"][0]["charged_cpu_seconds"], 30)
        self.assertFalse(result["automatic_replay"])
        self.assertTrue(self.activate()["activated"])
        self.assertTrue(self.activate()["already_active"])
        self.assertEqual(Store(self.destination).operation_status()["state"], "ready")


    def test_pending_intent_blocks_activation_before_source_seal(self):
        token = self.store.guard.begin("mutation")
        token.fail()
        self.capture()
        with self.assertRaises(g.GuardPaused):
            self.activate()
        self.assertEqual(json.loads((self.root / "config.json").read_bytes())["mode"], "local-guarded-public-data-v1")
        self.assertEqual(Broker("unused", self.root / "broker").read()["admission"], "active")


    def test_guard_temp_files_are_copied_and_fingerprinted(self):
        (self.root / (g.TEMP + "failed")).write_bytes(b"retained partial intent")
        self.capture()
        self.assertEqual((self.destination / (g.TEMP + "failed")).read_bytes(), b"retained partial intent")
        with self.assertRaises(g.GuardPaused):
            self.activate()


    def test_stale_guard_sidecar_invalidates_source_fingerprint(self):
        self.capture()
        self.store.logout("safety-write-after-checkpoint")
        with self.assertRaises(ValueError):
            self.activate()
        self.assertEqual(json.loads((self.root / "config.json").read_bytes())["mode"], "local-guarded-public-data-v1")


    def test_provider_failure_leaves_paired_pending_intents_no_retry(self):
        self.capture()
        Broker.fail_activation = True
        try:
            with self.assertRaises(ValueError):
                self.activate()
        finally:
            Broker.fail_activation = False
        self.assertEqual(json.loads((self.root / "config.json").read_bytes())["mode"], "retired")
        self.assertEqual(Store(self.destination, resume=False).operation_status()["state"], "paused")
        with self.assertRaises(g.GuardPaused):
            self.activate()


    def test_active_config_before_terminal_proof_remains_paused(self):
        self.capture()
        original = r.atomic_json
        def write(path, value):
            original(path, value)
            if Path(path) == self.destination / "config.json" and value.get("mode") == "local-guarded-public-data-v1":
                raise OSError("after active config parent sync")
        with patch.object(r, "atomic_json", side_effect=write), patch.object(g.Operation, "fail", autospec=True, side_effect=lambda token, *args: token.release()):
            with self.assertRaises(OSError):
                self.activate()
        self.assertEqual(json.loads((self.destination / "config.json").read_bytes())["mode"], "local-guarded-public-data-v1")
        self.assertEqual(Store(self.destination).operation_status()["state"], "paused")
        with self.assertRaises(g.GuardPaused):
            self.activate()


    def test_cutover_terminal_commit_failure_stays_held(self):
        self.capture()
        original = gr._finish
        def finish(token, store):
            if store.root == self.destination:
                raise OSError("destination completion fault")
            return original(token, store)
        with patch.object(gr, "_finish", side_effect=finish):
            with self.assertRaises(OSError):
                self.activate()
        self.assertEqual(Store(self.destination, resume=False).operation_status()["state"], "paused")
        self.assertEqual(Store(self.root, resume=False).operation_status()["state"], "paused")


    def test_recovery_status_and_checkpoint_do_not_run_startup_barrier(self):
        before = self.store.guard.receipts()
        with closing(self.store.connect()) as db:
            barrier = db.execute("SELECT barrier FROM operation_authority").fetchone()[0]
        self.capture()
        r.recovery_status(self.root)
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT barrier FROM operation_authority").fetchone()[0], barrier)
        self.assertEqual(self.store.guard.receipts(), before)

    def test_cutover_can_use_last_reserved_ordinary_slots(self):
        self.capture()
        count = sum(not item["essential"] for item in self.store.guard.load()["operations"].values())
        with patch.object(g, "MAX_OPERATIONS", count + 1):
            self.assertTrue(self.activate()["activated"])
            self.assertEqual(Store(self.destination, resume=False).operation_status()["reason"], "intent_limit")
        self.assertEqual(Store(self.destination, resume=False).operation_status()["state"], "ready")

    def test_restore_parent_sync_failure_precedes_provider_and_copy(self):
        report = r.checkpoint(self.root, self.backup)
        sync = r.sync_directory
        def fail_parent(path):
            if Path(path) == self.destination.parent:
                raise OSError("replacement parent synchronization failed")
            sync(path)
        with patch.object(r, "sync_directory", side_effect=fail_parent), patch.object(Broker, "call") as provider:
            with self.assertRaises(OSError):
                r.restore(self.backup, self.destination, trusted_sha256=report["checkpoint_sha256"], broker="unused")
            provider.assert_not_called()
        self.assertFalse((self.destination / "workspace.sqlite3").exists())
        self.assertEqual(json.loads((self.root / "config.json").read_bytes())["mode"], "local-guarded-public-data-v1")
