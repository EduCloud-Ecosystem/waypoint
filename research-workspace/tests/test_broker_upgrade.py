"""Qualification-oracle controls only; no Docker, broker execution or providers."""
import copy
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import verify_broker_upgrade as q


def passing_report():
    images = json.loads((q.HERE / "images.lock.json").read_text())
    report = {"format": "waypoint-broker-upgrade-qualification-v2",
              "checks": [{"name": name, "passed": True} for name in sorted(q.REQUIRED)],
              "provenance": {"baseline": {"sha256": "a"*64}, "candidate": {"sha256": "b"*64}},
              "attempts": [], "expected_attempts": []}
    for index, (slot, state, broker, image) in enumerate(q.ATTEMPT_PLAN):
        binding = {"slot": slot, "request_id": f"request-{index}", "project_id": f"project-{min(index // 2, 2)}",
                   "image": images[image], "state_role": state, "state_sha256": q.digest(state.encode()),
                   "request_sha256": q.digest(str(index).encode()), "broker_sha256": report["provenance"][broker]["sha256"]}
        final = {**{k: binding[k] for k in ("request_id", "project_id", "image")}, "admitted": True,
                 "execution_started": True, "cleanup_verified": True, "unresolved_liability": False,
                 "status": "succeeded", "exit_code": 0,
                 "compute": {"reserved_cpu_seconds": 30, "charged_cpu_seconds": 30, "provenance": "estimated_upper_bound"}}
        report["expected_attempts"].append(copy.deepcopy(binding))
        report["attempts"].append({**binding, "cleanup_verified": True, "outcome": "succeeded",
                                   "compute": copy.deepcopy(final["compute"]), "final_receipt": final,
                                   "final_receipt_sha256": q.digest(json.dumps(final).encode())})
    return report


