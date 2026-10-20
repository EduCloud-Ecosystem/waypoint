"""Bounded read-only storage observations; never admission or deletion authority."""

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import re
import stat
import time

from .recovery import safe_root
from .runtime import strict_json, validate_receipt
from .store import canonical, identifier

FORMAT = "educloud-storage-observation-v1"
MAX_ENTRIES = 16384
MAX_BYTES = 128 * 1024 * 1024
MAX_FILE = 64 * 1024 * 1024
MAX_ROWS = 4096
AUTHORITY = {"workspace.sqlite3", "config.json", "broker/ledger.json"}
# Exact schema text includes primary/unique constraints and named indexes.
SCHEMA_SQL = (
    "CREATE TABLE users(subject TEXT PRIMARY KEY, key_hash TEXT UNIQUE NOT NULL, active INTEGER NOT NULL)",
    "CREATE TABLE sessions(token_hash TEXT PRIMARY KEY, subject TEXT NOT NULL, expires REAL NOT NULL)",
    "CREATE TABLE projects(id TEXT PRIMARY KEY, payload TEXT NOT NULL)",
    "CREATE TABLE members(project_id TEXT NOT NULL, subject TEXT NOT NULL, role TEXT NOT NULL, PRIMARY KEY(project_id,subject))",
    "CREATE TABLE runs(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,subject TEXT NOT NULL, status TEXT NOT NULL,payload TEXT NOT NULL,cancel_requested INTEGER NOT NULL DEFAULT 0, request_key TEXT,request_hash TEXT,UNIQUE(project_id,subject,request_key))",
    "CREATE INDEX queued ON runs(status)",
)


