"""Synthetic arithmetic and actual CLI controls; no workload execution."""

import copy
from fractions import Fraction
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from research_workspace import assessment as a
from research_workspace import assessment_estimation as e
from research_workspace.assessment_preview import retained_record

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "assessment/fixtures"


def fixture(name="python-series.json"):
    return json.loads((FIXTURES / "estimation" / name).read_text())


def rebind(series):
    manifest = series["manifest"]
    for receipt in series["receipts"]:
        for name in ("measurement", "cleanup"):
            binding = receipt[name]["binding"]
            binding.update(manifest_sha256=a.manifest_digest(manifest), hardware=copy.deepcopy(manifest["hardware"]))
        receipt["measurement"]["cleanup_receipt_sha256"] = a.manifest_digest(receipt["cleanup"])
    return series


def owner_result(manifest, receipts, executable=None, **kwargs):
    return {"schema": "outfitter.assessment-receipts-result/v1", "validation": "valid_unverified",
            "manifest_sha256": a.manifest_digest(manifest), "receipts": receipts,
            "measurement_sha256": a.manifest_digest(receipts["measurement"]),
            "cleanup_sha256": a.manifest_digest(receipts["cleanup"]),
            "execution_authorized": False, "cleanup_trusted": False, "actual_resource_accepted": False}


def calculate(series=None, scenario=None):
    with patch.object(e, "validate_receipts", side_effect=owner_result):
        return e.estimate(scenario or fixture("scenario.json"), [series or fixture()], "/fixture/validator")


def model(output, metric="cpu_consumed"):
    return output["series"][0]["models"][metric]


def fraction(value):
    return None if value is None else Fraction(int(value["numerator"]), int(value["denominator"]))


