"""Transactional local authority. This pilot deliberately refuses shared hosting."""

import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid

from .recipes import IDS, digest, parse_csv, recipe_bytes
from .operation_guard import Guard, GuardPaused, GuardBusy, APPLICATION_ID, GUARDED_MODES, SCHEMA, FILE, LOCK, TEMP, sync_directory


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def unknown_compute():
    return {"reserved_cpu_seconds": None, "charged_cpu_seconds": None,
            "provenance": "unknown", "unresolved_liability": True}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise ValueError("Use a valid opaque identifier.")
    return value


def bounded_text(value, label, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{label} must contain 1–{maximum} characters.")
    return value.strip()


def private_write(path, content):
    path = Path(path)
    with path.open("xb") as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())
    path.chmod(0o600)
    sync_directory(path.parent)


class AccessError(Exception):
    pass


class TrackedConnection:
    """Business writes are marked before SQLite sees them, including failed writes."""
    def __init__(self, db, operation, keep=False):
        self.raw, self.operation, self.keep = db, operation, keep

    def execute(self, sql, parameters=()):
        if sql.lstrip().split(None, 1)[0].upper() not in {"SELECT", "BEGIN"}:
            self.operation.effect()
        return self.raw.execute(sql, parameters)

    def commit(self):
        if not self.keep:
            self.operation.complete_sql(self.raw)
        self.raw.commit()

    def effect(self):
        self.operation.effect()


