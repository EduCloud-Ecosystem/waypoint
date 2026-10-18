"""Private, offline checkpoints and one-way local authority replacement.

This is deliberately separate from public evidence. A restored copy cannot serve
until the current source has been checked and fenced. Loss of that source leaves
the replacement held; this is not off-host disaster recovery authorization.
"""

from contextlib import contextmanager, closing, ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import tempfile

from .runtime import Broker, strict_json
from .store import Store, canonical, now, private_write, unknown_compute

FORMAT = "educloud-private-checkpoint-v1"
MAX_FILES = 10000
MAX_FILE = 64 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
TABLES = ("users", "sessions", "projects", "members", "runs")


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


def atomic_json(path, value):
    """Durable replacement; interrupted writes never expose partial JSON."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".recovery-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(canonical(value).encode())
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def safe_root(value):
    path = Path(value).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Recovery paths must not traverse symbolic links.")
    return path.resolve()


def read_file(path, maximum=MAX_FILE):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
            raise ValueError("Recovery requires bounded regular files without links.")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = None
            payload = source.read(maximum + 1)
        if len(payload) != info.st_size:
            raise ValueError("Recovery file changed size during reading.")
        return payload
    finally:
        if descriptor is not None:
            os.close(descriptor)


def inventory(root):
    """Bounded, link-free directory inventory; no archive extraction."""
    result, size = {}, 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            if not stat.S_ISDIR((Path(directory) / name).lstat().st_mode):
                raise ValueError("Recovery refuses linked or special directories.")
        for name in files:
            path = Path(directory) / name
            content = read_file(path)
            size += len(content)
            if len(result) >= MAX_FILES or size > MAX_TOTAL:
                raise ValueError("Checkpoint exceeds the bounded local recovery capacity.")
            result[path.relative_to(root).as_posix()] = {"bytes": len(content), "sha256": sha(content)}
    return result


@contextmanager
def offline(root):
    root = safe_root(root)
    read_file(root / "config.json", 65536)
    lockpath = root / "server.lock"
    if lockpath.exists() or lockpath.is_symlink():
        read_file(lockpath, 1024)
    with lockpath.open("a") as lock:
        os.chmod(lockpath, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Stop this workspace service before recovery operations.") from exc
        yield root


@contextmanager
def frozen_db(root):
    read_file(root / "workspace.sqlite3")
    with closing(Store(root).connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        finally:
            db.rollback()


def database_digest(db):
    schema = [tuple(row) for row in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]
    if any(row[0] not in ("table", "index") for row in schema) or {row[1] for row in schema if row[0] == "table"} != set(TABLES):
        raise ValueError("Unsupported workspace database schema for recovery.")
    if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise ValueError("The workspace database failed its integrity check.")
    digest = hashlib.sha256(canonical(schema).encode())
    for table in TABLES:
        digest.update(table.encode())
        for row in db.execute(f"SELECT rowid,* FROM {table} ORDER BY rowid"):
            digest.update(canonical(tuple(row)).encode())
    return digest.hexdigest()


def fingerprint(root, db, config=None):
    config = read_file(root / "config.json", 65536) if config is None else config
    return sha(canonical({"database": database_digest(db), "config": sha(config),
                          "runs": inventory(root / "runs")}).encode())


def fresh_directory(value):
    path = safe_root(value)
    path.mkdir(mode=0o700, parents=False, exist_ok=False)
    return path


def copy_files(source, destination, files):
    for name, expected in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        content = read_file(source / name)
        if len(content) != expected["bytes"] or sha(content) != expected["sha256"]:
            raise ValueError("A checkpoint file changed during copying.")
        private_write(path, content)


def checkpoint(state, output, *, broker=None):
    with offline(state) as root, frozen_db(root) as db:
        config_bytes = read_file(root / "config.json", 65536)
        config = strict_json(config_bytes)
        if config.get("mode") not in {"local-public-data", "local-synthetic-oidc"}:
            raise ValueError("Only an active source can create a new recovery point.")
        if safe_root(output).is_relative_to(root):
            raise ValueError("Keep checkpoints outside the source authority directory.")
        target = fresh_directory(output)
        before = fingerprint(root, db)
        compute = Broker(broker or config["broker"], root / "broker")
        broker_report = compute.call("checkpoint", "--output", str(target / "broker"))
        # A second read connection sees the stable committed database while the
        # first connection excludes every writer, including operator commands.
        with closing(sqlite3.connect(root / "workspace.sqlite3")) as reader, closing(sqlite3.connect(target / "workspace.sqlite3")) as backup:
            reader.backup(backup)
        (target / "workspace.sqlite3").chmod(0o600)
        private_write(target / "config.json", config_bytes)
        (target / "runs").mkdir(mode=0o700)
        copy_files(root / "runs", target / "runs", inventory(root / "runs"))
        if before != fingerprint(root, db):
            raise ValueError("Workspace files changed during the recovery point.")
        manifest = {"format": FORMAT, "created_at": now(), "source_state": str(root),
                    "source_fingerprint": before, "broker": broker_report, "files": inventory(target)}
        payload = canonical(manifest).encode()
        private_write(target / "manifest.json", payload)
        return {"format": FORMAT, "checkpoint_sha256": sha(payload), "created_at": manifest["created_at"],
                "files": len(manifest["files"]), "broker": broker_report,
                "warning": "Private backup contains credentials and research authority. Retain its digest separately."}


def verified_checkpoint(checkpoint_path, expected):
    if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise ValueError("Supply the separately retained checkpoint SHA-256 digest.")
    root = safe_root(checkpoint_path)
    payload = read_file(root / "manifest.json", 4 * 1024 * 1024)
    if sha(payload) != expected:
        raise ValueError("The checkpoint does not match the trusted digest.")
    manifest = strict_json(payload)
    if not isinstance(manifest, dict) or set(manifest) != {"format", "created_at", "source_state", "source_fingerprint", "broker", "files"} or manifest["format"] != FORMAT:
        raise ValueError("Unsupported private checkpoint format.")
    actual = inventory(root)
    actual.pop("manifest.json")
    if actual != manifest["files"] or not {"config.json", "workspace.sqlite3"} <= set(actual):
        raise ValueError("Checkpoint content is missing, replaced or unlisted.")
    return root, manifest


def restore(checkpoint_path, output, *, trusted_sha256, broker):
    source, manifest = verified_checkpoint(checkpoint_path, trusted_sha256)
    destination = safe_root(output)
    if destination.is_relative_to(source) or destination.is_relative_to(safe_root(manifest["source_state"])):
        raise ValueError("Restore into a separate destination outside the checkpoint and original source.")
    Broker(broker, Path(output) / "broker")  # Validate the executable before copying.
    root = fresh_directory(output)
    files = {name: value for name, value in manifest["files"].items() if name == "workspace.sqlite3" or name.startswith("runs/")}
    copy_files(source, root, files)
    (root / "runs").mkdir(exist_ok=True, mode=0o700)
    config = strict_json(read_file(source / "config.json", 65536))
    config.update(mode="recovery-held", broker=str(Path(broker).resolve()))
    private_write(root / "config.json", canonical(config).encode())
    with closing(Store(root).connect()) as db:
        database_digest(db)
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM sessions")
        for row in db.execute("SELECT subject FROM users").fetchall():
            db.execute("UPDATE users SET key_hash=? WHERE subject=?", (sha(secrets.token_bytes(32)), row["subject"]))
        for row in db.execute("SELECT id,payload,status FROM runs WHERE status IN ('queued','running')").fetchall():
            run = strict_json(row["payload"])
            queued = row["status"] == "queued"
            run.update(status="cancelled" if queued else "interrupted", finished_at=now(),
                       error="Restored without replay. Queued work was cancelled." if queued else "Restored unfinished work requires operator reconciliation; no retry was issued.",
                       cleanup_verified=queued, compute=run["compute"] if queued else unknown_compute())
            db.execute("UPDATE runs SET status=?,payload=?,cancel_requested=1 WHERE id=?", (run["status"], canonical(run), row["id"]))
        db.commit()
    compute = Broker(broker, root / "broker")
    report = compute.call("restore", "--checkpoint", str(source / "broker"), "--checkpoint-sha256", manifest["broker"]["checkpoint_sha256"])
    if report.get("admission") != "held":
        raise ValueError("The restored compute authority did not remain held.")
    with frozen_db(root) as db:
        restored_fingerprint = fingerprint(root, db)
    atomic_json(root / "recovery.json", {"format": FORMAT, "phase": "held", "checkpoint_sha256": trusted_sha256,
                "source_state": manifest["source_state"], "source_fingerprint": manifest["source_fingerprint"],
                "restored_fingerprint": restored_fingerprint, "restored_at": now()})
    return recovery_status(root)


def recovery_status(state):
    root = safe_root(state)
    config = strict_json(read_file(root / "config.json", 65536))
    recovery = strict_json(read_file(root / "recovery.json", 65536)) if (root / "recovery.json").exists() else None
    with closing(Store(root).connect()) as db:
        runs = [strict_json(row[0]) for row in db.execute("SELECT payload FROM runs ORDER BY rowid")]
    compute = Broker(config["broker"], root / "broker").call("recovery-status")
    return {"format": FORMAT, "mode": config["mode"], "recovery": recovery,
            "run_counts": {status: sum(run["status"] == status for run in runs) for status in ("queued", "running", "succeeded", "failed", "cancelled", "interrupted")},
            "unresolved_runs": [{"run_id": run["id"], "status": run["status"], "compute": run["compute"]} for run in runs if run["compute"]["unresolved_liability"]],
            "broker": compute, "automatic_replay": False,
            "warning": "Historical unknown run records remain visible even after separate broker reconciliation."}


def activate(state, source_state, checkpoint_path, *, trusted_sha256):
    archive, manifest = verified_checkpoint(checkpoint_path, trusted_sha256)
    destination, source = safe_root(state), safe_root(source_state)
    if destination == source or str(source) != manifest["source_state"]:
        raise ValueError("Activation requires the original source and a distinct replacement.")
    with ExitStack() as stack:
        for path in sorted((destination, source)):
            stack.enter_context(offline(path))
        source_db = stack.enter_context(frozen_db(source))
        dest_db = stack.enter_context(frozen_db(destination))
        record = strict_json(read_file(destination / "recovery.json", 65536))
        if record.get("checkpoint_sha256") != trusted_sha256 or record.get("source_state") != str(source):
            raise ValueError("This destination belongs to a different recovery point.")
        dest_config = strict_json(read_file(destination / "config.json", 65536))
        source_config = strict_json(read_file(source / "config.json", 65536))
        active_mode = strict_json(read_file(archive / "config.json", 65536))["mode"]
        if active_mode not in {"local-public-data", "local-synthetic-oidc"}:
            raise ValueError("Unsupported checkpoint identity mode.")
        retired = {"destination": str(destination), "checkpoint_sha256": trusted_sha256}
        compute = Broker(dest_config["broker"], destination / "broker")
        if record.get("phase") == "active" and dest_config.get("mode") == active_mode and source_config.get("recovery_retired") == retired:
            result = compute.call("activate", "--source-state-dir", str(source / "broker"),
                                  "--checkpoint", str(archive / "broker"),
                                  "--checkpoint-sha256", manifest["broker"]["checkpoint_sha256"])
            if result.get("admission") != "active":
                raise ValueError("The provider authority no longer confirms this replacement.")
            return {"activated": True, "already_active": True, "automatic_replay": False}
        if dest_config.get("mode") != "recovery-held" or fingerprint(destination, dest_db) != record["restored_fingerprint"]:
            raise ValueError("The held replacement changed; create a fresh restore.")
        if source_config.get("mode") == "retired":
            if source_config.get("recovery_retired") != retired:
                raise ValueError("The source is retired to another destination.")
        elif source_config.get("mode") != active_mode:
            raise ValueError("The source is not eligible for local replacement.")
        original_config = read_file(archive / "config.json", 65536)
        if fingerprint(source, source_db, original_config if source_config.get("mode") == "retired" else None) != manifest["source_fingerprint"]:
            raise ValueError("The source changed after backup. Reconcile there and take a fresh checkpoint.")
        source_broker = Broker(dest_config["broker"], source / "broker").call("recovery-status")
        destination_broker = compute.call("recovery-status")
        if source_broker.get("unresolved") or destination_broker.get("unresolved"):
            raise ValueError("Unfinished provider liabilities require source reconciliation and a fresh checkpoint.")
        if source_broker.get("admission") == "active" and source_broker.get("authority_sha256") != manifest["broker"]["authority_sha256"]:
            raise ValueError("The source compute authority changed after backup.")
        # Seal access first. A crash or refused provider cutover leaves both
        # applications stopped. Retrying this same pair is the only auto path.
        source_config.update(mode="retired", recovery_retired=retired)
        atomic_json(source / "config.json", source_config)
        result = compute.call("activate", "--source-state-dir", str(source / "broker"),
                              "--checkpoint", str(archive / "broker"),
                              "--checkpoint-sha256", manifest["broker"]["checkpoint_sha256"])
        if result.get("admission") != "active":
            raise ValueError("Compute replacement did not establish active authority; both services remain stopped.")
        dest_config["mode"] = active_mode
        record.update(phase="active", activated_at=now())
        # The active marker precedes service admission; recovery retries can
        # still complete safely if the process dies between these writes.
        atomic_json(destination / "recovery.json", record)
        atomic_json(destination / "config.json", dest_config)
        return {"activated": True, "automatic_replay": False, "credentials": ("Sign in again with the configured identity provider; transient authorization was discarded." if active_mode == "local-synthetic-oidc" else "Issue fresh keys for active subjects with user-rotate."), "broker": result}
