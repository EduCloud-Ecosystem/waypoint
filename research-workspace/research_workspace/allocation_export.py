"""Local draft allocation exports from explicitly approved retained metadata."""

from fractions import Fraction
import hashlib
import json
import re
import unicodedata

from . import assessment as a
from . import assessment_estimation as e

ESTIMATE_LIMIT = 262144
NARRATIVE_LIMIT = 32768
PREVIEW_LIMIT = 1048576
GATES = ("current_requirements_ready", "readiness_pass", "actual_resource_accepted",
         "execution_authorized", "upload_admission", "cleanup_trusted", "retention_finalized")
PROFILES = {
    "access_explore": {"title": "ACCESS Explore - optional planning copy", "pages": 2, "pathway": None,
        "narrative": ("title", "planned_work_summary", "keywords", "field_of_science", "supporting_grant_details", "preferred_resource", "alternative_resource")},
    "access_accelerate": {"title": "ACCESS Accelerate", "pages": 3, "pathway": None,
        "narrative": ("title", "planned_work_summary", "keywords", "field_of_science", "supporting_grant_details", "research_objectives",
                      "resource_estimates", "computational_plan", "software_special_needs", "team_preparedness", "preferred_resource", "alternative_resource")},
    "access_discover": {"title": "ACCESS Discover", "pages": 1, "pathway": None,
        "narrative": ("title", "objectives", "resource_needs", "preferred_resource", "alternative_resource")},
    "nairr_research_draft": {"title": "NAIRR research - internal draft", "pages": 8, "pathway": "research",
        "narrative": ("title", "research_objectives", "workload_plan", "resource_justification", "preferred_resource", "alternative_resource")},
    "nairr_classroom_draft": {"title": "NAIRR Classroom - internal draft", "pages": 8, "pathway": "instructional",
        "narrative": ("title", "learning_objectives", "course_schedule", "assignment_plan", "access_and_support", "preferred_resource", "alternative_resource")},
}
LABELS = {"planned_work_summary": "Planned work summary", "keywords": "Project keywords", "field_of_science": "Field of science",
          "supporting_grant_details": "Supporting grant details (operator-declared)", "resource_estimates": "Compute, storage and other resource estimates",
          "computational_plan": "Computational plan", "software_special_needs": "Software and specialized needs", "team_preparedness": "Team and preparedness",
          "title": "Project title", "objectives": "Research or education objectives", "resource_needs": "Resource needs",
          "preferred_resource": "Preferred resource (unverified)", "alternative_resource": "Alternative resource (unverified)",
          "research_objectives": "Research objectives", "workload_plan": "Workload and computational plan",
          "resource_justification": "Resource justification", "learning_objectives": "Learning objectives",
          "course_schedule": "Course schedule", "assignment_plan": "Assignment plan", "access_and_support": "Access and support"}


def literal(*allowed):
    def validate(value):
        if not any(type(value) is type(item) and value == item for item in allowed):
            a.reject()
    return validate


def array(spec, maximum=12):
    def validate(value):
        if type(value) is not list or len(value) > maximum:
            a.reject()
        for item in value:
            check(item, spec)
    return validate


def nullable(spec):
    return lambda value: check(value, spec) if value is not None else None


def check(value, spec):
    if type(spec) is dict:
        a.fields(value, spec)
        for key, child in spec.items():
            check(value[key], child)
    else:
        spec(value)


def digest(value):
    if type(value) is not str or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        a.reject()


def flag(value):
    if type(value) is not bool:
        a.reject()


def integer_string(value, *, positive=False):
    if type(value) is not str or re.fullmatch(r"-?(0|[1-9][0-9]{0,77})", value) is None or value == "-0":
        a.reject()
    parsed = int(value)
    if abs(parsed).bit_length() > e.MAX_RATIONAL_BITS or positive and parsed <= 0:
        a.reject()
    return parsed


def rational(value):
    a.fields(value, ("numerator", "denominator"))
    numerator = integer_string(value["numerator"])
    denominator = integer_string(value["denominator"], positive=True)
    result = Fraction(numerator, denominator)
    if e.rational(result) != value:
        a.reject()
    return result


NAT = a.number
FALSE = literal(False)
ROLE = literal("fitting", "held_out")
SCOPE = literal("whole_attempt", "process", "sandbox")
OBSERVATION = {"source_kind": literal("synthetic_fixture", "legacy_projection", "unverified_observation"),
               "authenticated": FALSE, "independently_verified": FALSE}
