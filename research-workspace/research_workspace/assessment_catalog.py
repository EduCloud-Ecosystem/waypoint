"""Read-only allowlist of disabled streaming assessment source candidates.

This module never imports a recipe, launches an interpreter, resolves an image,
or grants admission. It is separate from the accepted ordinary study catalog.
"""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat


ROOT = Path(__file__).resolve().parent.parent
MAX_BYTES = 65536
HASH = re.compile(r"[0-9a-f]{64}\Z")
PROFILE = {
    "id": "summary-fixed6-csv", "version": 1, "max_input_bytes": 88000013,
    "max_rows": 1000000, "max_groups": 128, "max_group_bytes": 64,
    "max_numeric_field_bytes": 21, "max_record_bytes": 88,
    "chunk_bytes": 65536, "scale": 1000000,
}
OUTPUT = {
    "schema": "educloud.summary-stream-result/v1",
    "workload_stdout_bytes": 61440, "workload_stderr_bytes": 256,
    "checker_stdout_bytes": 1024, "checker_stderr_bytes": 256,
    "manifest_output_bytes": 65536, "mean_fraction_digits": 12,
    "rounding": "half_even",
}
READINESS = dict.fromkeys((
    "execution_authorized", "ordinary_dispatch_available",
    "production_available", "actual_resource_accepted",
    "cleanup_trusted", "large_input_qualified",
), False)
SOURCES = {
    "summary-python-fixed6-stream-v1": ("summary-python", "python", "recipes/assessment/summary_fixed6.py"),
    "summary-r-fixed6-stream-v1": ("summary-r", "r", "recipes/assessment/summary_fixed6.R"),
}
CHECKER_SOURCE = "recipes/assessment/check_summary_fixed6.py"


def _refuse():
    raise ValueError("Assessment candidate catalog is unavailable or invalid.")


def _exact(value, expected):
    """Reject bool/int aliases and other coercive equality in fixed schemas."""
    if type(value) is not type(expected):
        _refuse()
    if isinstance(expected, dict):
        if set(value) != set(expected):
            _refuse()
        for key in expected:
            _exact(value[key], expected[key])
    elif isinstance(expected, list):
        if len(value) != len(expected):
            _refuse()
        for actual, item in zip(value, expected):
            _exact(actual, item)
    elif value != expected:
        _refuse()


def _fields(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        _refuse()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _refuse()
        result[key] = value
    return result


def _identity(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _read_packaged(relative):
    allowed = {"assessment-environments.json", CHECKER_SOURCE}
    allowed.update(record[2] for record in SOURCES.values())
    if type(relative) is not str or relative not in allowed:
        _refuse()
    parts = PurePosixPath(relative).parts
    directory = descriptor = None
    try:
        directory = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for name in parts[:-1]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= MAX_BYTES:
            _refuse()
        with os.fdopen(descriptor, "rb") as source:
            descriptor = None
            data = source.read(MAX_BYTES + 1)
            after = os.fstat(source.fileno())
        current = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
        if (len(data) != before.st_size or len(data) > MAX_BYTES
                or _identity(before) != _identity(after)
                or _identity(before) != _identity(current)
                or not stat.S_ISREG(current.st_mode)):
            _refuse()
        return data
    except (OSError, ValueError):
        _refuse()
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def _runtime(language):
    return {"language": language, "version": None, "image": None,
            "interpreter_identity_sha256": None, "qualification": "NOT RUN"}


def _checked_source(entry):
    digest = entry["code_sha256"]
    if type(digest) is not str or not HASH.fullmatch(digest):
        _refuse()
    data = _read_packaged(entry["source"])
    if hashlib.sha256(data).hexdigest() != digest:
        _refuse()
    return data


def load_catalog():
    try:
        value = json.loads(_read_packaged("assessment-environments.json"),
                           object_pairs_hook=_pairs,
                           parse_constant=lambda _: _refuse())
    except (ValueError, UnicodeError, RecursionError):
        _refuse()
    _fields(value, ("schema", "input_profile", "output_profile",
                    "readiness", "candidates", "checker"))
    _exact(value["schema"], "waypoint.assessment-candidates/v1")
    _exact(value["input_profile"], PROFILE)
    _exact(value["output_profile"], OUTPUT)
    _exact(value["readiness"], READINESS)
    entries = value["candidates"]
    if type(entries) is not list or len(entries) != len(SOURCES):
        _refuse()
    seen = set()
    for entry in entries:
        _fields(entry, ("recipe_id", "environment_id", "status", "source",
                        "code_sha256", "capability_mapping_ids", "runtime",
                        "additional_packages", "checker_environment_id"))
        identity = entry["environment_id"]
        if type(identity) is not str or identity not in SOURCES or identity in seen:
            _refuse()
        seen.add(identity)
        recipe, language, path = SOURCES[identity]
        _exact(entry["recipe_id"], recipe)
        _exact(entry["source"], path)
        _exact(entry["status"], "candidate")
        _exact(entry["runtime"], _runtime(language))
        _exact(entry["additional_packages"], [])
        _exact(entry["checker_environment_id"], "summary-fixed6-reference-v1")
        _exact(entry["capability_mapping_ids"], {
            condition: f"cpu-summary-fixed6-{language}-{condition}-v1"
            for condition in ("baseline", "alternative")
        })
        _checked_source(entry)
    checker = value["checker"]
    _fields(checker, ("recipe_id", "environment_id", "source", "code_sha256",
                      "status", "runtime", "additional_packages"))
    _exact(checker["recipe_id"], "summary-fixed6-reference")
    _exact(checker["environment_id"], "summary-fixed6-reference-v1")
    _exact(checker["source"], CHECKER_SOURCE)
    _exact(checker["runtime"], _runtime("python"))
    _exact(checker["additional_packages"], [])
    if checker["code_sha256"] is None:
        _exact(checker["status"], "unavailable")
    else:
        _exact(checker["status"], "candidate")
        _checked_source(checker)
    return deepcopy(value)


def list_candidates():
    """Return source metadata; every catalog readiness flag remains false."""
    return load_catalog()["candidates"]


def candidate_source(environment_id):
    if type(environment_id) is not str or environment_id not in SOURCES:
        _refuse()
    for entry in load_catalog()["candidates"]:
        if entry["environment_id"] == environment_id:
            return _checked_source(entry)
    _refuse()


def checker_source():
    entry = load_catalog()["checker"]
    if entry["status"] != "candidate":
        _refuse()
    return _checked_source(entry)
