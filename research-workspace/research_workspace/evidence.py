"""Bounded, deterministic research evidence exports using the standard library.

No filesystem paths are opened and no archive contents are extracted or executed.
The caller supplies a consistent, authorized snapshot and explicitly selected bytes.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import stat
import struct
import zipfile
import zlib
from datetime import datetime, timedelta


FORMAT = "educloud-research-evidence-v1"
MAX_RUNS = 100
MAX_FILES = 3 * MAX_RUNS + 7
MAX_FILE_BYTES = 256 * 1024
MAX_INPUT_BYTES = 64 * 1024
MAX_RECIPE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_BYTES = MAX_TOTAL_BYTES + 256 * 1024
MAX_COMPRESSION_RATIO = 100
WARNING = (
    "Descriptive, provisional evidence only. Integrity and lineage checks do not "
    "establish scientific validity, independent reproduction, or authenticity."
)

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE = re.compile(r"(?:sha256:|[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:)[0-9a-f]{64}\Z")
_HOST_PATH = re.compile(
    r"(?:^|[\s\"'(=])(?:/(?!/)[A-Za-z0-9._-]+(?:/|\b)|[A-Za-z]:[\\/])"
)
_CREDENTIAL = re.compile(
    r"(?i)(?:\bBearer\s+\S+|\b(?:password|secret|access[_ -]?key|api[_ -]?key|"
    r"session[_ -]?token|authorization)\s*[:=]\s*\S+)"
)
_PROJECT_FIELDS = frozenset({
    "id", "name", "question", "success_criterion", "created_at", "ends_at",
    "budget_cpu_seconds", "status",
})
_RUN_FIELDS = frozenset({
    "id", "project_id", "recipe", "condition", "status", "created_at", "started_at",
    "finished_at", "input_sha256", "recipe_sha256", "environment", "parameters",
    "metrics", "error", "cleanup_verified", "compute",
})
_ENV_FIELDS = frozenset({"image", "runtime", "runner"})
_COMPUTE_FIELDS = frozenset({
    "reserved_cpu_seconds", "charged_cpu_seconds", "provenance", "unresolved_liability",
})
_METRIC_FIELDS = frozenset({"count", "mean", "groups", "condition"})
_FIXED_FILES = frozenset({
    "project.json", "runs.json", "comparison.json", "checksums.json", "README.md",
    "METHODS.md", "ro-crate-metadata.json",
})
_STATUSES = frozenset({"queued", "running", "succeeded", "failed", "cancelled", "interrupted"})


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError) as exc:
        raise ValueError("Evidence must contain finite, UTF-8 JSON values") from exc


def _json(data: bytes, *, canonical: bool = True) -> object:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON keys are not permitted")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("Non-finite JSON numbers are not permitted")

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
        encoded = _canonical(value)  # Also rejects exponent overflow (e.g. 1e999).
        if canonical and encoded != data:
            raise ValueError("Manifest JSON is not canonical")
        return value
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Invalid evidence JSON") from exc


def _keys(value: object, fields: frozenset[str], label: str) -> dict:
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"Unexpected or missing {label} fields")
    return value


def _text(value: object, label: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not value and not empty):
        raise ValueError(f"Invalid {label}")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValueError(f"Invalid control character in {label}")
    if _HOST_PATH.search(value) or _CREDENTIAL.search(value):
        raise ValueError(f"Unsafe credential or host path in {label}")
    _canonical(value)
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"Invalid {label}")
    return value


def _number(value: object, label: str, *, positive: bool = False) -> float:
    if not _finite(value) or value < 0:
        raise ValueError(f"Invalid finite nonnegative {label}")
    if positive and value == 0:
        raise ValueError(f"Invalid positive {label}")
    return value


def _finite(value: object) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _choice(value: object, choices: set | frozenset, label: str):
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"Invalid {label}")


def _timestamp(value: object, label: str, *, optional: bool = False):
    if value is None and optional:
        return
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError(f"Invalid {label}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if "T" not in value or parsed.utcoffset() != timedelta(0):
            raise ValueError()
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a UTC ISO-8601 timestamp") from exc


def _metrics(value: object, condition: str, *, empty: bool) -> dict:
    if empty and value == {}:
        return {}
    value = _keys(value, _METRIC_FIELDS, "metrics")
    if value["condition"] != condition:
        raise ValueError("Result condition does not match its run")
    if type(value["count"]) is not int or value["count"] < 1:
        raise ValueError("Invalid result count")
    if not _finite(value["mean"]):
        raise ValueError("Invalid result mean")
    if type(value["groups"]) is not list or not 1 <= len(value["groups"]) <= 1000:
        raise ValueError("Invalid result groups")
    names = set()
    for group in value["groups"]:
        group = _keys(group, frozenset({"group", "count", "mean"}), "group")
        _text(group["group"], "group", 80)
        if group["group"] in names:
            raise ValueError("Duplicate result groups")
        names.add(group["group"])
        if type(group["count"]) is not int or group["count"] < 1:
            raise ValueError("Invalid group count")
        if not _finite(group["mean"]):
            raise ValueError("Invalid group mean")
    return value


def _validate_records(project: dict, runs: list[dict]):
    _keys(project, _PROJECT_FIELDS, "project")
    _identifier(project["id"], "project ID")
    for field, limit in (("name", 200), ("question", 4000), ("success_criterion", 4000)):
        _text(project[field], field, limit)
    _timestamp(project["created_at"], "project created_at")
    _timestamp(project["ends_at"], "project ends_at")
    _number(project["budget_cpu_seconds"], "project budget", positive=True)
    _choice(project["status"], {"active", "closed"}, "project status")
    if type(runs) is not list or len(runs) > MAX_RUNS:
        raise ValueError("Too many or invalid run records")
    seen = set()
    for run in runs:
        _keys(run, _RUN_FIELDS, "run")
        run_id = _identifier(run["id"], "run ID")
        if run_id in seen:
            raise ValueError("Duplicate run ID")
        seen.add(run_id)
        if run["project_id"] != project["id"]:
            raise ValueError("Run belongs to a different project")
        _choice(run["recipe"], {"summary-python", "summary-r"}, "recipe")
        _choice(run["condition"], {"baseline", "alternative"}, "condition")
        _choice(run["status"], _STATUSES, "run status")
        _timestamp(run["created_at"], "run created_at")
        _timestamp(run["started_at"], "run started_at", optional=True)
        _timestamp(run["finished_at"], "run finished_at", optional=True)
        for field in ("input_sha256", "recipe_sha256"):
            if not isinstance(run[field], str) or not _SHA256.fullmatch(run[field]):
                raise ValueError(f"Invalid {field}")
        environment = run["environment"]
        if environment != {} or run["status"] == "succeeded":
            _keys(environment, _ENV_FIELDS, "environment")
            if not isinstance(environment["image"], str) or not _IMAGE.fullmatch(environment["image"]):
                raise ValueError("Environment requires an immutable image SHA-256")
            runtime = "python" if run["recipe"] == "summary-python" else "r"
            if environment["runtime"] != runtime or environment["runner"] != "outfitter-docker-v1":
                raise ValueError("Environment does not match the reviewed recipe")
        _keys(run["parameters"], frozenset(), "parameters")
        _metrics(run["metrics"], run["condition"], empty=run["status"] != "succeeded")
        if run["error"] is not None:
            _text(run["error"], "error", 1000, empty=True)
        if type(run["cleanup_verified"]) is not bool:
            raise ValueError("Invalid cleanup verification")
        compute = _keys(run["compute"], _COMPUTE_FIELDS, "compute")
        _choice(compute["provenance"], {"estimated_upper_bound", "not_admitted", "unknown", "reserved"}, "compute provenance")
        if type(compute["unresolved_liability"]) is not bool:
            raise ValueError("Invalid compute liability")
        if compute["provenance"] == "unknown":
            if compute["reserved_cpu_seconds"] is not None or compute["charged_cpu_seconds"] is not None or compute["unresolved_liability"] is not True:
                raise ValueError("Unknown accounting must retain null amounts and unresolved liability")
        else:
            _number(compute["reserved_cpu_seconds"], "reserved compute")
            _number(compute["charged_cpu_seconds"], "charged compute")
            if compute["provenance"] == "not_admitted" and (compute["reserved_cpu_seconds"] != 0 or compute["charged_cpu_seconds"] != 0 or compute["unresolved_liability"] is not False):
                raise ValueError("Not-admitted accounting must record zero amounts and no liability")
            if compute["provenance"] == "reserved" and (compute["reserved_cpu_seconds"] <= 0 or compute["charged_cpu_seconds"] != 0 or compute["unresolved_liability"] is not True):
                raise ValueError("Reserved accounting must retain its positive reservation and unresolved liability")
    _canonical(project)
    _canonical(runs)


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _allowed_paths(runs: list[dict]) -> tuple[set[str], set[str]]:
    required, optional = set(), set()
    for run in runs:
        prefix = f"runs/{run['id']}/"
        required.update({prefix + "input.csv", prefix + ("recipe.py" if run["recipe"] == "summary-python" else "recipe.R")})
        optional.add(prefix + "result.json")
    return required, optional


def _file_cap(name: str) -> int:
    if name.endswith("/input.csv"):
        return MAX_INPUT_BYTES
    if name.endswith(("/recipe.py", "/recipe.R")):
        return MAX_RECIPE_BYTES
    return MAX_FILE_BYTES


def _safe_name(name: object) -> str:
    if not isinstance(name, str) or len(name) > 150:
        raise ValueError("Invalid archive member name")
    if name in _FIXED_FILES:
        return name
    parts = name.split("/")
    if len(parts) != 3 or parts[0] != "runs" or not _ID.fullmatch(parts[1]) or parts[2] not in {"input.csv", "recipe.py", "recipe.R", "result.json"}:
        raise ValueError("Unsafe or unexpected archive member")
    return name


def _validate_files(runs: list[dict], files: dict[str, bytes]):
    if type(files) is not dict:
        raise ValueError("Selected files must be a dictionary of relative names and bytes")
    required, optional = _allowed_paths(runs)
    if not required <= set(files) or not set(files) <= required | optional:
        raise ValueError("Selected files do not match the declared run lineage")
    for name, data in files.items():
        _safe_name(name)
        if type(data) is not bytes:
            raise ValueError("Selected files must be bytes, not paths, symlinks, or file handles")
        if len(data) > _file_cap(name):
            raise ValueError("Evidence file is too large")
    for run in runs:
        prefix = f"runs/{run['id']}/"
        recipe = prefix + ("recipe.py" if run["recipe"] == "summary-python" else "recipe.R")
        if _hash(files[prefix + "input.csv"]) != run["input_sha256"]:
            raise ValueError("Input SHA-256 does not match run lineage")
        if _hash(files[recipe]) != run["recipe_sha256"]:
            raise ValueError("Recipe SHA-256 does not match run lineage")
        result = files.get(prefix + "result.json")
        if result is None:
            if run["status"] == "succeeded" or run["metrics"]:
                raise ValueError("Recorded results are missing their result.json evidence")
        else:
            value = _json(result, canonical=False)
            _metrics(value, run["condition"], empty=False)
            if _canonical(value) != _canonical(run["metrics"]):
                raise ValueError("Run metrics do not match result.json")


def _comparison(runs: list[dict], files: dict[str, bytes]) -> dict:
    gaps = []
    for run in runs:
        run_id = run["id"]
        if run["status"] != "succeeded":
            gaps.append({"run_id": run_id, "code": "run_not_succeeded", "detail": run["status"]})
        if f"runs/{run_id}/result.json" not in files:
            gaps.append({"run_id": run_id, "code": "result_not_available"})
        if not run["environment"]:
            gaps.append({"run_id": run_id, "code": "environment_not_recorded"})
        if not run["cleanup_verified"]:
            gaps.append({"run_id": run_id, "code": "cleanup_not_verified"})
        if run["compute"]["unresolved_liability"]:
            gaps.append({"run_id": run_id, "code": "unresolved_compute_liability"})
    successful = {r["condition"] for r in runs if r["status"] == "succeeded"}
    for condition in ("baseline", "alternative"):
        if condition not in successful:
            gaps.append({"run_id": None, "code": "condition_without_successful_run", "condition": condition})
    return {
        "format": FORMAT,
        "runs": [{field: r[field] for field in (
            "id", "condition", "recipe", "status", "input_sha256", "recipe_sha256", "metrics"
        )} for r in runs],
        "gaps": gaps,
        "warning": WARNING,
    }


def _readme(project: dict) -> bytes:
    return (
        "# Research evidence bundle\n\n"
        f"Project ID: {project['id']}\n\n{WARNING}\n\n"
        "project.json records the study question and declared success criterion.\n"
        "runs.json includes every supplied run, including failures and incomplete work.\n"
        "comparison.json reports descriptive metrics and explicit evidence gaps.\n"
        "METHODS.md describes the reviewed conditions and a bounded rerun procedure.\n"
        "Selected input, recipe, and available result bytes live under runs/<run-id>/.\n\n"
        "checksums.json lists the exact SHA-256 and size of every other archive member.\n"
        "It excludes itself to avoid a recursive checksum. Record the whole archive's\n"
        "SHA-256 separately in a trusted system to detect wholesale replacement.\n"
        "There is no signature, external attestation, or automated publication.\n\n"
        "RO-Crate-style JSON-LD describes the files. No formal RO-Crate profile\n"
        "certification, source license grant, or publication date is asserted.\n"
        "Do not execute bundled recipes until independently reviewed.\n"
    ).encode("utf-8")


_METHODS = (
    "# Methods and limitations\n\n"
    "This version supports public or synthetic CSV data with columns group,value.\n"
    "The baseline retains all rows. The alternative removes one smallest and one\n"
    "largest value from each group with at least five rows. Each condition reports\n"
    "the retained count and arithmetic mean overall and per group. These are\n"
    "descriptive summaries, not significance tests or scientific acceptance.\n\n"
    "For each run, use its own exact input.csv and recipe.py or recipe.R. Inspect\n"
    "the recipe's entry point and run condition before rerunning. Recreate the\n"
    "recorded immutable environment through Outfitter with explicit CPU, memory,\n"
    "time and output limits, network disabled, and reviewed read-only input.\n"
    "Compare independently calculated expected values against result.json.\n"
    "The export does not download images, execute code, rerun jobs or certify that\n"
    "an image remains obtainable. It does not include credentials or the runner.\n\n"
    "A queued, failed, cancelled or interrupted run may have no result. Inspect\n"
    "comparison.json gaps, run errors, cleanup_verified and compute liabilities.\n"
    "Charged CPU seconds are conservative estimated upper bounds, not a metered\n"
    "measurement. Not-admitted work records zero charge; unknown accounting uses\n"
    "null amounts with unresolved liability, never an invented zero or refund.\n"
    "A valid bundle can contain unresolved cleanup or execution.\n\n"
    "Input and recipe hashes bind declared lineage to exact bytes. Canonical JSON\n"
    "manifests are hashed, and result JSON must match the declared run metrics.\n"
    "These checks establish internal consistency, not authenticity or validity.\n"
    "Membership, sessions, keys, raw logs and local host paths are excluded from\n"
    "metadata. The exporting operator remains responsible for the selected data\n"
    "and recipe content, its classification, and permission to share it.\n"
).encode("utf-8")


def _crate(project: dict, members: dict[str, bytes]) -> dict:
    # checksums.json is included without a hash: it subsequently hashes this file.
    paths = sorted(set(members) | {"checksums.json"})
    graph = [{
        "@id": "ro-crate-metadata.json", "@type": "CreativeWork", "about": {"@id": "./"},
        "name": "Research evidence metadata",
    }, {
        "@id": "./", "@type": "Dataset", "name": project["name"],
        "description": project["question"], "dateCreated": project["created_at"],
        "license": "No rights are granted by this export; review source licenses and project agreements before reuse.",
        "hasPart": [{"@id": p} for p in paths],
    }]
    for path in paths:
        item = {"@id": path, "@type": "File", "name": path}
        if path in members:
            item.update({"contentSize": str(len(members[path])), "sha256": _hash(members[path])})
        graph.append(item)
    return {"@context": "https://w3id.org/ro/crate/1.2/context", "@graph": graph}


def _members(project: dict, runs: list[dict], files: dict[str, bytes]) -> dict[str, bytes]:
    members = dict(files)
    members.update({
        "project.json": _canonical(project), "runs.json": _canonical(runs),
        "comparison.json": _canonical(_comparison(runs, files)),
        "README.md": _readme(project), "METHODS.md": _METHODS,
    })
    members["ro-crate-metadata.json"] = _canonical(_crate(project, members))
    members["checksums.json"] = _canonical({
        "format": FORMAT, "algorithm": "sha256",
        "files": {name: {"sha256": _hash(data), "size": len(data)} for name, data in sorted(members.items())},
    })
    if len(members) > MAX_FILES or sum(map(len, members.values())) > MAX_TOTAL_BYTES:
        raise ValueError("Evidence bundle exceeds total limits")
    if any(len(data) > _file_cap(name) for name, data in members.items()):
        raise ValueError("Evidence member exceeds its size limit")
    return members


def build_bundle(project: dict, runs: list[dict], files: dict[str, bytes]) -> bytes:
    """Return a deterministic ZIP; exclude private top-level record fields.

    Nested public metadata is strict: unexpected fields fail closed. The service
    must supply all runs in one consistent snapshot and authorize the download.
    """
    if type(project) is not dict or type(runs) is not list or any(type(r) is not dict for r in runs):
        raise ValueError("Invalid project or run records")
    project = {k: v for k, v in project.items() if k in _PROJECT_FIELDS}
    runs = [{k: v for k, v in run.items() if k in _RUN_FIELDS} for run in runs]
    _validate_records(project, runs)
    runs.sort(key=lambda r: r["id"])
    _validate_files(runs, files)
    members = _members(project, runs, files)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
        for name, data in sorted(members.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, data)
    payload = output.getvalue()
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise ValueError("Evidence archive exceeds its size limit")
    return payload


def _read_archive(payload: bytes) -> dict[str, bytes]:
    if type(payload) is not bytes or len(payload) > MAX_ARCHIVE_BYTES:
        raise ValueError("Invalid or oversized evidence archive")
    try:
        with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
            infos = archive.infolist()
            if archive.comment or len(infos) > MAX_FILES:
                raise ValueError("Unsupported archive comment or excessive member count")
            members, total, spans = {}, 0, []
            for info in infos:
                name = _safe_name(info.filename)
                if info.orig_filename != name or name in members:
                    raise ValueError("Duplicate or ambiguous archive member name")
                mode = info.external_attr >> 16
                if info.is_dir() or stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                    raise ValueError("Only regular files are allowed; symlinks are refused")
                if info.flag_bits & ~0x800 or info.extra or info.comment:
                    raise ValueError("Encrypted or unsupported archive member metadata")
                if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise ValueError("Unsupported archive compression")
                if not 0 <= info.file_size <= _file_cap(name) or info.compress_size < 0:
                    raise ValueError("Archive member exceeds its size limit")
                total += info.file_size
                if total > MAX_TOTAL_BYTES or info.file_size > max(1, info.compress_size) * MAX_COMPRESSION_RATIO:
                    raise ValueError("Excessive expansion or total uncompressed size")
                # Validate local headers as well as the central directory. This
                # refuses alternate names, hidden extra fields and overlapping data.
                offset = info.header_offset
                if offset < 0 or offset + 30 > len(payload):
                    raise ValueError("Invalid local ZIP header")
                header = struct.unpack_from("<4s5H3I2H", payload, offset)
                signature, _, flags, compression, _, _, crc, compressed, uncompressed, name_len, extra_len = header
                local_name = payload[offset + 30:offset + 30 + name_len]
                if signature != b"PK\x03\x04" or flags != info.flag_bits or compression != info.compress_type or crc != info.CRC or compressed != info.compress_size or uncompressed != info.file_size or extra_len:
                    raise ValueError("Local ZIP metadata differs from the directory")
                if local_name != name.encode("utf-8"):
                    raise ValueError("Local and directory names differ")
                end = offset + 30 + name_len + extra_len + compressed
                if end > archive.start_dir:
                    raise ValueError("Archive member overlaps the directory")
                spans.append((offset, end))
                compressed_data = payload[offset + 30 + name_len:end]
                if compression == zipfile.ZIP_STORED:
                    data = compressed_data
                else:
                    # ZipExtFile trusts the declared uncompressed size and can
                    # truncate a lying member. Inspect the raw stream instead.
                    inflater = zlib.decompressobj(-zlib.MAX_WBITS)
                    data = inflater.decompress(compressed_data, _file_cap(name) + 1)
                    if not inflater.eof or inflater.unconsumed_tail or inflater.unused_data:
                        raise ValueError("Truncated, excessive, or trailing compressed stream")
                if len(data) != info.file_size or len(data) > _file_cap(name) or zlib.crc32(data) != info.CRC:
                    raise ValueError("Actual member size or CRC differs from declaration")
                members[name] = data
            cursor = 0
            for begin, end in sorted(spans):
                if begin != cursor:
                    raise ValueError("Overlapping, prefixed, or hidden archive members")
                cursor = end
            if cursor != archive.start_dir:
                raise ValueError("Unexpected data before archive directory")
            # This format emits neither ZIP64 nor a comment/trailing payload.
            if len(payload) < 22 or payload[-22:-18] != b"PK\x05\x06" or payload[-2:] != b"\0\0":
                raise ValueError("Unsupported trailing archive data")
            _, disk, central_disk, disk_count, count, central_size, central_offset, _ = struct.unpack_from("<4s4H2IH", payload, len(payload) - 22)
            expected_size = sum(46 + len(info.filename.encode("utf-8")) for info in infos)
            if disk or central_disk or disk_count != len(infos) or count != len(infos) or central_offset != cursor or central_size != expected_size or cursor + central_size + 22 != len(payload):
                raise ValueError("Unsupported or inconsistent archive directory")
            return members
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, struct.error, OverflowError, zlib.error) as exc:
        raise ValueError("Invalid evidence archive") from exc


def verify_bundle(payload: bytes) -> dict:
    """Check bounded archive safety, exact bytes, public schemas and run lineage.

    Raises ValueError on any failure. A valid result is not a claim of scientific
    validity, authenticity, successful execution, or independent reproducibility.
    """
    members = _read_archive(payload)
    if not _FIXED_FILES <= set(members):
        raise ValueError("Required evidence manifests are missing")
    manifest = _json(members["checksums.json"])
    _keys(manifest, frozenset({"format", "algorithm", "files"}), "checksum manifest")
    if manifest["format"] != FORMAT or manifest["algorithm"] != "sha256" or type(manifest["files"]) is not dict:
        raise ValueError("Unsupported checksum manifest")
    if set(manifest["files"]) != set(members) - {"checksums.json"}:
        raise ValueError("Checksum membership is not exact")
    for name, entry in manifest["files"].items():
        _keys(entry, frozenset({"sha256", "size"}), "checksum entry")
        if type(entry["size"]) is not int or entry != {"sha256": _hash(members[name]), "size": len(members[name])}:
            raise ValueError("Evidence checksum or declared size does not match")
    project, runs = _json(members["project.json"]), _json(members["runs.json"])
    _validate_records(project, runs)
    if runs != sorted(runs, key=lambda r: r["id"]):
        raise ValueError("Run records must use canonical ID ordering")
    files = {name: data for name, data in members.items() if name not in _FIXED_FILES}
    _validate_files(runs, files)
    expected = _members(project, runs, files)
    if members != expected:
        raise ValueError("Derived manifests or methods differ from the evidence records")
    return {
        "valid": True, "project_id": project["id"], "run_count": len(runs),
        "file_count": len(members), "bundle_sha256": _hash(payload),
        "gaps": _comparison(runs, files)["gaps"], "warning": WARNING,
    }
