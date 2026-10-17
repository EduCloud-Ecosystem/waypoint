"""Authenticated, bounded automated repeatability of reviewed evidence bundles.

The external archive hash establishes the operator's chosen source, not scientific
validity. Archive code is never executed: exact packaged code and image matches
are independently required. No archived execution authority is imported.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import io
import json
import math
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import time
import uuid
import zipfile

from .catalog import recipe_source, resolve_environment
from .evidence import MAX_ARCHIVE_BYTES, build_bundle, verify_bundle
from .recipes import IDS, parse_csv, validate_result
from .runtime import Broker, strict_json, validate_images, validate_receipt


FORMAT = "educloud-research-reproduction-v1"
LIMITS = {"cpus": 1, "memory_mb": 256, "pids_limit": 64, "timeout_seconds": 30,
          "max_output_bytes": 65536, "per_run_cpu_seconds": 30,
          "maximum_budget_cpu_seconds": 300, "automatic_retries": 0}
WARNING = (
    "Automated repeatability of selected public or synthetic evidence, not independent "
    "human reproduction or scientific validity. CPU-second charges are conservative "
    "ceilings, not measured use or provider costs. Docker containment depends on the "
    "supervised watchdog; host or daemon loss can prevent cleanup. No images or "
    "dependencies are downloaded and no cloud resources are created."
)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode() + b"\n"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _compute(unknown=False):
    return {"reserved_cpu_seconds": None if unknown else 0,
            "charged_cpu_seconds": None if unknown else 0,
            "provenance": "unknown" if unknown else "not_admitted",
            "unresolved_liability": unknown}


def _verified(payload, trusted_sha256):
    if not isinstance(trusted_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", trusted_sha256):
        raise ValueError("Supply the expected whole-bundle SHA-256 retained outside the archive.")
    if type(payload) is not bytes or len(payload) > MAX_ARCHIVE_BYTES:
        raise ValueError("The evidence archive exceeds the verification size bound.")
    if not hmac.compare_digest(_sha(payload), trusted_sha256):
        raise ValueError("The archive does not match the externally retained expected SHA-256.")
    verification = verify_bundle(payload)
    # Only read from the same immutable bytes after full safety and lineage checks.
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    return verification, json.loads(members["runs.json"]), members


def _plan(payload, trusted_sha256, selected_run_ids):
    verification, originals, members = _verified(payload, trusted_sha256)
    ids = {run["id"] for run in originals}
    if selected_run_ids is None:
        chosen = {run["id"] for run in originals if run["status"] == "succeeded"}
    else:
        if (type(selected_run_ids) is not list or any(type(value) is not str for value in selected_run_ids)
                or len(selected_run_ids) != len(set(selected_run_ids)) or not set(selected_run_ids) <= ids):
            raise ValueError("Selected run IDs must be a unique list of IDs in this archive.")
        chosen = set(selected_run_ids)
    rows, sources = [], {}
    for original in originals:
        run_id = original["id"]
        prefix = f"runs/{run_id}/"
        filename = IDS[original["recipe"]][1]
        content = members[prefix + "input.csv"]
        image = original["environment"].get("image")
        eligible, environment_id, reason = False, None, "The exact recipe and image are not in the trusted packaged catalog."
        try:
            entry = resolve_environment(original["recipe"], original["recipe_sha256"], image)
            source = recipe_source(entry)
            if (type(source) is not bytes or source != members[prefix + filename]
                    or _sha(source) != original["recipe_sha256"]):
                raise ValueError("Packaged and archived recipe bytes differ.")
            parse_csv(content.decode("utf-8"))
            eligible, environment_id, reason = True, entry["id"], "Trusted packaged recipe and immutable image match; input is valid."
            sources[run_id] = (content, source)
        except (ValueError, UnicodeError, OSError):
            # Catalog and input diagnostics may contain local paths; keep them private.
            pass
        result = members.get(prefix + "result.json")
        selected = run_id in chosen
        rows.append({
            "source_run_id": run_id, "original_status": original["status"],
            "original_error": original["error"], "selected": selected, "eligible": eligible,
            "eligibility_reason": reason, "trusted_environment_id": environment_id,
            "input_sha256": original["input_sha256"], "recipe_sha256": original["recipe_sha256"],
            "image": image, "original_result_sha256": _sha(result) if result is not None else None,
            "original_compute": copy.deepcopy(original["compute"]),
            "original_cleanup_verified": original["cleanup_verified"],
            "gaps": [gap for gap in verification["gaps"] if gap.get("run_id") == run_id],
            "outcome": "NOT RUN", "reason": "Selected; no execution has occurred." if selected else "Not selected for replay.",
            "new_run_id": None, "attempted": False, "comparison": {"status": "NOT RUN"},
            "cleanup_verified": True, "compute": _compute(),
        })
    plan = {
        "format": FORMAT, "mode": "plan",
        "source": {"bundle_sha256": trusted_sha256, "project_id": verification["project_id"],
                   "run_count": len(originals), "gaps": verification["gaps"]},
        "selection": {"mode": "successful_default" if selected_run_ids is None else "explicit",
                      "selected_count": len(chosen), "unselected_count": len(originals) - len(chosen),
                      "required_cpu_seconds": len(chosen) * 30},
        "limits": dict(LIMITS), "executable": bool(chosen) and len(chosen) <= 10 and all(row["eligible"] for row in rows if row["selected"]),
        "local_environment_preflight": "NOT RUN", "runs": rows, "warning": WARNING,
    }
    return plan, originals, sources


def plan_reproduction(payload: bytes, trusted_sha256: str, selected_run_ids=None) -> dict:
    """Verify and plan without running subprocesses, importing archive code or writing files."""
    return _plan(payload, trusted_sha256, selected_run_ids)[0]


def _atomic(path, content):
    """Replace only our own checkpoint files, persisting file and directory metadata."""
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _bounded_process(command, directory, timeout=70):
    """Bound observer memory/time; killing its CLI never refunds provider liability."""
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    caps = {"stdout": 262144, "stderr": 65536}
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("Broker observation deadline exceeded.")
                for key, _ in selector.select(min(remaining, 0.25)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    name = key.data
                    room = caps[name] - len(buffers[name])
                    buffers[name].extend(chunk[:room])
                    if len(chunk) > room:
                        raise ValueError("Broker response exceeded its size bound.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("Broker observation deadline exceeded.")
            process.wait(timeout=remaining)
        if process.returncode != 0:
            raise ValueError("Broker did not return an accepted execution receipt.")
        return strict_json(bytes(buffers["stdout"]))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
        for name, content in buffers.items():
            _atomic(directory / ("broker-" + name + ".private"), bytes(content))


def _same_metrics(left, right):
    if isinstance(right, dict):
        return isinstance(left, dict) and set(left) == set(right) and all(_same_metrics(left[key], value) for key, value in right.items())
    if isinstance(right, list):
        return isinstance(left, list) and len(left) == len(right) and all(_same_metrics(a, b) for a, b in zip(left, right))
    if type(left) in (int, float) and type(right) in (int, float) and (isinstance(left, float) or isinstance(right, float)):
        return math.isfinite(left) and math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-9)
    return type(left) is type(right) and left == right


def _checkpoint(output, report, project, runs, files):
    payload = build_bundle(project, runs, files)
    report["evidence_sha256"] = _sha(payload)
    # A crash between these writes produces a detectable report/evidence mismatch.
    _atomic(output / "evidence.zip", payload)
    serialized = _json(report)
    _atomic(output / "report.json", serialized)
    return {"report": report, "artifacts": {
        "report": {"name": "report.json", "sha256": _sha(serialized)},
        "evidence": {"name": "evidence.zip", "sha256": _sha(payload)},
    }}


def reproduce_bundle(bundle_path, *, trusted_sha256, output, broker, budget_cpu_seconds, selected_run_ids=None) -> dict:
    """Perform one fresh bounded replay. Reject unsafe preflight before provider effects.

    The return contains the exact report.json object plus hashes of report.json and
    evidence.zip. Preserve those hashes separately when transferring the artifacts.
    Existing output directories are always refused, including empty ones.
    """
    if (type(budget_cpu_seconds) not in (int, float) or not 0 < budget_cpu_seconds <= 300
            or not math.isfinite(budget_cpu_seconds) or budget_cpu_seconds != int(budget_cpu_seconds)):
        raise ValueError("Supply a whole, finite CPU-second budget between 1 and 300.")
    budget = int(budget_cpu_seconds)
    descriptor = os.open(bundle_path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        information = os.fstat(source.fileno())
        if not stat.S_ISREG(information.st_mode) or information.st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("The evidence archive must be a bounded regular file.")
        payload = source.read(MAX_ARCHIVE_BYTES + 1)
    report, originals, sources = _plan(payload, trusted_sha256, selected_run_ids)
    selected = [row for row in report["runs"] if row["selected"]]
    if any(not row["eligible"] for row in selected):
        raise ValueError("Every selected run must match a trusted packaged recipe, immutable image and valid input.")
    if report["selection"]["required_cpu_seconds"] > budget:
        raise ValueError("The supplied budget must reserve 30 CPU-seconds for every selected run before execution.")
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Replay requires a new output directory; existing state is never overwritten or reused.")
    client = Broker(broker, output / "state.private" / "broker")
    # Preflight every image before initialization or dispatch; never pull an image.
    chosen_ids = {row["source_run_id"] for row in selected}
    for runtime, image in sorted({(IDS[original["recipe"]][0], original["environment"]["image"])
                                  for original in originals if original["id"] in chosen_ids}):
        validate_images({runtime: image})
    output.mkdir(mode=0o700)  # Atomic refusal if another process won the name.
    private = output / "state.private"
    private.mkdir(mode=0o700)
    _atomic(output / ".gitignore", b"state.private/\n*.tmp\n")
    timestamp, project_id = _now(), "replay_" + uuid.uuid4().hex
    report.update(mode="execution", status="NOT RUN", phase="prepared", fresh_project_id=project_id,
                  budget_cpu_seconds=budget, started_at=timestamp, finished_at=None,
                  local_environment_preflight="PASS")
    original_by_id = {original["id"]: original for original in originals}
    runs, files, directories = [], {}, {}
    for row in selected:
        original = original_by_id[row["source_run_id"]]
        row["new_run_id"] = "replay_" + uuid.uuid4().hex
        run = copy.deepcopy(original)
        run.update(id=row["new_run_id"], project_id=project_id, status="queued", created_at=timestamp,
                   started_at=None, finished_at=None, metrics={}, error=None,
                   cleanup_verified=True, compute=_compute())
        runs.append(run)
        content, source = sources[original["id"]]
        filename = IDS[run["recipe"]][1]
        prefix = f"runs/{run['id']}/"
        files.update({prefix + "input.csv": content, prefix + filename: source})
        directory = private / run["id"]
        directory.mkdir(mode=0o700)
        for name in ("input", "recipe"):
            (directory / name).mkdir(mode=0o700)
        _atomic(directory / "input" / "data.csv", content)
        _atomic(directory / "recipe" / filename, source)
        directories[run["id"]] = directory
    mapping = [{"source_run_id": row["source_run_id"], "new_run_id": row["new_run_id"]} for row in selected]
    project = {
        "id": project_id, "name": "Automated evidence repeatability",
        "question": "Replay selected evidence from externally retained bundle SHA-256 " + trusted_sha256,
        "success_criterion": "Independently checked outputs match retained descriptive results. Origin run mapping: " + _json(mapping).decode().strip(),
        "created_at": timestamp, "ends_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "budget_cpu_seconds": budget, "status": "active",
    }
    _checkpoint(output, report, project, runs, files)
    initialized = not selected
    if selected:
        try:
            client.init_project(project_id, budget)
            initialized = True
        except (ValueError, OSError, subprocess.SubprocessError):
            report.update(status="FAIL", phase="initialization_failed")
            for row in selected:
                row["reason"] = "Fresh broker authority could not be initialized; no runs were dispatched."
    stop = not initialized
    for row, run in zip(selected, runs):
        if stop:
            run.update(status="cancelled", finished_at=_now(), error="Not dispatched after an earlier unresolved operation.")
            if initialized:
                row["reason"] = "Not dispatched because an earlier replay requires operator reconciliation."
            continue
        directory = directories[run["id"]]
        runtime, filename = IDS[run["recipe"]]
        command = (["python", f"/recipe/{filename}"] if runtime == "python" else ["Rscript", "--vanilla", f"/recipe/{filename}"])
        command += ["/input/data.csv", run["condition"]]
        request = {"schema_version": 1, "request_id": run["id"], "project_id": project_id,
                   "image": run["environment"]["image"], "command": command,
                   "input_dir": str(directory / "input"), "recipe_dir": str(directory / "recipe"),
                   **{key: LIMITS[key] for key in ("cpus", "memory_mb", "pids_limit", "timeout_seconds", "max_output_bytes")},
                   "isolation": "reviewed-local", "allow_supervised_reaper": True}
        request_path = directory / "request.private.json"
        _atomic(request_path, _json(request))
        run.update(status="interrupted", started_at=_now(), cleanup_verified=False, compute=_compute(True),
                   error="Execution observation is incomplete; retained authority requires reconciliation before further work.")
        row.update(attempted=True, outcome="FAIL", reason=run["error"], cleanup_verified=False, compute=_compute(True))
        report.update(status="FAIL", phase="executing")
        _checkpoint(output, report, project, runs, files)  # Persist uncertainty before provider effect.
        try:
            receipt = validate_receipt(_bounded_process(client.command("run", "--request", str(request_path)), directory), run)
            _atomic(directory / "receipt.private.json", _json(receipt))
            run["compute"] = {**receipt["compute"], "unresolved_liability": receipt["unresolved_liability"]}
            run["cleanup_verified"] = receipt["cleanup_verified"]
            run["status"] = receipt["status"] if receipt["status"] in ("succeeded", "failed", "cancelled") else "interrupted"
            run["error"] = None if run["status"] == "succeeded" else "Bounded execution did not produce an accepted successful outcome."
            if run["status"] == "succeeded":
                try:
                    result = strict_json(receipt["stdout"])
                    validate_result(result, sources[row["source_run_id"]][0].decode(), run["condition"])
                    run["metrics"] = result
                    result_bytes = _json(result)
                    files[f"runs/{run['id']}/result.json"] = result_bytes
                    original = original_by_id[row["source_run_id"]]
                    available = original["status"] == "succeeded" and row["original_result_sha256"] is not None
                    matched = available and _same_metrics(original["metrics"], result)
                    row["comparison"] = {"status": "PASS" if matched else "FAIL" if available else "NOT RUN",
                                         "reference_result_sha256": row["original_result_sha256"],
                                         "reproduced_result_sha256": _sha(result_bytes),
                                         "relative_tolerance": 1e-12, "absolute_tolerance": 1e-9}
                    row["outcome"] = "PASS" if matched else "FAIL"
                    row["reason"] = ("Independent result check passed and archived metrics matched." if matched else
                                     "Independent result check passed but archived metrics differed." if available else
                                     "Independent result check passed; no original successful result is available for comparison.")
                except (ValueError, TypeError, UnicodeError):
                    run.update(status="failed", metrics={}, error="The output failed the independent result check.")
                    row["reason"] = run["error"]
            else:
                row["reason"] = run["error"]
        except (ValueError, OSError, subprocess.SubprocessError, KeyboardInterrupt):
            # Never turn an unvalidated receipt or absent observation into a zero charge.
            run.update(status="interrupted", cleanup_verified=False, compute=_compute(True),
                       error="Execution could not be reconciled; retain private broker authority and do not retry automatically.")
            row["reason"] = run["error"]
        run["finished_at"] = _now()
        row.update(cleanup_verified=run["cleanup_verified"], compute=copy.deepcopy(run["compute"]))
        stop = run["compute"]["unresolved_liability"] or not run["cleanup_verified"]
        _checkpoint(output, report, project, runs, files)
    project["status"] = "closed"
    if initialized:
        report["status"] = "PASS" if selected and all(row["outcome"] == "PASS" for row in selected) else "FAIL" if selected else "NOT RUN"
        report["phase"] = "reconciliation_required" if stop else "completed"
    report["finished_at"] = _now()
    return _checkpoint(output, report, project, runs, files)