IDENTITY = {"attempt_id": a.opaque_id, "sample_role": ROLE, "sample_size": NAT, "repetition": NAT, "measurement_sha256": digest}
EXCLUSIONS = literal("source_not_synthetic", "execution_reserved", "execution_creating", "execution_running",
                     "execution_failed", "execution_cancelled", "execution_unknown", "out_of_memory", "execution_not_eligible",
                     "reuse_unknown", "reused", "quality_unknown", "quality_failed", "metric_unknown", "metric_unsupported",
                     "held_out_provenance_mismatch")
WARNINGS = literal("insufficient_default_samples", "smaller_design_provisional", "out_of_memory_observed",
                   "mixed_metric_provenance", "unsupported_metric_scope", "insufficient_fitting_sizes",
                   "unsupported_negative_model", "held_out_incomplete", "held_out_outside_declared_tolerance",
                   "outside_fitting_range", "outside_observed_range", "unsupported_negative_prediction", "target_at_or_above_oom_boundary")
LIABILITIES = literal("termination_unresolved", "access_revocation_unknown", "execution_cleanup_unknown",
                      "data_cleanup_unknown", "recovery_cleanup_unknown", "accounting_unresolved")
CLEANUP = {"attempt_id": a.opaque_id, "cleanup_sha256": digest, "observation": OBSERVATION,
           "outcome": literal("unresolved"), "termination": literal("verified", "failed", "unknown"),
           "access_revocation": literal("verified", "failed", "unknown"),
           "observed_scopes": array({"kind": literal("execution_resource", "temporary_data", "recovery_copies"),
                                      "scope_id": a.opaque_id, "deletion": literal("reported_complete", "unknown", "failed", "not_applicable")}, 3),
           "observed_at": nullable(a.timestamp), "method": {"name": literal("legacy_provider_assertion", "unverified_scope_observation"), "version": literal(1)},
           "verifier_declared": flag, "remaining_liabilities": array(LIABILITIES, 6), "quarantine": literal(True), "reuse_denied": literal(True)}


def held_out(value):
    if type(value) is not dict:
        a.reject()
    shape = {"status": literal("not_evaluated", "incomplete", "within_declared_tolerance", "outside_declared_tolerance"),
             "errors": array({"attempt_id": a.opaque_id, "prediction_nanoseconds": rational, "observed_nanoseconds": NAT,
                              "absolute_error_nanoseconds": rational, "relative_error": nullable(rational), "within_declared_tolerance": flag}, 3)}
    if value.get("status") != "not_evaluated":
        shape["mean_absolute_error_nanoseconds"] = nullable(rational)
    check(value, shape)


MODEL = {"metric": literal(*e.METRICS), "unit": literal("nanoseconds"), "method": literal(e.MODEL_METHOD),
         "provenance": {"source_kind": literal("synthetic_fixture"), "profiles": array({"unit": literal("nanoseconds"), "scope": SCOPE, "source": literal("unverified_collector")}, 3)},
         "selected_observations": array({**IDENTITY, "value": NAT, "unit": literal("nanoseconds"), "scope": SCOPE, "source": literal("unverified_collector")}),
         "excluded_observations": array({**IDENTITY, "reason_codes": array(EXCLUSIONS, 10)}),
         "declared_design_complete": flag, "default_design_complete": flag,
         "repeat_variability": array({"sample_role": ROLE, "sample_size": NAT, "count": NAT, "minimum": NAT,
                                      "median": rational, "maximum": NAT, "range": NAT}, 4),
         "confidence_interval": literal(None), "prediction_uncertainty": literal("not_estimated"),
         "status": literal("unavailable", "provisional"),
         "coefficients": nullable({"intercept_nanoseconds": rational, "slope_nanoseconds_per_row": rational,
                                   "size_means": array({"rows": NAT, "nanoseconds": rational}, 3)}),
         "prediction": nullable({"target_rows": NAT, "nanoseconds": rational, "rounded_up_nanoseconds": integer_string}),
         "held_out": held_out, "warning_codes": array(WARNINGS, 16), "oom_boundary_rows": nullable(NAT),
         "fitting_range_rows": nullable({"minimum": NAT, "maximum": NAT}), "observed_range_rows": nullable({"minimum": NAT, "maximum": NAT})}


