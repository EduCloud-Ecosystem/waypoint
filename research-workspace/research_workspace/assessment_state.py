"""AS1 metadata policy for the existing scoped Store/Owner/Guard.

This module has no state or Application constructor. Fresh positive fixtures
are constructed only in tests. Production entry points refuse this profile.
Limits describe serialized metadata, never physical capacity or cleanup proof.
"""
import hashlib
import json
import re
from datetime import datetime

from . import finite_authority as finite

MARKER = 0x45415331
MODE = "local-assessment-synthetic-AS1"
FORMAT = "educloud-assessment-authority-AS1"
GUARD_FORMAT = "educloud-assessment-operation-guard-AS1"
M = 2**53 - 1
G = 2**63 - 1
LIMITS = {
    "rows": 16, "subject_rows": 4, "manifest": 65536, "row_other": 2048,
    "row_fields": 67584, "row_json": 135168, "aggregate_fields": 1081344,
    "aggregate_json": 2359296, "metadata_row": 1024, "summary": 32768,
    "status": 16384, "observations": 8, "observation": 1024,
    "control": 131072, "reply": 65536, "chunk": 65536,
    "ordinary": 128, "essential": 1, "guard_entry": 1024,
    "guard_envelope": 2048, "guard_document": 262144,
}
FIELDS = ("id", "project_id", "subject", "request_key", "manifest_json",
          "manifest_sha256", "outfitter_authority_id", "created_at", "expires_at",
          "local_phase", "deny_reason", "version", "last_scope_generation",
          "declared_upload_ceiling_bytes", "admitting_owner_epoch")
META_FIELDS = tuple(x for x in FIELDS if x != "manifest_json")
FULL = ",".join(FIELDS)
META = ",".join(META_FIELDS)
BY_ID = f"SELECT {FULL} FROM assessment_sessions WHERE id=?"
BY_KEY = f"SELECT {FULL} FROM assessment_sessions WHERE project_id=? AND subject=? AND request_key=?"
ALL_META = f"SELECT {META} FROM assessment_sessions ORDER BY id"
SUBJECT_META = f"SELECT {META} FROM assessment_sessions WHERE subject=? ORDER BY id"
COUNT = "SELECT count(*) FROM assessment_sessions"
SUBJECT_COUNT = "SELECT count(*) FROM assessment_sessions WHERE subject=?"
INSERT = f"INSERT INTO assessment_sessions({FULL}) VALUES({','.join('?' for _ in FIELDS)})"
UPDATE = ("UPDATE assessment_sessions SET local_phase=?,deny_reason=?,version=?,last_scope_generation=? "
          "WHERE id=? AND version=? AND local_phase=?")
AUTH_SESSION = ("SELECT s.subject,s.expires FROM sessions s JOIN users u ON u.subject=s.subject "
                "WHERE s.token_hash=? AND s.expires>? AND u.active=1")
LOGOUT_SUBJECT = "SELECT subject FROM sessions WHERE token_hash=?"
SCHEMA = (
    "CREATE TABLE assessment_sessions(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,"
    "subject TEXT NOT NULL,request_key TEXT NOT NULL,manifest_json TEXT NOT NULL,"
    "manifest_sha256 TEXT NOT NULL,outfitter_authority_id TEXT NOT NULL,created_at TEXT NOT NULL,"
    "expires_at TEXT NOT NULL,local_phase TEXT NOT NULL,deny_reason TEXT NOT NULL,"
    "version INTEGER NOT NULL,last_scope_generation INTEGER,declared_upload_ceiling_bytes INTEGER NOT NULL,"
    "admitting_owner_epoch TEXT NOT NULL,UNIQUE(project_id,subject,request_key))",
)
TEXT = {"id": 32, "project_id": 32, "subject": 80, "request_key": 32,
        "manifest_json": 65536, "manifest_sha256": 64, "outfitter_authority_id": 32,
        "created_at": 27, "expires_at": 20, "local_phase": 7, "deny_reason": 17,
        "admitting_owner_epoch": 32}
INTS = {"version", "last_scope_generation", "declared_upload_ceiling_bytes"}
PHASES = {"pending", "bound", "denied"}
REASONS = {"none", "cancelled", "access_ended", "expired", "restart", "clock_rollback",
           "ipc_uncertain", "storage_uncertain"}
