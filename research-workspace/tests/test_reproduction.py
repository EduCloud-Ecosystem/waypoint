"""Adversarial repeatability controls; providers are simulated unless noted."""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from research_workspace import reproduction
from research_workspace.evidence import build_bundle, verify_bundle
from research_workspace.recipes import expected_result, recipe_bytes


CSV = b"group,value\nA,1\nA,3\n"
IMAGE = "python@sha256:" + "a" * 64
SOURCE = recipe_bytes("summary-python")
HASH = hashlib.sha256(SOURCE).hexdigest()


def fixture(statuses=("succeeded",), source=SOURCE, image=IMAGE):
    project = {"id": "originalproject", "name": "Retained study", "question": "Are means repeatable?",
               "success_criterion": "Descriptive means match.", "created_at": "2026-10-17T10:00:00Z",
               "ends_at": "2026-10-17T11:00:00Z", "budget_cpu_seconds": 9999, "status": "closed"}
    runs, files = [], {}
    for index, status in enumerate(statuses):
        run_id = f"original{index}"
        metrics = expected_result(CSV.decode(), "baseline") if status == "succeeded" else {}
        run = {"id": run_id, "project_id": project["id"], "recipe": "summary-python", "condition": "baseline",
               "status": status, "created_at": project["created_at"], "started_at": project["created_at"],
               "finished_at": project["created_at"], "input_sha256": hashlib.sha256(CSV).hexdigest(),
               "recipe_sha256": hashlib.sha256(source).hexdigest(),
               "environment": {"image": image, "runtime": "python", "runner": "outfitter-docker-v1"},
               "parameters": {}, "metrics": metrics, "error": None if metrics else "Original bounded failure.",
               "cleanup_verified": True, "compute": {"reserved_cpu_seconds": 123, "charged_cpu_seconds": 123,
               "provenance": "estimated_upper_bound", "unresolved_liability": False}}
        runs.append(run)
        files[f"runs/{run_id}/input.csv"] = CSV
        files[f"runs/{run_id}/recipe.py"] = source
        if metrics:
            files[f"runs/{run_id}/result.json"] = json.dumps(metrics).encode()
    return project, runs, files


def archived(payload):
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


class ReproductionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "output"
        self.bundle = self.root / "source.zip"
        self.broker = self.root / "broker"
        self.broker.write_text("private executable placeholder")
        self.entry = {"id": "summary-python-v1", "recipe": "summary-python", "recipe_sha256": HASH, "image": IMAGE}
        def resolve(recipe, sha, image):
            if (recipe, sha, image) != ("summary-python", HASH, IMAGE):
                raise ValueError("Not trusted")
            return dict(self.entry)
        self.resolve = self.enterContext(patch.object(reproduction, "resolve_environment", side_effect=resolve))
        self.source = self.enterContext(patch.object(reproduction, "recipe_source", return_value=SOURCE))
        self.preflight = self.enterContext(patch.object(reproduction, "validate_images"))
        self.init = self.enterContext(patch.object(reproduction.Broker, "init_project", return_value={}))
        self.observe = self.enterContext(patch.object(reproduction, "_bounded_process", side_effect=self.receipt))
        self.write_fixture()

    def write_fixture(self, statuses=("succeeded",), **kwargs):
        self.project, self.runs, self.files = fixture(statuses, **kwargs)
        self.save_fixture()

    def save_fixture(self):
        self.payload = build_bundle(self.project, self.runs, self.files)
        self.bundle.write_bytes(self.payload)
        self.hash = hashlib.sha256(self.payload).hexdigest()

    def replay(self, **kwargs):
        arguments = {"trusted_sha256": self.hash, "output": self.output,
                     "broker": self.broker, "budget_cpu_seconds": 300}
        arguments.update(kwargs)
        return reproduction.reproduce_bundle(self.bundle, **arguments)

    def receipt(self, command, directory):
        request = json.loads(Path(command[-1]).read_text())
        content = (Path(request["input_dir"]) / "data.csv").read_text()
        return {"schema_version": 1, "request_id": request["request_id"], "project_id": request["project_id"],
                "image": request["image"], "status": "succeeded", "admitted": True, "execution_started": True,
                "cleanup_verified": True, "unresolved_liability": False, "exit_code": 0,
                "stdout": json.dumps(expected_result(content, request["command"][-1])),
                "stderr": "private-diagnostic-123 /home/private/operator", "error": "",
                "compute": {"reserved_cpu_seconds": 30, "charged_cpu_seconds": 30, "provenance": "estimated_upper_bound"}}

    def test_planning_requires_external_hash_and_full_archive_integrity(self):
        for value in (None, "", "a" * 63, "A" * 64, "0" * 64):
            with self.subTest(value=value), self.assertRaises(ValueError):
                reproduction.plan_reproduction(self.payload, value)
        tampered = self.payload.replace(b"Are means repeatable?", b"Are means fabricated?", 1)
        with self.assertRaises(ValueError):
            reproduction.plan_reproduction(tampered, hashlib.sha256(tampered).hexdigest())
        self.observe.assert_not_called()
        self.init.assert_not_called()
        self.preflight.assert_not_called()

    def test_plan_defaults_to_success_but_retains_every_original_failure_and_gap(self):
        self.write_fixture(("succeeded", "failed", "cancelled", "interrupted"))
        result = reproduction.plan_reproduction(self.payload, self.hash)
        self.assertEqual(result["selection"], {"mode": "successful_default", "selected_count": 1,
                                             "unselected_count": 3, "required_cpu_seconds": 30})
        self.assertEqual(len(result["runs"]), 4)
        self.assertTrue(result["executable"])
        self.assertTrue(all(row["outcome"] == "NOT RUN" for row in result["runs"]))
        self.assertTrue(all(row["gaps"] for row in result["runs"][1:]))
        self.assertEqual(result["runs"][1]["original_error"], "Original bounded failure.")
        self.preflight.assert_not_called()
        self.init.assert_not_called()

    def test_explicit_selection_is_exact_and_unique(self):
        self.write_fixture(("succeeded", "failed"))
        for selection in (["missing"], ["original0", "original0"], "original0", [True]):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                reproduction.plan_reproduction(self.payload, self.hash, selection)
        plan = reproduction.plan_reproduction(self.payload, self.hash, ["original1"])
        self.assertEqual([row["selected"] for row in plan["runs"]], [False, True])

    def test_authentic_archive_with_arbitrary_code_or_untrusted_image_cannot_execute(self):
        for options in ({"source": b"raise RuntimeError('untrusted code')\n"}, {"image": "python@sha256:" + "b" * 64}):
            with self.subTest(options=options):
                self.write_fixture(**options)
                self.assertFalse(reproduction.plan_reproduction(self.payload, self.hash)["executable"])
                with self.assertRaises(ValueError):
                    self.replay()
                self.assertFalse(self.output.exists())
        self.preflight.assert_not_called()
        self.init.assert_not_called()
        self.observe.assert_not_called()

    def test_packaged_bytes_must_match_even_if_resolver_claims_trust(self):
        self.source.return_value = SOURCE + b"\n# changed after catalog review\n"
        with self.assertRaises(ValueError):
            self.replay()
        self.observe.assert_not_called()

    def test_invalid_input_is_rejected_before_compute(self):
        content = b"group,value\nA,1_000\n"
        self.files["runs/original0/input.csv"] = content
        self.runs[0]["input_sha256"] = hashlib.sha256(content).hexdigest()
        self.save_fixture()
        with self.assertRaises(ValueError):
            self.replay()
        self.init.assert_not_called()

    def test_budget_requires_finite_bounded_whole_amount_and_full_pre_reservation(self):
        for value in (None, True, False, 0, -1, 301, 30.5, float("nan"), float("inf"), 10 ** 400):
            with self.subTest(value=str(value)[:30]), self.assertRaises(ValueError):
                self.replay(budget_cpu_seconds=value)
        self.write_fixture(("succeeded", "succeeded"))
        with self.assertRaises(ValueError):
            self.replay(budget_cpu_seconds=59)
        self.assertFalse(self.output.exists())
        self.init.assert_not_called()
        self.observe.assert_not_called()

    def test_every_environment_preflight_precedes_output_and_broker_initialization(self):
        self.preflight.side_effect = ValueError("Local image absent")
        with self.assertRaises(ValueError):
            self.replay()
        self.assertFalse(self.output.exists())
        self.init.assert_not_called()
        self.observe.assert_not_called()

    def test_later_missing_environment_prevents_every_selected_execution(self):
        self.write_fixture(("succeeded", "succeeded"))
        other_image = "python@sha256:" + "b" * 64
        self.runs[1]["environment"]["image"] = other_image
        self.save_fixture()
        self.resolve.side_effect = lambda recipe, sha, image: {**self.entry, "image": image}
        self.preflight.side_effect = [None, ValueError("Second image absent")]
        with self.assertRaises(ValueError):
            self.replay()
        self.assertEqual(self.preflight.call_count, 2)
        for call in self.preflight.call_args_list:
            self.assertEqual(call.kwargs, {"require_active": False})
        self.assertFalse(self.output.exists())
        self.init.assert_not_called()
        self.observe.assert_not_called()

    def test_nonregular_source_is_refused_without_blocking_or_dispatch(self):
        fifo = self.root / "archive-pipe"
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            reproduction.reproduce_bundle(fifo, trusted_sha256=self.hash, output=self.output,
                                          broker=self.broker, budget_cpu_seconds=30)
        self.init.assert_not_called()

    def test_existing_output_file_directory_and_symlink_are_never_reused(self):
        targets = [self.root / "existing-file", self.root / "existing-directory", self.root / "existing-link"]
        targets[0].write_text("preserve me")
        targets[1].mkdir()
        targets[2].symlink_to(self.root / "not-created")
        for target in targets:
            with self.subTest(target=target.name), self.assertRaises(ValueError):
                self.replay(output=target)
        self.assertEqual(targets[0].read_text(), "preserve me")
        self.assertTrue(targets[2].is_symlink())
        self.init.assert_not_called()
        self.observe.assert_not_called()

    def test_success_uses_fresh_authority_exact_packaged_code_and_sanitized_artifacts(self):
        result = self.replay(budget_cpu_seconds=30)
        report = result["report"]
        self.assertEqual(report["status"], "PASS")
        row = report["runs"][0]
        self.assertNotEqual(row["new_run_id"], "original0")
        self.assertNotEqual(report["fresh_project_id"], self.project["id"])
        self.init.assert_called_once_with(report["fresh_project_id"], 30)
        self.assertEqual(row["compute"]["charged_cpu_seconds"], 30)
        self.assertEqual(row["original_compute"]["charged_cpu_seconds"], 123)
        directory = self.output / "state.private" / row["new_run_id"]
        request = json.loads((directory / "request.private.json").read_text())
        self.assertEqual(request["cpus"], 1)
        self.assertEqual(request["memory_mb"], 256)
        self.assertEqual(request["timeout_seconds"], 30)
        self.assertEqual((directory / "recipe/recipe.py").read_bytes(), SOURCE)
        payload = (self.output / "evidence.zip").read_bytes()
        verified = verify_bundle(payload)
        members = archived(payload)
        self.assertIn(self.hash.encode(), members["project.json"])
        self.assertIn(b"original0", members["project.json"])
        self.assertEqual(verified["run_count"], 1)
        self.assertEqual(json.loads((self.output / "report.json").read_text()), report)
        for name, artifact in result["artifacts"].items():
            self.assertEqual(artifact["sha256"], hashlib.sha256((self.output / artifact["name"]).read_bytes()).hexdigest())
        public = (self.output / "report.json").read_bytes() + payload
        self.assertNotIn(b"private-diagnostic-123", public)
        self.assertNotIn(str(self.root).encode(), public)
        self.assertEqual((self.output / "state.private").stat().st_mode & 0o777, 0o700)
        self.assertIn("state.private/", (self.output / ".gitignore").read_text())

    def test_uncertain_checkpoint_exists_before_dispatch(self):
        def observe(command, directory):
            saved = json.loads((self.output / "report.json").read_text())
            row = saved["runs"][0]
            self.assertTrue(row["attempted"])
            self.assertEqual(row["compute"], reproduction._compute(True))
            self.assertEqual(saved["status"], "FAIL")
            verify_bundle((self.output / "evidence.zip").read_bytes())
            return self.receipt(command, directory)
        self.observe.side_effect = observe
        self.assertEqual(self.replay()["report"]["status"], "PASS")

    def test_malformed_receipt_retains_unknown_and_stops_following_runs(self):
        self.write_fixture(("succeeded", "succeeded"))
        def observe(command, directory):
            receipt = self.receipt(command, directory)
            receipt["image"] = "wrong image"
            return receipt
        self.observe.side_effect = observe
        report = self.replay()["report"]
        first, second = report["runs"]
        self.assertEqual(report["phase"], "reconciliation_required")
        self.assertEqual(first["outcome"], "FAIL")
        self.assertEqual(first["compute"], reproduction._compute(True))
        self.assertEqual(second["outcome"], "NOT RUN")
        self.assertFalse(second["attempted"])
        self.observe.assert_called_once()
        saved = json.loads(archived((self.output / "evidence.zip").read_bytes())["runs.json"])
        self.assertEqual({run["status"] for run in saved}, {"interrupted", "cancelled"})

    def test_timeout_and_failed_cli_never_invent_zero_charge_or_retry(self):
        for index, exception in enumerate((subprocess.TimeoutExpired("private-command", 70), ValueError("nonzero exit /private/secret"))):
            with self.subTest(index=index):
                self.observe.reset_mock()
                self.observe.side_effect = exception
                result = self.replay(output=self.root / f"attempt-{index}")
                row = result["report"]["runs"][0]
                self.assertEqual(row["compute"], reproduction._compute(True))
                self.assertFalse(row["cleanup_verified"])
                self.assertNotIn("private-command", json.dumps(result))
                self.observe.assert_called_once()

    def test_known_unresolved_reservation_survives_and_stops_following_runs(self):
        self.write_fixture(("succeeded", "succeeded"))
        def observe(command, directory):
            receipt = self.receipt(command, directory)
            receipt.update(status="unknown", cleanup_verified=False, unresolved_liability=True)
            receipt["compute"] = {"reserved_cpu_seconds": 30, "charged_cpu_seconds": 0, "provenance": "reserved"}
            return receipt
        self.observe.side_effect = observe
        report = self.replay()["report"]
        self.assertEqual(report["runs"][0]["compute"], {"reserved_cpu_seconds": 30, "charged_cpu_seconds": 0,
                                                       "provenance": "reserved", "unresolved_liability": True})
        self.assertEqual(report["runs"][1]["outcome"], "NOT RUN")
        self.assertEqual(report["phase"], "reconciliation_required")
        self.observe.assert_called_once()

    def test_refusal_is_not_admitted_and_settled_failure_keeps_charge(self):
        for admitted in (False, True):
            def observe(command, directory):
                receipt = self.receipt(command, directory)
                receipt.update(status="failed", admitted=admitted, execution_started=admitted, exit_code=1 if admitted else None)
                receipt["compute"] = {"reserved_cpu_seconds": 30 if admitted else 0, "charged_cpu_seconds": 30 if admitted else 0,
                                      "provenance": "estimated_upper_bound" if admitted else "not_admitted"}
                return receipt
            self.observe.side_effect = observe
            row = self.replay(output=self.root / str(admitted))["report"]["runs"][0]
            self.assertEqual(row["outcome"], "FAIL")
            self.assertTrue(row["cleanup_verified"])
            self.assertFalse(row["compute"]["unresolved_liability"])
            self.assertEqual(row["compute"]["charged_cpu_seconds"], 30 if admitted else 0)

    def test_result_failure_cannot_publish_metrics_even_with_success_receipt(self):
        def observe(command, directory):
            receipt = self.receipt(command, directory)
            receipt["stdout"] = '{"count":2,"mean":999,"groups":[],"condition":"baseline"}'
            return receipt
        self.observe.side_effect = observe
        report = self.replay()["report"]
        self.assertEqual(report["status"], "FAIL")
        members = archived((self.output / "evidence.zip").read_bytes())
        run = json.loads(members["runs.json"])[0]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["metrics"], {})
        self.assertFalse(any(name.endswith("result.json") for name in members))

    def test_reference_difference_is_visible_without_invalidating_checked_new_result(self):
        self.runs[0]["metrics"]["mean"] = 50.0
        self.files["runs/original0/result.json"] = json.dumps(self.runs[0]["metrics"]).encode()
        self.save_fixture()
        report = self.replay()["report"]
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["runs"][0]["comparison"]["status"], "FAIL")
        run = json.loads(archived((self.output / "evidence.zip").read_bytes())["runs.json"])[0]
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["metrics"]["mean"], 2)

    def test_explicit_failed_control_without_reference_is_not_a_repeatability_pass(self):
        self.write_fixture(("failed",))
        report = self.replay(selected_run_ids=["original0"])["report"]
        self.assertEqual(report["status"], "FAIL")
        row = report["runs"][0]
        self.assertTrue(row["attempted"])
        self.assertEqual(row["comparison"]["status"], "NOT RUN")
        self.assertEqual(row["original_status"], "failed")

    def test_no_selected_runs_still_produces_honest_report_and_empty_evidence(self):
        self.write_fixture(("failed",))
        report = self.replay()["report"]
        self.assertEqual(report["status"], "NOT RUN")
        self.assertEqual(report["selection"]["unselected_count"], 1)
        self.init.assert_not_called()
        self.observe.assert_not_called()
        self.assertEqual(verify_bundle((self.output / "evidence.zip").read_bytes())["run_count"], 0)

    def test_initialization_failure_is_retained_without_dispatch(self):
        self.init.side_effect = ValueError("private initialization details")
        report = self.replay()["report"]
        self.assertEqual(report["phase"], "initialization_failed")
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["runs"][0]["outcome"], "NOT RUN")
        self.observe.assert_not_called()


