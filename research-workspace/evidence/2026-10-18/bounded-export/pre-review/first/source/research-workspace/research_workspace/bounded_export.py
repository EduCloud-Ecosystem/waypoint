"""Fresh-profile export: scoped readers, verified copied bytes, final authority.

No checkpoint, operation intent, accounting write, scratch file or retry. The
final comparison establishes current equality, not an intervening-change log.
"""
from contextlib import closing
import hashlib
import math
import os
from pathlib import PurePosixPath
import stat
import time

from . import evidence, finite_authority as finite
from .recipes import IDS
from .store import AccessError, identifier


class ExportChanged(ValueError):
    pass


def _rows(db, table, where, parameters, fields, limit):
    """Check types/encoded lengths in SQL before materializing bounded rows.

    Names, predicates and field specifications are internal constants only.
    Both selects use the caller's same read snapshot.
    """
    checks = []
    for name, kind, maximum, nullable in fields:
        check = f"typeof({name}) IN ({kind})"
        if maximum is not None:
            check += f" AND length(CAST({name} AS BLOB)) <= {maximum}"
        checks.append(f"({check}" + (f" OR {name} IS NULL)" if nullable else ")"))
    suffix = f" FROM {table} WHERE {where} ORDER BY rowid LIMIT {limit + 1}"
    sizes = db.execute("SELECT " + " AND ".join(checks) + suffix, parameters).fetchall()
    if len(sizes) > limit or any(row[0] != 1 for row in sizes):
        raise ValueError("The export manifest exceeds its supported bounds.")
    return [tuple(row) for row in db.execute("SELECT " + ",".join(f[0] for f in fields) + suffix, parameters)]


def _text(name, maximum, nullable=False):
    return name, "'text'", maximum, nullable


def _integer(name):
    return name, "'integer'", None, False


def _capture(store, project_id, subject, token):
    if not isinstance(token, str) or not token or len(token) > 256:
        raise AccessError("A current local session is required for export.")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with store.guard.locked():
        with closing(store.connect()) as db:
            db.execute("BEGIN")
            sessions = _rows(db, "sessions", "token_hash=?", (token_hash,),
                             [_text("token_hash", 64), _text("subject", 80),
                              ("expires", "'integer','real'", None, False)], 1)
            users = _rows(db, "users", "subject=?", (subject,),
                          [_text("subject", 80), _text("key_hash", 64), _integer("active")], 1)
            members = _rows(db, "members", "project_id=? AND subject=?", (project_id, subject),
                            [_text("project_id", 80), _text("subject", 80), _text("role", 8)], 1)
            if (not sessions or sessions[0][1] != subject or not math.isfinite(sessions[0][2])
                    or not users or users[0][2] != 1 or not members
                    or members[0][2] not in {"owner", "editor", "viewer"}):
                raise AccessError("Current project and session access is required for export.")
            projects = _rows(db, "projects", "id=?", (project_id,),
                             [_text("id", 80), _text("payload", finite.ENCODED_LIMITS["project"])], 1)
            if not projects:
                raise AccessError("Current project access is required for export.")
            runs = _rows(db, "runs", "project_id=?", (project_id,),
                         [_text("id", 80), _text("project_id", 80), _text("subject", 80),
                          _text("status", 32), _text("payload", finite.ENCODED_LIMITS["run"]),
                          _integer("cancel_requested"), _text("request_key", 80, True),
                          _text("request_hash", 64, True)], evidence.MAX_RUNS)
            # Sample expiry after materializing the bounded manifest. The final
            # invocation's successful equality check is the publication point.
            if sessions[0][2] <= time.time():
                raise AccessError("Your session ended before export authorization.")
            return (tuple(sorted(store.guard.binding.items())), projects, runs, members, sessions, users)


def _read_artifact(root, relative, maximum):
    """Bound every read, reject symlinks at every component and close all FDs."""
    parts = PurePosixPath(relative).parts
    if not parts or any(p in {"", ".", "..", "/"} for p in parts):
        raise ValueError("Unsupported export artifact path.")
    descriptors = []
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(directory)
        for part in parts[:-1]:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            descriptors.append(directory)
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise ValueError("Unsupported or oversized export artifact.")
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
        if (len(content) != before.st_size or len(content) > maximum
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError("An export artifact changed during its read.")
        return content
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _build(store, manifest):
    _, projects, rows, _, _, _ = manifest
    project = finite.strict(projects[0][1])
    if project.get("id") != projects[0][0]:
        raise ValueError("Project export identity differs.")
    runs = []
    files = {}
    total = 0
    for row in rows:
        run = finite.strict(row[4])
        if (run.get("id"), run.get("project_id"), run.get("status")) != (row[0], row[1], row[3]):
            raise ValueError("Run export identity differs.")
        identifier(run["id"])
        if run["recipe"] not in IDS:
            raise ValueError("Unsupported export recipe.")
        prefix = f"runs/{run['id']}/"
        recipe = IDS[run["recipe"]][1]
        selected = [("input.csv", "input/data.csv", 65536), (recipe, "recipe/" + recipe, 65536)]
        if run["status"] == "succeeded":
            selected.append(("result.json", "result.json", 65536))
        for public, private, maximum in selected:
            content = _read_artifact(store.root, prefix + private, min(maximum, evidence.MAX_TOTAL_BYTES - total))
            total += len(content)
            files[prefix + public] = content
        runs.append(run)
    # Existing validation checks hashes, strict result JSON/canonical metrics,
    # lineage, public fields and all serialized member/archive limits.
    return evidence.build_bundle(project, runs, files)


def export_bundle(store, project_id, subject, token):
    identifier(project_id)
    identifier(subject)
    try:
        manifest = _capture(store, project_id, subject, token)
        payload = _build(store, manifest)
        # Keep the guard across the equality comparison, not only its SELECTs.
        with store.guard.locked():
            current = _capture(store, project_id, subject, token)
            if current != manifest:
                raise ExportChanged("Export state changed. Request a new export.")
            if current[4][0][2] <= time.time():
                raise AccessError("Your session ended before export authorization.")
        return payload
    except (AccessError, ExportChanged):
        raise
    except Exception:
        # Never return raw file paths, credentials or private comparison data.
        raise ValueError("Export could not be verified; existing records were preserved.") from None
