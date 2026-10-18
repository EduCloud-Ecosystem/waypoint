"""Current local authority at the last spawn boundary; no real processes."""
from contextlib import closing, contextmanager
import copy
import datetime as dt
import json
import threading
import unittest
from unittest.mock import Mock, patch

from research_workspace import runtime, store as storage
from research_workspace.identity import Identity
from research_workspace.recipes import expected_result
from research_workspace.store import Store, canonical
from test_identity import config as identity_config
from test_operation_guard import GuardFixture, FakeBroker, FaultDB
import test_workspace as workspace
from test_workspace import CSV, IMAGES


REAL_DATETIME = dt.datetime


class Clock(REAL_DATETIME):
    advanced = False

    @classmethod
    def now(cls, tz=None):
        value = REAL_DATETIME.now(tz)
        return value + dt.timedelta(days=8) if cls.advanced else value


class DispatchCases:
    def setUp(self):
        self.fixture = GuardFixture() if self.guarded else workspace.WorkspaceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store, self.pid = self.fixture.store, self.fixture.pid
        if self.guarded:
            self.store.add_user("bob")
        self.store.membership(self.pid, "alice", "bob", "editor")
        self.store.submit(self.pid, "bob", {"recipe": "summary-python", "condition": "baseline", "csv": CSV}, IMAGES, "dispatch")
        self.run = self.store.claim()
        self.broker = FakeBroker()
        self.directory = self.store.root / "runs" / self.run["id"]
        Clock.advanced = False
        self.addCleanup(setattr, Clock, "advanced", False)

    def deny(self, action, store=None):
        store = store or self.store
        if action == "cancel": store.cancel(self.pid, self.run["id"], "alice")
        elif action == "revoke": store.revoke_user("bob")
        elif action == "close": store.close_project(self.pid, "alice")
        elif action == "remove": store.membership(self.pid, "alice", "bob")
        elif action == "demote": store.membership(self.pid, "alice", "bob", "viewer")
        elif action == "expire": Clock.advanced = True
        else: raise AssertionError(action)

    def saved(self):
        return self.store.run(self.pid, self.run["id"], "alice")

    def assert_denied(self, charged=0):
        saved = self.saved()
        self.assertEqual(saved["status"], "cancelled")
        self.assertTrue(saved["cleanup_verified"])
        self.assertEqual(saved["metrics"], {})
        self.assertEqual(saved["compute"], {"reserved_cpu_seconds": charged, "charged_cpu_seconds": charged,
                         "provenance": "estimated_upper_bound" if charged else "not_admitted", "unresolved_liability": False})
        self.assertFalse((self.directory / "result.json").exists())
        self.assertTrue((self.directory / "request.private.json").is_file())
        self.assertEqual((self.directory / "input/data.csv").read_text(), CSV)
        if self.guarded:
            self.assertEqual(self.store.operation_status()["state"], "ready")
            self.assertEqual(self.fixture.reopened()["state"], "ready")

    def after_write(self, callback):
        original = runtime.private_write
        def write(path, content):
            original(path, content)
            if path.name == "request.private.json": callback()
        return patch.object(runtime, "private_write", side_effect=write)

    def receipt(self):
        return canonical({"schema_version": 1, "request_id": self.run["id"], "project_id": self.pid,
            "image": self.run["environment"]["image"], "status": "succeeded", "admitted": True,
            "execution_started": True, "cleanup_verified": True, "unresolved_liability": False,
            "stdout": canonical(expected_result(CSV, "baseline")), "stderr": "", "error": "", "exit_code": 0,
            "compute": {"reserved_cpu_seconds": 30, "charged_cpu_seconds": 30, "provenance": "estimated_upper_bound"}}).encode()

    def thread(self, callback, errors):
        def execute():
            try: callback()
            except BaseException as exc: errors.append(exc)
        thread = threading.Thread(target=execute, daemon=True)
        thread.start()
        return thread

    def late_denial(self, action):
        with patch.object(storage.dt, "datetime", Clock), self.after_write(lambda: self.deny(action)), patch.object(runtime.subprocess, "Popen") as spawn:
            runtime.Worker(self.store, self.broker).execute(self.run)
            spawn.assert_not_called()
        self.assert_denied()

    def control_first(self, action):
        # A distinct Store commits its denial while the worker waits at the
        # last fence. The write reservation is shared across connections.
        other = Store(self.store.root, resume=False)
        prepared, enter_control, commit_control, try_launch = (threading.Event() for _ in range(4))
        errors = []
        original = other.transaction
        @contextmanager
        def transaction(*args, **kwargs):
            with original(*args, **kwargs) as db:
                yield db
                enter_control.set()
                if not commit_control.wait(3): raise AssertionError("control not released")
        def prepared_wait():
            prepared.set()
            if not try_launch.wait(3): raise AssertionError("dispatch not released")
        with patch.object(other, "transaction", transaction), self.after_write(prepared_wait), patch.object(runtime.subprocess, "Popen") as spawn:
            worker = self.thread(lambda: runtime.Worker(self.store, self.broker).execute(self.run), errors)
            self.assertTrue(prepared.wait(2))
            control = self.thread(lambda: self.deny(action, other), errors)
            self.assertTrue(enter_control.wait(2))
            try_launch.set()
            self.assertFalse(commit_control.wait(.03))
            spawn.assert_not_called()
            commit_control.set()
            worker.join(3); control.join(3)
            self.assertFalse(worker.is_alive() or control.is_alive())
            spawn.assert_not_called()
        self.assertEqual(errors, [])
        self.assert_denied()

    def launch_first(self, action):
        other = Store(self.store.root, resume=False)
        spawned, leave_spawn, control_started, control_done = (threading.Event() for _ in range(4))
        errors = []
        process = Mock(returncode=0)
        def spawn(*args, **kwargs):
            spawned.set()
            if not leave_spawn.wait(3): raise AssertionError("spawn not released")
            return process
        def control():
            control_started.set()
            self.deny(action, other)
            control_done.set()
        def collect(*args, **kwargs):
            # Safety controls must finish while the provider is being awaited.
            if not control_done.wait(3): raise AssertionError("control blocked during provider wait")
            return self.receipt(), b""
        with patch.object(runtime.subprocess, "Popen", side_effect=spawn) as popen, patch.object(runtime, "collect", side_effect=collect):
            worker = self.thread(lambda: runtime.Worker(self.store, self.broker).execute(self.run), errors)
            self.assertTrue(spawned.wait(2))
            controller = self.thread(control, errors)
            self.assertTrue(control_started.wait(2))
            self.assertFalse(control_done.wait(.05))
            leave_spawn.set()
            worker.join(3); controller.join(3)
            self.assertFalse(worker.is_alive() or controller.is_alive())
            self.assertEqual(popen.call_count, 1)
        self.assertEqual(errors, [])
        self.assert_denied(charged=30)
        self.assertTrue((self.directory / "receipt.private.json").is_file())

    def test_expiry_after_wait_for_identity_publication(self):
        waiting, release = threading.Event(), threading.Event()
        errors = []
        @contextmanager
        def publication(run_id):
            waiting.set()
            if not release.wait(3): raise AssertionError("identity fence not released")
            yield True
        identity = Mock()
        identity.publication = publication
        with patch.object(storage.dt, "datetime", Clock), patch.object(runtime.subprocess, "Popen") as spawn:
            worker = self.thread(lambda: runtime.Worker(self.store, self.broker, identity).execute(self.run), errors)
            self.assertTrue(waiting.wait(2))
            Clock.advanced = True
            release.set()
            worker.join(3)
            self.assertFalse(worker.is_alive())
            spawn.assert_not_called()
        self.assertEqual(errors, [])
        self.assert_denied()

    def test_superseded_recovery_retains_unknown_accounting(self):
        with self.after_write(self.store.recover), patch.object(runtime.subprocess, "Popen") as spawn:
            runtime.Worker(self.store, self.broker).execute(self.run)
            spawn.assert_not_called()
        self.assertEqual(self.saved()["status"], "interrupted")
        self.assertEqual(self.saved()["compute"], storage.unknown_compute())
        self.assertFalse(self.saved()["cleanup_verified"])
        if self.guarded:
            self.assertEqual(self.store.operation_status()["state"], "paused")

    def test_superseded_completion_retains_result_and_charge(self):
        settled = copy.deepcopy(self.run)
        result = expected_result(CSV, "baseline")
        settled.update(status="succeeded", metrics=result, cleanup_verified=True,
                       compute={"reserved_cpu_seconds": 30, "charged_cpu_seconds": 30,
                                "provenance": "estimated_upper_bound", "unresolved_liability": False})
        with self.after_write(lambda: self.store.finish(settled, result)), patch.object(runtime.subprocess, "Popen") as spawn:
            runtime.Worker(self.store, self.broker).execute(self.run)
            spawn.assert_not_called()
        self.assertEqual(self.saved()["status"], "succeeded")
        self.assertEqual(self.saved()["compute"]["charged_cpu_seconds"], 30)
        self.assertEqual(json.loads((self.directory / "result.json").read_text()), result)

    def refusal_settlement(self, refusal):
        identity = None
        if refusal == "identity":
            identity = Mock()
            identity.check_run.side_effect = storage.AccessError("identity ended")
        elif refusal == "integrity":
            (self.directory / "input/data.csv").chmod(0o600)
            (self.directory / "input/data.csv").write_text("modified")
        elif refusal == "early_cancel":
            self.deny("cancel")
        original = self.store.finish
        def settle(*args, **kwargs):
            self.store.recover()
            return original(*args, **kwargs)
        control = self.after_write(lambda: self.deny("cancel")) if refusal == "final_cancel" else patch.object(runtime, "private_write", wraps=runtime.private_write)
        with control, patch.object(self.store, "finish", side_effect=settle), patch.object(runtime.subprocess, "Popen") as spawn:
            runtime.Worker(self.store, self.broker, identity).execute(self.run)
            spawn.assert_not_called()
        self.assertEqual(self.saved()["status"], "interrupted")
        self.assertEqual(self.saved()["compute"], storage.unknown_compute())
        self.assertFalse(self.saved()["cleanup_verified"])
        if self.guarded:
            self.assertEqual(self.store.operation_status()["state"], "paused")

    def test_late_settlement_cannot_overwrite_completed_charge(self):
        settled = copy.deepcopy(self.run)
        result = expected_result(CSV, "baseline")
        settled.update(status="succeeded", metrics=result, cleanup_verified=True,
                       compute={"reserved_cpu_seconds": 30, "charged_cpu_seconds": 30,
                                "provenance": "estimated_upper_bound", "unresolved_liability": False})
        original = self.store.finish
        def settle(*args, **kwargs):
            original(settled, result)
            return original(*args, **kwargs)
        identity = Mock()
        identity.check_run.side_effect = storage.AccessError("identity ended")
        with patch.object(self.store, "finish", side_effect=settle), patch.object(runtime.subprocess, "Popen") as spawn:
            runtime.Worker(self.store, self.broker, identity).execute(self.run)
            spawn.assert_not_called()
        self.assertEqual(self.saved()["status"], "succeeded")
        self.assertEqual(self.saved()["compute"]["charged_cpu_seconds"], 30)
        self.assertEqual(json.loads((self.directory / "result.json").read_text()), result)

    def test_held_and_retired_configuration_cannot_launch(self):
        original = (self.store.root / "config.json").read_bytes()
        for mode in ("recovery-held", "retired"):
            config = json.loads(original); config["mode"] = mode
            (self.store.root / "config.json").write_text(canonical(config))
            with self.subTest(mode=mode), patch.object(runtime.subprocess, "Popen") as spawn:
                # The guard may already hold from the first refused lifecycle.
                try: self.store.launch_run(self.run, spawn)
                except (storage.DispatchSuperseded, storage.GuardPaused): pass
                else: self.fail("held authority must refuse dispatch")
                spawn.assert_not_called()
        self.assertEqual(self.saved()["compute"], storage.unknown_compute())

    def test_spawn_attempt_failure_retains_unknown_liability(self):
        worker = runtime.Worker(self.store, self.broker)
        # Feed the already claimed run once through the actual failure handler.
        def failed_spawn(*args, **kwargs):
            worker.stopped.set()
            raise OSError("spawn outcome unknown")
        with patch.object(self.store, "claim", return_value=self.run), patch.object(runtime.subprocess, "Popen", side_effect=failed_spawn) as spawn:
            worker.loop()
            self.assertEqual(spawn.call_count, 1)
        self.assertEqual(self.saved()["status"], "interrupted")
        self.assertEqual(self.saved()["compute"], storage.unknown_compute())
        self.assertTrue((self.directory / "request.private.json").is_file())
        if self.guarded:
            self.assertEqual(self.store.operation_status()["state"], "paused")

    def cleanup_failure(self, failure):
        real = self.store._connect
        process = Mock(returncode=None)
        process.poll.return_value = None
        worker = runtime.Worker(self.store, self.broker)
        armed = False
        def connect():
            nonlocal armed
            db = real()
            if armed:
                armed = False
                return FaultDB(db, **{failure: True})
            return db
        def arm():
            nonlocal armed
            armed = True
        def spawned(*args, **kwargs):
            worker.stopped.set()
            return process
        with self.after_write(arm), patch.object(self.store, "_connect", side_effect=connect), patch.object(self.store, "claim", return_value=self.run), patch.object(runtime.subprocess, "Popen", side_effect=spawned) as spawn, patch.object(runtime, "collect") as collect:
            worker.loop()
            spawn.assert_called_once()
            collect.assert_not_called()
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)
        process.stdout.close.assert_called_once_with()
        process.stderr.close.assert_called_once_with()
        self.assertEqual(self.saved()["compute"], storage.unknown_compute())
        self.assertEqual(self.saved()["status"], "interrupted")

    def test_rollback_failure_after_spawn_is_not_a_refusal(self):
        self.cleanup_failure("rollback")

    def test_close_failure_after_spawn_is_not_a_refusal(self):
        self.cleanup_failure("close")

    def test_legitimate_public_run(self):
        with patch.object(runtime.subprocess, "Popen", return_value=Mock(returncode=0)) as spawn, patch.object(runtime, "collect", return_value=(self.receipt(), b"")):
            runtime.Worker(self.store, self.broker).execute(self.run)
            spawn.assert_called_once()
        self.assertEqual(self.saved()["status"], "succeeded")
        self.assertEqual(self.saved()["compute"]["charged_cpu_seconds"], 30)
        self.assertTrue((self.directory / "result.json").is_file())

    def test_legitimate_oidc_run(self):
        # Local authority is the same boundary in both public and OIDC modes.
        cfg = identity_config()
        identity = Identity(cfg, self.store, clock=lambda: 1002)
        claims = {"active": True, "iss": cfg["issuer"], "aud": ["research"], "sub": "external-bob",
                  "sid": "synthetic-session", "iat": 1002, "auth_time": 1002, "exp": 1600}
        identity._browser_token = lambda cookie: "synthetic-token"
        identity._request = lambda request: claims
        # Startup timestamp is 1002; issued-at must follow it.
        identity.clock = lambda: 1003
        claims.update(iat=1003, auth_time=1003)
        _, cookie = identity.begin("synthetic-cookie")
        context = identity.authenticate(cookie, "synthetic-cookie")
        identity.bind(self.run["id"], context)
        config_path = self.store.root / "config.json"
        cfg = json.loads(config_path.read_text())
        cfg["mode"] = "local-guarded-synthetic-oidc-v1" if self.guarded else "local-synthetic-oidc-v1"
        config_path.write_text(canonical(cfg))
        with patch.object(runtime.subprocess, "Popen", return_value=Mock(returncode=0)) as spawn, patch.object(runtime, "collect", return_value=(self.receipt(), b"")):
            runtime.Worker(self.store, self.broker, identity).execute(self.run)
            spawn.assert_called_once()
        self.assertEqual(self.saved()["status"], "succeeded")
        self.assertEqual(self.saved()["compute"]["charged_cpu_seconds"], 30)
        self.assertTrue((self.directory / "result.json").is_file())


for action in ("cancel", "revoke", "close", "remove", "demote", "expire"):
    setattr(DispatchCases, "test_late_" + action, lambda self, action=action: self.late_denial(action))
    if action != "expire":
        setattr(DispatchCases, "test_control_first_" + action, lambda self, action=action: self.control_first(action))
        setattr(DispatchCases, "test_launch_first_" + action, lambda self, action=action: self.launch_first(action))

for refusal in ("identity", "integrity", "early_cancel", "final_cancel"):
    setattr(DispatchCases, "test_superseded_" + refusal + "_settlement", lambda self, refusal=refusal: self.refusal_settlement(refusal))


class GuardedDispatchTests(DispatchCases, unittest.TestCase):
    guarded = True


class LegacyDispatchTests(DispatchCases, unittest.TestCase):
    guarded = False