def validate_estimate(value, manifests):
    a.fields(value, ("schema", "scenario", "scenario_sha256", "method", "arithmetic", "series", "comparison", "provenance",
                     "readiness_pass", "actual_resource_accepted", "execution_authorized", "cleanup_trusted", "upload_admission",
                     "retention_finalized", "provider_conversions"))
    if value["schema"] != "waypoint.assessment-estimate/v1" or value["method"] != e.MODEL_METHOD or value["provenance"] != "synthetic_only" or value["provider_conversions"] != "not_supported":
        a.reject()
    for key in GATES:
        if key in value and value[key] is not False:
            a.reject()
    e.validate_scenario(value["scenario"])
    if value["scenario_sha256"] != a.manifest_digest(value["scenario"]):
        a.reject()
    expected_arithmetic = {"kind": "exact_rational", "maximum_bits": 256, "coefficient_rounding": "none",
                           "demand_rounding": "ceiling_base_unit", "nanoseconds_per_hour": 3600000000000}
    if a.canonical_bytes(value["arithmetic"]) != a.canonical_bytes(expected_arithmetic):
        a.reject()
    if type(manifests) is not list or not 1 <= len(manifests) <= 2 or type(value["series"]) is not list or not 1 <= len(value["series"]) <= 2:
        a.reject()
    catalog = {}
    for manifest in manifests:
        a.validate_manifest(manifest)
        if manifest["data_classification"] != "synthetic":
            a.reject()
        catalog[a.manifest_digest(manifest)] = manifest
    if len(catalog) != len(manifests) or len({m["project_id"] for m in manifests}) != 1:
        a.reject()
    used, ids = set(), set()
    for series in value["series"]:
        a.fields(series, ("series_id", "manifest_sha256", "manifest_summary", "pathway", "software", "hardware", "measurement",
                          "models", "totals", "capacity_observations", "instruction", "missing_stages", "cleanup_claims",
                          "receipt_count", "planned_slots", "provisional", "readiness_pass", "actual_resource_accepted",
                          "execution_authorized", "cleanup_trusted", "retention_finalized"))
        a.opaque_id(series["series_id"]); digest(series["manifest_sha256"])
        if series["series_id"] in ids or series["manifest_sha256"] not in catalog:
            a.reject()
        ids.add(series["series_id"]); used.add(series["manifest_sha256"])
        manifest = catalog[series["manifest_sha256"]]
        summary = a.manifest_summary(manifest); summary.pop("expired")
        summary.update(expires_at=manifest["expires_at"], expiry_use="historical_metadata_only")
        for key, expected in (("manifest_summary", summary), ("pathway", manifest["pathway"]), ("software", manifest["workload"]),
                              ("hardware", manifest["hardware"]), ("measurement", manifest["measurement"]),
                              ("planned_slots", manifest["ceilings"]["max_attempts"]), ("missing_stages", ["setup", "transfer", "idle"])):
            if a.canonical_bytes({"value": series[key]}) != a.canonical_bytes({"value": expected}):
                a.reject()
        if series["provisional"] is not True or any(series[k] is not False for k in ("readiness_pass", "actual_resource_accepted", "execution_authorized", "cleanup_trusted", "retention_finalized")):
            a.reject()
        a.number(series["receipt_count"], 0, series["planned_slots"])
        check(series["cleanup_claims"], array(CLEANUP))
        cleanup_ids = {c["attempt_id"] for c in series["cleanup_claims"]}
        if len(cleanup_ids) != len(series["cleanup_claims"]) or len(cleanup_ids) != series["receipt_count"]:
            a.reject()
        for claim in series["cleanup_claims"]:
            if len(claim["observed_scopes"]) != 3:
                a.reject()
            for index, kind in enumerate(("execution_resource", "temporary_data", "recovery_copies")):
                scope = claim["observed_scopes"][index]
                if scope["kind"] != kind or scope["scope_id"] != (claim["attempt_id"] if index == 0 else manifest["assessment_id"]):
                    a.reject()
        slots = {s["attempt_id"]: s for s in manifest["reservations"]}
        if not cleanup_ids <= set(slots):
            a.reject()
        a.fields(series["models"], e.METRICS); a.fields(series["totals"], e.METRICS)
        counts = [value["scenario"][k] for k in ("stage_executions", "experiments", "repetitions")]
        counts.append(1 + value["scenario"]["retry_attempts_per_execution"])
        instruction = manifest["instruction"]
        if instruction is not None:
            counts.extend((instruction["enrollment"], instruction["assignment_count"]))
        attempts = e.product(counts)
        for name in e.METRICS:
            model = series["models"][name]; check(model, MODEL)
            if model["metric"] != name:
                a.reject()
            observations = model["selected_observations"] + model["excluded_observations"]
            if {r["attempt_id"] for r in observations} != cleanup_ids or len(observations) != len(cleanup_ids):
                a.reject()
            for row in observations:
                if any(row[k] != slots[row["attempt_id"]][k] for k in ("sample_role", "sample_size", "repetition")):
                    a.reject()
            validate_model_math(model, manifest, value["scenario"])
            prediction = None if model["prediction"] is None else rational(model["prediction"]["nanoseconds"])
            expected = e.demand_total(prediction, attempts, value["scenario"], name)
            if a.canonical_bytes(series["totals"][name]) != a.canonical_bytes(expected):
                a.reject()
        check(series["capacity_observations"], {name: {"unit": literal("bytes"), "observation_count": NAT,
              "observed_maximum": nullable(NAT), "scopes": array(SCOPE, 3), "target_capacity": literal(None),
              "reason": literal("mixed_metric_provenance", "target_capacity_not_qualified")}
              for name in ("peak_memory", "scratch_peak", "output_peak")})
        for capacity in series["capacity_observations"].values():
            if capacity["observation_count"] > series["receipt_count"] or len(capacity["scopes"]) != len(set(capacity["scopes"])):
                a.reject()
            if capacity["observation_count"] == 0 and (capacity["observed_maximum"] is not None or capacity["scopes"]):
                a.reject()
        expected_instruction = None if instruction is None else {**instruction,
            "persistent_storage_assumption_bytes": e.product((instruction["enrollment"], instruction["per_student_storage_bytes"])),
            "concurrent_working_memory_bytes": None, "concurrent_working_storage_bytes": None, "reason": "concurrency_not_observed"}
        if a.canonical_bytes({"v": series["instruction"]}) != a.canonical_bytes({"v": expected_instruction}):
            a.reject()
    if used != set(catalog) or a.canonical_bytes({"v": value["comparison"]}) != a.canonical_bytes({"v": e.comparison(value["series"])}):
        a.reject()
    return value


