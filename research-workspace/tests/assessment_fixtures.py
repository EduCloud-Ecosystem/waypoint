"""Fresh AS1 construction exists only in tests; uses real scoped machinery."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import time

from research_workspace import assessment_state as a
from research_workspace import operation_guard as g
from research_workspace.store import Store, private_write


def fresh_store(root, *, subjects=("alice", "bob", "carol", "dave")):
    root = Path(root).resolve()
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    store = object.__new__(Store)
    store.root, store.profile_version, store.finite = root, "AS1", True
    store._assessment, store._assessment_service = a.AS1, None
    store._initializing, store.initial_key = True, None
    binding = {"format": a.GUARD_FORMAT, "authority_id": "6" * 32,
               "logical_authority_sha256": a.policy_digest()}
    store.guard = g.Guard(root, binding, store.write, assessment=a.AS1)
    store.guard.publication.initializing = True
    private_write(root / "config.json", a.encode({"version": 1, "mode": a.MODE,
        "operation_guard": binding, "logical_authority": a.policy()}))
    fd = os.open(root / g.LOCK, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    g.sync_directory(root)
    tokens = {subject: secrets.token_urlsafe(32) for subject in subjects}
    with store.guard.locked():
        store.guard.publish({**binding, "generation": 0, "sequence": 0, "operations": {}}, stage="bootstrap")
        token = store.guard.begin("initialize", subject="alice", request={"action": "initialize"}, bootstrap=True)
        try:
            with store.write(role="initialize") as db:
                db.executescript(";".join(a.schema()))
                db.execute("BEGIN IMMEDIATE")
                db.execute(f"PRAGMA application_id={g.APPLICATION_ID}")
                db.execute(f"PRAGMA user_version={a.MARKER}")
                db.execute("INSERT INTO operation_authority VALUES(?,0,?)", (binding["authority_id"], secrets.token_hex(16)))
                token.register(db)
                db.commit()
                token.effect()
                for subject in subjects:
                    db.execute("INSERT INTO users VALUES(?,?,1)", (subject, hashlib.sha256(subject.encode()).hexdigest()))
                    db.execute("INSERT INTO sessions VALUES(?,?,?)", (hashlib.sha256(tokens[subject].encode()).hexdigest(), subject, time.time() + 28800))
                # Known project metadata mirrors the real broker test seed;
                # no InitProject or compute grant is invoked here.
                for pid in ("2" * 32, "3" * 32):
                    project = {"id": pid, "owner": "alice", "name": "Synthetic assessment fixture",
                               "question": "Metadata ownership control", "success_criterion": "Retain uncertainty",
                               "created_at": "2026-10-19T12:00:00Z", "ends_at": "2099-01-01T00:00:00Z",
                               "budget_cpu_seconds": 1000, "status": "active"}
                    db.execute("INSERT INTO projects VALUES(?,?)", (pid, a.encode(project).decode()))
                    for subject in subjects:
                        db.execute("INSERT INTO members VALUES(?,?,?)", (pid, subject, "owner" if subject == "alice" else "editor"))
                token.complete_sql(db)
                db.commit()
        finally:
            token.release()
    store._initializing = False
    store.guard.publication.initializing = False
    store.guard.resume()
    return store, tokens


def metadata_row(subject="alice", *, identity=None, manifest=None, version=1, denied=False):
    """Envelope/Owner fixture, not a validated benchmark or admission receipt."""
    if manifest is None:
        path = Path(__file__).resolve().parents[1] / "assessment/fixtures/research-default.json"
        manifest = a.encode(json.loads(path.read_bytes())).decode("ascii")
    return {"id": identity or secrets.token_hex(16), "project_id": "2" * 32,
            "subject": subject, "request_key": secrets.token_hex(16), "manifest_json": manifest,
            "manifest_sha256": hashlib.sha256(manifest.encode()).hexdigest(), "outfitter_authority_id": "5" * 32,
            "created_at": "2026-10-19T12:00:00.000000Z", "expires_at": "2026-10-19T13:00:00Z",
            "local_phase": "denied" if denied else "pending", "deny_reason": "cancelled" if denied else "none",
            "version": version, "last_scope_generation": None, "declared_upload_ceiling_bytes": 1024,
            "admitting_owner_epoch": g.BOOT}


def insert_row(store, row):
    a.validate_row(row)
    with store.transaction(request={"action": "assessment-observation"}) as db:
        db.execute(a.INSERT, tuple(row[k] for k in a.FIELDS))