AUTH_WRITES = {
    "INSERT INTO users VALUES(?,?,1)", "UPDATE users SET active=0 WHERE subject=?",
    "DELETE FROM sessions WHERE subject=?", "UPDATE users SET key_hash=? WHERE subject=?",
    "DELETE FROM sessions WHERE expires<=?", "INSERT INTO sessions VALUES(?,?,?)",
    "DELETE FROM sessions WHERE token_hash=?", "DELETE FROM members WHERE project_id=? AND subject=?",
    "INSERT INTO members VALUES(?,?,?) ON CONFLICT(project_id,subject) DO UPDATE SET role=excluded.role",
    "UPDATE projects SET payload=? WHERE id=?",
}
GUARD_WRITES = {
    "UPDATE operation_authority SET barrier=? WHERE authority_id=?",
    "UPDATE operation_authority SET sequence=? WHERE authority_id=?",
    "DELETE FROM operation_receipts WHERE essential=1", "INSERT INTO operation_receipts VALUES(?,?,?,?,?)",
    "UPDATE operation_receipts SET outcome=? WHERE id=? AND digest=? AND outcome='started'",
}


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def invalid():
    raise finite.InvalidProfile("Unsupported or uncertain assessment metadata.")


def policy():
    return {"format": FORMAT, "limits": dict(LIMITS), "row_counter_max": M,
            "guard_counter_max": G, "safety_sequence_tail": 64, "safety_generation_tail": 256,
            "publications_per_transaction": 4, "keep": False,
            "old_scoped_policy_sha256": finite.profile_digest(4)}


def policy_digest():
    return hashlib.sha256(encode(policy())).hexdigest()


class _Policy:
    """Typed internal binding; never selected from a flag, header or config."""


AS1 = _Policy()


def active(guard):
    return getattr(guard, "assessment", None) is AS1


def binding_valid(binding):
    return (type(binding) is dict
            and set(binding) == {"format", "authority_id", "logical_authority_sha256"}
            and binding["format"] == GUARD_FORMAT
            and type(binding["authority_id"]) is str
            and re.fullmatch("[a-f0-9]{32}", binding["authority_id"]) is not None
            and binding["logical_authority_sha256"] == policy_digest())


def schema():
    from .storage_inventory import SCHEMA_SQL
    from .operation_guard import SCHEMA as GUARD_SCHEMA
    return SCHEMA_SQL + GUARD_SCHEMA + SCHEMA


def validate_binding(root, binding, db):
    if not binding_valid(binding) or db.execute("PRAGMA user_version").fetchone()[0] != MARKER:
        invalid()
    authority = db.execute("SELECT authority_id,sequence FROM operation_authority").fetchmany(2)
    if (len(authority) != 1 or authority[0][0] != binding["authority_id"]
            or type(authority[0][1]) is not int or not 0 <= authority[0][1] <= G):
        invalid()
    config = finite.read_config(root)
    if (set(config) != {"version", "mode", "operation_guard", "logical_authority"}
            or type(config["version"]) is not int or config["version"] != 1
            or config["mode"] != MODE or config["operation_guard"] != binding
            or encode(config["logical_authority"]) != encode(policy())):
        invalid()


def guard_shape(data):
    """Additional AS1 bounds; existing guard validation remains authoritative."""
    operations = data["operations"]
    if (sum(not x["essential"] for x in operations.values()) > LIMITS["ordinary"]
            or len(operations) > LIMITS["ordinary"] + 1
            or any(len(encode({key: value})) > LIMITS["guard_entry"] for key, value in operations.items())
            or len(encode({**data, "operations": {}})) > LIMITS["guard_envelope"]
            or len(encode(data)) > LIMITS["guard_document"]):
        invalid()


def admission(data, essential):
    from .operation_guard import GuardPaused
    seq_tail, gen_tail = (0, 0) if essential else (64, 256)
    if (data["sequence"] + 1 > G - seq_tail or data["generation"] + 4 > G - gen_tail
            or not essential and sum(not x["essential"] for x in data["operations"].values()) >= LIMITS["ordinary"]):
        raise GuardPaused("intent_limit")
    # Reserve the maximum essential entry and publication/failure envelope,
    # independently of the short encoding of the current ordinary candidate.
    if (LIMITS["ordinary"] + 1) * LIMITS["guard_entry"] + LIMITS["guard_envelope"] > LIMITS["guard_document"]:
        invalid()


def guard_request(request):
    """Only a small closed metadata projection may be hashed by an AS1 guard."""
    allowed = {"action", "scope_id", "project_id", "subject", "role", "request_key",
               "manifest_sha256", "declared_upload_ceiling_bytes", "reason"}
    if (type(request) is not dict or set(request) - allowed or len(encode(request)) > 2048
            or any(type(v) not in (str, int, type(None)) for v in request.values())
            or any(type(v) is str and (not v.isascii() or len(v) > 80) for v in request.values())
            or any(type(v) is int and not 0 <= v <= M for v in request.values())):
        invalid()
    actions = {"initialize", "add-user", "revoke-user", "rotate-user", "sign-in", "logout", "membership",
               "close-project", "assessment-prepare", "assessment-observation", "spawn", "assessment-deny",
               "assessment-restart", "assessment-logout"}
    if request.get("action") not in actions:
        invalid()
    for key in ("scope_id", "project_id", "request_key"):
        if key in request and (type(request[key]) is not str or re.fullmatch("[a-f0-9]{32}", request[key]) is None):
            invalid()
    if "manifest_sha256" in request and re.fullmatch("[a-f0-9]{64}", str(request["manifest_sha256"])) is None:
        invalid()
    if "subject" in request and re.fullmatch("[A-Za-z0-9_-]{1,80}", str(request["subject"])) is None:
        invalid()
    if "reason" in request and request["reason"] not in REASONS:
        invalid()
    if "role" in request and request["role"] not in {None, "owner", "editor", "viewer"}:
        invalid()


