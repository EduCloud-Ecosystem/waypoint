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
from . import finite_authority as finite
from . import assessment_state as astate
from . import assessment_execution_state as ex2
from . import sqlite_ownership as owned


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


class DispatchSuperseded(Exception):
    """The claimed run is no longer current; do not overwrite its settlement."""


class TrackedConnection:
    """Business writes are marked before SQLite sees them, including failed writes."""
    def __init__(self, db, operation, keep=False):
        self.raw, self.operation, self.keep = db, operation, keep

    def execute(self, sql, parameters=()):
        if sql.lstrip().split(None, 1)[0].upper() not in {"SELECT", "BEGIN"}:
            self.operation.effect()
        return self.raw.execute(sql, parameters)

    def commit(self):
        if self.operation.guard.publication:
            self.operation.check_publication()
        if not self.keep:
            self.operation.complete_sql(self.raw)
        self.raw.commit()

    def effect(self):
        self.operation.effect()


class Store:
    @property
    def scoped(self):
        return self.profile_version == 4 or getattr(self, "_assessment", None) in (astate.AS1, ex2.EX2)

    def __init__(self, root, create=False, *, guarded=False, config=None, subject=None, resume=True):
        self.root = Path(root).resolve()
        owned.available(self.root)
        self.guard = None
        self._assessment = None
        self._assessment_service = None
        if create and isinstance(config, dict) and config.get("mode") in (astate.MODE, ex2.MODE):
            astate.invalid()
        self.finite = bool(create and isinstance(config, dict) and
                           (config.get("mode") in finite.MODES or "logical_authority" in config))
        self.profile_version = 0
        self._initializing = create
        self.initial_key = None
        bootstrap = None
        if self.finite:
            if not guarded:
                raise finite.InvalidProfile("The finite profile requires guarded initialization.")
            if "operation_guard" in config:
                raise finite.InvalidProfile("Fresh initialization cannot reuse an authority binding.")
            finite.bounded_path(self.root)
            self.profile_version = finite.validate_config(config)
            proposed = {"format": finite.PROFILE_GUARDS[self.profile_version],
                        "authority_id": "0" * 32,
                        "logical_authority_sha256": finite.profile_digest(self.profile_version)}
            finite.validate_config({**config, "operation_guard": proposed}, binding=proposed)
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
                self.guard = Guard.create(self.root, self.write if self.scoped else self._connect, finite_profile=self.finite,
                                          profile_version=self.profile_version or 1)
                bootstrap = self.guard.begin("initialize", subject=subject, request=config, bootstrap=True)
            try:
                with self.guard.locked() if self.guard and self.guard.publication else contextlib.nullcontext():
                    (self.root / "runs").mkdir(mode=0o700)
                    with self.write(role="initialize" if self._initializing else "dispatch-reservation") as db:
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
                            if self.finite:
                                db.execute(f"PRAGMA user_version={finite.database_version(self.profile_version)}")
                            db.execute("INSERT INTO operation_authority VALUES(?,0,?)", (self.guard.binding["authority_id"], uuid.uuid4().hex))
                            bootstrap.register(db)
                            db.commit()
                            bootstrap.effect()
                            config = {**config, "operation_guard": self.guard.binding}
                            if self.finite:
                                finite.validate_config(config, binding=self.guard.binding)
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

        self._initializing = False
        if self.guard and self.guard.publication:
            self.guard.publication.initializing = False
        if not create and self._open_scoped(resume):
            return
        if not create:
            with contextlib.closing(sqlite3.connect((self.root / "workspace.sqlite3").as_uri() + "?mode=ro", uri=True)) as probe:
                guarded_db = probe.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
                guarded_tables = probe.execute("SELECT count(*) FROM sqlite_master WHERE name IN ('operation_authority','operation_receipts')").fetchone()[0]
                version = probe.execute("PRAGMA user_version").fetchone()[0]
            if version == finite.SCOPED_USER_VERSION:
                raise finite.InvalidProfile("Stripped or mixed scoped authority cannot use legacy discovery.")
            config = finite.read_config(self.root) if version else json.loads((self.root / "config.json").read_bytes())
            evidence = guarded_db or guarded_tables or any((self.root / name).exists() for name in (FILE, LOCK)) or any(self.root.glob(TEMP + "*"))
            binding = config.get("operation_guard")
            logical = config.get("logical_authority")
            if (config.get("mode") == ex2.MODE
                    or isinstance(binding, dict) and binding.get("format") == ex2.GUARD_FORMAT
                    or isinstance(logical, dict) and logical.get("format") == ex2.FORMAT):
                ex2.invalid()
            finite_evidence = (version or "logical_authority" in config or config.get("mode") in finite.MODES
                               or isinstance(binding, dict) and binding.get("format") in finite.GUARD_FORMATS)
            if finite_evidence:
                if version not in finite.USER_VERSIONS or not isinstance(binding, dict) or binding.get("format") not in finite.GUARD_FORMATS:
                    raise finite.InvalidProfile("Missing or unsupported finite authority binding.")
                self.profile_version = finite.validate_config(config, binding=binding)
                if version != finite.database_version(self.profile_version):
                    raise finite.InvalidProfile("The database and finite policy differ.")
                self.finite = True
            if binding is not None or evidence or config.get("mode") in GUARDED_MODES:
                if not guarded_db or guarded_tables != 2 or binding is None or config.get("mode") not in GUARDED_MODES | {"recovery-held", "retired"}:
                    raise GuardPaused("missing_binding")
                self.guard = Guard(self.root, binding, self._connect)
                if self.finite:
                    # A profile mismatch is not an uncertainty hold that safety
                    # writes may bypass. Validate outside the resume catch.
                    self.guard.load()
                    self.guard.receipts()
                try:
                    if resume:
                        self.guard.resume()
                except Exception:
                    # Valid held stores remain available for reads and safety controls.
                    self.guard.lease.failure = "durability_unverified"

    def _open_scoped(self, resume):
        try:
            config = finite.read_config(self.root)
        except (ValueError, OSError):
            return False  # Existing rejection-only legacy discriminator.
        binding = config.get("operation_guard")
        policy = config.get("logical_authority")
        if (config.get("mode") in (astate.MODE, ex2.MODE)
                or isinstance(binding, dict) and binding.get("format") in (astate.GUARD_FORMAT, ex2.GUARD_FORMAT)
                or isinstance(policy, dict) and policy.get("format") in (astate.FORMAT, ex2.FORMAT)):
            astate.invalid()
        recognizable = (config.get("mode") == finite.SCOPED_MODE
                        or isinstance(binding, dict) and binding.get("format") == finite.SCOPED_GUARD_FORMAT
                        or isinstance(policy, dict) and policy.get("format") == finite.SCOPED_FORMAT)
        if not recognizable:
            return False
        if finite.validate_config(config, binding=binding) != 4:
            raise finite.InvalidProfile("The scoped binding must match its complete policy.")
        self.profile_version, self.finite = 4, True
        self.guard = Guard(self.root, binding, self.write)
        with self.read(role="discovery"):
            pass
        self.guard.receipts()
        try:
            if resume:
                self.guard.resume()
        except Exception:
            if owned.held(self.root):
                raise
            self.guard.lease.failure = "durability_unverified"
        return True

    @contextlib.contextmanager
    def read(self, *, role="online-read"):
        if self.scoped:
            if self._assessment and role == "online-read":
                role = "ex2-read-meta" if self._assessment is ex2.EX2 else "assessment-read"
            with owned.Owner(self.root, role, guard=self.guard, readonly=True) as db:
                yield db
        else:
            with contextlib.closing(self.connect()) as db:
                yield db

    @contextlib.contextmanager
    def write(self, *, role="mutation"):
        if self.scoped:
            if self._assessment and role == "mutation":
                if self._assessment is ex2.EX2:
                    ex2.invalid()
                role = "assessment-write"
            with owned.Owner(self.root, role, guard=self.guard, initialize=self._initializing) as db:
                yield db
        else:
            with contextlib.closing(self._connect()) as db:
                yield db

    def _connect(self):
        if self.scoped:
            raise ValueError("Use a scoped SQLite owner for this profile.")
        db = sqlite3.connect(self.root / "workspace.sqlite3", timeout=10)
        try:
            db.row_factory = sqlite3.Row
            if not self._initializing:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if self.finite:
                    finite.validate_binding(self.root, self.guard.binding, db)
                    self.guard.load()
                elif version:
                    raise finite.InvalidProfile("Unsupported database authority profile.")
                else:
                    config_path = self.root / "config.json"
                    config = json.loads(config_path.read_bytes()) if config_path.exists() else {}
                    binding = config.get("operation_guard")
                    if ("logical_authority" in config or config.get("mode") in finite.MODES | {ex2.MODE}
                            or isinstance(binding, dict) and binding.get("format") in finite.GUARD_FORMATS | {ex2.GUARD_FORMAT}):
                        raise finite.InvalidProfile("A finite profile cannot use an unrestricted open store.")
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
        if self.scoped and owned.held(self.root):
            return {"guard_enabled": True, "state": "paused", "reason": "sqlite_ownership_unconfirmed",
                    "quota_enforced": False, "can_create_project": False, "can_submit_run": False}
        return self.guard.status() if self.guard else {"guard_enabled": False, "state": "ready", "reason": None, "quota_enforced": False}

    def serialized(self, value, kind):
        return finite.bounded(value, kind).decode() if self.finite else canonical(value)

    @contextlib.contextmanager
    def transaction(self, *, essential=False, operation=None, keep=False, kind="mutation", resource_id="workspace", subject="operator", request=None, request_key=None, preflight=None):
        if self._assessment is ex2.EX2 and (keep or operation is not None or kind not in {"initialize", "mutation"}):
            raise ValueError("EX2 transactions use independent short intents only.")
        if self._assessment and (keep or operation is not None or kind not in {"initialize", "mutation"}):
            raise ValueError("Assessment transactions are short, independent mutations.")
        if self.scoped:
            with self._scoped_transaction(essential=essential, operation=operation, keep=keep, kind=kind,
                    resource_id=resource_id, subject=subject, request=request, request_key=request_key,
                    preflight=preflight) as db:
                yield db
            return
        if self.guard:
            with self.guard.locked():
                if self.finite and preflight is not None:
                    with self.read() as probe:
                        preflight(probe)
                token = operation or self.guard.begin(kind, resource_id=resource_id, subject=subject, request=request,
                                                       request_key=request_key, essential=essential)
                if operation is not None and self.guard.publication:
                    item = self.guard.load()["operations"][operation.identity]
                    self.guard.check(essential=item["essential"], admitted=operation)
                raw = db = None
                failed = False
                original_error = None
                try:
                    raw = self._connect()
                    raw.execute("BEGIN IMMEDIATE")
                    self.guard.validate_schema(raw)
                    if self.finite and preflight is not None:
                        preflight(raw)
                    config_path = self.root / "config.json"
                    if config_path.exists() and json.loads(config_path.read_bytes()).get("mode") in ("recovery-held", "retired"):
                        raise ValueError("This workspace is held or retired for recovery; mutations are disabled.")
                    db = TrackedConnection(raw, token, keep)
                    yield db
                    db.commit()
                except BaseException as original:
                    failed = True
                    original_error = original
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
                        token.fail(error=original)
                    raise
                finally:
                    try:
                        if raw is not None:
                            raw.close()
                    except BaseException as close_error:
                        token.fail(error=original_error or close_error)
                        if original_error is not None and self.guard.publication:
                            original_error.add_note("The business connection close also failed.")
                        else:
                            raise
                    finally:
                        if failed or not keep or (self.guard.publication and db is not None and not db.keep):
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

    @contextlib.contextmanager
    def _scoped_transaction(self, *, essential, operation, keep, kind, resource_id,
                            subject, request, request_key, preflight):
        if self._assessment is ex2.EX2:
            # EX2 authority is the closed Store state machine, never a caller's
            # preflight callback or an exposed generic mutation adapter.
            ex2.invalid()
        with self.guard.locked():
            owned.available(self.root)
            if preflight is not None:
                with self.read() as probe:
                    preflight(probe)
            token = operation or self.guard.begin(kind, resource_id=resource_id, subject=subject,
                    request=request, request_key=request_key, essential=essential)
            adapter = None
            failed = True
            body_error = None
            try:
                if operation is not None:
                    item = self.guard.load()["operations"][token.identity]
                    self.guard.check(essential=item["essential"], admitted=token)
                role = "assessment-spawn" if self._assessment and request and request.get("action") == "spawn" else "mutation"
                with self.write(role=role) as db:
                    try:
                        db.execute("BEGIN IMMEDIATE")
                        if preflight is not None:
                            preflight(db)
                        if finite.read_config(self.root).get("mode") in {"recovery-held", "retired"}:
                            raise ValueError("This workspace is held or retired for recovery; mutations are disabled.")
                        adapter = owned.Mutation(db,token,keep)
                        yield adapter
                        adapter.commit()
                    except BaseException as original:
                        body_error = original
                        if token.effects:
                            token.fail(error=original)
                        try:
                            db.rollback()
                            if not token.effects and not owned.held(self.root):
                                db.execute("BEGIN IMMEDIATE")
                                token.complete_sql(db,refused=True)
                                db.commit()
                        except BaseException:
                            token.fail(error=original)
                        raise
                failed = False
            except BaseException as original:
                if original is not body_error or owned.held(self.root):
                    token.fail(error=original)
                raise
            finally:
                if failed or not keep or adapter is not None and not adapter.keep:
                    token.release()

    def _ex2_local(self):
        fixture = getattr(self, "_ex2_fixture", None)
        if (self._assessment is not ex2.EX2 or type(fixture) is not ex2._LocalFixture
                or fixture.store is not self or self._assessment_service is not None):
            ex2.invalid()
        return fixture

    def _ex2_authorized(self, db, token, project_id=None, *, owner=False, active=True):
        fixture = self._ex2_local()
        wall, tick = fixture.now()
        if type(token) is not str or not token or len(token) > 256:
            raise AccessError("Current authorization is required.")
        identity = ex2.row(db, "CURRENT_AUTH", (digest(token.encode()), wall), required=False)
        if identity is None:
            raise AccessError("Current authorization is required.")
        project = None
        if project_id is not None:
            found = ex2.row(db, "CURRENT_PROJECT", (project_id, identity["subject"]), required=False)
            if found is None or found["role"] not in ({"owner"} if owner else {"owner", "editor"}):
                raise AccessError("Current project authorization is required.")
            project = json.loads(found["payload"])
            if (project.get("id") != project_id or owner and project.get("owner") != identity["subject"]
                    or active and (project.get("status") != "active" or ex2.epoch(project["ends_at"]) <= wall)):
                raise AccessError("Current project authorization is required.")
        return identity, project, wall, tick

    def _ex2_facts(self, db, *, subject, project_id):
        fixture = self._ex2_local()
        for expected in fixture.facts:
            value = ex2.row(db, "SECURITY_BY_ID", (expected["fact_id"],))
            if any(value[key] != expected[key] for key in ("fact_id", "action", "subject", "project_id", "request_key")):
                ex2.invalid()
            if (value["consumed"] and (value["subject"] is None or value["subject"] == subject)
                    and (value["project_id"] is None or value["project_id"] == project_id)):
                raise AccessError("This fixture's authorization has ended.")

    def _ex2_launch_seed(self, metadata):
        """Deferred J2b boundary. No generic callback or process selection."""
        ex2.invalid()

    def _ex2_validate_seed_result(self, child):
        """Deferred J2b original-child/assets validation; no receipt import."""
        ex2.invalid()

    def _ex2_trigger_external(self, command, control):
        """Deferred J2b native trigger; local metadata cannot authorize a process."""
        ex2.invalid()

    def _ex2_seed(self, token, project_id, manifest_json):
        return self._ex2_run("fixture-seed-spawn", token,
            {"project_id": project_id, "manifest_json": manifest_json})

    def _ex2_accept_seed(self, child):
        fixture = self._ex2_local()
        try:
            if fixture.state != "dispatched" or child is not fixture.child or child is None or fixture.published:
                ex2.invalid()
            # This potentially blocking external boundary is outside Guard/Owner.
            binding = self._ex2_validate_seed_result(child)
            wall, tick = fixture.now()
            if (type(binding) is not str or binding != fixture.binding or tick >= fixture.deadline
                    or wall >= ex2.epoch(fixture.scope_expiry)):
                ex2.invalid()
            fixture.state = "accepted"
        except BaseException:
            ex2.post_wait_hold(self)
            raise

    def _ex2_admit(self, token, manifest_json):
        return self._ex2_run("scope-admit", token, {"manifest_json": manifest_json})

    def _ex2_pending(self, token, command_id, version):
        return self._ex2_run("command-pending", token, {"command_id": command_id, "version": version})

    def _ex2_trigger(self, token, command_id, version):
        return self._ex2_run("command-trigger", token, {"command_id": command_id, "version": version})

    def _ex2_observe(self, token, command_id, version, observation):
        return self._ex2_run("command-observe", token,
            {"command_id": command_id, "version": version, "observation": observation})

    def _ex2_snapshot(self, token, *, full=False, slot_index=None):
        """Current-auth, one bounded materialization, with no Guard intent."""
        fixture = self._ex2_local()
        if not fixture.published or type(full) is not bool or full and slot_index is not None:
            ex2.invalid()
        if slot_index is not None and (type(slot_index) is not int or slot_index not in (0, 1)):
            ex2.invalid()
        with self.guard.locked(), self.write(role="ex2-command") as db:
            db.execute("BEGIN IMMEDIATE")
            identity, project, wall, tick = self._ex2_authorized(db, token, fixture.project_id)
            if identity["subject"] != fixture.subject or digest(token.encode()) != fixture.token_hash:
                raise AccessError("The original current session is required.")
            scope = ex2.row(db, "SCOPE_FULL_BY_ID" if full else "SCOPE_META_BY_ID", (fixture.scope_id,))
            if full and (scope["manifest_json"] != fixture.manifest or scope["binding_json"] != fixture.binding):
                ex2.invalid()
            if slot_index is not None:
                result = dict(scope=scope, slot=ex2.row(db, "SLOT_FULL_BY_ID", (fixture.scope_id, slot_index)))
                maximum = ex2.LIMITS["one_slot_status_bytes"]
            else:
                result = dict(scope=scope, slots=ex2.rows(db, "SLOT_META_FOR_SCOPE", (fixture.scope_id,)),
                    commands=ex2.rows(db, "COMMANDS_FOR_SCOPE", (fixture.scope_id,)))
                maximum = (ex2.LIMITS["single_full_scope_manifest_document_bytes"] if full
                           else ex2.LIMITS["scope_and_slot_summary_bytes"])
            result.update(execution_authorized=False, upload_authorized=False, cleanup_trusted=False,
                          actual_resource_accepted=False)
            if len(ex2.encode(result)) > maximum:
                ex2.invalid()
            db.rollback()
            return result

    def _ex2_security(self, token, fact_id, version, *, role=None, replacement_key=None):
        return self._ex2_run("deny-auth", token, {"fact_id": fact_id, "version": version,
            "role": role, "replacement_key": replacement_key})

    def _ex2_deny(self, scope_id, version, reason):
        return self._ex2_run("deny-scope", None, {"scope_id": scope_id, "version": version, "reason": reason})

    def _ex2_control(self, scope, command, slot):
        fixture = self._ex2_local()
        value = dict(schema="outfitter.assessment-execution-control/v1", operation=command["operation"],
            binding=json.loads(fixture.binding), expected_generation=None, admitting_owner_epoch=None,
            writer_epoch=None, manifest=None, declared_upload_ceiling_bytes=None, expires_at=None,
            content_length=None, slot=None, handoff_nonce=None, reason=None)
        operation = command["operation"]
        if operation == "prepare":
            value.update(admitting_owner_epoch=fixture.owner_epoch, manifest=json.loads(fixture.manifest),
                declared_upload_ceiling_bytes=1024, expires_at=scope["expires_at"])
        elif command["lane"] == "P":
            value.update(expected_generation=scope["scope_generation"], admitting_owner_epoch=fixture.owner_epoch)
            if operation == "execute":
                value.update(slot={key: slot[key] for key in ex2.FIRST_MANIFEST["reservations"][0]},
                             handoff_nonce=slot["handoff_nonce"])
            else:
                value["writer_epoch"] = scope["writer_epoch"]
                if operation == "ingest":
                    value["content_length"] = 21
        else:
            if slot is not None:
                value["slot"] = {key: slot[key] for key in ex2.FIRST_MANIFEST["reservations"][0]}
            if operation == "cancel":
                value["reason"] = "cancelled"
        return ex2.encode(value) + b"\n"

    def _ex2_preflight(self, db, action, token, payload, *, admitted=False):
        fixture = self._ex2_local()
        if action == "fixture-seed-spawn":
            identity, project, wall, tick = self._ex2_authorized(db, token, payload["project_id"])
            if (fixture.state != "fresh" or fixture.published or fixture.child is not None
                    or self.guard.load()["sequence"] != (2 if admitted else 1)
                    or tick + 12 >= fixture.deadline or wall >= fixture.policy_expiry):
                ex2.invalid()
            manifest = payload["manifest_json"]
            manifest_hash = ex2.first_fixture_manifest(manifest, project_id=project["id"], expires_at=fixture.manifest_expiry)
            for table in ("ex2_scopes", "ex2_slots", "ex2_commands"):
                if db.execute(ex2.SQL_READS[table + "_COUNT"]).fetchone()[0] != 0:
                    ex2.invalid()
            self._ex2_facts(db, subject=identity["subject"], project_id=project["id"])
            expires = fixture.bounded_scope_expiry(identity["expires"], ex2.epoch(project["ends_at"]))
            if ex2.epoch(expires) <= wall:
                ex2.invalid()
            metadata = dict(schema="outfitter.execution-session-seed-metadata/v1",
                waypoint_authority_id=self.guard.binding["authority_id"], project_id=project["id"],
                scope_id=fixture.scope_id, owner_epoch=fixture.owner_epoch,
                sampled_at=ex2.utc(fixture.sample, micros=True), session_deadline=ex2.utc(fixture.wall_deadline, micros=True),
                authorization_expiry=ex2.utc(identity["expires"]), project_expiry=ex2.utc(ex2.epoch(project["ends_at"])),
                policy_expiry=ex2.utc(fixture.policy_expiry), manifest_sha256=manifest_hash)
            return dict(subject=identity["subject"], project=project, identity=identity, expires=expires,
                metadata=metadata, request=dict(action=action, scope_id=fixture.scope_id,
                    project_id=project["id"], subject=identity["subject"], manifest_sha256=manifest_hash,
                    control_sha256=digest(ex2.encode(metadata))), role="ex2-fixture-seed-spawn", essential=False)
        if action == "scope-admit":
            identity, project, wall, tick = self._ex2_authorized(db, token, fixture.project_id)
            if (fixture.state != "accepted" or fixture.published or digest(token.encode()) != fixture.token_hash
                    or identity["subject"] != fixture.subject or identity["expires"] != fixture.auth_expiry
                    or tick >= fixture.deadline or wall >= ex2.epoch(fixture.scope_expiry)
                    or payload["manifest_json"] != fixture.manifest):
                ex2.invalid()
            ex2.first_fixture_manifest(payload["manifest_json"], project_id=fixture.project_id,
                                       expires_at=fixture.manifest_expiry)
            self._ex2_facts(db, subject=fixture.subject, project_id=fixture.project_id)
            for table in ("ex2_scopes", "ex2_slots", "ex2_commands"):
                if db.execute(ex2.SQL_READS[table + "_COUNT"]).fetchone()[0] != 0:
                    ex2.invalid()
            scope = fixture.scope()
            return dict(subject=fixture.subject, scope=scope,
                request=dict(action=action, scope_id=fixture.scope_id, project_id=fixture.project_id,
                    subject=fixture.subject, manifest_sha256=scope["manifest_sha256"], binding_sha256=scope["binding_sha256"]),
                role="ex2-admit", essential=False)
        if action == "deny-auth":
            # Local security actions can run before admission, but never during
            # unpublished seed bootstrap. No new sign-in or credential factory.
            if fixture.state not in {"accepted", "active"}:
                ex2.invalid()
            expected = next((x for x in fixture.facts if x["fact_id"] == payload["fact_id"]), None)
            fact = ex2.row(db, "SECURITY_BY_ID", (payload["fact_id"],))
            if (expected is None or fact["consumed"] != 0 or type(payload["version"]) is not int
                    or fact["version"] != payload["version"] or fact["version"] >= ex2.M
                    or any(fact[key] != expected[key] for key in ("action", "subject", "project_id", "request_key"))):
                ex2.invalid()
            operation = fact["action"]
            identity, project, wall, tick = self._ex2_authorized(db, token, fact["project_id"],
                owner=operation in {"membership", "close-project"}, active=operation == "close-project")
            if operation in {"revoke-user", "rotate-user", "logout"} and identity["subject"] != fact["subject"]:
                raise AccessError("Only the current subject can change this fixture credential.")
            if operation != "membership" and payload["role"] is not None or operation != "rotate-user" and payload["replacement_key"] is not None:
                ex2.invalid()
            if operation == "membership":
                if payload["role"] not in (None, "editor", "viewer") or fact["subject"] == project["owner"]:
                    ex2.invalid()
                member = ex2.row(db, "MEMBER_ROLE", (fact["project_id"], fact["subject"]), required=False)
                if member is None or member["role"] == payload["role"]:
                    ex2.invalid()
                if ex2.row(db, "ACTIVE_USER", (fact["subject"],), required=False) is None:
                    ex2.invalid()
            if operation == "rotate-user" and (type(payload["replacement_key"]) is not str
                    or not 32 <= len(payload["replacement_key"]) <= 256
                    or digest(payload["replacement_key"].encode()) in fixture.initial_key_hashes.values()):
                ex2.invalid()
            scopes = ex2.rows(db, "SCOPE_ALL_META")
            scopes = [scope for scope in scopes if (fact["subject"] is None or scope["subject"] == fact["subject"])
                and (fact["project_id"] is None or scope["project_id"] == fact["project_id"])]
            slots = [slot for scope in scopes for slot in ex2.rows(db, "SLOT_META_FOR_SCOPE", (scope["scope_id"],))]
            self._ex2_denial_versions(scopes, slots)
            return dict(subject=identity["subject"], fact=fact, scopes=scopes, slots=slots, project=project,
                request=dict(action=action, fact_id=fact["fact_id"], subject=identity["subject"]),
                role="ex2-deny-auth", essential=True)
        if not fixture.published or fixture.state != "active":
            ex2.invalid()
        if action == "deny-scope":
            scope = ex2.row(db, "SCOPE_META_BY_ID", (payload["scope_id"],))
            if (scope["scope_id"] != fixture.scope_id or scope["deny_reason"] != "none"
                    or type(payload["version"]) is not int or scope["version"] != payload["version"]
                    or payload["reason"] not in {"expired", "restart", "clock_rollback", "access_ended",
                        "ipc_uncertain", "storage_uncertain", "publication_uncertain"}):
                ex2.invalid()
            wall, tick = fixture.now()
            if payload["reason"] != "expired":
                # Other first-denial origins need their reviewed original
                # runtime witness. A caller-supplied label is not that witness.
                ex2.invalid()
            if wall < ex2.epoch(scope["expires_at"]) and tick < fixture.deadline:
                ex2.invalid()
            slots = ex2.rows(db, "SLOT_META_FOR_SCOPE", (scope["scope_id"],))
            self._ex2_denial_versions([scope], slots)
            return dict(subject=scope["subject"], scope=scope, scopes=[scope], slots=slots,
                request=dict(action=action, scope_id=scope["scope_id"], reason=payload["reason"]),
                role="ex2-deny-scope", essential=True)
        identity, project, wall, tick = self._ex2_authorized(db, token, fixture.project_id)
        if (digest(token.encode()) != fixture.token_hash or identity["subject"] != fixture.subject
                or identity["expires"] != fixture.auth_expiry or wall >= ex2.epoch(fixture.scope_expiry)
                or tick >= fixture.deadline):
            raise AccessError("This original fixture session has ended.")
        self._ex2_facts(db, subject=fixture.subject, project_id=fixture.project_id)
        command = ex2.row(db, "COMMAND_BY_ID", (payload["command_id"],))
        expected = next((x for x in fixture.commands if x["command_id"] == command["command_id"]), None)
        scope = ex2.row(db, "SCOPE_META_BY_ID" if action == "command-observe" and command["slot_index"] is not None
                        else "SCOPE_FULL_BY_ID", (fixture.scope_id,))
        if "manifest_json" in scope and (scope["manifest_json"] != fixture.manifest or scope["binding_json"] != fixture.binding):
            ex2.invalid()
        if (expected is None or type(payload["version"]) is not int or command["version"] != payload["version"]
                or command["version"] >= ex2.M or scope["version"] >= ex2.M
                or any(command[k] != expected[k] for k in ("scope_id", "lane", "ordinal", "operation", "slot_index", "opportunity", "request_key", "handoff_nonce"))
                or any(scope[k] != fixture.scope()[k] for k in ("scope_id", "project_id", "subject", "manifest_sha256", "binding_sha256", "expires_at", "admitting_owner_epoch", "helper_epoch"))):
            ex2.invalid()
        slot = None
        slots = ex2.rows(db, "SLOT_META_FOR_SCOPE", (scope["scope_id"],))
        if command["slot_index"] is not None:
            slot = next((x for x in slots if x["slot_index"] == command["slot_index"]), None)
            original = fixture.slots[command["slot_index"]]
            if slot is None or slot["version"] >= ex2.M or any(slot[k] != original[k] for k in
                    ("scope_id", "slot_index", "attempt_id", "reservation_id", "stage_id", "sample_role", "sample_size",
                     "repetition", "handoff_nonce", "request_key", "binding_digest")):
                ex2.invalid()
        phases = {"command-pending": "unused", "command-trigger": "pending", "command-observe": "triggered"}
        if action not in phases or command["state"] != phases[action]:
            ex2.invalid()
        pending = ex2.rows(db, "PENDING_LANE", (scope["scope_id"], command["lane"]))
        if (action == "command-pending" and pending or action != "command-pending"
                and (len(pending) != 1 or pending[0]["command_id"] != command["command_id"])):
            ex2.invalid()
        if scope["deny_reason"] not in ({"none"} if command["lane"] == "P" else {"none", "cancelled"}):
            ex2.invalid()
        if action != "command-observe":
            if action == "command-pending" and command["operation"] == "cancel" and (
                    slot["deny_reason"] if slot is not None else scope["deny_reason"]) != "none":
                ex2.invalid()
            if command["operation"] == "cancel":
                self._ex2_denial_versions([] if slot else [scope], [slot] if slot else slots)
            if command["lane"] == "P":
                if scope["local_phase"] != ("pending" if command["operation"] == "prepare" else "bound"):
                    ex2.invalid()
                prior = ex2.rows(db, "COMMANDS_FOR_SCOPE", (scope["scope_id"],))
                if any(x["lane"] == "P" and x["ordinal"] < command["ordinal"] and x["state"] != "observed" for x in prior):
                    ex2.invalid()
                if slot is not None and (slot["deny_reason"] != "none" or slot["local_phase"] != (
                        "planned" if action == "command-pending" else "pending")):
                    ex2.invalid()
                if command["operation"] == "execute" and (scope["scope_state"] != "sealed" or not scope["upload_complete"]
                        or scope["execution_deny_latched"] or wall + 30 > ex2.epoch(scope["expires_at"])):
                    ex2.invalid()
            if tick + (42 if command["operation"] == "execute" else 12) >= fixture.deadline:
                ex2.invalid()
        control = self._ex2_control(scope, command, slot)
        if action == "command-trigger" and (command["control_sha256"] != digest(control)
                or command["control_length"] != len(control)
                or command["expected_generation"] != (scope["scope_generation"] if command["lane"] == "P" and command["operation"] != "prepare" else None)):
            ex2.invalid()
        target = None
        if action == "command-observe":
            if slot is None:
                target = scope
                if target["manifest_json"] != fixture.manifest or target["binding_json"] != fixture.binding:
                    ex2.invalid()
            else:
                target = ex2.row(db, "SLOT_FULL_BY_ID", (scope["scope_id"], slot["slot_index"]))
            # Contradictory definitive evidence is an uncertainty hold even
            # before begin; unknown/malformed evidence never promotes a cache.
            try:
                observed = ex2.observation(payload["observation"], command, scope, target)
            except BaseException:
                ex2.post_wait_hold(self)
                raise
        else:
            observed = None
        request = dict(action=action, command_id=command["command_id"], scope_id=scope["scope_id"],
            project_id=scope["project_id"], subject=scope["subject"], lane=command["lane"], ordinal=command["ordinal"])
        if action != "command-observe":
            request["control_sha256"] = digest(control)
        return dict(subject=scope["subject"], scope=scope, slot=slot, slots=slots, command=command,
            control=control, target=target, observed=observed, request=request, role="ex2-command", essential=command["lane"] == "S")

    def _ex2_late_current(self, db, token, checked, *, trigger=False):
        fixture = self._ex2_local()
        identity, project, wall, tick = self._ex2_authorized(db, token, fixture.project_id)
        if (identity["subject"] != fixture.subject or identity["expires"] != fixture.auth_expiry
                or digest(token.encode()) != fixture.token_hash or wall >= ex2.epoch(fixture.scope_expiry)
                or tick + ((42 if checked["command"]["operation"] == "execute" else 12) if trigger else 0) >= fixture.deadline
                or trigger and checked["command"]["operation"] == "execute" and wall + 30 > ex2.epoch(fixture.scope_expiry)):
            ex2.invalid()
        return wall

    @staticmethod
    def _ex2_denial_versions(scopes, slots):
        if any(value["deny_reason"] == "none" and value["version"] >= ex2.M for value in scopes + slots):
            ex2.invalid()

    @staticmethod
    def _ex2_apply_denial(db, scopes, slots, reason):
        for scope in scopes:
            if scope["deny_reason"] == "none":
                ex2.change(db, "SCOPE_DENY", (reason, scope["scope_id"], scope["version"]))
        for slot in slots:
            if slot["deny_reason"] == "none":
                ex2.change(db, "SLOT_DENY", (reason, slot["scope_id"], slot["slot_index"], slot["version"]))

    def _ex2_mutate(self, db, action, token, payload, checked):
        fixture = self._ex2_local()
        if action in {"scope-admit", "deny-auth"}:
            db.effect()
            late = self._ex2_preflight(db, action, token, payload, admitted=True)
            if late != checked:
                ex2.invalid()
        elif action in {"command-pending", "command-observe"}:
            db.effect()
            self._ex2_late_current(db, token, checked, trigger=action == "command-pending")
        if action == "fixture-seed-spawn":
            # Publication may block. Recheck real current auth after effect and
            # immediately before the only external dispatch seam.
            db.effect()
            late = self._ex2_preflight(db, action, token, payload, admitted=True)
            if late["request"] != checked["request"]:
                ex2.invalid()
            fixture.subject, fixture.project_id = checked["subject"], checked["project"]["id"]
            fixture.token_hash, fixture.auth_expiry = digest(token.encode()), checked["identity"]["expires"]
            fixture.scope_expiry, fixture.manifest = checked["expires"], payload["manifest_json"]
            fixture.plan()
            fixture.state = "attempted"
            try:
                fixture.child = self._ex2_launch_seed(checked["metadata"])
            except BaseException as failure:
                # Retain only the handle attached to the actual original
                # failed launch. Teardown belongs to deferred J2b outside locks.
                fixture.child = getattr(failure, "child", None)
                raise
            if fixture.child is None:
                ex2.invalid()
            return fixture.child
        if action == "scope-admit":
            ex2.insert(db, "ex2_scopes", checked["scope"])
            for slot in fixture.slots:
                ex2.insert(db, "ex2_slots", slot)
            for command in fixture.commands:
                ex2.insert(db, "ex2_commands", command)
            return fixture.scope_id
        if action == "deny-auth":
            fact = checked["fact"]
            ex2.change(db, "SECURITY_CONSUME", (fact["fact_id"], fact["version"]))
            operation, subject, project = fact["action"], fact["subject"], fact["project_id"]
            if operation == "revoke-user":
                ex2.change(db, "UPDATE users SET active=0 WHERE subject=?", (subject,))
                if db.execute("DELETE FROM sessions WHERE subject=?", (subject,)).rowcount != 1:
                    ex2.invalid()
            elif operation == "rotate-user":
                if db.execute("DELETE FROM sessions WHERE subject=?", (subject,)).rowcount != 1:
                    ex2.invalid()
                ex2.change(db, "UPDATE users SET key_hash=? WHERE subject=?", (digest(payload["replacement_key"].encode()), subject))
            elif operation == "logout":
                ex2.change(db, "DELETE FROM sessions WHERE token_hash=?", (digest(token.encode()),))
            elif operation == "membership":
                if payload["role"] is None:
                    ex2.change(db, "DELETE FROM members WHERE project_id=? AND subject=?", (project, subject))
                else:
                    ex2.change(db, "INSERT INTO members VALUES(?,?,?) ON CONFLICT(project_id,subject) DO UPDATE SET role=excluded.role",
                        (project, subject, payload["role"]))
            elif operation == "close-project":
                updated = {**checked["project"], "status": "closed"}
                ex2.change(db, "UPDATE projects SET payload=? WHERE id=?", (ex2.encode(updated).decode("ascii"), project))
            self._ex2_apply_denial(db, checked["scopes"], checked["slots"], "access_ended")
            return fact["fact_id"]
        if action == "deny-scope":
            self._ex2_apply_denial(db, checked["scopes"], checked["slots"], payload["reason"])
            return checked["scope"]["scope_id"]
        command, scope, slot = checked["command"], checked["scope"], checked["slot"]
        if action == "command-pending":
            generation = scope["scope_generation"] if command["lane"] == "P" and command["operation"] != "prepare" else None
            ex2.change(db, "COMMAND_PENDING", (generation, digest(checked["control"]), len(checked["control"]), command["command_id"], command["version"]))
            if command["operation"] == "execute":
                ex2.change(db, "SLOT_PHASE", ("pending", scope["scope_id"], slot["slot_index"], "planned", slot["version"]))
            elif command["operation"] == "cancel":
                self._ex2_apply_denial(db, [] if slot else [scope], [slot] if slot else checked["slots"], "cancelled")
        elif action == "command-trigger":
            variant = ("TRIGGER_SAFETY" if command["lane"] == "S" else "TRIGGER_PREPARE" if command["operation"] == "prepare"
                       else "TRIGGER_EXECUTE" if command["operation"] == "execute" else "TRIGGER_INGEST_OR_SEAL")
            parameters = (command["command_id"], command["control_sha256"], command["control_length"],
                command["expected_generation"], command["version"], scope["project_id"], scope["subject"],
                fixture.owner_epoch, fixture.helper_epoch, ex2.utc(fixture.now()[0]), scope["version"])
            if variant == "TRIGGER_EXECUTE":
                parameters += (slot["version"],)
            # effect can publish; the final authorization check follows it.
            db.effect()
            self._ex2_late_current(db, token, checked, trigger=True)
            ex2.change(db, variant, parameters)
            if command["operation"] == "execute":
                ex2.change(db, "SLOT_PHASE", ("triggered", scope["scope_id"], slot["slot_index"], "pending", slot["version"]))
            self._ex2_late_current(db, token, checked, trigger=True)
            sent = self._ex2_trigger_external(command, checked["control"])
            if type(sent) is not int or sent != 1:
                ex2.invalid()
        elif action == "command-observe":
            value = payload["observation"]
            generation, reply_hash, equal, promote = checked["observed"]
            target = checked["target"]
            cache_field = "scope_generation" if slot is None else "attempt_generation"
            ex2.complete_command_observation(db, command, correlated=True, validated=True, reply_code=value.code,
                reply_generation=generation, reply_sha256=reply_hash, cache_generation=target[cache_field], cache_equal=equal)
            if promote:
                projection = value.projection
                if slot is None:
                    version = scope["version"]
                    if command["operation"] == "prepare":
                        ex2.change(db, "SCOPE_BIND", (generation, value.writer_epoch, value.watch_epoch,
                            projection["scope_state"], scope["scope_id"], version))
                        version += 1
                    ex2.change(db, "SCOPE_OBSERVE", tuple(projection[key] for key in ex2.SCOPE_CACHE)
                        + (scope["scope_id"], generation if command["operation"] == "prepare" else scope["scope_generation"], version))
                else:
                    ex2.change(db, "SLOT_OBSERVE", tuple(projection[key] for key in ex2.SLOT_CACHE)
                        + (scope["scope_id"], slot["slot_index"], slot["attempt_generation"], slot["version"]))
            if command["operation"] == "execute" and slot["deny_reason"] == "none":
                ex2.change(db, "SLOT_PHASE", ("observed", scope["scope_id"], slot["slot_index"], "triggered", slot["version"] + int(promote)))
        return command["command_id"]

    def _ex2_run(self, action, token, payload):
        """Closed state transition; no callable supplies semantic authority."""
        fixture = self._ex2_local()
        with self.guard.locked():
            owned.available(self.root)
            # Business reads use the exact writer role for full observations;
            # a readonly metadata Owner performs all other prebegin gates.
            if action.startswith("command-"):
                with self.write(role="ex2-command") as probe:
                    probe.execute("BEGIN IMMEDIATE")
                    checked = self._ex2_preflight(probe, action, token, payload)
                    probe.rollback()
            else:
                with self.read(role="ex2-read-meta") as probe:
                    checked = self._ex2_preflight(probe, action, token, payload)
            request = checked["request"]
            ex2.guard_request(request)
            fixed_key = ex2.request_phase_key(request, self.guard.binding)
            essential, subject = checked["essential"], checked["subject"]
            resource = request.get("scope_id", request.get("fact_id", "workspace"))
            if action == "fixture-seed-spawn":
                resource = request["project_id"]
            current = self.guard.check(essential=essential, kind="mutation")
            ex2.admission(current, essential)
            if self.guard.lookup("mutation", subject, fixed_key) is not None:
                ex2.invalid()
            operation = None
            try:
                # This try starts BEFORE begin, including publication failure.
                operation = self.guard.begin("mutation", resource_id=resource, subject=subject,
                    request=request, request_key=fixed_key, essential=essential)
                with self.write(role=checked["role"]) as owner:
                    try:
                        owner.execute("BEGIN IMMEDIATE")
                        checked = self._ex2_preflight(owner, action, token, payload, admitted=True)
                        if checked["request"] != request:
                            ex2.invalid()
                        adapter = owned.Mutation(owner, operation, False)
                        result = self._ex2_mutate(adapter, action, token, payload, checked)
                        adapter.commit()
                    except BaseException as original:
                        try:
                            owner.rollback()
                        except BaseException:
                            pass
                        try:
                            earlier_failure = self.guard.lease.failure
                            try:
                                operation.fail(error=original)
                            finally:
                                if earlier_failure:
                                    self.guard.lease.failure = earlier_failure
                        except BaseException:
                            pass
                        raise
                if action == "fixture-seed-spawn":
                    fixture.state = "dispatched"
                elif action == "scope-admit":
                    fixture.state, fixture.published = "active", True
                return result
            except BaseException:
                owned.hold(self.root)
                self.guard.lease.failure = self.guard.lease.failure or "operation_failed"
                raise
            finally:
                if operation is not None:
                    try:
                        operation.release()
                    except BaseException:
                        owned.hold(self.root)
                        self.guard.lease.failure = self.guard.lease.failure or "operation_failed"
                        raise

    @contextlib.contextmanager
    def _safety_transaction(self, **kwargs):
        affected = []
        try:
            with self.transaction(**kwargs) as db:
                yield db, affected
        except BaseException:
            if self._assessment_service is not None:
                self._assessment_service.close_lifelines()
            raise
        if self._assessment_service is not None and affected:
            self._assessment_service.denied(affected)

    def _deny_assessments(self, db, affected, *, subject=None, project_id=None, reason="access_ended"):
        if not self._assessment:
            return
        for row in db.execute(astate.ALL_META).fetchall():
            value = astate.validate_row(row, metadata=True)
            if subject is not None and value["subject"] != subject:
                continue
            if project_id is not None and value["project_id"] != project_id:
                continue
            if value["local_phase"] != "denied":
                changed = db.execute(astate.UPDATE, ("denied", reason, value["version"] + 1,
                    value["last_scope_generation"], value["id"], value["version"], value["local_phase"]))
                if changed.rowcount != 1:
                    astate.invalid()
                value.update(local_phase="denied", deny_reason=reason, version=value["version"] + 1)
            affected.append(value)

    def add_user(self, subject):
        identifier(subject)
        key = secrets.token_urlsafe(32)
        with self.transaction(subject=subject, request={"action": "add-user", "subject": subject},
                              preflight=lambda db: finite.admit(db, users=1)) as db:
            if db.execute("SELECT 1 FROM users WHERE subject=?", (subject,)).fetchone():
                raise ValueError("This subject already exists; existing credentials were preserved.")
            db.execute("INSERT INTO users VALUES(?,?,1)", (subject, digest(key.encode())))
        return key

    def revoke_user(self, subject):
        with self._safety_transaction(essential=True, subject=subject, request={"action": "revoke-user", "subject": subject}) as (db, affected):
            db.execute("UPDATE users SET active=0 WHERE subject=?", (subject,))
            db.execute("DELETE FROM sessions WHERE subject=?", (subject,))
            if not self._assessment:
                db.execute("UPDATE runs SET cancel_requested=1 WHERE subject=? AND status IN ('queued','running')", (subject,))
            self._deny_assessments(db, affected, subject=subject)

    def rotate_user(self, subject):
        identifier(subject)
        key = secrets.token_urlsafe(32)
        with self._safety_transaction(essential=True, subject=subject, request={"action": "rotate-user", "subject": subject}) as (db, affected):
            if not db.execute("SELECT 1 FROM users WHERE subject=? AND active=1", (subject,)).fetchone():
                raise ValueError("Only an existing active subject can receive a replacement key.")
            db.execute("DELETE FROM sessions WHERE subject=?", (subject,))
            db.execute("UPDATE users SET key_hash=? WHERE subject=?", (digest(key.encode()), subject))
            self._deny_assessments(db, affected, subject=subject)
        return key

    def session(self, key=None, token=None):
        if self.finite and key is not None:
            if not isinstance(key, str) or len(key) > 256:
                raise AccessError("Sign in with a valid access key.")
            sampled = None
            def preflight(db):
                nonlocal sampled
                if sampled is None:
                    sampled = time.time()
                row = db.execute("SELECT subject FROM users WHERE key_hash=? AND active=1", (digest(key.encode()),)).fetchone()
                if row is None:
                    raise AccessError("Sign in with a valid access key.")
                finite.admit(db, subject=row["subject"], sampled_time=sampled, sessions=1)
                return row
            with self.transaction(request={"action": "sign-in"}, preflight=preflight) as db:
                row = preflight(db)
                token = secrets.token_urlsafe(32)
                db.execute("DELETE FROM sessions WHERE expires<=?", (sampled,))
                db.execute("INSERT INTO sessions VALUES(?,?,?)", (digest(token.encode()), row["subject"], sampled+28800))
                return row["subject"], token
        with self.transaction(essential=True) if key is not None else self.read() as db:
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
        request = {"action": "logout"} if self._assessment else {"action": "logout", "token_sha256": digest(token.encode())}
        with self._safety_transaction(essential=True, request=request) as (db, affected):
            if self._assessment:
                prior = db.execute(astate.LOGOUT_SUBJECT, (digest(token.encode()),)).fetchone()
                if prior is not None:
                    self._deny_assessments(db, affected, subject=prior["subject"], reason="access_ended")
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
        with self.read() as db:
            if not db.execute("SELECT 1 FROM users WHERE subject=? AND active=1", (subject,)).fetchone():
                raise AccessError("This identity is no longer active.")
            return [json.loads(row[0]) for row in db.execute("SELECT p.payload FROM projects p JOIN members m ON m.project_id=p.id JOIN users u ON u.subject=m.subject WHERE m.subject=? AND u.active=1 ORDER BY p.rowid DESC", (subject,))]

    def create_project(self, subject, data, broker, global_budget, request_key=None):
        if self._assessment:
            raise ValueError("Assessment study, execution and recovery operations are unsupported.")
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
        self.serialized(project, "project")
        if self.guard:
            return self._guarded_project(subject, data, project, broker, global_budget, request_key)
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM users WHERE subject=? AND active=1", (subject,)).fetchone():
                raise AccessError("This identity is no longer active.")
            committed = sum(json.loads(row[0])["budget_cpu_seconds"] for row in db.execute("SELECT payload FROM projects"))
            if committed+budget > global_budget:
                raise ValueError("The workspace's finite project allocation is exhausted. Ask the operator.")
            broker.init_project(project["id"], budget)
            db.execute("INSERT INTO projects VALUES(?,?)", (project["id"], self.serialized(project, "project")))
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
                    with self.read() as db:
                        self._access(db, intent["resource_id"], subject)
                        return json.loads(db.execute("SELECT payload FROM projects WHERE id=?", (intent["resource_id"],)).fetchone()[0])
                raise GuardPaused("existing_intent")
            if self.finite:
                with self.read() as db:
                    finite.admit(db, projects=1, members=1)
            token = self.guard.begin("project", resource_id=project["id"], subject=subject, request=data, request_key=request_key)
            try:
                with self.transaction(operation=token, keep=True) as db:
                    if not db.execute("SELECT 1 FROM users WHERE subject=? AND active=1", (subject,)).fetchone():
                        raise AccessError("This identity is no longer active.")
                    committed = sum(json.loads(row[0])["budget_cpu_seconds"] for row in db.execute("SELECT payload FROM projects"))
                    if committed + project["budget_cpu_seconds"] > global_budget:
                        raise ValueError("The workspace's finite project allocation is exhausted. Ask the operator.")
                    if self.finite:
                        finite.admit(db, projects=1, members=1)
            except BaseException:
                token.release()
                raise
        try:
            # Only the actual spawn is fenced. No guard or DB lock during wait.
            broker.init_project(project["id"], project["budget_cpu_seconds"], launch=token.launch)
            with self.transaction(operation=token) as db:
                if not db.execute("SELECT 1 FROM users WHERE subject=? AND active=1", (subject,)).fetchone():
                    raise AccessError("This identity is no longer active; project initialization requires reconciliation.")
                if self.finite:
                    finite.admit(db, projects=1, members=1)
                db.execute("INSERT INTO projects VALUES(?,?)", (project["id"], self.serialized(project, "project")))
                db.execute("INSERT INTO members VALUES(?,?,?)", (project["id"], subject, "owner"))
            return project
        except BaseException:
            token.fail()
            raise
        finally:
            token.release()

    def project(self, project_id, subject):
        with self.read() as db:
            project, role = self._access(db, project_id, subject)
            members = [dict(row) for row in db.execute("SELECT subject,role FROM members WHERE project_id=? ORDER BY subject", (project_id,))]
            runs = [json.loads(row[0]) for row in db.execute("SELECT payload FROM runs WHERE project_id=? ORDER BY rowid DESC", (project_id,))]
            return dict(project=project, role=role, members=members, runs=runs)

    def membership(self, project_id, actor, subject, role=None):
        if self.finite:
            with self.guard.locked():
                with self.read() as db:
                    existing = db.execute("SELECT role FROM members WHERE project_id=? AND subject=?", (project_id, subject)).fetchone()
                return self._membership(project_id, actor, subject, role,
                                        insertion=existing is None and role is not None)
        return self._membership(project_id, actor, subject, role)

    def _membership(self, project_id, actor, subject, role=None, *, insertion=False):
        identifier(subject)
        with self._safety_transaction(essential=role in (None, "viewer") and not insertion, resource_id=project_id, subject=actor,
                              request={"action": "membership", "subject": subject, "role": role},
                              preflight=(lambda db: finite.admit(db, members=1)) if insertion else None) as (db, affected):
            project, _ = self._access(db, project_id, actor, {"owner"})
            if subject == project["owner"]:
                raise ValueError("The project owner cannot be removed or demoted.")
            if role is None:
                db.execute("DELETE FROM members WHERE project_id=? AND subject=?", (project_id, subject))
                if not self._assessment:
                    db.execute("UPDATE runs SET cancel_requested=1 WHERE project_id=? AND subject=? AND status IN ('queued','running')", (project_id, subject))
                self._deny_assessments(db, affected, subject=subject, project_id=project_id)
            else:
                if role not in ("editor", "viewer"):
                    raise ValueError("Choose editor or viewer.")
                if not db.execute("SELECT 1 FROM users WHERE subject=? AND active=1", (subject,)).fetchone():
                    raise ValueError("The operator must provision that collaborator first.")
                db.execute("INSERT INTO members VALUES(?,?,?) ON CONFLICT(project_id,subject) DO UPDATE SET role=excluded.role", (project_id, subject, role))
                if role == "viewer":
                    if not self._assessment:
                        db.execute("UPDATE runs SET cancel_requested=1 WHERE project_id=? AND subject=? AND status IN ('queued','running')", (project_id, subject))
                    self._deny_assessments(db, affected, subject=subject, project_id=project_id)

    def close_project(self, project_id, subject):
        with self._safety_transaction(essential=True, resource_id=project_id, subject=subject, request={"action": "close-project"}) as (db, affected):
            project, _ = self._access(db, project_id, subject, {"owner"})
            project["status"] = "closed"
            db.execute("UPDATE projects SET payload=? WHERE id=?", (self.serialized(project, "project"), project_id))
            if not self._assessment:
                db.execute("UPDATE runs SET cancel_requested=1 WHERE project_id=? AND status IN ('queued','running')", (project_id,))
            self._deny_assessments(db, affected, project_id=project_id)
        return project

    def submit(self, project_id, subject, data, images, request_key=None, authorize=None):
        if self._assessment:
            raise ValueError("Assessment study, execution and recovery operations are unsupported.")
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
            with self.read() as db:
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
        self.serialized(run, "run")
        with (self.transaction(kind="submit", resource_id=rid, subject=subject, request={"project_id": project_id, "request_hash": request_hash},
                               preflight=lambda db: finite.admit(db, runs=1)) if self.guard else self.transaction()) as db:
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
            db.execute("INSERT INTO runs(id,project_id,subject,status,payload,request_key,request_hash) VALUES(?,?,?,?,?,?,?)", (rid, project_id, subject, "queued", self.serialized(run, "run"), request_key, request_hash))
            if authorize is not None:
                authorize(rid)
        return run

    def run(self, project_id, run_id, subject):
        with self.read() as db:
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
        if self._assessment:
            raise ValueError("Assessment study, execution and recovery operations are unsupported.")
        with self.transaction(essential=True, request={"action": "restart", "cancel_queued": cancel_queued}) as db:
            if cancel_queued:
                for row in db.execute("SELECT id,payload FROM runs WHERE status='queued'").fetchall():
                    run = json.loads(row["payload"])
                    run.update(status="cancelled", finished_at=now(), cleanup_verified=True,
                               error="Transient identity authorization ended at restart. Submit a new run after signing in; this run was not replayed.")
                    db.execute("UPDATE runs SET status='cancelled',payload=? WHERE id=?", (self.serialized(run, "run"), row["id"]))
            for row in db.execute("SELECT id,payload FROM runs WHERE status='running'").fetchall():
                run = json.loads(row["payload"])
                run.update(status="interrupted", finished_at=now(), error="The service restarted during execution. Operator reconciliation is required; this run was not retried.", cleanup_verified=False, compute=unknown_compute())
                db.execute("UPDATE runs SET status='interrupted',payload=? WHERE id=?", (self.serialized(run, "run"), row["id"]))

    def claim(self, on_cancel=None):
        if self._assessment:
            raise ValueError("Assessment study, execution and recovery operations are unsupported.")
        if self.guard:
            with self.guard.locked():
                self.guard.check(kind="claim")
                with self.read() as db:
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
                    db.execute("UPDATE runs SET status='cancelled',payload=? WHERE id=?", (self.serialized(run, "run"), row["id"]))
                    if on_cancel is not None:
                        on_cancel(row["id"])
                    if operation is not None:
                        if self.guard.publication:
                            # The terminal refusal must retain its token through
                            # the context's publication check, commit and close.
                            db.keep = False
                        else:
                            operation.complete_sql(db.raw)
                            db.raw.commit()
                            operation.release()
                        return None
                    continue
                run.update(status="running", started_at=now(), compute=unknown_compute())
                db.execute("UPDATE runs SET status='running',payload=? WHERE id=?", (self.serialized(run, "run"), row["id"]))
                return run
        return None

    def should_cancel(self, run_id):
        with self.read() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None or row["cancel_requested"]:
                return True
            try:
                self._access(db, row["project_id"], row["subject"], {"owner", "editor"}, active=True)
                return False
            except (AccessError, ValueError):
                return True

    def launch_run(self, run, spawn, publication=None):
        if self._assessment:
            raise ValueError("Assessment study, execution and recovery operations are unsupported.")
        """Fence only spawn, using the same writer order as local safety controls."""
        def current_launch():
            # BEGIN IMMEDIATE orders controls from other Store instances and
            # processes too. No remote check or broker wait belongs in this fence.
            with self.write(role="initialize" if self._initializing else "dispatch-reservation") as db:
                try:
                    db.execute("BEGIN IMMEDIATE")
                    if self.guard:
                        self.guard.validate_schema(db)
                    config = json.loads((self.root / "config.json").read_bytes())
                    if config.get("mode") in ("recovery-held", "retired"):
                        raise DispatchSuperseded("This workspace is held or retired; dispatch is disabled.")
                    with publication if publication is not None else contextlib.nullcontext(True) as authorized:
                        row = db.execute("SELECT * FROM runs WHERE id=?", (run["id"],)).fetchone()
                        if row is None or row["status"] != "running" or row["project_id"] != run["project_id"]:
                            raise DispatchSuperseded("The claimed run is no longer current.")
                        # Check expiry after acquiring identity publication too:
                        # waiting for that local lock can cross the project end.
                        try:
                            self._access(db, row["project_id"], row["subject"], {"owner", "editor"}, active=True)
                        except (AccessError, ValueError):
                            return None
                        if not authorized or row["cancel_requested"]:
                            return None
                        return spawn()
                finally:
                    # v4 owner cleanup preserves a spawn error if rollback fails.
                    # Failure after spawn remains uncertain, never a refusal.
                    if not self.scoped:
                        db.rollback()

        if self.guard:
            with self.guard.locked():
                token = self.guard.running(run["id"])
                if token is None:
                    with self.read() as db:
                        row = db.execute("SELECT status FROM runs WHERE id=?", (run["id"],)).fetchone()
                    if row is None or row["status"] != "running":
                        raise DispatchSuperseded("The claimed run is no longer current.")
                    raise GuardPaused("missing_run_intent")
                return token.launch(current_launch)
        return current_launch()

    def finish(self, run, result=None, publication=None, *, claimed=False):
        if self._assessment:
            raise ValueError("Assessment study, execution and recovery operations are unsupported.")
        try:
            return self._finish(run, result, publication, claimed=claimed)
        except DispatchSuperseded:
            if self.guard:
                token = self.guard.running(run["id"])
                if token:
                    token.fail()
                if self.guard.publication:
                    raise
            # Another authority already settled or interrupted the run. Do not
            # replace its files or accounting using this worker's stale copy.
            return
        except BaseException:
            if self.guard:
                token = self.guard.running(run["id"])
                if token:
                    token.fail()
            raise

    def _finish(self, run, result=None, publication=None, *, claimed=False):
        with self.transaction(essential=True, kind="settle", resource_id=run["id"], request=run) as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run["id"],)).fetchone()
            if claimed and (row is None or row["status"] != "running" or row["project_id"] != run["project_id"]):
                raise DispatchSuperseded("The claimed run is no longer current.")
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
                payload = self.serialized(run, "run")
                result_payload = self.serialized(result, "result").encode() if result is not None else None
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
                    if self.scoped:
                        db.complete(run_token, authority=db.operation)
                    else:
                        run_token.complete_sql(db.raw, authority=db.operation if self.guard.publication else None)
                elif run_token:
                    run_token.fail("unresolved_compute")
                # Commit visibility before releasing the local identity guard.
                # The transaction context's subsequent commit has no changes.
                db.commit()
                if run_token:
                    run_token.release()

    def snapshot(self, project_id, subject):
        if self._assessment:
            raise ValueError("Assessment study, execution and recovery operations are unsupported.")
        if self.profile_version in (2, 3, 4):
            raise ValueError("This profile requires the complete authorized export operation.")
        with self.read() as db:
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

    def export_bundle(self, project_id, subject, token):
        if self._assessment:
            raise ValueError("Assessment study, execution and recovery operations are unsupported.")
        if self.profile_version not in (2, 3, 4):
            raise ValueError("Bounded export requires its explicit fresh profile.")
        from .bounded_export import export_bundle
        return export_bundle(self, project_id, subject, token)
