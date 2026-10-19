"""Content-free assessment preparation; no store, provider, or bundle effects.

Structure and declared approval are never execution or cleanup authority.
"""

from datetime import datetime, timezone
import hashlib
import json
import os
import re
import stat


MANIFEST_SCHEMA = "educloud.benchmark-manifest/v1"
MAX_DOCUMENT_BYTES = 65536
MAX_INTEGER = 9007199254740991
MAX_DEPTH = 12
ID = re.compile(r"[a-f0-9]{32}\Z")
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")


class AssessmentError(ValueError):
    """A fixed, content-free public error."""


def reject():
    raise AssessmentError("Invalid or unsupported assessment document.")


def strict_document(payload):
    if type(payload) is not bytes or not 0 < len(payload) <= MAX_DOCUMENT_BYTES:
        reject()

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                reject()
            result[key] = value
        return result

    def integer(text):
        if len(text) > 17:
            reject()
        value = int(text)
        if abs(value) > MAX_INTEGER:
            reject()
        return value

    def noninteger(_):
        reject()

    try:
        value = json.loads(payload.decode("ascii"), object_pairs_hook=pairs,
                           parse_int=integer, parse_float=noninteger,
                           parse_constant=noninteger)
        _bounded_tree(value)
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        reject()
    if type(value) is not dict:
        reject()
    return value


def _bounded_tree(value, depth=0):
    if depth > MAX_DEPTH:
        reject()
    if type(value) is dict:
        for key, child in value.items():
            if type(key) is not str or not key.isascii() or len(key) > 64:
                reject()
            _bounded_tree(child, depth + 1)
    elif type(value) is list:
        if len(value) > 64:
            reject()
        for child in value:
            _bounded_tree(child, depth + 1)
    elif type(value) is str:
        if not value.isascii() or len(value) > 256:
            reject()
    elif type(value) is int:
        if abs(value) > MAX_INTEGER:
            reject()
    elif value is not None and type(value) not in (bool, int):
        reject()


def read_document(path):
    """Read one bounded regular file without following its final symlink."""
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= MAX_DOCUMENT_BYTES:
            reject()
        with os.fdopen(descriptor, "rb") as source:
            descriptor = None
            payload = source.read(MAX_DOCUMENT_BYTES + 1)
            after = os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or len(payload) != before.st_size:
            reject()
        return strict_document(payload)
    except (OSError, ValueError):
        raise AssessmentError("Assessment input could not be read as a bounded regular document.") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def fields(value, names):
    if type(value) is not dict or set(value) != set(names):
        reject()


def number(value, minimum=0, maximum=MAX_INTEGER):
    if type(value) is not int or not minimum <= value <= maximum:
        reject()
    return value


def choice(value, options):
    if type(value) is not str or value not in options:
        reject()
    return value


def opaque_id(value):
    if type(value) is not str or ID.fullmatch(value) is None:
        reject()


def timestamp(value):
    if type(value) is not str or TIMESTAMP.fullmatch(value) is None:
        reject()
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        reject()


