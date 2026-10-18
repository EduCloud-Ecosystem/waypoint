"""Versioned logical authority limits for fresh opt-in disposable stores.

These are row/serialized-field limits, never disk quotas or SQLite headroom.
Counts come from existing authority tables. No separate capacity ledger exists.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import time
import sqlite3
from contextlib import closing

FORMAT = "educloud-finite-authority-v1"
GUARD_FORMAT = "educloud-operation-guard-v2"
MODE = "local-finite-public-data-v1"
USER_VERSION = 0x45464131
EXPORT_MODE = "local-finite-public-data-v2"
EXPORT_FORMAT = "educloud-finite-authority-v2"
EXPORT_GUARD_FORMAT = "educloud-operation-guard-v3"
EXPORT_USER_VERSION = 0x45464132
PUBLICATION_MODE = "local-finite-public-data-v3"
PUBLICATION_FORMAT = "educloud-finite-authority-v3"
PUBLICATION_GUARD_FORMAT = "educloud-operation-guard-v4"
PUBLICATION_USER_VERSION = 0x45464133
PROFILE_MODES = {1: MODE, 2: EXPORT_MODE, 3: PUBLICATION_MODE}
PROFILE_FORMATS = {1: FORMAT, 2: EXPORT_FORMAT, 3: PUBLICATION_FORMAT}
PROFILE_GUARDS = {1: GUARD_FORMAT, 2: EXPORT_GUARD_FORMAT, 3: PUBLICATION_GUARD_FORMAT}
PROFILE_DATABASES = {1: USER_VERSION, 2: EXPORT_USER_VERSION, 3: PUBLICATION_USER_VERSION}
MODES = set(PROFILE_MODES.values())
GUARD_FORMATS = set(PROFILE_GUARDS.values())
USER_VERSIONS = set(PROFILE_DATABASES.values())
ROW_LIMITS = {"users": 128, "projects": 20, "members": 1024, "runs": 1000,
              "sessions": 256, "sessions_per_subject": 8}
ENCODED_LIMITS = {"config": 65536, "project": 32768, "run": 262144,
                  "result": 65536, "request": 32768, "receipt": 2097152,
                  "recovery": 65536, "checkpoint_manifest": 4194304}
PATH_LIMIT = 1024
COUNTER_LIMIT = 2**63 - 1


class LogicalLimit(ValueError):
    pass


class InvalidProfile(ValueError):
    pass


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def profile(version=1):
    if type(version) is not int or version not in PROFILE_FORMATS:
        raise InvalidProfile("Unsupported finite authority version.")
    result = {"format": PROFILE_FORMATS[version], "row_limits": dict(ROW_LIMITS),
            "encoded_limits": dict(ENCODED_LIMITS), "path_utf8_bytes": PATH_LIMIT,
            "counter_limit": COUNTER_LIMIT}
    if version in (2, 3):
        result["export_protocol"] = "educloud-bounded-export-v1"
    if version == 3:
        result["guard_publication"] = {
            "format": "educloud-bounded-guard-publication-v1",
            "ordinary_scratch": ".operation-guard-ordinary.pending",
            "safety_scratch": ".operation-guard-safety.pending",
            "max_payload_bytes": 4194304, "unresolved_essential": "refuse",
            "residue": "preserve", "failure_notes": "origin_role"}
    return result


def profile_digest(version=1):
    return hashlib.sha256(encode(profile(version))).hexdigest()


def binding_version(binding):
    if not isinstance(binding, dict):
        raise InvalidProfile("Missing finite authority binding.")
    for version, name in PROFILE_GUARDS.items():
        if (binding.get("format") == name
                and binding.get("logical_authority_sha256") == profile_digest(version)):
            return version
    raise InvalidProfile("Unsupported finite authority binding.")


def database_version(version):
    profile(version)  # Reject unknown versions, including booleans.
    return PROFILE_DATABASES[version]


def bounded(value, kind):
    payload = encode(value)
    if len(payload) > ENCODED_LIMITS[kind]:
        raise LogicalLimit("The record exceeds this disposable profile's field-size limit.")
    return payload


def bounded_path(value):
    if len(str(value).encode()) > PATH_LIMIT:
        raise LogicalLimit("The path exceeds this disposable profile's supported length.")


def strict(content):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise InvalidProfile("Duplicate finite-profile field.")
            out[key] = value
        return out
    return json.loads(content, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(InvalidProfile("Nonfinite profile value.")))


def read_config(root):
    descriptor = os.open(Path(root) / "config.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > ENCODED_LIMITS["config"]:
            raise InvalidProfile("Invalid finite-profile configuration file.")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = None
            payload = source.read(ENCODED_LIMITS["config"] + 1)
        if len(payload) != info.st_size:
            raise InvalidProfile("Changing finite-profile configuration.")
        return strict(payload)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def validate_config(config, *, binding=None):
    allowed = {"version", "mode", "broker", "images", "total_budget_cpu_seconds",
               "operation_guard", "logical_authority", "recovery_retired"}
    required = {"version", "mode", "broker", "images", "total_budget_cpu_seconds", "logical_authority"}
    version = next((v for v in PROFILE_MODES if isinstance(config, dict)
                    and encode(config.get("logical_authority")) == encode(profile(v))), None)
    mode = PROFILE_MODES.get(version)
    if (not isinstance(config, dict) or set(config) - allowed or not required <= set(config)
            or version is None or config.get("mode") not in {mode, "recovery-held", "retired"}):
        raise InvalidProfile("Missing or unsupported finite authority profile; unrestricted fallback is disabled.")
    if binding is not None and config.get("operation_guard") != binding:
        raise InvalidProfile("The configuration and finite authority binding differ.")
    if binding is not None and binding_version(binding) != version:
        raise InvalidProfile("The policy and finite authority binding differ.")
    bounded(config, "config")
    if (type(config["version"]) is not int or config["version"] != 1
            or type(config["total_budget_cpu_seconds"]) is not int
            or not 30 <= config["total_budget_cpu_seconds"] <= 36000
            or not isinstance(config["images"], dict)):
        raise InvalidProfile("Unsupported finite-profile configuration values.")
    if not isinstance(config.get("broker"), str):
        raise InvalidProfile("The finite profile requires a broker path.")
    bounded_path(config["broker"])
    return version


def validate_binding(root, binding, db):
    version = binding_version(binding)
    if db.execute("PRAGMA user_version").fetchone()[0] != database_version(version):
        raise InvalidProfile("The database and finite authority binding differ.")
    authority = db.execute("SELECT authority_id,sequence FROM operation_authority").fetchmany(2)
    if (len(authority) != 1 or authority[0][0] != binding.get("authority_id")
            or type(authority[0][1]) is not int or not 0 <= authority[0][1] <= COUNTER_LIMIT):
        raise InvalidProfile("Missing or mismatched database authority; safety writes cannot bypass this binding.")
    bounded_path(root)
    validate_config(read_config(root), binding=binding)


def refuse_unqualified_recovery(archive):
    """Inspect both config and immutable checkpoint DB before destination effects."""
    config = read_config(archive)
    binding = config.get("operation_guard")
    with closing(sqlite3.connect((Path(archive) / "workspace.sqlite3").as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
    if (version or "logical_authority" in config or config.get("mode") in MODES
            or isinstance(binding, dict) and binding.get("format") in GUARD_FORMATS):
        raise ValueError("Finite-profile restore/cutover is not qualified. The checkpoint and source are preserved; no destination or provider effect was created.")


def admit(db, *, subject=None, sampled_time=None, **growth):
    """Run inside guard + SQLite transaction; callers also preflight before intent."""
    for table, amount in growth.items():
        if table not in {"users", "projects", "members", "runs", "sessions"} or amount < 0:
            raise ValueError("Unsupported logical admission check.")
        if table == "sessions":
            if subject is None:
                raise ValueError("Session admission requires its subject.")
            sampled_time = time.time() if sampled_time is None else sampled_time
            count = db.execute("SELECT count(*) FROM sessions WHERE expires>?", (sampled_time,)).fetchone()[0]
            personal = db.execute("SELECT count(*) FROM sessions WHERE expires>? AND subject=?",
                                  (sampled_time, subject)).fetchone()[0]
            if personal + amount > ROW_LIMITS["sessions_per_subject"]:
                raise LogicalLimit("This subject's finite session allowance is full. Existing sessions remain available.")
        else:
            count = db.execute("SELECT count(*) FROM " + table).fetchone()[0]
        if count + amount > ROW_LIMITS[table]:
            raise LogicalLimit("This disposable profile's logical allowance is full. Existing records are preserved.")