def export_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def export_digest(value):
    return hashlib.sha256(export_bytes(value)).hexdigest()


def profile_info(profile):
    if profile not in PROFILES:
        raise a.AssessmentError("This program or ACCESS tier has no complete export profile.")
    return PROFILES[profile]


def validate_narrative(value, profile):
    info = profile_info(profile)
    a.fields(value, ("schema", "fields"))
    if value["schema"] != "waypoint.allocation-narrative/v1":
        a.reject()
    a.fields(value["fields"], info["narrative"])
    for key, text in value["fields"].items():
        if type(text) is not str or not text.strip() or len(text) > (160 if key == "title" else 2048):
            a.reject()
        if unicodedata.normalize("NFC", text) != text or any(unicodedata.category(c).startswith("C") and c != "\n" for c in text):
            a.reject()
    if len(export_bytes(value)) > NARRATIVE_LIMIT:
        a.reject()
    return value


def read_bytes(path, limit):
    """Stable bounded single-link regular file; never print its path or contents."""
    import os
    import stat
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= limit:
            a.reject()
        with os.fdopen(fd, "rb") as source:
            fd = None
            payload = source.read(limit + 1)
            after = os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or len(payload) != before.st_size:
            a.reject()
        return payload
    except (OSError, ValueError):
        raise a.AssessmentError("Export input could not be read as a bounded regular file.") from None
    finally:
        if fd is not None:
            os.close(fd)


def read_export_document(path, *, limit=PREVIEW_LIMIT):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                a.reject()
            result[key] = value
        return result

    def tree(value, depth=0):
        if depth > 20:
            a.reject()
        if type(value) is dict:
            if len(value) > 64:
                a.reject()
            for key, child in value.items():
                if not key.isascii() or len(key) > 96:
                    a.reject()
                tree(child, depth + 1)
        elif type(value) is list:
            if len(value) > 128:
                a.reject()
            for child in value:
                tree(child, depth + 1)
        elif type(value) is str:
            if len(value) > 8192:
                a.reject()
        elif type(value) is int:
            if abs(value) > a.MAX_INTEGER:
                a.reject()
        elif value is not None and type(value) is not bool:
            a.reject()

    def noninteger(_):
        a.reject()
    try:
        value = json.loads(read_bytes(path, limit).decode("utf-8"), object_pairs_hook=pairs,
                           parse_float=noninteger, parse_constant=noninteger)
        tree(value)
        if type(value) is not dict:
            a.reject()
        return value
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise a.AssessmentError("Invalid or unsupported export document.") from None