def _schema(db):
    return [(kind, name, table, "".join(sql.split()).upper() if sql else None)
            for kind, name, table, sql in db.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name").fetchmany(101)]


def _supported_schema(db):
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(";".join(SCHEMA_SQL))
        return _schema(db) == _schema(reference)
    finally:
        reference.close()


class InventoryError(ValueError):
    observed_bytes_lower_bound = 0
    observed_entries_lower_bound = 0


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_regular(parent, name, expected, maximum, capture=False):
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or _identity(before) != _identity(expected):
            raise InventoryError("Inventory refuses linked, replaced or special files.")
        if before.st_size > maximum:
            raise InventoryError("Inventory exceeds its bounded read allowance; usage is not zero.")
        digest = hashlib.sha256(); size = 0; chunks = []
        while True:
            chunk = os.read(fd, min(65536, maximum - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > maximum:
                raise InventoryError("A file grew beyond the bounded read allowance.")
            digest.update(chunk)
            if capture:
                chunks.append(chunk)
        if size != before.st_size or _identity(os.fstat(fd)) != _identity(before):
            raise InventoryError("A file changed while being inventoried.")
        return digest.hexdigest(), b"".join(chunks) if capture else None
    finally:
        os.close(fd)


def scan(root, *, max_entries=MAX_ENTRIES, max_bytes=MAX_BYTES):
    """Count the root, directories and every regular file; no exclusions."""
    root = safe_root(root)
    if type(max_entries) is not int or type(max_bytes) is not int or not 1 <= max_entries <= MAX_ENTRIES or not 1 <= max_bytes <= MAX_BYTES:
        raise ValueError("Choose positive inventory bounds within the supported scan ceiling.")
    entries, identities, captured = {}, {}, {}
    total = 0

    def add(path, info, kind, digest=None):
        nonlocal total
        if len(entries) >= max_entries:
            raise InventoryError("Inventory exceeds its bounded entry allowance; usage is not zero.")
        size = info.st_size if kind == "file" else 0
        total += size
        if total > max_bytes:
            raise InventoryError("Inventory exceeds its bounded byte allowance; usage is not zero.")
        entries[path] = {"kind": kind, "bytes": size}
        if digest is not None:
            entries[path]["sha256"] = digest
        identities[path] = _identity(info)

    def walk(fd, prefix, depth):
        if depth > 16:
            raise InventoryError("Inventory refuses excessive directory nesting.")
        before = os.fstat(fd)
        add(prefix or ".", before, "directory")
        names = []
        with os.scandir(fd) as children:
            for child in children:
                names.append(child.name)
                if len(names) + len(entries) > max_entries:
                    raise InventoryError("Inventory exceeds its bounded entry allowance; usage is not zero.")
        for name in sorted(names):
            relative = (prefix + "/" if prefix else "") + name
            if len(relative.encode()) > 2048:
                raise InventoryError("Inventory refuses excessively long paths.")
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if _identity(os.fstat(child)) != _identity(info):
                        raise InventoryError("A directory changed during inventory.")
                    walk(child, relative, depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                capture = relative in AUTHORITY or bool(re.fullmatch(r"broker/outputs/[0-9a-f]{64}\.json", relative))
                digest, payload = _read_regular(fd, name, info, min(MAX_FILE, max_bytes - total), capture)
                add(relative, info, "file", digest)
                if payload is not None:
                    captured[relative] = payload
            else:
                raise InventoryError("Inventory refuses symbolic links and special filesystem entries.")
        if _identity(os.fstat(fd)) != _identity(before):
            raise InventoryError("Directory contents changed during inventory.")

    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            walk(fd, "", 0)
        except InventoryError as exc:
            exc.observed_bytes_lower_bound = total
            exc.observed_entries_lower_bound = len(entries)
            raise
    finally:
        os.close(fd)
    report = {"format": FORMAT, "complete": True, "logical_bytes": total,
              "filesystem_entries": len(entries), "files": sum(x["kind"] == "file" for x in entries.values()),
              "directories": sum(x["kind"] == "directory" for x in entries.values()),
              "inventory_sha256": hashlib.sha256(canonical(entries).encode()).hexdigest(), "entries": entries}
    return report, identities, captured


@contextmanager
def observed_offline(root):
    """Only lock an existing lifecycle file. Never create/chmod a report lock."""
    path = root / "server.lock"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        yield False
        return
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise InventoryError("The service lock is linked or invalid.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InventoryError("Stop the workspace service before this offline observation.") from exc
        yield True
        if _identity(path.lstat()) != _identity(info):
            raise InventoryError("The lifecycle lock changed during observation.")
    finally:
        os.close(fd)


def _authority(report, captured, offline):
    if not offline:
        return None, "Lifecycle lock is absent; no offline retention classification is established."
    entries = report["entries"]
    if any(entries.get(name, {}).get("bytes", 0) for name in ("workspace.sqlite3-wal", "workspace.sqlite3-journal")):
        return None, "Nonempty SQLite WAL/journal requires operator reconciliation; no stale base-file classification."
    if not AUTHORITY <= captured.keys():
        return None, "Required workspace or broker authority is absent."
    try:
        config = strict_json(captured["config.json"])
        ledger = strict_json(captured["broker/ledger.json"])
        if config.get("mode") not in ("local-public-data", "local-synthetic-oidc") or ledger.get("version") != 2 or ledger.get("authority", {}).get("admission") != "active":
            return None, "Held, retired or unsupported authority remains protected."
        if (not isinstance(ledger.get("owner"), str) or not re.fullmatch(r"[0-9a-f]{32}", ledger["owner"])
                or not isinstance(ledger.get("runs"), dict) or not isinstance(ledger.get("projects"), dict)
                or len(ledger["runs"]) > 512 or len(ledger["projects"]) > 512):
            return None, "Broker ownership or run authority is unsupported."
        if any(row.get("unresolved_liability") is not False or row.get("status") not in ("succeeded", "failed", "cancelled") for row in ledger["runs"].values()):
            return None, "Unresolved or unsupported broker work remains protected."
        for pid, project in ledger["projects"].items():
            identifier(pid)
            if (set(project) != {"project_id", "budget_cpu_seconds"} or project["project_id"] != pid
                    or type(project["budget_cpu_seconds"]) is not int or not 0 < project["budget_cpu_seconds"] <= 1000000000):
                raise ValueError("Unsupported broker project authority")
        if any(row.get("project_id") not in ledger["projects"] or row.get("request_id") != rid
               for rid, row in ledger["runs"].items()):
            raise ValueError("Orphan broker reservation")
        raw = bytearray(captured["workspace.sqlite3"])
        if len(raw) < 100 or raw[:16] != b"SQLite format 3\x00":
            raise ValueError("Invalid database header")
        # SQLite's documented deserialize workaround changes only the memory copy.
        # Never do this when a nonempty WAL/journal could contain newer authority.
        raw[18:20] = b"\x01\x01"
        db = sqlite3.connect(":memory:")
        try:
            if ("TEMP_STORE=0",) in db.execute("PRAGMA compile_options").fetchall():
                raise ValueError("SQLite cannot guarantee memory-only temporary storage")
            db.deserialize(bytes(raw))
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA temp_store=MEMORY")
            if db.execute("PRAGMA temp_store").fetchone()[0] != 2 or db.execute("PRAGMA query_only").fetchone()[0] != 1 or db.execute("PRAGMA trusted_schema").fetchone()[0] != 0:
                raise ValueError("Required in-memory read safeguards are unavailable")
            deadline = time.monotonic() + 2
            db.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
            if not _supported_schema(db):
                raise ValueError("Unsupported schema or constraints")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Invalid database")
            projects = db.execute("SELECT id,payload FROM projects").fetchmany(MAX_ROWS + 1)
            runs = db.execute("SELECT id,project_id,status,payload,cancel_requested FROM runs").fetchmany(MAX_ROWS + 1)
            if len(projects) > MAX_ROWS or len(runs) > MAX_ROWS:
                raise ValueError("Authority exceeds bounded classification")
            if (len({pid for pid, _ in projects}) != len(projects)
                    or len({row[0] for row in runs}) != len(runs)):
                raise ValueError("Duplicate authority identities")
            for pid, _ in projects:
                identifier(pid)
            for rid, pid, _, _, _ in runs:
                identifier(rid); identifier(pid)
            return ({"projects": {pid: strict_json(payload) for pid, payload in projects},
                     "runs": runs, "ledger": ledger, "captured": captured}, None)
        finally:
            db.close()
    except (ValueError, TypeError, AttributeError, KeyError, sqlite3.Error):
        return None, "Authority is invalid, unsupported or too large; all artifacts remain protected."


def _candidates(authority, entries):
    candidates = {}
    if authority is None:
        return candidates
    from .recipes import IDS
    for rid, pid, status, payload, cancelled in authority["runs"]:
        try:
            run = strict_json(payload); project = authority["projects"][pid]
            receipt = dict(authority["ledger"]["runs"][rid])
            if authority["ledger"]["projects"][pid]["budget_cpu_seconds"] != project["budget_cpu_seconds"]:
                continue
            output_hash = receipt.get("output_sha256")
            if output_hash:
                output_name = "broker/outputs/" + output_hash + ".json"
                if (entries.get(output_name, {}).get("sha256") != output_hash
                        or entries[output_name]["bytes"] != receipt.get("output_bytes")
                        or receipt.get("output_error") or receipt.get("stdout") or receipt.get("stderr")):
                    continue
                output = strict_json(authority["captured"][output_name])
                if set(output) != {"stdout", "stderr"}:
                    continue
                receipt.update(output)
            elif receipt.get("output_bytes", 0) or receipt.get("output_error"):
                continue
            validate_receipt(receipt, run)
            if canonical(strict_json(receipt["stdout"])) != canonical(run["metrics"]):
                continue
            if (project["id"] != pid or project["status"] != "closed" or cancelled or status != "succeeded"
                    or run["id"] != rid or run["project_id"] != pid or run["status"] != status
                    or run["cleanup_verified"] is not True or run["compute"]["unresolved_liability"] is not False
                    or receipt["request_id"] != rid or receipt["project_id"] != pid
                    or receipt.get("owner") != authority["ledger"]["owner"]
                    or receipt["status"] != "succeeded" or receipt["cleanup_verified"] is not True
                    or receipt["unresolved_liability"] is not False
                    or receipt["image"] != run["environment"]["image"]
                    or receipt["admitted"] is not True or receipt["execution_started"] is not True or receipt["exit_code"] != 0
                    or any(receipt["compute"][key] != run["compute"][key] for key in ("reserved_cpu_seconds", "charged_cpu_seconds", "provenance"))
                    or run["compute"]["provenance"] != "estimated_upper_bound"
                    or run["compute"]["reserved_cpu_seconds"] != 30 or run["compute"]["charged_cpu_seconds"] != 30):
                continue
            prefix = "runs/" + rid + "/"
            expected = {prefix + "input/data.csv": run["input_sha256"],
                        prefix + "recipe/" + IDS[run["recipe"]][1]: run["recipe_sha256"]}
            if any(entries.get(name, {}).get("sha256") != digest for name, digest in expected.items()):
                continue
            result = prefix + "result.json"
            if entries.get(result, {}).get("sha256") != hashlib.sha256(canonical(run["metrics"]).encode()).hexdigest():
                continue
            for name in (*expected, result):
                candidates[name] = "Known artifact of a successful settled run in a closed project; human retention review only."
        except (ValueError, TypeError, KeyError):
            continue
    return candidates


def _observe(state, *, retention=False):
    from .assessment_state import refuse_state
    refuse_state(state)
    root = safe_root(state)
    with observed_offline(root) as offline:
        report, identities, captured = scan(root)
        authority, reason = _authority(report, captured, offline) if retention else (None, None)
        candidates = _candidates(authority, report["entries"]) if retention else {}
        after, after_ids, _ = scan(root)
        if after != report or after_ids != identities:
            raise InventoryError("Workspace changed during observation; no consistent inventory or retention plan was produced.")
        report.update(command="retention-plan" if retention else "storage-status", existing_service_lock_held=offline,
                      read_only=True, quota_enforced=False, deletion_authorized=False,
                      scope="Logical file bytes and all directory/file entries under this root. External backups/replays, Docker images and identity-provider volumes are outside this observation.",
                      warning="This is a bounded observation, not storage admission, available host capacity or permission to delete. Other operator writers must remain stopped.")
        if retention:
            report["authority_classification"] = "AVAILABLE" if authority else "UNAVAILABLE"
            report["protected_reason"] = reason
            report["retention_plan"] = {name: {
                "classification": "review_candidate" if name in candidates else "protected",
                "reason": candidates.get(name, "Authority, failed/unresolved/active work, directory, unknown or unclassified artifact; preserve.")}
                for name in report["entries"]}
            report["review_candidate_files"] = len(candidates)
        return report


def observe(state, *, retention=False):
    try:
        return _observe(state, retention=retention)
    except (InventoryError, OSError, ValueError) as exc:
        return {"format": FORMAT, "complete": False, "read_only": True,
                "quota_enforced": False, "deletion_authorized": False, "review_candidate_files": 0,
                "observed_bytes_lower_bound": getattr(exc, "observed_bytes_lower_bound", 0),
                "observed_entries_lower_bound": getattr(exc, "observed_entries_lower_bound", 0),
                "error": str(exc) if isinstance(exc, ValueError) else "Filesystem is unavailable or changed; no complete inventory was produced.",
                "warning": "Lower bounds are not complete usage. Preserve every artifact; no admission or deletion is authorized."}
