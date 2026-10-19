"""Deterministic synthetic estimation over already retained, untrusted metadata."""

from fractions import Fraction
import time

from .assessment import (AssessmentError, MAX_INTEGER, canonical_bytes, choice, fields,
                         manifest_digest, manifest_summary, number, opaque_id, reject,
                         validate_manifest)
from .assessment_preview import retained_record, validate_receipts


SERIES_LIMIT_BYTES = 262144
OUTPUT_LIMIT_BYTES = 262144
VALIDATION_BUDGET_SECONDS = 30
MAX_RATIONAL_BITS = 256
METRICS = ("cpu_consumed", "execution_duration")
SUPPORTED_SCOPES = {"cpu_consumed": "process", "execution_duration": "whole_attempt"}
MODEL_METHOD = "affine_size_means_ols_v1"


def bounded(value):
    value = Fraction(value)
    if value.numerator.bit_length() > MAX_RATIONAL_BITS or value.denominator.bit_length() > MAX_RATIONAL_BITS:
        raise AssessmentError("Assessment arithmetic exceeds its supported bounds.")
    return value


def rational(value):
    if value is None:
        return None
    value = bounded(value)
    return {"numerator": str(value.numerator), "denominator": str(value.denominator)}


def rounded_up(value):
    value = bounded(value)
    if value < 0:
        reject()
    return str(-(-value.numerator // value.denominator))


def product(values):
    result = 1
    for value in values:
        result *= value
        number(result, 1, MAX_INTEGER)
    return result


def validate_scenario(value):
    fields(value, ("schema", "scenario_id", "target_rows", "stage_executions", "experiments",
                   "repetitions", "retry_attempts_per_execution", "overheads", "held_out_tolerance"))
    if value["schema"] != "waypoint.assessment-scenario/v1":
        reject()
    opaque_id(value["scenario_id"])
    number(value["target_rows"], 1, 1000000000)
    for name in ("stage_executions", "experiments", "repetitions"):
        number(value[name], 1, 1000000)
    number(value["retry_attempts_per_execution"], 0, 10)
    fields(value["overheads"], ("setup", "transfer", "idle"))
    for overhead in value["overheads"].values():
        fields(overhead, ("scope", "basis", "cpu_nanoseconds", "duration_nanoseconds"))
        choice(overhead["scope"], ("once", "per_attempt"))
        choice(overhead["basis"], ("additional_outside_measured_execution",))
        for name in ("cpu_nanoseconds", "duration_nanoseconds"):
            if overhead[name] is not None:
                number(overhead[name])
    tolerance = value["held_out_tolerance"]
    fields(tolerance, ("relative_parts_per_million", "absolute_nanoseconds"))
    number(tolerance["relative_parts_per_million"], 0, 1000000)
    number(tolerance["absolute_nanoseconds"])
    product(value[k] for k in ("stage_executions", "experiments", "repetitions"))
    return value


def validate_series(value):
    fields(value, ("schema", "series_id", "manifest", "receipts"))
    if value["schema"] != "waypoint.assessment-series/v1":
        reject()
    opaque_id(value["series_id"])
    manifest = validate_manifest(value["manifest"])
    if manifest["data_classification"] != "synthetic":
        reject()
    receipts = value["receipts"]
    if type(receipts) is not list or len(receipts) > manifest["ceilings"]["max_attempts"]:
        reject()
    if len(canonical_bytes(value)) > SERIES_LIMIT_BYTES:
        reject()
    return value


def common_exclusions(record):
    reasons = []
    execution = record["execution_claim"]
    if record["observation"]["source_kind"] != "synthetic_fixture":
        reasons.append("source_not_synthetic")
    if execution["status"] != "succeeded":
        reasons.append("execution_" + execution["status"])
    if execution["failure_category"] == "out_of_memory":
        reasons.append("out_of_memory")
    if not execution["admitted"] or not execution["execution_started"] or execution["cancel_requested"]:
        reasons.append("execution_not_eligible")
    if execution["reused"] is not False:
        reasons.append("reuse_unknown" if execution["reused"] is None else "reused")
    if record["quality_claim"]["status"] != "passed":
        reasons.append("quality_" + record["quality_claim"]["status"])
    return reasons


def median(values):
    values = sorted(Fraction(v) for v in values)
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


def metric_model(manifest, rows, metric_name, scenario):
    eligible, excluded = [], []
    profiles = set()
    for row in rows:
        record = row["record"]
        metric = record["measured_consumption"][metric_name]
        binding = record["binding"]
        identity = {"attempt_id": binding["attempt_id"], "sample_role": binding["sample_role"],
                    "sample_size": binding["sample_size"], "repetition": binding["repetition"],
                    "measurement_sha256": row["measurement_sha256"]}
        reasons = common_exclusions(record)
        if metric["availability"] != "available":
            reasons.append("metric_" + metric["availability"])
        if reasons:
            excluded.append({**identity, "reason_codes": reasons})
        else:
            if binding["sample_role"] == "fitting":
                profiles.add((metric["unit"], metric["scope"], metric["source"]))
            eligible.append({**identity, "value": metric["value"], "unit": metric["unit"],
                             "scope": metric["scope"], "source": metric["source"]})
    fitting = [r for r in eligible if r["sample_role"] == "fitting"]
    held_out = [r for r in eligible if r["sample_role"] == "held_out"]
    if len(profiles) == 1:
        mismatched = [r for r in held_out if (r["unit"], r["scope"], r["source"]) not in profiles]
        for row in mismatched:
            excluded.append({k: v for k, v in row.items() if k not in ("value", "unit", "scope", "source")} |
                            {"reason_codes": ["held_out_provenance_mismatch"]})
        held_out = [r for r in held_out if r not in mismatched]
        eligible = fitting + held_out
    design = manifest["design"]
    declared_complete = len(fitting) == len(design["fitting_sizes"]) * design["repetitions"] and len(held_out) == design["repetitions"]
    default_complete = declared_complete and len(design["fitting_sizes"]) == 3 and design["repetitions"] == 3
    warnings = []
    if not default_complete:
        warnings.append("insufficient_default_samples")
    if design["smaller_design"] is not None:
        warnings.append("smaller_design_provisional")
    oom_sizes = [r["record"]["binding"]["sample_size"] for r in rows
                 if r["record"]["execution_claim"]["failure_category"] == "out_of_memory"]
    oom_boundary = min(oom_sizes) if oom_sizes else None
    if oom_boundary is not None:
        warnings.append("out_of_memory_observed")
    variability = []
    for role in ("fitting", "held_out"):
        for size in sorted({r["sample_size"] for r in eligible if r["sample_role"] == role}):
            values = [r["value"] for r in eligible if r["sample_role"] == role and r["sample_size"] == size]
            variability.append({"sample_role": role, "sample_size": size, "count": len(values),
                                "minimum": min(values), "median": rational(median(values)),
                                "maximum": max(values), "range": max(values) - min(values)})
    result = {"metric": metric_name, "unit": "nanoseconds", "method": MODEL_METHOD,
              "provenance": {"source_kind": "synthetic_fixture", "profiles":
                             [{"unit": u, "scope": s, "source": p} for u, s, p in sorted(profiles)]},
              "selected_observations": eligible, "excluded_observations": excluded,
              "declared_design_complete": declared_complete, "default_design_complete": default_complete,
              "repeat_variability": variability, "confidence_interval": None,
              "prediction_uncertainty": "not_estimated", "status": "unavailable",
              "coefficients": None, "prediction": None, "held_out": {"status": "not_evaluated", "errors": []},
              "warning_codes": warnings, "oom_boundary_rows": oom_boundary,
              "fitting_range_rows": None, "observed_range_rows": None}
    if len(profiles) > 1:
        warnings.append("mixed_metric_provenance")
        return result, None
    if profiles and next(iter(profiles))[1] != SUPPORTED_SCOPES[metric_name]:
        warnings.append("unsupported_metric_scope")
        return result, None
    sizes = sorted({r["sample_size"] for r in fitting})
    if len(sizes) < 2:
        warnings.append("insufficient_fitting_sizes")
        return result, None
    means = {size: sum(Fraction(r["value"]) for r in fitting if r["sample_size"] == size) /
                  sum(r["sample_size"] == size for r in fitting) for size in sizes}
    x_mean = Fraction(sum(sizes), len(sizes))
    y_mean = sum(means.values()) / len(sizes)
    slope = bounded(sum((x - x_mean) * (means[x] - y_mean) for x in sizes) /
                    sum((x - x_mean)**2 for x in sizes))
    intercept = bounded(y_mean - slope * x_mean)
    result["coefficients"] = {"intercept_nanoseconds": rational(intercept),
                              "slope_nanoseconds_per_row": rational(slope),
                              "size_means": [{"rows": x, "nanoseconds": rational(means[x])} for x in sizes]}
    result["fitting_range_rows"] = {"minimum": min(sizes), "maximum": max(sizes)}
    observed_sizes = [r["sample_size"] for r in eligible]
    result["observed_range_rows"] = {"minimum": min(observed_sizes), "maximum": max(observed_sizes)}
    if slope < 0 or any(intercept + slope*x < 0 for x in sizes):
        warnings.append("unsupported_negative_model")
        return result, None
    errors = []
    tolerance = scenario["held_out_tolerance"]
    for row in held_out:
        prediction = bounded(intercept + slope*row["sample_size"])
        absolute = abs(prediction - row["value"])
        relative = absolute / row["value"] if row["value"] != 0 else None
        limit = max(Fraction(tolerance["absolute_nanoseconds"]),
                    Fraction(row["value"]*tolerance["relative_parts_per_million"], 1000000))
        errors.append({"attempt_id": row["attempt_id"], "prediction_nanoseconds": rational(prediction),
                       "observed_nanoseconds": row["value"], "absolute_error_nanoseconds": rational(absolute),
                       "relative_error": rational(relative), "within_declared_tolerance": prediction >= 0 and absolute <= limit})
    evaluation = "incomplete" if len(errors) != design["repetitions"] else "within_declared_tolerance" if all(e["within_declared_tolerance"] for e in errors) else "outside_declared_tolerance"
    result["held_out"] = {"status": evaluation, "errors": errors,
                          "mean_absolute_error_nanoseconds": rational(sum(Fraction(int(e["absolute_error_nanoseconds"]["numerator"]), int(e["absolute_error_nanoseconds"]["denominator"])) for e in errors) / len(errors)) if errors else None}
    if evaluation != "within_declared_tolerance":
        warnings.append("held_out_" + evaluation)
    target = scenario["target_rows"]
    if target < min(sizes) or target > max(sizes):
        warnings.append("outside_fitting_range")
    if target < min(observed_sizes) or target > max(observed_sizes):
        warnings.append("outside_observed_range")
    prediction = bounded(intercept + slope*target)
    if prediction < 0:
        warnings.append("unsupported_negative_prediction")
        return result, None
    if oom_boundary is not None and target >= oom_boundary:
        warnings.append("target_at_or_above_oom_boundary")
        return result, None
    result.update(status="provisional", prediction={"target_rows": target, "nanoseconds": rational(prediction),
                                                   "rounded_up_nanoseconds": rounded_up(prediction)})
    return result, prediction


def demand_total(prediction, attempts, scenario, metric):
    dimension = "cpu_nanoseconds" if metric == "cpu_consumed" else "duration_nanoseconds"
    unknown = [name + "_unknown" for name, value in sorted(scenario["overheads"].items()) if value[dimension] is None]
    modeled = bounded(prediction * attempts) if prediction is not None else None
    if modeled is None:
        unknown.append("prediction_unavailable")
    overhead = sum(value[dimension] * (attempts if value["scope"] == "per_attempt" else 1)
                   for value in scenario["overheads"].values() if value[dimension] is not None)
    total = bounded(modeled + overhead) if not unknown else None
    return {"kind": "cpu_demand" if metric == "cpu_consumed" else "serial_busy_time",
            "attempts": attempts, "modeled_nanoseconds": rational(modeled), "total_nanoseconds": rational(total),
            "rounded_up_total_nanoseconds": rounded_up(total) if total is not None else None,
            "hours": rational(total / 3600000000000) if total is not None else None,
            "overhead_provenance": "declared_synthetic_assumption", "reason_codes": unknown,
            "elapsed_completion_time": None}


def estimate_series(series, scenario, rows):
    manifest = series["manifest"]
    multipliers = [scenario[k] for k in ("stage_executions", "experiments", "repetitions")]
    multipliers.append(1 + scenario["retry_attempts_per_execution"])
    instruction = manifest["instruction"]
    if instruction is not None:
        multipliers.extend((instruction["enrollment"], instruction["assignment_count"]))
    attempts = product(multipliers)
    models, totals = {}, {}
    for metric in METRICS:
        models[metric], prediction = metric_model(manifest, rows, metric, scenario)
        totals[metric] = demand_total(prediction, attempts, scenario, metric)
    capacities = {}
    for metric in ("peak_memory", "scratch_peak", "output_peak"):
        observations = [r["record"]["measured_consumption"][metric] for r in rows if not common_exclusions(r["record"])
                        and r["record"]["measured_consumption"][metric]["availability"] == "available"]
        scopes = {m["scope"] for m in observations}
        capacities[metric] = {"unit": "bytes", "observation_count": len(observations),
                              "observed_maximum": max((m["value"] for m in observations), default=None) if len(scopes) <= 1 else None,
                              "scopes": sorted(scopes), "target_capacity": None,
                              "reason": "mixed_metric_provenance" if len(scopes) > 1 else "target_capacity_not_qualified"}
    instruction_summary = None if instruction is None else {
        **instruction, "persistent_storage_assumption_bytes": product((instruction["enrollment"], instruction["per_student_storage_bytes"])),
        "concurrent_working_memory_bytes": None, "concurrent_working_storage_bytes": None,
        "reason": "concurrency_not_observed"}
    metadata = manifest_summary(manifest)
    # Calculation is reproducible from historical metadata; expiry never grants
    # execution, and current-clock admission belongs outside this pure path.
    metadata.pop("expired")
    metadata.update(expires_at=manifest["expires_at"], expiry_use="historical_metadata_only")
    return {"series_id": series["series_id"], "manifest_sha256": manifest_digest(manifest),
            "manifest_summary": metadata, "pathway": manifest["pathway"],
            "software": manifest["workload"], "hardware": manifest["hardware"], "measurement": manifest["measurement"],
            "models": models, "totals": totals, "capacity_observations": capacities,
            "instruction": instruction_summary, "missing_stages": ["setup", "transfer", "idle"],
            "cleanup_claims": [{"attempt_id": r["record"]["binding"]["attempt_id"],
                                "cleanup_sha256": r["cleanup_sha256"], **r["record"]["cleanup_claim"]} for r in rows],
            "receipt_count": len(rows), "planned_slots": manifest["ceilings"]["max_attempts"],
            "provisional": True, "readiness_pass": False, "actual_resource_accepted": False,
            "execution_authorized": False, "cleanup_trusted": False, "retention_finalized": False}


def comparison(results):
    if len(results) != 2:
        return None
    left, right = results
    differences = {}
    for metric in METRICS:
        a, b = left["totals"][metric]["total_nanoseconds"], right["totals"][metric]["total_nanoseconds"]
        compatible = (left["pathway"] == right["pathway"] and left["instruction"] == right["instruction"]
                      and left["models"][metric]["provenance"] == right["models"][metric]["provenance"])
        delta = None
        if compatible and a is not None and b is not None:
            delta = bounded(Fraction(int(b["numerator"]), int(b["denominator"])) - Fraction(int(a["numerator"]), int(a["denominator"])))
        differences[metric] = {"unit": "nanoseconds", "right_minus_left": rational(delta),
                               "reason": "synthetic_assumption_only" if delta is not None else "incompatible_or_unavailable",
                               "left_warning_codes": left["models"][metric]["warning_codes"],
                               "right_warning_codes": right["models"][metric]["warning_codes"]}
    return {"left_series_id": left["series_id"], "right_series_id": right["series_id"],
            "differences": differences, "winner": None, "target_resource_accepted": False}


def estimate(scenario, series_values, executable):
    validate_scenario(scenario)
    if type(series_values) is not list or not 1 <= len(series_values) <= 2:
        reject()
    for series in series_values:
        validate_series(series)
    if len({v["series_id"] for v in series_values}) != len(series_values):
        reject()
    if len({v["manifest"]["project_id"] for v in series_values}) != 1:
        reject()
    deadline = time.monotonic() + VALIDATION_BUDGET_SECONDS
    results = []
    for series in sorted(series_values, key=lambda s: s["series_id"]):
        rows, attempts, receipt_ids = [], set(), set()
        for receipts in series["receipts"]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssessmentError("Assessment validation exceeded its aggregate deadline.")
            result = validate_receipts(series["manifest"], receipts, executable, timeout=remaining)
            try:
                record = retained_record(series["manifest"], result)
                attempt = record["binding"]["attempt_id"]
                receipt_id = receipts["cleanup"]["receipt_id"]
            except (ValueError, KeyError, TypeError, IndexError):
                raise AssessmentError("Assessment series contains unsupported retained metadata.") from None
            if attempt in attempts or receipt_id in receipt_ids:
                reject()
            attempts.add(attempt); receipt_ids.add(receipt_id)
            rows.append({"record": record, "measurement_sha256": result["measurement_sha256"],
                         "cleanup_sha256": result["cleanup_sha256"]})
        rows.sort(key=lambda r: tuple(r["record"]["binding"][k] for k in
                                     ("sample_role", "sample_size", "repetition", "attempt_id")))
        results.append(estimate_series(series, scenario, rows))
    if time.monotonic() >= deadline:
        raise AssessmentError("Assessment validation exceeded its aggregate deadline.")
    result = {"schema": "waypoint.assessment-estimate/v1", "scenario": scenario,
              "scenario_sha256": manifest_digest(scenario), "method": MODEL_METHOD,
              "arithmetic": {"kind": "exact_rational", "maximum_bits": MAX_RATIONAL_BITS,
                             "coefficient_rounding": "none", "demand_rounding": "ceiling_base_unit",
                             "nanoseconds_per_hour": 3600000000000},
              "series": results, "comparison": comparison(results), "provenance": "synthetic_only",
              "readiness_pass": False, "actual_resource_accepted": False,
              "execution_authorized": False, "cleanup_trusted": False,
              "upload_admission": False, "retention_finalized": False,
              "provider_conversions": "not_supported"}
    if len(canonical_bytes(result)) > OUTPUT_LIMIT_BYTES:
        raise AssessmentError("Assessment estimate exceeds its output limit.")
    return result