def validate_model_math(model, manifest, scenario):
    """Recompute every derived model field from the retained selected values.

    Original receipts remain untrusted claims. This checks arithmetic consistency,
    not receipt authenticity or whether an operator really measured a value.
    """
    rows = []
    for row in model["selected_observations"]:
        rows.append({"measurement_sha256": row["measurement_sha256"], "record": {
            "binding": row, "observation": {"source_kind": "synthetic_fixture"},
            "execution_claim": {"status": "succeeded", "failure_category": None, "admitted": True,
                                "execution_started": True, "cancel_requested": False, "reused": False},
            "quality_claim": {"status": "passed"}, "measured_consumption": {model["metric"]: {
                "availability": "available", **{k: row[k] for k in ("value", "unit", "scope", "source")}}}}})
    for row in model["excluded_observations"]:
        if not row["reason_codes"] or len(set(row["reason_codes"])) != len(row["reason_codes"]):
            a.reject()
        if "out_of_memory" in row["reason_codes"]:
            rows.append({"measurement_sha256": row["measurement_sha256"], "record": {
                "binding": row, "observation": {"source_kind": "synthetic_fixture"},
                "execution_claim": {"status": "failed", "failure_category": "out_of_memory", "admitted": True,
                                    "execution_started": True, "cancel_requested": False, "reused": False},
                "quality_claim": {"status": "unknown"}, "measured_consumption": {model["metric"]: {"availability": "unknown"}}}})
    expected, _ = e.metric_model(manifest, rows, model["metric"], scenario)
    for key in expected:
        if key != "excluded_observations" and export_bytes(expected[key]) != export_bytes(model[key]):
            a.reject()


def requirements(profile, source, *, include_planning_pdf=False):
    from .allocation_sources import ACCESS_PROFILES, URLS
    profile_info(profile)
    if type(include_planning_pdf) is not bool or include_planning_pdf and profile != "access_explore":
        a.reject()
    a.fields(source, ("url", "format_review_date", "status", "retrieved_at", "content_sha256", "current_requirements_ready"))
    if source["url"] != URLS[profile] or source["current_requirements_ready"] is not False:
        a.reject()
    if profile in ACCESS_PROFILES:
        if source["format_review_date"] != "2026-10-19":
            a.reject()
        a.choice(source["status"], ("snapshot_only", "retrieved_unreviewed", "unavailable"))
        if source["retrieved_at"] is not None:
            a.timestamp(source["retrieved_at"])
        if source["status"] == "retrieved_unreviewed":
            digest(source["content_sha256"])
            if source["retrieved_at"] is None:
                a.reject()
        elif source["content_sha256"] is not None:
            a.reject()
        missing = {
            "access_discover": ["Current application and eligibility review", "PI and co-PI CV PDFs", "Project and personnel form fields",
                "Conditional graduate advisor or GRFP letter", "Selected resource availability and unit conversion review",
                "Scientific and resource expert review", "Institutional authorization and submission"],
            "access_explore": ["Current Explore summary/form correspondence and eligibility review", "PI and co-PI CV PDFs (uncollected)",
                "Project/personnel form verification", "Conditional graduate advisor or NSF GRFP letter (uncollected)",
                "Selected resource availability and units", "Scientific and resource expert review", "Institutional authorization and submission"],
            "access_accelerate": ["Current Accelerate application/form correspondence and eligibility review", "PI and co-PI CV PDFs (uncollected)",
                "Project/personnel form verification", "Confirm applicant-specific supporting-letter requirements (uncollected)",
                "Independent supporting performance data and resource-use review", "Selected resource availability and unit conversion review",
                "Scientific and resource expert review", "Institutional authorization and submission"],
        }[profile]
    elif profile in ("nairr_research_draft", "nairr_classroom_draft"):
        if source != {"url": URLS[profile], "format_review_date": None, "status": "robots_blocked_not_verified",
                      "retrieved_at": None, "content_sha256": None, "current_requirements_ready": False}:
            a.reject()
        missing = ["Current official " + ("Classroom" if profile == "nairr_classroom_draft" else "research") + " instructions and form correspondence",
                   "Eligibility and program-specific attachments", "Selected resource availability and units", "Scientific and resource expert review",
                   "Institutional authorization and submission"]
    else:
        a.reject()
    bases = {"access_discover": "reviewed_discover_description", "access_accelerate": "reviewed_accelerate_description",
             "access_explore": "internal_optional_planning_bound_not_official" if include_planning_pdf else "not_applicable_worksheet_only",
             "nairr_research_draft": "internal_draft_bound_not_official", "nairr_classroom_draft": "internal_draft_bound_not_official"}
    result = {"source": source, "profile_version": 1, "minimum_font_points": 11, "margin_inches": 1,
              "maximum_pages": None if profile == "access_explore" and not include_planning_pdf else PROFILES[profile]["pages"],
              "page_limit_basis": bases[profile], "missing_documents_and_reviews": missing, "submission_ready": False}
    if profile == "access_explore":
        if not include_planning_pdf:
            result.update(minimum_font_points=None, margin_inches=None)
        result.update(required_narrative_pdf_established=False, official_summary_length_limit_established=False,
                      optional_planning_pdf_included=include_planning_pdf)
    return result


