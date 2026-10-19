"""Read-only operator preview using Outfitter's pure receipt validator.

The executable is an operator-selected local dependency, not uploaded code.
No Store, worker, provider, private state, or study export is initialized here.
"""

import os
from pathlib import Path
import subprocess

from .assessment import (AssessmentError, canonical_bytes, choice, fields, manifest_digest,
                         manifest_summary, number, reject, strict_document, timestamp, validate_manifest)
from .bounded_process import collect


VALIDATION_TIMEOUT_SECONDS = 5
METRIC_UNITS = {
    **dict.fromkeys(("cpu_consumed", "gpu_allocated_duration", "setup_duration", "loading_duration",
                    "transfer_duration", "queue_duration", "execution_duration", "cleanup_duration",
                    "sandbox_overhead", "instrumentation_overhead"), "nanoseconds"),
    **dict.fromkeys(("peak_memory", "scratch_peak", "output_peak", "io_read", "io_write", "gpu_peak_memory"), "bytes"),
    "gpu_utilization": "parts_per_million", "completed_units": "count",
}


def validate_receipts(manifest, receipts, executable, *, timeout=None):
    """Call one fixed, pure command with finite input, output and wall time."""
    validate_manifest(manifest)
    request = canonical_bytes({"operation": "validate", "manifest": manifest, "receipts": receipts})
    if len(request) > 2 * 65536:
        reject()
    budget = VALIDATION_TIMEOUT_SECONDS if timeout is None else min(VALIDATION_TIMEOUT_SECONDS, timeout)
    if budget <= 0:
        reject()
    process = None
    try:
        # Never resolve an arbitrary upload field or a PATH fallback as code.
        program = Path(executable)
        if not program.is_absolute() or not program.is_file() or not os.access(program, os.X_OK):
            reject()
        process = subprocess.Popen([str(program), "assessment-receipts"], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        os.set_blocking(process.stdin.fileno(), False)
        offset = 0

        def feed():
            nonlocal offset
            if process.stdin.closed:
                return
            try:
                offset += os.write(process.stdin.fileno(), request[offset:offset + 65536])
            except BlockingIOError:
                return
            if offset == len(request):
                process.stdin.close()

        stdout, _ = collect(process, timeout=budget, tick=feed)
        if process.returncode != 0 or offset != len(request):
            reject()
        result = strict_document(stdout)
        fields(result, ("schema", "validation", "manifest_sha256", "measurement_sha256",
                        "cleanup_sha256", "execution_authorized", "cleanup_trusted",
                        "actual_resource_accepted", "receipts"))
        if (result["schema"] != "outfitter.assessment-receipts-result/v1"
                or result["validation"] != "valid_unverified"
                or any(result[key] is not False for key in
                       ("execution_authorized", "cleanup_trusted", "actual_resource_accepted"))
                or result["manifest_sha256"] != manifest_digest(manifest)
                or canonical_bytes(result["receipts"]) != canonical_bytes(receipts)):
            reject()
        fields(receipts, ("schema", "measurement", "cleanup"))
        if (receipts["schema"] != "educloud.receipt-set/v1"
                or result["measurement_sha256"] != manifest_digest(receipts["measurement"])
                or result["cleanup_sha256"] != manifest_digest(receipts["cleanup"])):
            reject()
        return result
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        raise AssessmentError("Receipt validation failed or exceeded its local bounds.") from None
    finally:
        if process is not None:
            # collect normally closes output pipes; handle pre-collection errors too.
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()


def boolean(value):
    if type(value) is not bool:
        reject()
    return value


def observation(value):
    fields(value, ("source_kind", "authenticated", "independently_verified"))
    choice(value["source_kind"], ("legacy_projection", "unverified_observation", "synthetic_fixture"))
    if value["authenticated"] is not False or value["independently_verified"] is not False:
        reject()
    return value


def retained_record(manifest, result):
    """Project only typed, closed fields after owner validation.

    Outfitter owns cross-field receipt semantics. These additional checks bound
    the display vocabulary even if an incompatible local helper is selected.
    Arbitrary receipt labels, verifier labels and helper diagnostics are omitted.
    """
    measurement, cleanup = result["receipts"]["measurement"], result["receipts"]["cleanup"]
    binding = measurement["binding"]
    selected = next((r for r in manifest["reservations"] if r["attempt_id"] == binding["attempt_id"]), None)
    if selected is None:
        reject()
    expected = {**selected, "manifest_sha256": manifest_digest(manifest),
                "assessment_id": manifest["assessment_id"], "project_id": manifest["project_id"],
                "environment_id": manifest["workload"]["environment_id"], "image": manifest["workload"]["image"],
                "hardware": manifest["hardware"], "measurement_method": manifest["measurement"]}
    if canonical_bytes(binding) != canonical_bytes(expected) or canonical_bytes(cleanup["binding"]) != canonical_bytes(expected):
        reject()
    metrics = measurement["measured_consumption"]
    fields(metrics, METRIC_UNITS)
    for name, unit in METRIC_UNITS.items():
        metric = metrics[name]
        fields(metric, ("availability", "value", "unit", "scope", "source"))
        choice(metric["availability"], ("available", "unknown", "unsupported"))
        choice(metric["scope"], ("whole_attempt", "process", "sandbox"))
        if metric["unit"] != unit:
            reject()
        if metric["availability"] == "available":
            number(metric["value"], 0, 1000000 if name == "gpu_utilization" else 9007199254740991)
            choice(metric["source"], ("unverified_collector",))
        elif metric["value"] is not None or metric["source"] != "none":
            reject()
    execution = measurement["execution"]
    fields(execution, ("status", "admitted", "execution_started", "cancel_requested", "exit_code",
                       "failure_category", "diagnostics_available", "reused"))
    choice(execution["status"], ("reserved", "creating", "running", "succeeded", "failed", "cancelled", "unknown"))
    choice(execution["failure_category"], ("none", "not_admitted", "execution_failed", "timeout", "out_of_memory", "cancelled", "unknown"))
    for name in ("admitted", "execution_started", "cancel_requested", "diagnostics_available"):
        boolean(execution[name])
    if execution["reused"] is not None:
        boolean(execution["reused"])
    if execution["exit_code"] is not None:
        number(execution["exit_code"], 0, 255)
    ceiling = measurement["reservation_ceiling"]
    fields(ceiling, ("cpu_seconds", "source"))
    number(ceiling["cpu_seconds"], 0, 30)
    choice(ceiling["source"], ("legacy_broker_receipt",))
    accounting = measurement["conservative_accounting"]
    fields(accounting, ("charged_cpu_seconds", "provenance", "unresolved_liability"))
    number(accounting["charged_cpu_seconds"], 0, 30)
    choice(accounting["provenance"], ("not_admitted", "reserved", "estimated_upper_bound"))
    boolean(accounting["unresolved_liability"])
    charge = measurement["provider_reported_charge"]
    fields(charge, ("availability", "value", "unit", "source"))
    choice(charge["availability"], ("unknown", "unsupported"))
    if charge["value"] is not None or charge["unit"] != "unknown" or charge["source"] != "none":
        reject()
    quality = measurement["quality"]
    fields(quality, ("status", "source"))
    choice(quality["status"], ("unknown", "passed", "failed"))
    choice(quality["source"], ("none", "unverified_checker"))
    choice(cleanup["outcome"], ("unresolved",))
    for key in ("termination", "access_revocation"):
        choice(cleanup[key], ("verified", "failed", "unknown"))
    method = cleanup["method"]
    fields(method, ("name", "version"))
    choice(method["name"], ("legacy_provider_assertion", "unverified_scope_observation"))
    number(method["version"], 1, 1)
    if cleanup["observed_at"] is not None:
        timestamp(cleanup["observed_at"])
    scopes = cleanup["observed_scopes"]
    if type(scopes) is not list or len(scopes) != 3:
        reject()
    for index, kind in enumerate(("execution_resource", "temporary_data", "recovery_copies")):
        scope = scopes[index]
        fields(scope, ("kind", "scope_id", "deletion"))
        if scope["kind"] != kind or scope["scope_id"] != expected["attempt_id" if index == 0 else "assessment_id"]:
            reject()
        choice(scope["deletion"], ("reported_complete", "unknown", "failed", "not_applicable"))
    liabilities = cleanup["remaining_liabilities"]
    if type(liabilities) is not list or len(liabilities) > 6:
        reject()
    for liability in liabilities:
        choice(liability, ("termination_unresolved", "access_revocation_unknown", "execution_cleanup_unknown",
                           "data_cleanup_unknown", "recovery_cleanup_unknown", "accounting_unresolved"))
    if cleanup["quarantine"] is not True or cleanup["reuse_denied"] is not True:
        reject()
    return {
        "binding": expected, "observation": observation(measurement["observation"]),
        "execution_claim": execution, "measured_consumption": metrics,
        "reservation_ceiling": ceiling, "conservative_accounting": accounting,
        "provider_reported_charge": charge, "quality_claim": quality,
        "cleanup_claim": {
            "observation": observation(cleanup["observation"]), "outcome": cleanup["outcome"],
            "termination": cleanup["termination"], "access_revocation": cleanup["access_revocation"],
            "observed_scopes": scopes, "observed_at": cleanup["observed_at"], "method": method,
            "verifier_declared": cleanup["verifier_id"] is not None,
            "remaining_liabilities": liabilities, "quarantine": True, "reuse_denied": True,
        },
    }


def preview_receipts(manifest, receipts, executable, *, now=None):
    result = validate_receipts(manifest, receipts, executable)
    try:
        record = retained_record(manifest, result)
        return {"schema": "waypoint.assessment-preview/v1", **manifest_summary(manifest, now=now),
                "validation": "valid_unverified", "actual_resource_accepted": False,
                "cleanup_trusted": False, "measurement_sha256": result["measurement_sha256"],
                "cleanup_sha256": result["cleanup_sha256"], "retained_record": record}
    except (ValueError, KeyError, TypeError, IndexError):
        raise AssessmentError("Receipt preview contains unsupported retained metadata.") from None