def validate_row(row, *, metadata=False):
    expected = META_FIELDS if metadata else FIELDS
    value = dict(zip(row.keys(), row)) if not isinstance(row, dict) else row
    if set(value) != set(expected):
        invalid()
    for key in expected:
        item = value[key]
        if key in TEXT and (type(item) is not str or not item.isascii() or len(item) > TEXT[key]):
            invalid()
        if key in INTS and not (key == "last_scope_generation" and item is None):
            if type(item) is not int or not 0 <= item <= M:
                invalid()
    for key in ("id", "project_id", "request_key", "outfitter_authority_id", "admitting_owner_epoch"):
        if re.fullmatch("[a-f0-9]{32}", value[key]) is None:
            invalid()
    if (re.fullmatch("[a-f0-9]{64}", value["manifest_sha256"]) is None
            or re.fullmatch("[A-Za-z0-9_-]{1,80}", value["subject"]) is None
            or value["local_phase"] not in PHASES or value["deny_reason"] not in REASONS
            or (value["local_phase"] == "denied") != (value["deny_reason"] != "none")
            or value["version"] < 1 or value["declared_upload_ceiling_bytes"] < 1
            or value["local_phase"] != "denied" and value["version"] >= M):
        invalid()
    if (re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z", value["created_at"]) is None
            or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value["expires_at"]) is None):
        invalid()
    try:
        datetime.fromisoformat(value["created_at"].replace("Z", "+00:00"))
        datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00"))
    except ValueError:
        invalid()
    if metadata:
        if len(encode(value)) > LIMITS["metadata_row"]:
            invalid()
    else:
        from .assessment import strict_document, validate_manifest, canonical_bytes, timestamp
        other = {k: v for k, v in value.items() if k != "manifest_json"}
        if (len(encode(other)) > LIMITS["row_other"] or len(encode(value)) > LIMITS["row_json"]
                or hashlib.sha256(value["manifest_json"].encode("ascii")).hexdigest() != value["manifest_sha256"]):
            invalid()
        manifest = validate_manifest(strict_document(value["manifest_json"].encode("ascii")))
        if (canonical_bytes(manifest).decode("ascii") != value["manifest_json"]
                or manifest["project_id"] != value["project_id"]
                or value["declared_upload_ceiling_bytes"] > manifest["ceilings"]["scratch_bytes"]
                or timestamp(value["expires_at"]) > timestamp(manifest["expires_at"])):
            invalid()
    return value


def refuse_state(root):
    """Rejection only, before recovery/export destinations or content scans."""
    import os
    import stat
    from pathlib import Path
    root = Path(root)
    config = root / "config.json"
    if config.is_file():
        value = finite.read_config(root)
        logical, binding = value.get("logical_authority"), value.get("operation_guard")
        if (value.get("mode") == MODE or isinstance(logical, dict) and logical.get("format") == FORMAT
                or isinstance(binding, dict) and binding.get("format") == GUARD_FORMAT):
            invalid()
    # A rejection discriminator is not a live SQLite reader or an authority
    # validator. In particular, malformed legacy files must reach their
    # existing conservative inventory path, including no deserialize fallback.
    # SQLite stores user_version at header bytes 60..63 (big endian).
    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fd = os.open("workspace.sqlite3", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                         dir_fd=parent)
        except FileNotFoundError:
            return
        try:
            before = os.fstat(fd)
            identity = lambda info: (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                                     info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                invalid()
            header = bytearray()
            while len(header) < 100:
                chunk = os.read(fd, 100 - len(header))
                if not chunk:
                    break
                header.extend(chunk)
            current = os.stat("workspace.sqlite3", dir_fd=parent, follow_symlinks=False)
            if identity(before) != identity(os.fstat(fd)) or identity(before) != identity(current):
                invalid()
            if (len(header) >= 64 and header[:16] == b"SQLite format 3\x00"
                    and int.from_bytes(header[60:64], "big") == MARKER):
                invalid()
        finally:
            os.close(fd)
    finally:
        os.close(parent)