def human_number(value):
    if value is None:
        return "unknown"
    fraction = rational(value)
    return str(fraction.numerator) if fraction.denominator == 1 else str(fraction.numerator) + "/" + str(fraction.denominator)


def readable_code(code):
    labels = {"insufficient_default_samples": "samples below the default design", "smaller_design_provisional": "smaller design remains provisional",
              "out_of_memory_observed": "an out-of-memory failure was observed", "mixed_metric_provenance": "incompatible measurement provenance",
              "unsupported_metric_scope": "unsupported measurement scope", "insufficient_fitting_sizes": "too few fitting sizes",
              "unsupported_negative_model": "unsupported negative model", "held_out_incomplete": "held-out evaluation incomplete",
              "held_out_outside_declared_tolerance": "held-out error exceeds declared tolerance", "outside_fitting_range": "target outside fitting range",
              "outside_observed_range": "target outside observed range", "unsupported_negative_prediction": "unsupported negative prediction",
              "target_at_or_above_oom_boundary": "target at or above observed memory-failure boundary",
              "termination_unresolved": "termination unresolved", "access_revocation_unknown": "access revocation unknown",
              "execution_cleanup_unknown": "execution cleanup unknown", "data_cleanup_unknown": "data cleanup unknown",
              "recovery_cleanup_unknown": "recovery-copy cleanup unknown", "accounting_unresolved": "accounting unresolved",
              "setup_unknown": "setup overhead unknown", "transfer_unknown": "transfer overhead unknown", "idle_unknown": "idle overhead unknown",
              "prediction_unavailable": "prediction unavailable"}
    return labels[code]


def visible_fields(profile, narrative, estimate, requirement):
    fields = [{"id": "narrative." + name, "label": LABELS[name], "text": narrative["fields"][name]} for name in PROFILES[profile]["narrative"]]
    fields.append({"id": "evidence", "label": "Evidence boundary", "text":
        "Synthetic arithmetic only; observations unauthenticated and not independently verified. Predictions are provisional. No target resource acceptance, execution, upload, cleanup, retention or submission authority. Credits and dollars are not calculated."})
    for index, series in enumerate(estimate["series"], 1):
        cpu, duration = (series["totals"][m] for m in e.METRICS)
        warnings = sorted({w for model in series["models"].values() for w in model["warning_codes"]} |
                          {r for total in series["totals"].values() for r in total["reason_codes"]})
        liabilities = sorted({w for claim in series["cleanup_claims"] for w in claim["remaining_liabilities"]})
        text = (f"{'Python' if series['software']['recipe_id'] == 'summary-python' else 'R'} summary environment; {series['receipt_count']}/{series['planned_slots']} receipt pairs; "
                f"{cpu['attempts']} projected attempts at {estimate['scenario']['target_rows']} rows. "
                f"CPU demand {human_number(cpu['total_nanoseconds'])} ns; serial busy time {human_number(duration['total_nanoseconds'])} ns. "
                "Elapsed completion, uncertainty intervals and target memory/storage capacity unknown. "
                "Setup, transfer and idle are unmeasured; declared overhead assumptions are in the worksheet. "
                f"Warnings: {', '.join(readable_code(w) for w in warnings) or 'none reported'}. Cleanup remains unresolved and quarantined; "
                f"liabilities: {', '.join(readable_code(w) for w in liabilities) or 'untrusted cleanup claims'}. ")
        if series["instruction"] is not None:
            item = series["instruction"]
            text += (f"Class size {item['enrollment']}; assignments {item['assignment_count']}; "
                     f"assumed persistent storage {item['persistent_storage_assumption_bytes']} bytes. "
                     "Concurrency is a declared assumption; concurrent working memory/storage unknown.")
        fields.append({"id": f"series.{index}", "label": f"Resource estimate {index}", "text": text})
    if estimate["comparison"] is not None:
        fields.append({"id": "comparison", "label": "Comparison", "text": "Two synthetic series are retained with their separate assumptions and warnings. No winner or hardware recommendation; differences are in the worksheet."})
    fields.append({"id": "requirements", "label": "Requirements status", "text":
        "DRAFT. Current requirements are not ready. " +
        {"access_discover": "Discover description format reviewed 2026-10-19; other application documents and current eligibility/resource/form review remain outstanding.",
         "access_explore": "Explore summary/project fields reviewed 2026-10-19. This is a planning worksheet; any PDF is an optional internal copy with a two-page internal cap. No separate required narrative PDF or official summary length limit was established. External documents and current eligibility/resource/form review remain outstanding.",
         "access_accelerate": "Accelerate three-page description format reviewed 2026-10-19. Supporting data, external documents and scientific, team, resource and current-form review remain outstanding.",
         "nairr_research_draft": "Official instructions could not be verified because access was blocked. This is an internal draft, not a confirmed application form.",
         "nairr_classroom_draft": "Official instructions could not be verified because access was blocked. This is an internal draft, not a confirmed application form."}[profile]})
    return fields


