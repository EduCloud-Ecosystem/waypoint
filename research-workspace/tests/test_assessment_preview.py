"""Pure fixture and bounded CLI transport checks; no real provider evidence."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from research_workspace import assessment, assessment_preview


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "assessment/fixtures"


def document(name):
    return json.loads((FIXTURES / name).read_text())


def manifest():
    return document("research-default.json")


def receipts():
    return document("legacy-receipts.json")


def result_for(value, plan=None):
    plan = plan or manifest()
    result = document("legacy-validation.json")
    result.update(receipts=value, manifest_sha256=assessment.manifest_digest(plan),
                  measurement_sha256=assessment.manifest_digest(value["measurement"]),
                  cleanup_sha256=assessment.manifest_digest(value["cleanup"]))
    return result


class ProjectionTests(unittest.TestCase):
    def test_shared_receipt_digests_and_unknowns_do_not_become_measurements(self):
        value = receipts()
        self.assertEqual(assessment.manifest_digest(value),
                         "27061101e45bcff439f0e3bfff871d4f3bfe82ec6e02afb7410492e1b318d809")
        self.assertEqual(result_for(value), document("legacy-validation.json"))
        record = assessment_preview.retained_record(manifest(), result_for(value))
        self.assertEqual(record["conservative_accounting"]["charged_cpu_seconds"], 30)
        self.assertEqual(record["reservation_ceiling"]["cpu_seconds"], 30)
        for metric in record["measured_consumption"].values():
            self.assertEqual(metric["availability"], "unsupported")
            self.assertIsNone(metric["value"])
        self.assertIsNone(record["provider_reported_charge"]["value"])
        self.assertEqual(record["cleanup_claim"]["termination"], "unknown")
        self.assertEqual(record["cleanup_claim"]["access_revocation"], "unknown")
        self.assertEqual(record["cleanup_claim"]["outcome"], "unresolved")
        self.assertTrue(record["cleanup_claim"]["quarantine"])

    def test_synthetic_available_counter_preserves_unit_scope_and_untrusted_source(self):
        value = receipts()
        value["measurement"]["observation"]["source_kind"] = "synthetic_fixture"
        value["measurement"]["measured_consumption"]["cpu_consumed"] = {
            "availability": "available", "value": 500, "unit": "nanoseconds",
            "scope": "process", "source": "unverified_collector"}
        record = assessment_preview.retained_record(manifest(), result_for(value))
        self.assertEqual(record["measured_consumption"]["cpu_consumed"]["value"], 500)
        self.assertEqual(record["measured_consumption"]["cpu_consumed"]["unit"], "nanoseconds")
        self.assertEqual(record["conservative_accounting"]["charged_cpu_seconds"], 30)
        self.assertFalse(record["observation"]["independently_verified"])

    def test_arbitrary_receipt_and_verifier_labels_are_not_in_retained_projection(self):
        value = receipts()
        value["cleanup"]["receipt_id"] = "PRIVATE_LABEL_SENTINEL"
        value["cleanup"]["verifier_id"] = "PRIVATE_VERIFIER_SENTINEL"
        record = assessment_preview.retained_record(manifest(), result_for(value))
        self.assertNotIn("PRIVATE_", json.dumps(record))
        self.assertTrue(record["cleanup_claim"]["verifier_declared"])

    def test_each_manifest_binding_and_attempt_slot_is_checked_again(self):
        variants = {"project_id": "4"*32, "assessment_id": "5"*32, "attempt_id": "6"*32,
                    "reservation_id": "7"*32, "stage_id": "setup", "sample_role": "held_out",
                    "sample_size": 20, "repetition": 2, "environment_id": "other",
                    "image": "python:latest", "hardware": {"cpu_count": 100},
                    "measurement_method": {"method": "none", "version": 1},
                    "manifest_sha256": "0"*64}
        for key, bad in variants.items():
            value = receipts()
            for kind in ("measurement", "cleanup"):
                value[kind]["binding"][key] = bad
            with self.subTest(key=key), self.assertRaises(assessment.AssessmentError):
                assessment_preview.retained_record(manifest(), result_for(value))

    def test_no_embedded_cleanup_promotion_or_raw_metric_fields(self):
        for field, bad in (("outcome", "verified"), ("termination", "reported_complete"),
                           ("access_revocation", "PRIVATE_SENTINEL")):
            value = receipts(); value["cleanup"][field] = bad
            with self.subTest(field=field), self.assertRaises(assessment.AssessmentError):
                assessment_preview.retained_record(manifest(), result_for(value))
        for key, bad in (("value", 0), ("unit", "PRIVATE_SENTINEL"),
                         ("filename", "PRIVATE_SENTINEL")):
            value = receipts(); value["measurement"]["measured_consumption"]["cpu_consumed"][key] = bad
            with self.subTest(key=key), self.assertRaises(assessment.AssessmentError):
                assessment_preview.retained_record(manifest(), result_for(value))

    def test_legacy_unknown_reuse_is_preserved(self):
        value = receipts(); value["measurement"]["execution"]["reused"] = None
        record = assessment_preview.retained_record(manifest(), result_for(value))
        self.assertIsNone(record["execution_claim"]["reused"])


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plan_path = self.root / "manifest.json"
        self.receipt_path = self.root / "receipts.json"
        self.plan_path.write_text(json.dumps(manifest()))
        self.receipt_path.write_text(json.dumps(receipts()))

    def helper(self, output=None, code=None):
        path = self.root / "fixture-validator"
        if code is None:
            output = output if output is not None else document("legacy-validation.json")
            code = ("import json,sys\n"
                    "assert sys.argv[1:] == ['assessment-receipts']\n"
                    "request=json.load(sys.stdin)\n"
                    "assert set(request) == {'operation','manifest','receipts'}\n"
                    "assert request['operation'] == 'validate'\n"
                    "assert 'state_dir' not in request\n"
                    f"sys.stdout.write({json.dumps(json.dumps(output))})\n")
        path.write_text(f"#!{sys.executable}\n" + code)
        path.chmod(0o700)
        return str(path)

    def cli(self, program):
        return subprocess.run([sys.executable, "-m", "research_workspace", "assessment-preview",
                               str(self.plan_path), str(self.receipt_path), "--outfitter", program],
                              cwd=self.root, capture_output=True, timeout=20,
                              env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"})

    def test_actual_cli_prints_exact_draft_and_creates_no_state(self):
        helper = self.helper()
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        result = self.cli(helper)
        self.assertEqual(result.returncode, 0, result.stderr)
        preview = json.loads(result.stdout)
        self.assertEqual(preview["validation"], "valid_unverified")
        self.assertEqual(preview["reservation_coverage"]["declared"], 1)
        for key in ("execution_authorized", "actual_resource_accepted", "cleanup_trusted", "retention_finalized"):
            self.assertIs(preview[key], False)
        self.assertEqual(preview["retained_record"],
                         assessment_preview.retained_record(manifest(), document("legacy-validation.json")))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_mismatched_or_promoted_owner_result_refused_without_echo(self):
        for field, bad in (("manifest_sha256", "0"*64), ("cleanup_sha256", "0"*64),
                           ("measurement_sha256", "0"*64), ("cleanup_trusted", True),
                           ("execution_authorized", 0), ("schema", "PRIVATE_SENTINEL"),
                           ("filename", "PRIVATE_SENTINEL")):
            value = document("legacy-validation.json"); value[field] = bad
            result = self.cli(self.helper(value))
            with self.subTest(field=field):
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, b"")
                self.assertNotIn(b"PRIVATE_", result.stderr)

    def test_failed_malformed_and_oversized_helper_diagnostics_are_never_echoed(self):
        scripts = ["import sys\nsys.stderr.write('PRIVATE_SENTINEL');sys.exit(1)\n",
                   "print('PRIVATE_SENTINEL')\n",
                   "import sys\nsys.stdout.write('PRIVATE_SENTINEL'*30000)\n",
                   "import sys\nsys.stderr.write('PRIVATE_SENTINEL'*6000)\n"]
        for script in scripts:
            result = self.cli(self.helper(code=script))
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, b"")
            self.assertNotIn(b"PRIVATE_", result.stderr)

    def test_nonresponding_helper_is_bounded_and_reaped(self):
        helper = self.helper(code="import time\ntime.sleep(30)\n")
        with patch.object(assessment_preview, "VALIDATION_TIMEOUT_SECONDS", 0.05), \
             self.assertRaises(assessment.AssessmentError):
            assessment_preview.preview_receipts(manifest(), receipts(), helper)

    def test_untrusted_path_lookup_and_missing_executable_refused(self):
        for path in ("outfitter", str(self.root / "PRIVATE_FILENAME_SENTINEL")):
            result = self.cli(path)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn(b"PRIVATE_", result.stderr)

    def test_preview_does_not_initialize_broker_store_or_export_studies(self):
        from research_workspace import runtime, store, evidence
        helper = self.helper()
        with patch.object(runtime.Broker, "__init__", side_effect=AssertionError("broker initialized")), \
             patch.object(store.Store, "__init__", side_effect=AssertionError("store initialized")), \
             patch.object(evidence, "build_bundle", side_effect=AssertionError("study export initialized")):
            result = assessment_preview.preview_receipts(manifest(), receipts(), helper)
        self.assertFalse(result["execution_authorized"])


if __name__ == "__main__":
    unittest.main()