def canonical_bytes(value):
    """Sorted ASCII JSON, integer tokens only, no whitespace or final newline."""
    _bounded_tree(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def manifest_digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def validate_manifest(value):
    fields(value, ("schema", "assessment_id", "project_id", "pathway", "data_classification",
                   "workload", "hardware", "measurement", "design", "quality", "ceilings",
                   "expires_at", "reservations", "instruction"))
    _bounded_tree(value)
    if value["schema"] != MANIFEST_SCHEMA:
        reject()
    for key in ("assessment_id", "project_id"):
        opaque_id(value[key])
    choice(value["pathway"], ("research", "instructional"))
    choice(value["data_classification"], ("public", "synthetic"))
    timestamp(value["expires_at"])
    workload = value["workload"]
    fields(workload, ("recipe_id", "recipe_sha256", "environment_id", "image", "stages", "dependencies"))
    choice(workload["recipe_id"], ("summary-python", "summary-r"))
    if workload["stages"] != ["analysis"] or workload["dependencies"] != []:
        reject()
    # Only the installed, reviewed active catalog is authority for software.
    # Uploaded hashes and a structurally valid snapshot cannot enable candidates.
    from .catalog import list_environments, recipe_source
    entries = list_environments()
    matching = [entry for entry in entries if entry["id"] == workload["environment_id"]]
    if len(matching) != 1:
        reject()
    entry = matching[0]
    if entry["status"] != "active" or any(workload[k] != entry[e] for k, e in (
            ("recipe_id", "recipe"), ("recipe_sha256", "recipe_sha256"), ("image", "image"))):
        reject()
    if hashlib.sha256(recipe_source(entry)).hexdigest() != workload["recipe_sha256"]:
        reject()
    hardware = value["hardware"]
    fields(hardware, ("kind", "architecture", "cpu_count", "memory_bytes", "gpu_count"))
    choice(hardware["kind"], ("local_docker",))
    choice(hardware["architecture"], ("arm64", "amd64"))
    number(hardware["cpu_count"], 1, 128)
    number(hardware["memory_bytes"], 1, 1 << 50)
    number(hardware["gpu_count"], 0, 0)
    method = value["measurement"]
    fields(method, ("method", "version"))
    choice(method["method"], ("outfitter_normalized",))
    number(method["version"], 1, 1)
    design = value["design"]
    fields(design, ("size_unit", "fitting_sizes", "held_out_size", "repetitions", "seeds",
                    "sampling", "exclusions", "skew", "smaller_design"))
    choice(design["size_unit"], ("rows",))
    sizes = design["fitting_sizes"]
    if type(sizes) is not list or not 1 <= len(sizes) <= 3:
        reject()
    for size in sizes:
        number(size, 1, 1000)
    if sorted(set(sizes)) != sizes:
        reject()
    number(design["held_out_size"], 1, 1000)
    if design["held_out_size"] in sizes:
        reject()
    repetitions = number(design["repetitions"], 1, 3)
    if type(design["seeds"]) is not list or len(design["seeds"]) != repetitions:
        reject()
    for seed in design["seeds"]:
        number(seed, 0, (1 << 32) - 1)
    if len(set(design["seeds"])) != repetitions:
        reject()
    choice(design["sampling"], ("synthetic_fixture", "random", "stratified", "convenience", "full"))
    choice(design["exclusions"], ("none", "declared"))
    choice(design["skew"], ("unknown", "declared", "none"))
    smaller = len(sizes) != 3 or repetitions != 3
    if smaller:
        fields(design["smaller_design"], ("approved_by_user", "reason"))
        if design["smaller_design"]["approved_by_user"] is not True:
            reject()
        choice(design["smaller_design"]["reason"], ("allowance_limited", "capacity_limited", "operator_selected"))
    elif design["smaller_design"] is not None:
        reject()
    quality = value["quality"]
    fields(quality, ("check", "relative_tolerance_parts_per_trillion", "absolute_tolerance_nanounits", "maximum_failure_parts_per_million"))
    choice(quality["check"], ("reviewed_summary",))
    number(quality["relative_tolerance_parts_per_trillion"], 1, 1)
    number(quality["absolute_tolerance_nanounits"], 1, 1)
    number(quality["maximum_failure_parts_per_million"], 0, 1000000)
    ceilings = value["ceilings"]
    fields(ceilings, ("max_attempts", "cpu_seconds", "memory_bytes", "scratch_bytes", "output_bytes", "timeout_seconds_per_attempt"))
    attempts = (len(sizes) + 1) * repetitions
    number(ceilings["max_attempts"], attempts, attempts)
    number(ceilings["cpu_seconds"], attempts * 30, attempts * 30)
    number(ceilings["memory_bytes"], 268435456, 268435456)
    number(ceilings["scratch_bytes"], 1, 128 * 1024**2)
    number(ceilings["output_bytes"], 1, 65536)
    number(ceilings["timeout_seconds_per_attempt"], 30, 30)
    if ceilings["memory_bytes"] > hardware["memory_bytes"]:
        reject()
    reservations = value["reservations"]
    if type(reservations) is not list or len(reservations) > attempts:
        reject()
    slots, attempt_ids, reservation_ids = set(), set(), set()
    for reservation in reservations:
        fields(reservation, ("attempt_id", "stage_id", "sample_role", "sample_size",
                             "repetition", "reservation_id"))
        opaque_id(reservation["attempt_id"])
        opaque_id(reservation["reservation_id"])
        choice(reservation["stage_id"], workload["stages"])
        choice(reservation["sample_role"], ("fitting", "held_out"))
        number(reservation["sample_size"], 1, 1000)
        number(reservation["repetition"], 1, repetitions)
        if (reservation["sample_role"] == "fitting" and reservation["sample_size"] not in sizes
                or reservation["sample_role"] == "held_out" and reservation["sample_size"] != design["held_out_size"]):
            reject()
        slot = tuple(reservation[k] for k in ("stage_id", "sample_role", "sample_size", "repetition"))
        if slot in slots or reservation["attempt_id"] in attempt_ids or reservation["reservation_id"] in reservation_ids:
            reject()
        slots.add(slot)
        attempt_ids.add(reservation["attempt_id"])
        reservation_ids.add(reservation["reservation_id"])
    instruction = value["instruction"]
    if value["pathway"] == "research":
        if instruction is not None:
            reject()
    else:
        fields(instruction, ("enrollment", "simultaneous_sessions", "assignment_count", "deadline_peak_sessions", "per_student_storage_bytes"))
        enrollment = number(instruction["enrollment"], 1, 100000)
        number(instruction["simultaneous_sessions"], 1, enrollment)
        number(instruction["deadline_peak_sessions"], 1, enrollment)
        number(instruction["assignment_count"], 1, 10000)
        number(instruction["per_student_storage_bytes"], 1, 1 << 50)
    if len(canonical_bytes(value)) > MAX_DOCUMENT_BYTES:
        reject()
    return value


def manifest_summary(value, *, now=None):
    validate_manifest(value)
    sampled = now or datetime.now(timezone.utc)
    declared = len(value["reservations"])
    planned = value["ceilings"]["max_attempts"]
    return {
        "manifest_sha256": manifest_digest(value), "structure_valid": True,
        "expired": timestamp(value["expires_at"]) <= sampled,
        "provisional": value["design"]["smaller_design"] is not None,
        "reservation_coverage": {"declared": declared, "planned": planned,
                                 "status": "unreserved" if declared == 0 else "complete" if declared == planned else "partial",
                                 "references_verified": False},
        "execution_authorized": False, "upload_admission": False,
        "cleanup_trust_verified": False, "retention_finalized": False,
    }