def make_preview(profile, estimate, manifests, narrative, font_proof, source, *, include_planning_pdf=False):
    info = profile_info(profile)
    validate_estimate(estimate, manifests)
    validate_narrative(narrative, profile)
    if info["pathway"] is not None and any(s["pathway"] != info["pathway"] for s in estimate["series"]):
        a.reject()
    if type(include_planning_pdf) is not bool or include_planning_pdf and profile != "access_explore":
        a.reject()
    if profile == "access_explore" and not include_planning_pdf:
        if font_proof is not None:
            raise a.AssessmentError("Explore worksheet-only preview does not accept a font.")
    else:
        from .allocation_pdf import validate_font_proof
        validate_font_proof(font_proof)
    requirement = requirements(profile, source, include_planning_pdf=include_planning_pdf)
    record = {"schema": "waypoint.allocation-record/v1", "profile": profile, "narrative": narrative,
              "estimate": estimate, "manifests": manifests, "font": font_proof, "requirements": requirement,
              "fields": visible_fields(profile, narrative, estimate, requirement), "gates": {k: False for k in GATES},
              "approval_meaning": "local_operator_export_consent_not_institutional_authority"}
    if profile == "access_explore":
        record["include_planning_pdf"] = include_planning_pdf
    result = {"schema": "waypoint.allocation-preview/v1", "record": record, "preview_sha256": export_digest(record)}
    if len(export_bytes(result)) > PREVIEW_LIMIT:
        a.reject()
    return json.loads(export_bytes(result))


def validate_preview(preview):
    a.fields(preview, ("schema", "record", "preview_sha256"))
    if preview["schema"] != "waypoint.allocation-preview/v1":
        a.reject()
    record = preview["record"]
    if type(record) is not dict:
        a.reject()
    keys = ("schema", "profile", "narrative", "estimate", "manifests", "font", "requirements", "fields", "gates", "approval_meaning")
    if record.get("profile") == "access_explore":
        keys += ("include_planning_pdf",)
    a.fields(record, keys)
    if type(record["requirements"]) is not dict or "source" not in record["requirements"]:
        a.reject()
    expected = make_preview(record["profile"], record["estimate"], record["manifests"], record["narrative"], record["font"], record["requirements"]["source"],
                            include_planning_pdf=record.get("include_planning_pdf", False))
    if export_bytes(expected) != export_bytes(preview):
        a.reject()
    return record


def needs_pdf(record):
    return record["profile"] != "access_explore" or record["include_planning_pdf"]


def worksheet_rows(value, pointer=""):
    """Lossless scalar projection; no arbitrary data outside the approved record."""
    if type(value) is dict and value:
        for key in sorted(value):
            yield from worksheet_rows(value[key], pointer + "/" + key.replace("~", "~0").replace("/", "~1"))
    elif type(value) is list and value:
        for index, child in enumerate(value):
            yield from worksheet_rows(child, pointer + "/" + str(index))
    else:
        yield pointer, export_bytes(value).decode("ascii")