class ProcessObservationTests(unittest.TestCase):
    """Exercise the actual local process reader; these do not use Docker."""

    def test_nonzero_success_shaped_response_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                reproduction._bounded_process([sys.executable, "-c", "print('{}'); raise SystemExit(2)"], Path(directory))

    def test_oversized_output_is_bounded_and_process_is_terminated(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                reproduction._bounded_process([sys.executable, "-c", "import sys,time;sys.stdout.write('x'*1000000);sys.stdout.flush();time.sleep(10)"], Path(directory), timeout=3)
            self.assertEqual((Path(directory) / "broker-stdout.private").stat().st_size, 262144)

    def test_stalled_process_has_a_finite_observation_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                reproduction._bounded_process([sys.executable, "-c", "import time;time.sleep(10)"], Path(directory), timeout=0.1)


class PackagedCatalogIntegrationTests(unittest.TestCase):
    def test_browser_fixture_resolves_exact_archived_r_without_executing(self):
        root = Path(__file__).resolve().parents[1]
        retained = json.loads((root / "evidence/2026-10-17/browser-verification.json").read_text())
        payload = (root / "evidence/2026-10-17/browser-evidence.zip").read_bytes()
        with patch.object(reproduction, "validate_images") as preflight, patch.object(reproduction.subprocess, "Popen") as process:
            plan = reproduction.plan_reproduction(payload, retained["observations"]["bundle"]["sha256"])
        self.assertTrue(plan["executable"])
        self.assertEqual({row["trusted_environment_id"] for row in plan["runs"] if row["selected"]},
                         {"summary-python-v1", "summary-r-v1"})
        self.assertEqual(plan["selection"]["unselected_count"], 1)
        process.assert_not_called()
        preflight.assert_not_called()


if __name__ == "__main__":
    unittest.main()