class QualificationControls(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_complete_oracle_rejects_empty_partial_duplicate_false_and_unknown(self):
        self.assertTrue(q.accepted(passing_report()))
        for variant in ("empty", "missing", "duplicate", "false", "unknown", "fewer_attempts", "unknown_cleanup"):
            with self.subTest(variant=variant):
                report = passing_report()
                if variant == "empty": report["checks"] = []
                if variant == "missing": report["checks"].pop()
                if variant == "duplicate": report["checks"][-1] = report["checks"][0]
                if variant == "false": report["checks"][0]["passed"] = False
                if variant == "unknown": report["checks"][0]["passed"] = "UNKNOWN"
                if variant == "fewer_attempts": report["attempts"].pop()
                if variant == "unknown_cleanup": report["attempts"][0]["cleanup_verified"] = 1
                self.assertFalse(q.accepted(report))

    def test_oracle_remains_enforced_under_optimized_python(self):
        code = """import sys; sys.path.insert(0, 'tests')
import verify_broker_upgrade as q
from test_broker_upgrade import passing_report
r=passing_report()
if not q.accepted(r): raise SystemExit('Valid report refused')
for entry in r['attempts']:
    entry['compute']['charged_cpu_seconds']=0
    entry['final_receipt']['compute']['charged_cpu_seconds']=0
if q.accepted(r): raise SystemExit('Zero charge accepted')
r=passing_report(); r['failure']=None
if q.accepted(r): raise SystemExit('Failure marker accepted')
"""
        completed = subprocess.run([sys.executable, "-O", "-c", code], cwd=q.HERE, capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_output_existing_directory_file_and_symlink_refused(self):
        for kind in ("directory", "file", "symlink", "dangling"):
            path = self.root / kind
            if kind == "directory": path.mkdir()
            if kind == "file": path.write_text("preserve")
            if kind == "symlink": path.symlink_to(self.root / "directory", target_is_directory=True)
            if kind == "dangling": path.symlink_to(self.root / "must-not-create", target_is_directory=True)
            with self.subTest(kind=kind), self.assertRaises(FileExistsError): q.Audit(path, {})
        self.assertEqual((self.root / "file").read_text(), "preserve")
        self.assertFalse((self.root / "must-not-create").exists())

    def test_refusal_oracle_fails_when_operation_unexpectedly_succeeds(self):
        audit = q.Audit(self.root / "output", {})
        with self.assertRaises(ValueError): audit.refused("unknown_activation_refused", lambda: None)
        report = json.loads((audit.root / "verification.json").read_text())
        self.assertEqual(report["checks"], [{"name": "unknown_activation_refused", "passed": False}])

    def request(self):
        return {"request_id": "fixture", "project_id": "fixture", "cpus": 1, "memory_mb": 256,
                "pids_limit": 64, "timeout_seconds": 30, "max_output_bytes": 65536,
                "image": next(iter(json.loads((q.HERE / "images.lock.json").read_text()).values()))}

    def test_attempt_budget_is_durable_before_launch_and_never_replenished(self):
        audit = self.recorded_audit()
        saved = json.loads((audit.root / "verification.json").read_text())
        self.assertEqual(len(saved["attempts"]), 7)
        self.assertTrue(all(a["outcome"] == "unknown_before_launch" for a in saved["attempts"]))
        with self.assertRaises(ValueError): audit.attempt(sys.executable, audit.private, audit.private / "request.json")
        self.assertEqual(len(audit.targets), 7)
        self.assertEqual(len(saved["expected_attempts"]), 7)

    def recorded_audit(self):
        provenance = {key: {"sha256": q.file_hash(sys.executable)} for key in ("baseline", "candidate")}
        audit = q.Audit(self.root / "output", provenance)
        images = json.loads((q.HERE / "images.lock.json").read_text())
        for index, (_, state, _, image) in enumerate(q.ATTEMPT_PLAN):
            request = {**self.request(), "request_id": f"request-{index}", "project_id": f"project-{min(index // 2, 2)}", "image": images[image]}
            path = audit.private / f"request-{index}.json"
            path.write_text(json.dumps(request))
            audit.attempt(sys.executable, audit.private / state, path)
        audit.report["checks"] = passing_report()["checks"]
        return audit

    def final_status(self, audit, request_id):
        request = next(r for _, _, r, _ in audit.targets if r["request_id"] == request_id)
        return {"schema_version": 1, **{k: request[k] for k in ("request_id", "project_id", "image")},
                "admitted": True, "execution_started": True, "cleanup_verified": True, "unresolved_liability": False,
                "stdout": "", "stderr": "", "error": "", "exit_code": 0, "status": "succeeded",
                "compute": {"reserved_cpu_seconds": 30, "charged_cpu_seconds": 30, "provenance": "estimated_upper_bound"}}

    def test_cleanup_requires_full_final_charge_after_prior_checks(self):
        audit = self.recorded_audit()
        def valid(_executable, _state, command, _flag, request_id):
            self.assertEqual(command, "status")
            return 0, self.final_status(audit, request_id)
        with patch.object(audit, "call", side_effect=valid):
            self.assertTrue(audit.cleanup())
        self.assertTrue(q.accepted(audit.report))
        bindings = copy.deepcopy(audit.report["expected_attempts"])
        valid_hashes = [entry["final_receipt_sha256"] for entry in audit.report["attempts"]]
        def refused(*args):
            _, receipt = valid(*args)
            receipt.update(admitted=False, execution_started=False, status="failed", exit_code=None, error="refused",
                           compute={"reserved_cpu_seconds": 0, "charged_cpu_seconds": 0, "provenance": "not_admitted"})
            return 0, receipt
        with patch.object(audit, "call", side_effect=refused):
            self.assertFalse(audit.cleanup())
        self.assertFalse(q.accepted(audit.report))
        self.assertEqual(audit.report["expected_attempts"], bindings)
        self.assertTrue(all(a["cleanup_verified"] is False and a["cleanup_error_type"] == "ValueError" for a in audit.report["attempts"]))
        # A rejected later observation cannot overwrite the earlier full charge.
        self.assertEqual(sum(a["compute"]["charged_cpu_seconds"] for a in audit.report["attempts"]), 210)
        self.assertEqual([entry["final_receipt_sha256"] for entry in audit.report["attempts"]], valid_hashes)
        self.assertTrue(all(entry["rejected_final_receipt_sha256"] != entry["final_receipt_sha256"] for entry in audit.report["attempts"]))

    def test_final_receipt_rejects_mutated_identity_and_nonsettled_accounting(self):
        audit = self.recorded_audit()
        request = audit.targets[0][2]
        good = self.final_status(audit, request["request_id"])
        self.assertTrue(q.settled_observation(q.final_receipt(good, request)))
        for field, value in (("request_id", "wrong"), ("project_id", "wrong"), ("image", "wrong"),
                             ("admitted", False), ("execution_started", False), ("cleanup_verified", 1),
                             ("unresolved_liability", "UNKNOWN"), ("status", "failed"), ("exit_code", False)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                q.final_receipt({**good, field: value}, request)

    def test_final_oracle_rejects_all_charge_types_and_missing_accounting(self):
        for location in ("summary", "receipt", "both"):
            for field in ("reserved_cpu_seconds", "charged_cpu_seconds"):
                for value in (0, 1, 29, 31, -1, True, False, "30", "UNKNOWN", 30.0, float("nan"), float("inf"), None, "MISSING"):
                    with self.subTest(location=location, field=field, value=value):
                        report = passing_report(); entry = report["attempts"][0]
                        computes = ([entry["compute"]] if location == "summary" else [entry["final_receipt"]["compute"]]
                                    if location == "receipt" else [entry["compute"], entry["final_receipt"]["compute"]])
                        for compute in computes:
                            if value == "MISSING": compute.pop(field)
                            else: compute[field] = value
                        self.assertFalse(q.accepted(report))
        for field in ("compute", "final_receipt", "final_receipt_sha256"):
            report = passing_report(); report["attempts"][0].pop(field)
            with self.subTest(missing=field): self.assertFalse(q.accepted(report))
        for provenance in (None, "reserved", "not_admitted", True):
            report = passing_report(); entry = report["attempts"][0]
            entry["compute"]["provenance"] = entry["final_receipt"]["compute"]["provenance"] = provenance
            with self.subTest(provenance=provenance): self.assertFalse(q.accepted(report))

    def test_final_oracle_rejects_failure_flags_and_binding_changes(self):
        for value in (None, False, {}, ""):
            report = passing_report(); report["failure"] = value
            with self.subTest(failure=value): self.assertFalse(q.accepted(report))
        for flag in ("admitted", "execution_started", "cleanup_verified", "unresolved_liability"):
            for value in ("UNKNOWN", 1, 0, None, not passing_report()["attempts"][0]["final_receipt"][flag]):
                report = passing_report(); report["attempts"][0]["final_receipt"][flag] = value
                with self.subTest(flag=flag, value=value): self.assertFalse(q.accepted(report))
        for field in q.BINDING_FIELDS:
            report = passing_report(); report["attempts"][0][field] = "wrong"
            with self.subTest(binding=field): self.assertFalse(q.accepted(report))
        for field in ("request_id", "project_id", "image"):
            report = passing_report(); report["attempts"][0]["final_receipt"][field] = "wrong"
            with self.subTest(receipt_identity=field): self.assertFalse(q.accepted(report))
        for field in ("slot", "image", "state_role", "broker_sha256"):
            report = passing_report()
            report["attempts"][0][field] = report["expected_attempts"][0][field] = "wrong"
            with self.subTest(plan=field): self.assertFalse(q.accepted(report))
        report = passing_report()
        for container in (report["attempts"][1], report["expected_attempts"][1], report["attempts"][1]["final_receipt"]):
            container["request_id"] = report["expected_attempts"][0]["request_id"]
        self.assertFalse(q.accepted(report))
        for field, value in (("status", "failed"), ("exit_code", True), ("exit_code", 0.0)):
            report = passing_report(); report["attempts"][0]["final_receipt"][field] = value
            with self.subTest(final=field, value=value): self.assertFalse(q.accepted(report))
        for field in ("expected_attempts", "attempts"):
            report = passing_report(); report[field].pop()
            with self.subTest(missing=field): self.assertFalse(q.accepted(report))

    def test_cleanup_target_binding_refuses_before_any_command(self):
        audit = self.recorded_audit()
        executable, state, request, entry = audit.targets[0]
        audit.targets[0] = (executable, state + "-wrong", request, entry)
        with patch.object(audit, "call", side_effect=lambda *args: (0, self.final_status(audit, args[-1]))) as calls:
            self.assertFalse(audit.cleanup())
        self.assertEqual(calls.call_count, 6)
        self.assertFalse(q.accepted(audit.report))

    def test_attempt_binding_refuses_duplicate_request_or_wrong_state_before_launch(self):
        for case in ("duplicate", "state", "image", "broker"):
            with self.subTest(case=case):
                provenance = {key: {"sha256": q.file_hash(sys.executable)} for key in ("baseline", "candidate")}
                audit = q.Audit(self.root / case, provenance)
                request = audit.private / "request.json"; request.write_text(json.dumps(self.request()))
                audit.attempt(sys.executable, audit.private / "ordinary/broker", request)
                value = self.request()
                if case != "duplicate": value["request_id"] = "another"
                if case == "image": value["image"] = json.loads((q.HERE / "images.lock.json").read_text())["r"]
                if case == "broker": audit.report["provenance"]["candidate"]["sha256"] = "f"*64
                request.write_text(json.dumps(value))
                state = "wrong/broker" if case == "state" else "ordinary-replacement/broker"
                with self.assertRaises(ValueError): audit.attempt(sys.executable, audit.private / state, request)
                self.assertEqual(len(audit.targets), 1)
                self.assertEqual(len(audit.report["expected_attempts"]), 1)

    def test_retained_observation_reader_rejects_lost_charge_without_processes(self):
        audit = self.recorded_audit()
        with patch.object(audit, "call", side_effect=lambda *args: (0, self.final_status(audit, args[-1]))):
            self.assertTrue(audit.cleanup())
        historical = copy.deepcopy(audit.report)
        historical.update(format="waypoint-broker-upgrade-qualification-v1", status="PASS")
        historical["provenance"]["images_lock_sha256"] = q.file_hash(q.HERE / "images.lock.json")
        historical.pop("expected_attempts")
        ledgers = {}
        for index, (executable, state, request, entry) in enumerate(audit.targets):
            state = Path(state); state.mkdir(parents=True, exist_ok=True)
            run = state.parent / "runs" / request["request_id"]; run.mkdir(parents=True)
            (run / "request.private.json").write_bytes((audit.private / f"request-{index}.json").read_bytes())
            (state.parent / "config.json").write_text(json.dumps({"broker": executable}))
            ledger = ledgers.setdefault(state, {"runs": {}})
            ledger["runs"][request["request_id"]] = {"request": request}
            prefix = audit.private / f"cli-{index+3:03d}"
            prefix.with_suffix(".stdout").write_text(json.dumps(self.final_status(audit, request["request_id"])))
            prefix.with_suffix(".stderr").write_bytes(b"")
            prefix.with_suffix(".json").write_text(json.dumps({"command": "status", "exit_code": 0}))
        for state, ledger in ledgers.items(): (state / "ledger.json").write_text(json.dumps(ledger))
        historical_path = self.root / "historical.json"
        historical_path.write_text(json.dumps(historical))
        (audit.root / "verification.json").write_bytes(historical_path.read_bytes())
        reader = runpy.run_path(str(q.HERE / "evidence/2026-10-18/broker-upgrade/review-accounting/recheck_retained_observations.py"))["recheck"]
        with patch.object(q.subprocess, "Popen", side_effect=AssertionError("No process allowed")):
            checked = reader(q, audit.root, historical_path)
            self.assertEqual((checked["checked_receipts"], checked["charged_cpu_seconds"]), (7, 210))
            path = audit.private / "cli-003.stdout"
            receipt = json.loads(path.read_bytes())
            receipt.update(admitted=False, execution_started=False, status="failed", exit_code=None,
                           compute={"reserved_cpu_seconds": 0, "charged_cpu_seconds": 0, "provenance": "not_admitted"})
            path.write_text(json.dumps(receipt))
            with self.assertRaises(ValueError): reader(q, audit.root, historical_path)

    def test_attempt_refuses_over_limit_boolean_and_unreviewed_image_before_recording(self):
        for field, value in (("timeout_seconds", 31), ("cpus", True), ("memory_mb", 257), ("image", "sha256:" + "f"*64)):
            with self.subTest(field=field):
                audit = q.Audit(self.root / field, {})
                request = audit.private / "request.json"
                request.write_text(json.dumps({**self.request(), field: value}))
                with self.assertRaises(ValueError): audit.attempt(sys.executable, audit.private, request)
                self.assertEqual(audit.report["attempts"], [])

    def test_failed_body_and_failed_cleanup_cannot_report_pass(self):
        with patch.object(q, "binary_identity", return_value={}), patch.object(q, "validate_images"), \
             patch.object(q, "bridge", side_effect=ValueError("injected private failure")), \
             patch.object(q.Audit, "cleanup", return_value=False):
            report = q.exercise(sys.executable, sys.executable, self.root / "output")
        self.assertEqual(report["status"], "FAIL")
        self.assertFalse(report["checks"][-1]["passed"])
        self.assertTrue((self.root / "output/state.private/failure.txt").is_file())
        self.assertNotIn("injected private", (self.root / "output/verification.json").read_text())

    def test_interruption_after_checks_cannot_publish_pass(self):
        def interrupt(audit, *_):
            audit.report.update(passing_report())
            audit.report["checks"] = [c for c in audit.report["checks"] if c["name"] != "owned_cleanup_verified"]
            raise KeyboardInterrupt("injected before final artifact publication")
        with patch.object(q, "binary_identity", return_value={}), patch.object(q, "validate_images"), \
             patch.object(q, "bridge", return_value=({}, {})), patch.object(q, "offline_controls"), \
             patch.object(q, "export_controls", side_effect=interrupt), patch.object(q.Audit, "cleanup", return_value=True):
            try:
                q.exercise(sys.executable, sys.executable, self.root / "output")
            except KeyboardInterrupt:
                pass
        report = json.loads((self.root / "output/verification.json").read_text())
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["failure"]["type"], "KeyboardInterrupt")

    def test_build_manifest_must_bind_binary_pin_metadata_and_builder(self):
        binary = self.root / "candidate"
        binary.write_bytes(b"fixture, never executed"); binary.chmod(0o700)
        pin = json.loads(q.PINS.read_text())["candidate"]
        metadata = ["\tpath\tgithub.com/EduCloud-Ecosystem/outfitter/cmd/outfitter"]
        original = {"format": "waypoint-broker-build-v1", "pin": pin, "binary_sha256": q.file_hash(binary),
                    "build_metadata": metadata, "pins_sha256": q.file_hash(q.PINS),
                    "builder_sha256": q.file_hash(q.HERE / "build_upgrade_brokers.py")}
        manifest = binary.with_name(binary.name + ".provenance.json")
        with patch.object(q, "observed_command", return_value=(0, ("fixture\n" + "\n".join(metadata)).encode(), b"")):
            manifest.write_text(json.dumps(original))
            self.assertEqual(q.binary_identity(binary, pin)["sha256"], q.file_hash(binary))
            for field in ("pin", "binary_sha256", "build_metadata", "pins_sha256", "builder_sha256"):
                value = copy.deepcopy(original); value[field] = "wrong"
                manifest.write_text(json.dumps(value))
                with self.subTest(field=field), self.assertRaises(ValueError): q.binary_identity(binary, pin)