def package_files(preview, approved_digest, font_payload):
    import csv
    import io
    record = validate_preview(preview)
    digest(approved_digest)
    if approved_digest != preview["preview_sha256"]:
        raise a.AssessmentError("Export requires the exact approved preview digest.")
    pdf = None
    if needs_pdf(record):
        from .allocation_pdf import render_pdf
        pdf = render_pdf(record, font_payload)
    elif font_payload is not None:
        raise a.AssessmentError("Explore worksheet-only export does not accept a font or a new render mode.")
    lines = ["# " + PROFILES[record["profile"]]["title"] + " - DRAFT", "",
             "Approved preview SHA-256: " + approved_digest, "",
             "Operator consent for this local export is not institutional approval or submission.", ""]
    for field in record["fields"]:
        lines += ["## " + field["label"], ""]
        # Indented literal blocks preserve all manual text without activating Markdown links/HTML.
        lines += ["    " + line for line in field["text"].split("\n")]
        lines.append("")
    worksheet = io.StringIO(newline="")
    writer = csv.writer(worksheet, lineterminator="\r\n")
    writer.writerow(("json_pointer", "json_value"))
    for pointer, value in worksheet_rows(record):
        # Every cell is explicitly text. Remove exactly the first apostrophe to
        # recover the JSON pointer/value; original leading characters survive.
        writer.writerow(("'" + pointer, "'" + value))
    files = {"narrative.md": ("\n".join(lines) + "\n").encode("utf-8"), "source.json": export_bytes(record),
             "worksheet.json": export_bytes(record), "worksheet.csv": worksheet.getvalue().encode("utf-8"),
             "retained-preview.json": export_bytes(preview), "requirements.json": export_bytes(record["requirements"]),
             "README.txt": ("Local synthetic DRAFT package. All authority/readiness gates are false.\n"
                 "package.json is PREPARED_DRAFT only. Completion requires the successful CLI receipt binding its hash, plus no INCOMPLETE marker.\n"
                 "A missing marker alone is not success; marker recreation can fail after uncertain publication. Crash durability is untested.\n"
                 "No application was submitted. Source refresh occurred only if explicitly requested before preview.\n"
                 "source.json and worksheet.json are the same approved canonical record. narrative.md and draft.pdf show its fields.\n"
                 "worksheet.csv losslessly flattens that record: remove exactly one leading apostrophe from both cells, then JSON-decode json_value.\n"
                 "Manual narrative is operator-authored and unverified. Metadata/receipt hashes establish integrity, not authenticity.\n"
                 "requirements.json lists missing documents/reviews. No font file or host input path is included.\n").encode("ascii")}
    if pdf is not None:
        files["draft.pdf"] = pdf
    else:
        files["README.txt"] = files["README.txt"].replace(b"narrative.md and draft.pdf show its fields.", b"narrative.md shows its fields. No PDF was requested in the approved Explore preview.")
    if sum(map(len, files.values())) > 8388608:
        a.reject()
    manifest = {"schema": "waypoint.allocation-package/v1", "status": "PREPARED_DRAFT", "approved_preview_sha256": approved_digest,
                "completion_requires": "successful_export_receipt_bound_to_this_manifest", "files": {name: {"sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload)} for name, payload in sorted(files.items())},
                "gates": {k: False for k in GATES}, "submission_performed": False}
    files["package.json"] = export_bytes(manifest)
    return files


def write_package(output, files):
    """Fresh directory only, no symlink traversal, explicit incomplete writes."""
    import os
    from pathlib import Path
    descriptors = []
    directory = None
    try:
        path = Path(output)
        if not path.is_absolute() or path.name in ("", ".", "..") or ".." in path.parts:
            a.reject()
        current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(current)
        for part in path.parts[1:-1]:
            current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            descriptors.append(current)
        os.mkdir(path.name, mode=0o700, dir_fd=current)
        directory = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
        descriptors.append(directory)

        def write(name, payload):
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as target:
                target.write(payload); target.flush(); os.fsync(target.fileno())

        write("INCOMPLETE", b"Export was not completed. Do not use this directory as an accepted package.\n")
        for name, payload in files.items():
            if name != "package.json":
                write(name, payload)
        write("package.json", files["package.json"])
        os.fsync(directory)
        os.unlink("INCOMPLETE", dir_fd=directory)
        os.fsync(directory)
    except (OSError, ValueError):
        marker_confirmed = False
        if directory is not None:
            try:
                # Retain original bytes. Re-create a stop marker if publication
                # removed it before a later durability operation failed.
                try:
                    write("INCOMPLETE", b"Publication or durability is uncertain. No successful export receipt was issued.\n")
                except FileExistsError:
                    pass
                os.stat("INCOMPLETE", dir_fd=directory, follow_symlinks=False)
                marker_confirmed = True
            except OSError:
                pass
        detail = " INCOMPLETE is present; its durability may be uncertain." if marker_confirmed else " An INCOMPLETE marker could not be confirmed."
        raise a.AssessmentError("Export refused or publication is uncertain; no completion receipt was issued. Preserve the directory. A missing marker does not establish success." + detail) from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def export_package(preview, approved_digest, font_payload, output):
    files = package_files(preview, approved_digest, font_payload)
    write_package(output, files)
    return {"schema": "waypoint.allocation-export-receipt/v1", "status": "COMPLETE_DRAFT",
            "approved_preview_sha256": approved_digest, "package_sha256": hashlib.sha256(files["package.json"]).hexdigest(),
            "publication_durability": "fsync_completed_not_crash_tested", "file_count": len(files), "gates": {k: False for k in GATES}}