class ArithmeticTests(unittest.TestCase):
    def test_known_coefficients_dimensions_and_once_only_overhead(self):
        output = calculate()
        result = model(output)
        self.assertEqual(fraction(result["coefficients"]["intercept_nanoseconds"]), 1000)
        self.assertEqual(fraction(result["coefficients"]["slope_nanoseconds_per_row"]), 50)
        self.assertEqual(fraction(result["prediction"]["nanoseconds"]), 2250)
        total = output["series"][0]["totals"]["cpu_consumed"]
        self.assertEqual(total["attempts"], 6)
        self.assertEqual(fraction(total["total_nanoseconds"]), 13600)
        self.assertEqual(fraction(total["hours"]), Fraction(13600, 3600000000000))
        self.assertEqual(result["held_out"]["status"], "within_declared_tolerance")
        self.assertTrue(result["default_design_complete"])
        self.assertFalse(output["readiness_pass"])
        self.assertFalse(output["cleanup_trusted"])

    def test_held_out_values_and_provenance_never_change_coefficients(self):
        baseline = model(calculate())["coefficients"]
        series = fixture()
        for receipt in series["receipts"][-3:]:
            receipt["measurement"]["measured_consumption"]["cpu_consumed"]["value"] = 100000
        changed = model(calculate(series))
        self.assertEqual(changed["coefficients"], baseline)
        self.assertEqual(changed["held_out"]["status"], "outside_declared_tolerance")
        for receipt in series["receipts"][-3:]:
            receipt["measurement"]["measured_consumption"]["cpu_consumed"]["scope"] = "sandbox"
        incompatible = model(calculate(series))
        self.assertEqual(incompatible["coefficients"], baseline)
        self.assertEqual(incompatible["held_out"]["status"], "incomplete")
        self.assertTrue(all("held_out_provenance_mismatch" in x["reason_codes"] for x in incompatible["excluded_observations"]))

    def test_failed_held_out_cannot_improve_evaluation(self):
        series = fixture()
        for receipt in series["receipts"][-3:]:
            receipt["measurement"]["measured_consumption"]["cpu_consumed"]["value"] = 100000
        self.assertEqual(model(calculate(series))["held_out"]["status"], "outside_declared_tolerance")
        for receipt in series["receipts"][-3:]:
            receipt["measurement"]["execution"].update(status="failed", exit_code=1, failure_category="execution_failed")
        output = calculate(series)
        self.assertEqual(model(output)["held_out"]["status"], "incomplete")
        self.assertFalse(model(output)["declared_design_complete"])
        self.assertFalse(output["readiness_pass"])

    def test_fractional_coefficients_and_demand_rounding_are_exact(self):
        series = fixture()
        for receipt in series["receipts"]:
            size = receipt["measurement"]["binding"]["sample_size"]
            receipt["measurement"]["measured_consumption"]["cpu_consumed"]["value"] = {10: 1, 20: 2, 30: 4, 40: 7}[size]
        result = calculate(series)
        fit = model(result)
        self.assertEqual(fraction(fit["coefficients"]["intercept_nanoseconds"]), Fraction(-2, 3))
        self.assertEqual(fraction(fit["coefficients"]["slope_nanoseconds_per_row"]), Fraction(3, 20))
        self.assertEqual(fraction(fit["prediction"]["nanoseconds"]), Fraction(37, 12))
        self.assertEqual(fit["prediction"]["rounded_up_nanoseconds"], "4")
        self.assertEqual(result["series"][0]["totals"]["cpu_consumed"]["rounded_up_total_nanoseconds"], "119")

    def test_unknown_metric_is_not_zero_and_no_dimension_is_dropped(self):
        series = fixture()
        for receipt in series["receipts"]:
            receipt["measurement"]["measured_consumption"]["cpu_consumed"].update(availability="unknown", value=None, source="none")
        result = calculate(series)
        self.assertEqual(model(result)["status"], "unavailable")
        self.assertEqual(model(result, "execution_duration")["status"], "provisional")
        self.assertIsNone(result["series"][0]["totals"]["cpu_consumed"]["total_nanoseconds"])
        for receipt in series["receipts"]:
            receipt["measurement"]["measured_consumption"]["cpu_consumed"].update(availability="available", value=0, source="unverified_collector")
        zero = model(calculate(series))
        self.assertEqual(fraction(zero["prediction"]["nanoseconds"]), 0)
        self.assertTrue(all(x["relative_error"] is None for x in zero["held_out"]["errors"]))

    def test_repeat_spread_is_not_confidence_or_prediction_uncertainty(self):
        result = model(calculate())
        self.assertEqual(result["repeat_variability"][0]["range"], 20)
        self.assertIsNone(result["confidence_interval"])
        self.assertEqual(result["prediction_uncertainty"], "not_estimated")

    def test_missing_failed_oom_and_reuse_unknown_remain_explicit(self):
        series = fixture(); series["receipts"] = series["receipts"][:3]
        incomplete = model(calculate(series))
        self.assertIn("insufficient_fitting_sizes", incomplete["warning_codes"])
        for key, value in (("reused", None), ("reused", True)):
            series = fixture(); series["receipts"][0]["measurement"]["execution"][key] = value
            fit = model(calculate(series))
            self.assertEqual(len(fit["excluded_observations"]), 1)
            self.assertFalse(fit["default_design_complete"])
        series = fixture()
        series["receipts"][-1]["measurement"]["execution"].update(status="failed", failure_category="out_of_memory", exit_code=137)
        scenario = fixture("scenario.json"); scenario["target_rows"] = 50
        output = calculate(series, scenario)
        self.assertEqual(model(output)["oom_boundary_rows"], 40)
        self.assertIn("target_at_or_above_oom_boundary", model(output)["warning_codes"])
        self.assertIsNone(model(output)["prediction"])
        self.assertTrue(output["series"][0]["cleanup_claims"][-1]["remaining_liabilities"])

    def test_charge_only_legacy_input_cannot_supply_fitting_values(self):
        series = fixture()
        series["manifest"] = json.loads((FIXTURES / "research-default.json").read_text())
        series["receipts"] = [json.loads((FIXTURES / "legacy-receipts.json").read_text())]
        result = model(calculate(series))
        self.assertIsNone(result["coefficients"])
        self.assertIn("source_not_synthetic", result["excluded_observations"][0]["reason_codes"])
        self.assertIn("metric_unsupported", result["excluded_observations"][0]["reason_codes"])

    def test_extrapolation_negative_model_and_mixed_scope_limit_output(self):
        scenario = fixture("scenario.json"); scenario["target_rows"] = 100
        output = calculate(scenario=scenario)
        self.assertIn("outside_fitting_range", model(output)["warning_codes"])
        self.assertIn("outside_observed_range", model(output)["warning_codes"])
        series = fixture()
        for receipt in series["receipts"]:
            receipt["measurement"]["measured_consumption"]["cpu_consumed"]["value"] = 100 - receipt["measurement"]["binding"]["sample_size"]
        self.assertIn("unsupported_negative_model", model(calculate(series))["warning_codes"])
        series = fixture(); series["receipts"][0]["measurement"]["measured_consumption"]["cpu_consumed"]["scope"] = "sandbox"
        self.assertIn("mixed_metric_provenance", model(calculate(series))["warning_codes"])

    def test_unknown_overhead_does_not_become_zero_or_change_coefficients(self):
        scenario = fixture("scenario.json"); scenario["overheads"]["idle"]["cpu_nanoseconds"] = None
        output = calculate(scenario=scenario)
        total = output["series"][0]["totals"]["cpu_consumed"]
        self.assertIsNone(total["total_nanoseconds"])
        self.assertEqual(fraction(total["modeled_nanoseconds"]), 13500)
        self.assertIn("idle_unknown", total["reason_codes"])

    def test_overhead_key_order_is_stable_and_scope_prevents_implicit_overlap(self):
        scenario = fixture("scenario.json")
        for overhead in scenario["overheads"].values():
            overhead["cpu_nanoseconds"] = None
        original = a.canonical_bytes(calculate(scenario=scenario))
        scenario["overheads"] = dict(reversed(list(scenario["overheads"].items())))
        self.assertEqual(original, a.canonical_bytes(calculate(scenario=scenario)))
        scenario["overheads"]["setup"]["basis"] = "already_in_measurement"
        with self.assertRaises(a.AssessmentError): calculate(scenario=scenario)
        series = fixture()
        for receipt in series["receipts"]:
            receipt["measurement"]["measured_consumption"]["cpu_consumed"]["scope"] = "whole_attempt"
        result = model(calculate(series))
        self.assertIn("unsupported_metric_scope", result["warning_codes"])
        self.assertIsNone(result["prediction"])

    def test_smaller_declared_design_remains_provisional_and_default_insufficient(self):
        series = fixture(); manifest = series["manifest"]
        manifest["design"].update(fitting_sizes=[10, 20], repetitions=1, seeds=[1],
            smaller_design={"approved_by_user": True, "reason": "allowance_limited"})
        manifest["ceilings"].update(max_attempts=3, cpu_seconds=90)
        manifest["reservations"] = [r for r in manifest["reservations"] if r["repetition"] == 1 and r["sample_size"] != 30]
        series["receipts"] = [r for r in series["receipts"] if r["measurement"]["binding"]["repetition"] == 1 and r["measurement"]["binding"]["sample_size"] != 30]
        result = model(calculate(rebind(series)))
        self.assertTrue(result["declared_design_complete"])
        self.assertFalse(result["default_design_complete"])
        self.assertIn("smaller_design_provisional", result["warning_codes"])
        self.assertEqual(result["status"], "provisional")

    def test_non_synthetic_manifest_refuses_and_unverified_source_cannot_fit(self):
        series = fixture(); series["manifest"]["data_classification"] = "public"
        with self.assertRaises(a.AssessmentError): calculate(rebind(series))
        series = fixture()
        for receipt in series["receipts"]:
            receipt["measurement"]["observation"]["source_kind"] = "unverified_observation"
        output = calculate(series)
        self.assertIsNone(model(output)["coefficients"])
        self.assertFalse(output["actual_resource_accepted"])
        self.assertTrue(all("source_not_synthetic" in item["reason_codes"] for item in model(output)["excluded_observations"]))

    def test_instructional_peak_assumptions_stay_separate_from_total_demand(self):
        series = fixture(); series["manifest"]["pathway"] = "instructional"
        series["manifest"]["instruction"] = {"enrollment": 10, "assignment_count": 2, "simultaneous_sessions": 3,
            "deadline_peak_sessions": 5, "per_student_storage_bytes": 1000}
        result = calculate(rebind(series))["series"][0]
        self.assertEqual(result["totals"]["cpu_consumed"]["attempts"], 120)
        self.assertEqual(result["instruction"]["persistent_storage_assumption_bytes"], 10000)
        self.assertEqual(result["instruction"]["deadline_peak_sessions"], 5)
        self.assertIsNone(result["instruction"]["concurrent_working_memory_bytes"])
        self.assertIsNone(result["capacity_observations"]["peak_memory"]["target_capacity"])

    def test_counts_integer_types_rationals_and_limits_are_bounded(self):
        for key, value in (("target_rows", True), ("stage_executions", 0),
                           ("repetitions", 1000001), ("provider_credits", 10)):
            scenario = fixture("scenario.json"); scenario[key] = value
            with self.subTest(key=key), self.assertRaises(a.AssessmentError):
                calculate(scenario=scenario)
        scenario = fixture("scenario.json"); scenario.update(stage_executions=1000000, experiments=1000000, repetitions=1000000)
        with self.assertRaises(a.AssessmentError): calculate(scenario=scenario)
        for value in (Fraction(1 << 256), Fraction(1, 1 << 256)):
            with self.assertRaises(a.AssessmentError): e.rational(value)

    def test_input_reordering_preserves_canonical_bytes_and_duplicates_refuse(self):
        series = fixture()
        baseline = calculate(series)
        series["receipts"].reverse()
        self.assertEqual(a.canonical_bytes(baseline), a.canonical_bytes(calculate(series)))
        series["receipts"][1] = copy.deepcopy(series["receipts"][0])
        with self.assertRaises(a.AssessmentError): calculate(series)
        series = fixture(); series["receipts"][1]["cleanup"]["receipt_id"] = series["receipts"][0]["cleanup"]["receipt_id"]
        with self.assertRaises(a.AssessmentError): calculate(series)

    def test_two_series_comparison_keeps_provenance_and_does_not_choose_winner(self):
        with patch.object(e, "validate_receipts", side_effect=owner_result):
            result = e.estimate(fixture("scenario.json"), [fixture("r-series.json"), fixture()], "/fixture/validator")
        self.assertEqual(fraction(result["comparison"]["differences"]["cpu_consumed"]["right_minus_left"]), 13500)
        self.assertIsNone(result["comparison"]["winner"])
        self.assertEqual(result["provider_conversions"], "not_supported")

    def test_aggregate_deadline_refuses_partial_report(self):
        with patch.object(e, "VALIDATION_BUDGET_SECONDS", 0), self.assertRaises(a.AssessmentError):
            calculate()
        with patch.object(e.time, "monotonic", side_effect=[0, 15, 31]), \
             patch.object(e, "validate_receipts", side_effect=owner_result) as validator, \
             self.assertRaises(a.AssessmentError):
            e.estimate(fixture("scenario.json"), [fixture()], "/fixture/validator")
        self.assertEqual(validator.call_count, 1)


class EstimationCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.scenario = self.root / "scenario.json"; self.scenario.write_bytes(a.canonical_bytes(fixture("scenario.json")))
        self.series = self.root / "series.json"; self.series.write_bytes(a.canonical_bytes(fixture()))
        self.helper = self.root / "validator"
        self.helper.write_text(f"#!{sys.executable}\n" + """import hashlib,json,sys
assert sys.argv[1:] == ['assessment-receipts']
request=json.load(sys.stdin)
assert set(request)=={'operation','manifest','receipts'} and request['operation']=='validate'
digest=lambda v:hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':')).encode('ascii')).hexdigest()
r=request['receipts']
print(json.dumps({'schema':'outfitter.assessment-receipts-result/v1','validation':'valid_unverified',
'manifest_sha256':digest(request['manifest']),'measurement_sha256':digest(r['measurement']),
'cleanup_sha256':digest(r['cleanup']),'receipts':r,'execution_authorized':False,
'cleanup_trusted':False,'actual_resource_accepted':False}))
""")
        self.helper.chmod(0o700)

    def cli(self, executable=None, extra=()):
        return subprocess.run([sys.executable, "-m", "research_workspace", "assessment-estimate", str(self.scenario),
            "--series", str(self.series), "--outfitter", str(executable or self.helper), *extra], cwd=self.root,
            env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, timeout=40)

    def test_actual_cli_is_canonical_content_free_and_creates_no_state(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        completed = self.cli()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(completed.stdout, a.canonical_bytes(result))
        self.assertFalse(result["readiness_pass"])
        self.assertFalse(result["execution_authorized"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        self.assertNotIn(str(self.root).encode(), completed.stdout)

    def test_invalid_request_and_oversize_input_emit_no_partial_data(self):
        scenario = fixture("scenario.json"); scenario["filename"] = "PRIVATE_SENTINEL"
        self.scenario.write_text(json.dumps(scenario))
        bad = self.cli()
        self.assertEqual(bad.returncode, 1); self.assertEqual(bad.stdout, b"")
        self.assertNotIn(b"PRIVATE_", bad.stderr)
        self.scenario.write_bytes(a.canonical_bytes(fixture("scenario.json")))
        self.series.write_bytes(b" " * (e.SERIES_LIMIT_BYTES + 1))
        self.assertEqual(self.cli().returncode, 1)

    def test_series_count_is_refused_before_extra_files_are_read(self):
        completed = self.cli(extra=("--series", "/PRIVATE_SENTINEL", "--series", "/ANOTHER_PRIVATE_SENTINEL"))
        self.assertEqual(completed.returncode, 1); self.assertEqual(completed.stdout, b"")
        self.assertNotIn(b"PRIVATE_", completed.stderr)

    @unittest.skipUnless(os.environ.get("ASSESSMENT_OUTFITTER"), "Pure Go validator path is supplied for cross-component acceptance")
    def test_actual_cli_with_real_pinned_pure_outfitter_validator(self):
        completed = self.cli(os.environ["ASSESSMENT_OUTFITTER"])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(fraction(model(json.loads(completed.stdout))["coefficients"]["slope_nanoseconds_per_row"]), 50)

    @unittest.skipUnless(os.environ.get("ASSESSMENT_OUTFITTER"), "Pure Go validator path is supplied for cross-component acceptance")
    def test_real_validator_matrix_preserves_unknowns_and_refuses_binding_attacks(self):
        executable = os.environ["ASSESSMENT_OUTFITTER"]
        baseline = self.cli(executable)
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        value = fixture(); value["receipts"].reverse(); self.series.write_bytes(a.canonical_bytes(value))
        self.assertEqual(self.cli(executable).stdout, baseline.stdout)

        for invalid in ("unit", "binding", "duplicate"):
            value = fixture()
            if invalid == "unit":
                value["receipts"][0]["measurement"]["measured_consumption"]["cpu_consumed"]["unit"] = "seconds"
            elif invalid == "binding":
                value["receipts"][0]["measurement"]["binding"]["hardware"]["cpu_count"] = 2
            else:
                value["receipts"][1] = copy.deepcopy(value["receipts"][0])
            self.series.write_bytes(a.canonical_bytes(value)); completed = self.cli(executable)
            with self.subTest(invalid=invalid):
                self.assertEqual(completed.returncode, 1, completed.stderr)
                self.assertEqual(completed.stdout, b"")

        value = fixture()
        for receipt in value["receipts"]:
            receipt["measurement"]["measured_consumption"]["cpu_consumed"].update(availability="unknown", value=None, source="none")
        self.series.write_bytes(a.canonical_bytes(value)); completed = self.cli(executable)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIsNone(model(json.loads(completed.stdout))["coefficients"])

        value = fixture(); value["receipts"][-1]["measurement"]["execution"].update(status="failed", failure_category="out_of_memory", exit_code=137)
        self.series.write_bytes(a.canonical_bytes(value))
        scenario = fixture("scenario.json"); scenario["target_rows"] = 50
        self.scenario.write_bytes(a.canonical_bytes(scenario)); completed = self.cli(executable)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertIsNone(model(result)["prediction"])
        self.assertIn("target_at_or_above_oom_boundary", model(result)["warning_codes"])
        self.assertFalse(result["readiness_pass"])

        value = fixture(); value["receipts"][0]["measurement"]["execution"]["reused"] = None
        self.series.write_bytes(a.canonical_bytes(value)); completed = self.cli(executable)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("reuse_unknown", model(json.loads(completed.stdout))["excluded_observations"][0]["reason_codes"])

        value = fixture()
        for receipt in value["receipts"][-3:]:
            receipt["measurement"]["quality"] = {"status": "unknown", "source": "none"}
        self.series.write_bytes(a.canonical_bytes(value)); completed = self.cli(executable)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(model(json.loads(completed.stdout))["held_out"]["status"], "incomplete")

        value["manifest"] = json.loads((FIXTURES / "research-default.json").read_text())
        value["receipts"] = [json.loads((FIXTURES / "legacy-receipts.json").read_text())]
        self.series.write_bytes(a.canonical_bytes(value)); completed = self.cli(executable)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIsNone(model(json.loads(completed.stdout))["coefficients"])


if __name__ == "__main__":
    unittest.main()