class Store:
    def __init__(self, root, create=False, *, guarded=False, config=None, subject=None, resume=True):
        self.root = Path(root).resolve()
        self.guard = None
        self.initial_key = None
        bootstrap = None
        if create:
            if self.root.exists() and any(self.root.iterdir()):
                raise ValueError("Initialization requires a fresh empty state directory.")
            if guarded and not self.root.parent.is_dir():
                raise ValueError("Create a durable parent directory before guarded initialization.")
            self.root.mkdir(parents=not guarded, exist_ok=True, mode=0o700)
            self.root.chmod(0o700)
            if guarded:
                sync_directory(self.root.parent)
            if guarded:
                if not isinstance(config, dict) or config.get("mode") not in GUARDED_MODES or subject is None:
                    raise ValueError("Guarded initialization requires an explicit fresh profile and subject.")
                identifier(subject)
                self.guard = Guard.create(self.root, self._connect)
                bootstrap = self.guard.begin("initialize", subject=subject, request=config, bootstrap=True)
            try:
                (self.root / "runs").mkdir(mode=0o700)
                with contextlib.closing(self._connect()) as db:
                    db.executescript("""
                    CREATE TABLE users(subject TEXT PRIMARY KEY, key_hash TEXT UNIQUE NOT NULL, active INTEGER NOT NULL);
                    CREATE TABLE sessions(token_hash TEXT PRIMARY KEY, subject TEXT NOT NULL, expires REAL NOT NULL);
                    CREATE TABLE projects(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                    CREATE TABLE members(project_id TEXT NOT NULL, subject TEXT NOT NULL, role TEXT NOT NULL,
                                         PRIMARY KEY(project_id,subject));
                    CREATE TABLE runs(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,subject TEXT NOT NULL,
                        status TEXT NOT NULL,payload TEXT NOT NULL,cancel_requested INTEGER NOT NULL DEFAULT 0,
                        request_key TEXT,request_hash TEXT,UNIQUE(project_id,subject,request_key));
                    CREATE INDEX queued ON runs(status);
                    """)
                    if bootstrap:
                        for statement in SCHEMA:
                            db.execute(statement)
                        db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                        db.execute("INSERT INTO operation_authority VALUES(?,0,?)", (self.guard.binding["authority_id"], uuid.uuid4().hex))
                        bootstrap.register(db)
                        db.commit()
                        bootstrap.effect()
                        config = {**config, "operation_guard": self.guard.binding}
                        private_write(self.root / "config.json", canonical(config).encode())
                        self.initial_key = secrets.token_urlsafe(32)
                        db.execute("INSERT INTO users VALUES(?,?,1)", (subject, digest(self.initial_key.encode())))
                        bootstrap.complete_sql(db)
                        db.commit()
                (self.root / "workspace.sqlite3").chmod(0o600)
                sync_directory(self.root)
            except BaseException:
                if bootstrap:
                    bootstrap.fail()
                raise
            finally:
                if bootstrap:
                    bootstrap.release()
        elif not (self.root / "workspace.sqlite3").is_file() or not (self.root / "config.json").is_file():
            raise ValueError("Workspace authority is missing. Restore it; do not reinitialize existing state.")

        if not create:
            config = json.loads((self.root / "config.json").read_bytes())
            with contextlib.closing(sqlite3.connect((self.root / "workspace.sqlite3").as_uri() + "?mode=ro", uri=True)) as probe:
                guarded_db = probe.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
                guarded_tables = probe.execute("SELECT count(*) FROM sqlite_master WHERE name IN ('operation_authority','operation_receipts')").fetchone()[0]
            evidence = guarded_db or guarded_tables or any((self.root / name).exists() for name in (FILE, LOCK)) or any(self.root.glob(TEMP + "*"))
            binding = config.get("operation_guard")
            if binding is not None or evidence or config.get("mode") in GUARDED_MODES:
                if not guarded_db or guarded_tables != 2 or binding is None or config.get("mode") not in GUARDED_MODES | {"recovery-held", "retired"}:
                    raise GuardPaused("missing_binding")
                self.guard = Guard(self.root, binding, self._connect)
                try:
                    if resume:
                        self.guard.resume()
                except Exception:
                    # Valid held stores remain available for reads and safety controls.
                    self.guard.lease.failure = "durability_unverified"

    def _connect(self):
        db = sqlite3.connect(self.root / "workspace.sqlite3", timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            return db
        except BaseException:
            db.close()
            raise

    def connect(self):
        db = self._connect()
        if self.guard:
            db.execute("PRAGMA query_only=ON")
        return db

    def operation_status(self):
        return self.guard.status() if self.guard else {"guard_enabled": False, "state": "ready", "reason": None, "quota_enforced": False}

    @contextlib.contextmanager
    def transaction(self, *, essential=False, operation=None, keep=False, kind="mutation", resource_id="workspace", subject="operator", request=None, request_key=None):
        if self.guard:
            with self.guard.locked():
                token = operation or self.guard.begin(kind, resource_id=resource_id, subject=subject, request=request,
                                                       request_key=request_key, essential=essential)
                raw = None
                failed = False
                try:
                    raw = self._connect()
                    raw.execute("BEGIN IMMEDIATE")
                    self.guard.validate_schema(raw)
                    config_path = self.root / "config.json"
                    if config_path.exists() and json.loads(config_path.read_bytes()).get("mode") in ("recovery-held", "retired"):
                        raise ValueError("This workspace is held or retired for recovery; mutations are disabled.")
                    db = TrackedConnection(raw, token, keep)
                    yield db
                    db.commit()
                except BaseException:
                    failed = True
                    # Latch uncertainty before cleanup. No exception class or change
                    # count can prove that an attempted effect did not happen.
                    if token.effects:
                        token.fail()
                    try:
                        if raw is not None:
                            raw.rollback()
                            if not token.effects and not token.borrowed:
                                raw.execute("BEGIN IMMEDIATE")
                                token.complete_sql(raw, refused=True)
                                raw.commit()
                    except BaseException:
                        token.fail()
                    raise
                finally:
                    try:
                        if raw is not None:
                            raw.close()
                    except BaseException:
                        token.fail()
                        raise
                    finally:
                        if failed or not keep:
                            token.release()
            return
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            config_path = self.root / "config.json"
            if config_path.exists() and json.loads(config_path.read_text()).get("mode") in ("recovery-held", "retired"):
                raise ValueError("This workspace is held or retired for recovery; mutations are disabled.")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def add_user(self, subject):
        identifier(subject)
        key = secrets.token_urlsafe(32)
        with self.transaction(subject=subject, request={"action": "add-user", "subject": subject}) as db:
            if db.execute("SELECT 1 FROM users WHERE subject=?", (subject,)).fetchone():
                raise ValueError("This subject already exists; existing credentials were preserved.")
            db.execute("INSERT INTO users VALUES(?,?,1)", (subject, digest(key.encode())))
        return key

    def revoke_user(self, subject):
        with self.transaction(essential=True, subject=subject, request={"action": "revoke-user", "subject": subject}) as db:
            db.execute("UPDATE users SET active=0 WHERE subject=?", (subject,))
            db.execute("DELETE FROM sessions WHERE subject=?", (subject,))
            db.execute("UPDATE runs SET cancel_requested=1 WHERE subject=? AND status IN ('queued','running')", (subject,))

    def rotate_user(self, subject):
        identifier(subject)
        key = secrets.token_urlsafe(32)
        with self.transaction(essential=True, subject=subject, request={"action": "rotate-user", "subject": subject}) as db:
            if not db.execute("SELECT 1 FROM users WHERE subject=? AND active=1", (subject,)).fetchone():
                raise ValueError("Only an existing active subject can receive a replacement key.")
            db.execute("DELETE FROM sessions WHERE subject=?", (subject,))
            db.execute("UPDATE users SET key_hash=? WHERE subject=?", (digest(key.encode()), subject))
        return key

    def session(self, key=None, token=None):
        with self.transaction(essential=True) if key is not None else contextlib.closing(self.connect()) as db:
            if key is not None:
                if not isinstance(key, str) or len(key) > 256:
                    raise AccessError("Sign in with a valid access key.")
                row = db.execute("SELECT subject FROM users WHERE key_hash=? AND active=1", (digest(key.encode()),)).fetchone()
                if row is None:
                    raise AccessError("Sign in with a valid access key.")
                token = secrets.token_urlsafe(32)
                db.execute("DELETE FROM sessions WHERE expires<=?", (time.time(),))
                db.execute("INSERT INTO sessions VALUES(?,?,?)", (digest(token.encode()), row["subject"], time.time()+28800))
                return row["subject"], token
            if not token or len(token) > 256:
                raise AccessError("Sign in to continue.")
            row = db.execute("SELECT s.subject FROM sessions s JOIN users u ON u.subject=s.subject WHERE s.token_hash=? AND s.expires>? AND u.active=1", (digest(token.encode()), time.time())).fetchone()
            if row is None:
                raise AccessError("Your session has ended. Sign in again.")
            return row["subject"]

    def logout(self, token):
        with self.transaction(essential=True, request={"action": "logout", "token_sha256": digest(token.encode())}) as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (digest(token.encode()),))

    @staticmethod
    def _access(db, project_id, subject, roles=None, active=False):
        row = db.execute("SELECT p.payload,m.role FROM projects p JOIN members m ON m.project_id=p.id JOIN users u ON u.subject=m.subject WHERE p.id=? AND m.subject=? AND u.active=1", (project_id, subject)).fetchone()
        if row is None or (roles and row["role"] not in roles):
            raise AccessError("You do not have access to this project or action.")
        project = json.loads(row["payload"])
        if active and (project["status"] != "active" or dt.datetime.fromisoformat(project["ends_at"]) <= dt.datetime.now(dt.timezone.utc)):
            raise ValueError("This project is closed or its run period has ended.")
        return project, row["role"]

    def projects(self, subject):
        with contextlib.closing(self.connect()) as db:
            if not db.execute("SELECT 1 FROM users WHERE subject=? AND active=1", (subject,)).fetchone():
                raise AccessError("This identity is no longer active.")
            return [json.loads(row[0]) for row in db.execute("SELECT p.payload FROM projects p JOIN members m ON m.project_id=p.id JOIN users u ON u.subject=m.subject WHERE m.subject=? AND u.active=1 ORDER BY p.rowid DESC", (subject,))]

    def create_project(self, subject, data, broker, global_budget, request_key=None):
        name = bounded_text(data.get("name"), "Project name", 100)
        question = bounded_text(data.get("question"), "Research question", 2000)
        criterion = bounded_text(data.get("success_criterion"), "Success criterion", 2000)
        end = dt.datetime.fromisoformat(str(data.get("ends_at", "")).replace("Z", "+00:00"))
        if end.tzinfo is None or not dt.datetime.now(dt.timezone.utc) < end <= dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=366):
            raise ValueError("Choose an end date within the next year, with a timezone.")
        budget = data.get("budget_cpu_seconds")
        if type(budget) not in (float, int) or not 30 <= budget <= 300:
            raise ValueError("This pilot supports project budgets from 30 to 300 CPU-seconds.")
        project = dict(id=uuid.uuid4().hex, name=name, question=question, success_criterion=criterion,
                       owner=subject, created_at=now(), ends_at=end.astimezone(dt.timezone.utc).isoformat(),
                       budget_cpu_seconds=budget, status="active")
        if self.guard:
            return self._guarded_project(subject, data, project, broker, global_budget, request_key)
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM users WHERE subject=? AND active=1", (subject,)).fetchone():
                raise AccessError("This identity is no longer active.")
            committed = sum(json.loads(row[0])["budget_cpu_seconds"] for row in db.execute("SELECT payload FROM projects"))
            if committed+budget > global_budget:
                raise ValueError("The workspace's finite project allocation is exhausted. Ask the operator.")
            broker.init_project(project["id"], budget)
            db.execute("INSERT INTO projects VALUES(?,?)", (project["id"], canonical(project)))
            db.execute("INSERT INTO members VALUES(?,?,?)", (project["id"], subject, "owner"))
        return project

    def _guarded_project(self, subject, data, project, broker, global_budget, request_key):
        if request_key is None:
            raise ValueError("A stable request ID is required for guarded project creation.")
        identifier(request_key)
        with self.guard.locked():
            previous = self.guard.lookup("project", subject, request_key)
            if previous:
                oid, intent = previous
                if intent["request_hash"] != digest(canonical(data).encode()):
                    raise ValueError("This request ID was already used for different inputs.")
                proof = self.guard.proof(self.guard.load())
                if proof[oid][2] == "committed" and intent["phase"] != "uncertain":
                    with contextlib.closing(self.connect()) as db:
                        self._access(db, intent["resource_id"], subject)
                        return json.loads(db.execute("SELECT payload FROM projects WHERE id=?", (intent["resource_id"],)).fetchone()[0])
                raise GuardPaused("existing_intent")
            token = self.guard.begin("project", resource_id=project["id"], subject=subject, request=data, request_key=request_key)
            try:
                with self.transaction(operation=token, keep=True) as db:
                    if not db.execute("SELECT 1 FROM users WHERE subject=? AND active=1", (subject,)).fetchone():
                        raise AccessError("This identity is no longer active.")
                    committed = sum(json.loads(row[0])["budget_cpu_seconds"] for row in db.execute("SELECT payload FROM projects"))
                    if committed + project["budget_cpu_seconds"] > global_budget:
                        raise ValueError("The workspace's finite project allocation is exhausted. Ask the operator.")
            except BaseException:
                token.release()
                raise
        try:
            # Only the actual spawn is fenced. No guard or DB lock during wait.
            broker.init_project(project["id"], project["budget_cpu_seconds"], launch=token.launch)
            with self.transaction(operation=token) as db:
                if not db.execute("SELECT 1 FROM users WHERE subject=? AND active=1", (subject,)).fetchone():
                    raise AccessError("This identity is no longer active; project initialization requires reconciliation.")
                db.execute("INSERT INTO projects VALUES(?,?)", (project["id"], canonical(project)))
                db.execute("INSERT INTO members VALUES(?,?,?)", (project["id"], subject, "owner"))
            return project
        except BaseException:
            token.fail()
            raise
        finally:
            token.release()

    def project(self, project_id, subject):
        with contextlib.closing(self.connect()) as db:
            project, role = self._access(db, project_id, subject)
            members = [dict(row) for row in db.execute("SELECT subject,role FROM members WHERE project_id=? ORDER BY subject", (project_id,))]
            runs = [json.loads(row[0]) for row in db.execute("SELECT payload FROM runs WHERE project_id=? ORDER BY rowid DESC", (project_id,))]
            return dict(project=project, role=role, members=members, runs=runs)

    def membership(self, project_id, actor, subject, role=None):
        identifier(subject)
        with self.transaction(essential=role in (None, "viewer"), resource_id=project_id, subject=actor, request={"action": "membership", "subject": subject, "role": role}) as db:
            project, _ = self._access(db, project_id, actor, {"owner"})
            if subject == project["owner"]:
                raise ValueError("The project owner cannot be removed or demoted.")
            if role is None:
                db.execute("DELETE FROM members WHERE project_id=? AND subject=?", (project_id, subject))
                db.execute("UPDATE runs SET cancel_requested=1 WHERE project_id=? AND subject=? AND status IN ('queued','running')", (project_id, subject))
            else:
                if role not in ("editor", "viewer"):
                    raise ValueError("Choose editor or viewer.")
                if not db.execute("SELECT 1 FROM users WHERE subject=? AND active=1", (subject,)).fetchone():
                    raise ValueError("The operator must provision that collaborator first.")
                db.execute("INSERT INTO members VALUES(?,?,?) ON CONFLICT(project_id,subject) DO UPDATE SET role=excluded.role", (project_id, subject, role))
                if role == "viewer":
                    db.execute("UPDATE runs SET cancel_requested=1 WHERE project_id=? AND subject=? AND status IN ('queued','running')", (project_id, subject))

    def close_project(self, project_id, subject):
        with self.transaction(essential=True, resource_id=project_id, subject=subject, request={"action": "close-project"}) as db:
            project, _ = self._access(db, project_id, subject, {"owner"})
            project["status"] = "closed"
            db.execute("UPDATE projects SET payload=? WHERE id=?", (canonical(project), project_id))
            db.execute("UPDATE runs SET cancel_requested=1 WHERE project_id=? AND status IN ('queued','running')", (project_id,))
        return project

    def submit(self, project_id, subject, data, images, request_key=None, authorize=None):
        if self.guard:
            with self.guard.locked():
                return self._submit(project_id, subject, data, images, request_key, authorize)
        return self._submit(project_id, subject, data, images, request_key, authorize)

    def _submit(self, project_id, subject, data, images, request_key=None, authorize=None):
        recipe = data.get("recipe")
        if recipe not in IDS or not images.get(IDS[recipe][0]):
            raise ValueError("That recipe's pinned runtime is unavailable.")
        condition = data.get("condition")
        if condition not in ("baseline", "alternative") or data.get("parameters", {}) != {}:
            raise ValueError("Choose a reviewed condition; arbitrary parameters are disabled.")
        content = data.get("csv")
        parse_csv(content)
        request_hash = digest(canonical({"recipe": recipe, "condition": condition, "csv": content}).encode())
        if request_key:
            identifier(request_key)
        if self.guard and request_key:
            with contextlib.closing(self.connect()) as db:
                self._access(db, project_id, subject, {"owner", "editor"}, active=True)
                previous = db.execute("SELECT request_hash,payload FROM runs WHERE project_id=? AND subject=? AND request_key=?", (project_id, subject, request_key)).fetchone()
                if previous:
                    if previous["request_hash"] != request_hash:
                        raise ValueError("This request ID was already used for different inputs.")
                    return json.loads(previous["payload"])
        rid = uuid.uuid4().hex
        runtime, filename = IDS[recipe]
        source = recipe_bytes(recipe)
        from .catalog import resolve_environment
        environment = resolve_environment(recipe, digest(source), images[runtime])
        if environment["status"] != "active":
            raise ValueError("This environment is unavailable for new runs.")
        run = dict(id=rid, project_id=project_id, recipe=recipe, condition=condition, status="queued",
                   created_at=now(), started_at=None, finished_at=None, input_sha256=digest(content.encode()),
                   recipe_sha256=digest(source), environment={"image": images[runtime], "runtime": runtime,
                   "runner": "outfitter-docker-v1", "specification": environment}, parameters={}, metrics={}, error="", cleanup_verified=False,
                   compute={"reserved_cpu_seconds": 0, "charged_cpu_seconds": 0, "provenance": "not_admitted", "unresolved_liability": False})
        with (self.transaction(kind="submit", resource_id=rid, subject=subject, request={"project_id": project_id, "request_hash": request_hash}) if self.guard else self.transaction()) as db:
            self._access(db, project_id, subject, {"owner", "editor"}, active=True)
            if request_key:
                previous = db.execute("SELECT request_hash,payload FROM runs WHERE project_id=? AND subject=? AND request_key=?", (project_id, subject, request_key)).fetchone()
                if previous:
                    if previous["request_hash"] != request_hash:
                        raise ValueError("This request ID was already used for different inputs.")
                    return json.loads(previous["payload"])
            if db.execute("SELECT count(*) FROM runs WHERE project_id=?", (project_id,)).fetchone()[0] >= 100:
                raise ValueError("This pilot supports at most 100 recorded runs per project.")
            if db.execute("SELECT count(*) FROM runs WHERE status IN ('queued','running')").fetchone()[0] >= 20:
                raise ValueError("The bounded queue is full. Wait for an existing run.")
            directory = self.root / "runs" / rid
            if self.guard:
                db.effect()
            directory.mkdir(mode=0o700)
            for folder, file, payload in (("input", "data.csv", content.encode()), ("recipe", filename, source)):
                (directory / folder).mkdir(mode=0o755)
                private_write(directory / folder / file, payload)
                (directory / folder / file).chmod(0o444)
            if self.guard:
                sync_directory(directory)
                sync_directory(directory.parent)
            db.execute("INSERT INTO runs(id,project_id,subject,status,payload,request_key,request_hash) VALUES(?,?,?,?,?,?,?)", (rid, project_id, subject, "queued", canonical(run), request_key, request_hash))
            if authorize is not None:
                authorize(rid)
        return run

    def run(self, project_id, run_id, subject):
        with contextlib.closing(self.connect()) as db:
            self._access(db, project_id, subject)
            row = db.execute("SELECT payload FROM runs WHERE id=? AND project_id=?", (run_id, project_id)).fetchone()
            if row is None:
                raise AccessError("That run is unavailable.")
            return json.loads(row[0])

    def cancel(self, project_id, run_id, subject):
        with self.transaction(essential=True, resource_id=run_id, subject=subject, request={"action": "cancel", "project_id": project_id}) as db:
            self._access(db, project_id, subject, {"owner", "editor"})
            row = db.execute("SELECT payload FROM runs WHERE id=? AND project_id=?", (run_id, project_id)).fetchone()
            if row is None:
                raise AccessError("That run is unavailable.")
            db.execute("UPDATE runs SET cancel_requested=1 WHERE id=?", (run_id,))
        return json.loads(row[0])

    def recover(self, *, cancel_queued=False):
        with self.transaction(essential=True, request={"action": "restart", "cancel_queued": cancel_queued}) as db:
            if cancel_queued:
                for row in db.execute("SELECT id,payload FROM runs WHERE status='queued'").fetchall():
                    run = json.loads(row["payload"])
                    run.update(status="cancelled", finished_at=now(), cleanup_verified=True,
                               error="Transient identity authorization ended at restart. Submit a new run after signing in; this run was not replayed.")
                    db.execute("UPDATE runs SET status='cancelled',payload=? WHERE id=?", (canonical(run), row["id"]))
            for row in db.execute("SELECT id,payload FROM runs WHERE status='running'").fetchall():
                run = json.loads(row["payload"])
                run.update(status="interrupted", finished_at=now(), error="The service restarted during execution. Operator reconciliation is required; this run was not retried.", cleanup_verified=False, compute=unknown_compute())
                db.execute("UPDATE runs SET status='interrupted',payload=? WHERE id=?", (canonical(run), row["id"]))

    def claim(self, on_cancel=None):
        if self.guard:
            with self.guard.locked():
                self.guard.check(kind="claim")
                with contextlib.closing(self.connect()) as db:
                    row = db.execute("SELECT id,subject FROM runs WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
                if row is None:
                    return None
                token = self.guard.begin("run", resource_id=row["id"], subject=row["subject"], request={"run_id": row["id"]})
                return self._claim(on_cancel, token)
        return self._claim(on_cancel)

    def _claim(self, on_cancel=None, operation=None):
        with self.transaction(operation=operation, keep=operation is not None) as db:
            for row in db.execute("SELECT * FROM runs WHERE status='queued' ORDER BY rowid").fetchall():
                run = json.loads(row["payload"])
                try:
                    self._access(db, row["project_id"], row["subject"], {"owner", "editor"}, active=True)
                    allowed = not row["cancel_requested"]
                except (AccessError, ValueError):
                    allowed = False
                if not allowed:
                    run.update(status="cancelled", finished_at=now(), cleanup_verified=True,
                               error="Cancelled before dispatch; access or the project run period ended.")
                    db.execute("UPDATE runs SET status='cancelled',payload=? WHERE id=?", (canonical(run), row["id"]))
                    if on_cancel is not None:
                        on_cancel(row["id"])
                    if operation is not None:
                        operation.complete_sql(db.raw)
                        db.raw.commit()
                        operation.release()
                        return None
                    continue
                run.update(status="running", started_at=now(), compute=unknown_compute())
                db.execute("UPDATE runs SET status='running',payload=? WHERE id=?", (canonical(run), row["id"]))
                return run
        return None

    def should_cancel(self, run_id):
        with contextlib.closing(self.connect()) as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None or row["cancel_requested"]:
                return True
            try:
                self._access(db, row["project_id"], row["subject"], {"owner", "editor"}, active=True)
                return False
            except (AccessError, ValueError):
                return True

    def finish(self, run, result=None, publication=None):
        try:
            return self._finish(run, result, publication)
        except BaseException:
            if self.guard:
                token = self.guard.running(run["id"])
                if token:
                    token.fail()
            raise

    def _finish(self, run, result=None, publication=None):
        with self.transaction(essential=True, kind="settle", resource_id=run["id"], request=run) as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run["id"],)).fetchone()
            try:
                self._access(db, row["project_id"], row["subject"], {"owner", "editor"}, active=True)
                allowed = not row["cancel_requested"]
            except (AccessError, ValueError):
                allowed = False
            with publication if publication is not None else contextlib.nullcontext(True) as identity_allowed:
                if (not allowed or not identity_allowed) and run["status"] == "succeeded":
                    run.update(status="cancelled", metrics={}, error="Access, identity or the project run period ended before result publication.")
                    result = None
                run["finished_at"] = now()
                payload = canonical(run)
                result_payload = canonical(result).encode() if result is not None else None
                if len(payload.encode()) > 262144 or (result_payload is not None and len(result_payload) > 65536):
                    raise ValueError("The final run record exceeds its bounded publication allowance.")
                if result_payload is not None:
                    if self.guard:
                        db.effect()
                    private_write(self.root / "runs" / run["id"] / "result.json", result_payload)
                db.execute("UPDATE runs SET status=?,payload=? WHERE id=?", (run["status"], payload, run["id"]))
                run_token = self.guard.running(run["id"]) if self.guard else None
                compute = run.get("compute", {})
                accounting = (compute.get("reserved_cpu_seconds"), compute.get("charged_cpu_seconds"), compute.get("provenance"))
                settled = (run.get("status") in {"succeeded", "failed", "cancelled"}
                           and run.get("cleanup_verified") is True and compute.get("unresolved_liability") is False
                           and all(type(value) is int for value in accounting[:2])
                           and accounting in {(0, 0, "not_admitted"), (30, 30, "estimated_upper_bound")})
                if run_token and settled:
                    run_token.complete_sql(db.raw)
                elif run_token:
                    run_token.fail("unresolved_compute")
                # Commit visibility before releasing the local identity guard.
                # The transaction context's subsequent commit has no changes.
                db.commit()
                if run_token:
                    run_token.release()

    def snapshot(self, project_id, subject):
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN")
            project, _ = self._access(db, project_id, subject)
            runs = [json.loads(row[0]) for row in db.execute("SELECT payload FROM runs WHERE project_id=? ORDER BY rowid", (project_id,))]
            files = {}
            for run in runs:
                directory = self.root / "runs" / run["id"]
                filename = IDS[run["recipe"]][1]
                files[f"runs/{run['id']}/input.csv"] = (directory / "input/data.csv").read_bytes()
                files[f"runs/{run['id']}/{filename}"] = (directory / "recipe" / filename).read_bytes()
                if run["status"] == "succeeded":
                    files[f"runs/{run['id']}/result.json"] = (directory / "result.json").read_bytes()
            return project, runs, files
