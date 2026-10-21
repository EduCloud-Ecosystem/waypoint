"""Scoped SQLite ownership for the explicitly bound v4 profile only.

Local cleanup acknowledgement is never global quiescence or storage capacity.
No checkpoints, retries, journal repair, pooling or automatic hold reset.
"""
from contextlib import contextmanager
from pathlib import Path
import math
import re
import sqlite3
import sys
import threading

from . import finite_authority as finite
from . import assessment_state as astate
from . import assessment_execution_state as ex2

_registry_lock = threading.Lock()
_registry = {}
_local = threading.local()


class OwnershipFailure(RuntimeError):
    def __init__(self):
        super().__init__("SQLite ownership is unconfirmed. This process is held; the requested action was not confirmed. Existing access and obligations remain subject to their recorded state.")
        self.code = "sqlite_ownership_unconfirmed"


class ReadBounds(ValueError):
    pass


class State:
    def __init__(self):
        self.failed = False
        self.quarantined = []


def state(root):
    key = str(Path(root).resolve())
    with _registry_lock:
        return _registry.setdefault(key, State())


def held(root):
    with _registry_lock:
        value = _registry.get(str(Path(root).resolve()))
        return bool(value and value.failed)


def hold(root, owner=None, error=None):
    _end_diagnostic_attempt(root)
    value = state(root)
    with _registry_lock:
        value.failed = True
        if owner is not None:
            owner._uncertain = True
            if error is not None and owner._cleanup_error is None:
                owner._cleanup_error = error
            if owner not in value.quarantined:
                value.quarantined.append(owner)


def available(root):
    if held(root):
        raise OwnershipFailure()


def _event(root, role, boundary):
    """Instrumentation seam; no authority or count-based permission."""


@contextmanager
def diagnostic(token):
    # Consumed before ANY diagnostic open, once per token, even if setup fails.
    with _registry_lock:
        if getattr(token, "_ownership_diagnostic_used", False) or getattr(_local, "diagnostic", None) is not None:
            raise OwnershipFailure()
        token._ownership_diagnostic_used = True
    _local.diagnostic = token
    try:
        yield
        if getattr(token,"_ownership_diagnostic_failed",False):
            raise OwnershipFailure()
    except BaseException:
        token._ownership_diagnostic_failed = True
        raise
    finally:
        _local.diagnostic = None


def _end_diagnostic_attempt(root):
    token = getattr(_local,"diagnostic",None)
    if token is not None and token.guard.root == Path(root).resolve():
        token._ownership_diagnostic_failed = True


def _permit(root, role, guard):
    token = getattr(_local, "diagnostic", None)
    if token is not None:
        if (getattr(token,"_ownership_diagnostic_failed",False)
                or token.guard is not guard or str(token.guard.root) != str(root)
                or role not in {"guard-proof", "memory-reference"}):
            raise OwnershipFailure()
    elif held(root):
        raise OwnershipFailure()


class Row(tuple):
    def __new__(cls, names, values):
        row = super().__new__(cls, values)
        row._names = tuple(names)
        return row

    def keys(self):
        return self._names

    def __getitem__(self, key):
        return super().__getitem__(self._names.index(key) if isinstance(key, str) else key)


class Values:
    """Detached values, never a database cursor or an owner reference."""
    def __init__(self, rows=(), rowcount=-1):
        self._rows, self._index, self.rowcount = tuple(rows), 0, rowcount

    def fetchone(self):
        if self._index == len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def fetchmany(self, size=1):
        if type(size) is not int or size < 0:
            raise ValueError("Use a bounded result size.")
        result = list(self._rows[self._index:self._index + size])
        self._index += len(result)
        return result

    def fetchall(self):
        return self.fetchmany(len(self._rows))

    def __iter__(self):
        return iter(self.fetchall())


# Application schema fields. Limits apply before payload transfer into Python.
_TEXT = {"id":80, "subject":80, "project_id":80, "role":8, "key_hash":64,
         "token_hash":64, "status":32, "request_key":80, "request_hash":64,
         "authority_id":32, "barrier":32, "digest":64, "outcome":16,
         "type":8, "name":80, "tbl_name":80, "sql":8192}
_NULLABLE = {"request_key", "request_hash", "sql"}
_INTS = {"rowid", "active", "cancel_requested", "sequence", "essential"}
_PRAGMAS = {"PRAGMA application_id", "PRAGMA user_version", "PRAGMA journal_mode",
            "PRAGMA synchronous", "PRAGMA foreign_keys", "PRAGMA query_only", "PRAGMA quick_check"}
_TABLES = set(finite.ROW_LIMITS) - {"sessions_per_subject"}
_TABLES |= {"operation_authority", "operation_receipts", "sqlite_master"}
_SCHEMA_SELECT = "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name"
_AUTHORITY_SELECT = "SELECT authority_id,sequence FROM operation_authority"
_RECEIPTS_SELECT = "SELECT id,sequence,digest,outcome,essential FROM operation_receipts"
_QUEUED_SELECT = "SELECT id FROM runs WHERE status='queued' LIMIT 21"
_PROOF_READS = {_SCHEMA_SELECT, _AUTHORITY_SELECT}
_ROLE_READS = {
    "discovery": _PROOF_READS,
    "guard-proof": _PROOF_READS | {_RECEIPTS_SELECT},
    "memory-reference": {_SCHEMA_SELECT},
    "barrier": _PROOF_READS,
    "started-receipt": _PROOF_READS,
    "backup-source": _PROOF_READS,
    "archive-rejection": set(),
    "backup-destination": set(),
}
_ROLE_WRITES = {
    "barrier": {"UPDATE operation_authority SET barrier=? WHERE authority_id=?"},
    "started-receipt": {
        "DELETE FROM operation_receipts WHERE essential=1",
        "INSERT INTO operation_receipts VALUES(?,?,?,?,?)",
        "UPDATE operation_authority SET sequence=? WHERE authority_id=?",
    },
}


def _ex2_writes(role):
    """One effective allowlist used at both EX2 statement gates."""
    writes = set(_ROLE_WRITES.get(role, ()))
    writes |= {sql for sql in (*ex2.SQL_MUTATIONS.values(),
               *ex2.COMPILED["auth_mutations"], *ex2.COMPILED["fixture_seed_sql"])
               if ex2.compiled_write(role, sql)}
    completion = "UPDATE operation_receipts SET outcome=? WHERE id=? AND digest=? AND outcome='started'"
    if role.startswith("ex2-") and not ex2.ROLE_BINDINGS[role].get("readonly"):
        writes.add(completion)
    if role == "ex2-test-initialize":
        writes |= _ROLE_WRITES["started-receipt"]
    return writes

# These complete, deterministic statements share a validated transaction with
# their metadata preflight. Caller-supplied expressions are never evaluated.
_SELECTS = frozenset({
    _SCHEMA_SELECT, _AUTHORITY_SELECT, _RECEIPTS_SELECT, _QUEUED_SELECT,
    "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name",
    "SELECT 1 FROM users WHERE subject=?",
    "SELECT 1 FROM users WHERE subject=? AND active=1",
    "SELECT subject FROM users",
    "SELECT subject FROM users ORDER BY subject",
    "SELECT subject FROM users WHERE key_hash=? AND active=1",
    "SELECT s.subject FROM sessions s JOIN users u ON u.subject=s.subject WHERE s.token_hash=? AND s.expires>? AND u.active=1",
    "SELECT p.payload,m.role FROM projects p JOIN members m ON m.project_id=p.id JOIN users u ON u.subject=m.subject WHERE p.id=? AND m.subject=? AND u.active=1",
    "SELECT p.payload FROM projects p JOIN members m ON m.project_id=p.id JOIN users u ON u.subject=m.subject WHERE m.subject=? AND u.active=1 ORDER BY p.rowid DESC",
    "SELECT payload FROM projects",
    "SELECT payload FROM projects WHERE id=?",
    "SELECT subject,role FROM members WHERE project_id=? ORDER BY subject",
    "SELECT role FROM members WHERE project_id=? AND subject=?",
    "SELECT payload FROM runs WHERE project_id=? ORDER BY rowid DESC",
    "SELECT payload FROM runs WHERE project_id=? ORDER BY rowid",
    "SELECT payload FROM runs ORDER BY rowid",
    "SELECT request_hash,payload FROM runs WHERE project_id=? AND subject=? AND request_key=?",
    "SELECT count(*) FROM runs WHERE project_id=?",
    "SELECT count(*) FROM runs WHERE status IN ('queued','running')",
    "SELECT count(*) FROM sessions WHERE expires>?",
    "SELECT count(*) FROM sessions WHERE expires>? AND subject=?",
    "SELECT payload FROM runs WHERE id=? AND project_id=?",
    "SELECT id,payload FROM runs WHERE status='queued'",
    "SELECT id,payload FROM runs WHERE status='running'",
    "SELECT id,payload,status FROM runs WHERE status IN ('queued','running')",
    "SELECT id,subject FROM runs WHERE status='queued' ORDER BY rowid LIMIT 1",
    "SELECT * FROM runs WHERE status='queued' ORDER BY rowid",
    "SELECT * FROM runs WHERE id=?",
    "SELECT status FROM runs WHERE id=?",
} | {"SELECT count(*) FROM " + table for table in _TABLES - {"sqlite_master"}}
  | {"SELECT rowid,* FROM " + table + " ORDER BY rowid" for table in _TABLES - {"sqlite_master"}})


def _export_selects():
    # Exact bounded_export._capture projections, predicates and limits. No
    # externally supplied SQL, field specification or expression enters here.
    specs = (
        ("sessions", "token_hash=?", ("token_hash","subject","expires"), 1),
        ("users", "subject=?", ("subject","key_hash","active"), 1),
        ("members", "project_id=? AND subject=?", ("project_id","subject","role"), 1),
        ("projects", "id=?", ("id","payload"), 1),
        ("runs", "project_id=?", ("id","project_id","subject","status","payload","cancel_requested","request_key","request_hash"), 100),
    )
    statements = set()
    for table, where, names, limit in specs:
        checks = []
        for name in names:
            kinds, maximum, nullable = _field(name, table)
            check = "typeof(" + name + ") IN (" + ",".join("'" + kind + "'" for kind in kinds) + ")"
            if maximum is not None:
                check += f" AND length(CAST({name} AS BLOB)) <= {maximum}"
            checks.append("(" + check + (f" OR {name} IS NULL)" if nullable else ")"))
        suffix = f" FROM {table} WHERE {where} ORDER BY rowid LIMIT {limit + 1}"
        statements.add("SELECT " + " AND ".join(checks) + suffix)
        statements.add("SELECT " + ",".join(names) + suffix)
    return statements


def _statement(sql):
    if type(sql) is not str:
        raise ValueError("SQLite statements require plain string values.")
    return sql


def _schema_allowed(role, sql, *, script, assessment=False, execution=False):
    from .storage_inventory import SCHEMA_SQL
    from .operation_guard import SCHEMA
    if type(sql) is not str:
        return False
    if assessment:
        allowed = [";".join(astate.schema())] if script and role in {"initialize", "memory-reference"} else ()
        key = lambda text: "".join(text.split()).upper().rstrip(";")
        return key(sql) in {key(statement) for statement in allowed}
    if execution:
        allowed = [";".join(ex2.schema())] if script and role in {"ex2-test-initialize", "memory-reference"} else ()
        key = lambda text: "".join(text.split()).upper().rstrip(";")
        return key(sql) in {key(statement) for statement in allowed}
    if script:
        allowed = ([";".join(SCHEMA_SQL)] if role == "initialize" else
                   [";".join(SCHEMA_SQL + SCHEMA)] if role == "memory-reference" else [])
    else:
        allowed = SCHEMA if role == "initialize" else ()
    key = lambda text: "".join(text.split()).upper().rstrip(";")
    return key(sql) in {key(statement) for statement in allowed}


def _query_limit(sql, assessment=False, execution=False):
    match = re.search(r"\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)", sql, re.I)
    table = match.group(1).lower() if match else None
    if execution:
        if sql in ex2.READ_LIMITS:
            return ex2.READ_LIMITS[sql], table
        if sql == _RECEIPTS_SELECT:
            return 97, "operation_receipts"
        if sql in _PROOF_READS:
            return (1 if sql == _AUTHORITY_SELECT else 100), table
        raise ReadBounds("Unregistered EX2 observation.")
    if assessment:
        special = {astate.BY_ID: (1, "assessment_sessions"), astate.BY_KEY: (1, "assessment_sessions"),
                   astate.ALL_META: (16, "assessment_sessions"), astate.SUBJECT_META: (4, "assessment_sessions"),
                   astate.COUNT: (1, "assessment_sessions"), astate.SUBJECT_COUNT: (1, "assessment_sessions"),
                   astate.AUTH_SESSION: (1, "sessions"), astate.LOGOUT_SUBJECT: (1, "sessions"),
                   _RECEIPTS_SELECT: (129, "operation_receipts")}
        if sql in special:
            return special[sql]
    if table not in _TABLES:
        raise ValueError("Unregistered observation table.")
    if re.match(r"SELECT\s+count\(\*\)", sql, re.I):
        return 1, table
    limit = {**finite.ROW_LIMITS, "operation_authority":1, "operation_receipts":4097,
             "sqlite_master":100}[table]
    if table == "runs":
        if re.search(r"\bstatus\s*=\s*'(queued|running)'", sql, re.I):
            limit = 20
        elif re.search(r"\bproject_id\s*=", sql, re.I):
            limit = 100
    return limit, table


def _field(name, table):
    if table in ex2.FIELDSETS and name in ex2.FIELDSETS[table]:
        spec = ex2.FIELDSETS[table][name]
        return (spec["sql"].lower(),), spec.get("max_ascii_bytes"), spec["nullable"]
    if table == "assessment_sessions":
        if name in astate.TEXT:
            return ("text",), astate.TEXT[name], False
        if name in astate.INTS:
            return ("integer",), None, name == "last_scope_generation"
    if name == "payload":
        return ("text",), finite.ENCODED_LIMITS["run" if table == "runs" else "project"], False
    if name in _TEXT:
        return ("text",), _TEXT[name], name in _NULLABLE
    if name == "expires":
        return ("integer", "real"), None, False
    if name in _INTS or name == "1" or name.lower() == "count(*)":
        return ("integer",), None, False
    # Export's preflight SELECT projects a single boolean expression. Its
    # independent type/length checks are evaluated by SQLite, not text returned.
    if name.startswith("(") or name.startswith("typeof("):
        return ("integer",), None, False
    raise ValueError("Unregistered observation field.")


class Owner:
    """Private scoped capability. Public Store factories never return this open."""
    scoped_sqlite = True

    def __init__(self, root, role, *, guard=None, path=None, readonly=False,
                 initialize=False, memory=False, archive=False, validate=True):
        self.root, self.role = Path(root).resolve(), role
        self.guard, self.path = guard, Path(path) if path is not None else self.root / "workspace.sqlite3"
        self.readonly, self.initialize, self.memory, self.archive = readonly, initialize, memory, archive
        self.validate = validate
        self.assessment = astate.active(guard)
        self.execution = ex2.active(guard)
        self._ex2_full_read = False
        self.__connection = None
        self.__cursors = []
        self._guard_scope = None
        self._active = False
        self._thread = threading.get_ident()
        self._entry_lock = threading.Lock()
        self._entered = False
        self._read_ended = False
        self._reservation_ended = False
        self._immediate = False
        self._freeze_owner = None
        self._rollback_attempted = False
        self._close_attempted = False
        self._validated = False
        self._uncertain = False
        self._cleanup_error = None
        self._stacked = False
        readers = {"discovery", "online-read", "guard-proof", "backup-source", "archive-rejection"}
        writers = {"initialize", "mutation", "started-receipt", "barrier", "dispatch-reservation", "offline-freeze", "backup-destination"}
        if self.assessment:
            allowed = {"discovery", "guard-proof", "memory-reference", "barrier", "started-receipt",
                       "initialize", "assessment-read", "assessment-write", "assessment-spawn"}
            if role not in allowed:
                raise ValueError("Unsupported assessment SQLite role.")
            readers |= {"assessment-read"}
            writers |= {"assessment-write", "assessment-spawn"}
        if self.execution:
            allowed = {"discovery", "guard-proof", "memory-reference", "barrier", "started-receipt"} | {
                name for name in ex2.ROLE_BINDINGS if name.startswith("ex2-")}
            if role not in allowed:
                raise ValueError("Unsupported EX2 SQLite role.")
            readers |= {name for name in allowed if name.startswith("ex2-") and ex2.ROLE_BINDINGS[name].get("readonly")}
            writers |= {name for name in allowed if name.startswith("ex2-") and not ex2.ROLE_BINDINGS[name].get("readonly")}
        if role not in readers | writers | {"memory-reference"}:
            raise ValueError("Unknown SQLite ownership role.")
        if readonly != (role in readers) or memory != (role == "memory-reference"):
            raise ValueError("SQLite role and opening mode differ.")
        if initialize != (role in {"initialize", "memory-reference", "backup-destination",
                                   "ex2-test-initialize"}):
            raise ValueError("Only fresh owners may create a database.")
        if archive != (role == "archive-rejection") or (guard is None and not archive):
            raise ValueError("SQLite owner requires its bound guard or offline archive role.")
        if path is not None and role != "backup-destination":
            raise ValueError("Only a backup destination may override the database path.")
        if role == "backup-destination" and path is None:
            raise ValueError("A backup destination requires its fresh explicit path.")
        if guard is not None and Path(guard.root).resolve() != self.root:
            raise ValueError("The SQLite owner and guard must have the same resolved root.")
        if type(validate) is not bool or (not validate and role not in {"initialize","memory-reference",
                "backup-destination","archive-rejection","ex2-test-initialize"}):
            raise ValueError("Existing authority owners require binding and schema validation.")

    def _check(self):
        if not self._active or self._thread != threading.get_ident() or self._read_ended or self._reservation_ended:
            raise RuntimeError("SQLite scope has ended or belongs to another thread.")
        if self._uncertain:
            raise self._cleanup_error or OwnershipFailure()
        _permit(self.root, self.role, self.guard)

    def _dependents(self):
        owners = getattr(_local,"owners",[])
        if self not in owners:
            return []
        return [owner for owner in owners[owners.index(self)+1:] if owner.root == self.root]

    def _require_freeze(self):
        self._check()
        if self.role != "offline-freeze" or not self._validated or not self._immediate:
            raise ValueError("Backup requires a validated immediate freeze reservation.")
        try:
            live = self.__connection.in_transaction
        except BaseException as failure:
            hold(self.root,self,failure)
            raise
        if not live:
            self._reservation_ended = True
            raise ValueError("The backup freeze reservation has ended.")

    @contextmanager
    def _cursor(self):
        try:
            cursor = self.__connection.cursor()
        except BaseException as failure:
            hold(self.root,self,failure)
            raise
        self.__cursors.append(cursor)
        try:
            yield cursor
        except BaseException:
            _end_diagnostic_attempt(self.root)
            raise
        finally:
            original = sys.exception()
            try:
                cursor.close()
            except BaseException as failure:
                hold(self.root, self, failure)
                if original is None:
                    raise
                original.add_note("An owned SQLite cursor close also failed: " + type(failure).__name__)
            else:
                self.__cursors.remove(cursor)

    def _control(self, sql):
        with self._cursor() as cursor:
            cursor.execute(sql)
            return cursor.fetchall()

    def __enter__(self):
        # Reject before replacing fields, acquiring the guard or opening SQL.
        # One owner belongs to its creating thread and is entered only once.
        with self._entry_lock:
            if self._thread != threading.get_ident() or self._entered:
                raise RuntimeError("SQLite owners are single-use and belong to their creating thread.")
            self._entered = True
        if self.guard is not None:
            self._guard_scope = self.guard.locked()
            self._guard_scope.__enter__()
        try:
            _permit(self.root, self.role, self.guard)
            parents = [x for x in getattr(_local,"owners",[]) if x.root == self.root]
            if self.role in {"backup-source","backup-destination"}:
                freezes = [owner for owner in parents if owner.role == "offline-freeze"]
                if len(freezes) != 1:
                    raise ValueError("Backup owners require their live parent freeze.")
                freezes[0]._require_freeze()
                self._freeze_owner = freezes[0]
            if parents and self.role not in {"memory-reference", "guard-proof"}:
                offline = self._freeze_owner is not None
                if not offline:
                    raise ValueError("End the current SQLite scope before opening another business or barrier owner.")
            _event(self.root, self.role, "before-open")
            try:
                if self.initialize and not self.memory and self.path.exists():
                    raise ValueError("A newly initialized database must be fresh.")
                if self.memory:
                    self.__connection = sqlite3.connect(":memory:", timeout=10)
                else:
                    mode = "rwc" if self.initialize else "ro" if self.readonly else "rw"
                    uri = self.path.as_uri() + "?mode=" + mode + ("&immutable=1" if self.archive else "")
                    self.__connection = sqlite3.connect(uri, uri=True, timeout=10)
            except BaseException:
                hold(self.root, self)
                raise
            self._active = True
            if not hasattr(_local,"owners"):
                _local.owners=[]
            _local.owners.append(self)
            self._stacked=True
            _event(self.root, self.role, "opened")
            if self.memory or self.role == "backup-destination":
                return self
            self._control("PRAGMA foreign_keys=ON")
            if self.readonly:
                self._control("PRAGMA query_only=ON")
                self._control("BEGIN")
                self._validate()
            else:
                if self.initialize:
                    self._control("PRAGMA journal_mode=WAL")
                elif self._control("PRAGMA journal_mode") != [("wal",)]:
                    raise finite.InvalidProfile("The scoped profile requires its existing WAL journal.")
                self._control("PRAGMA synchronous=FULL")
            return self
        except BaseException:
            _end_diagnostic_attempt(self.root)
            self.__exit__(*sys.exc_info())
            raise

    def _validate(self):
        if self.validate and not self.initialize and not self.memory:
            (ex2.validate_binding if self.execution else astate.validate_binding if self.assessment
             else finite.validate_binding)(self.root, self.guard.binding, self)
            self.guard.load()
            validate_schema(self)
        self._validated = True

    def execute(self, sql, parameters=()):
        sql = _statement(sql)
        self._check()
        normalized = " ".join(sql.split())
        if self.role in {"offline-freeze","dispatch-reservation"} and normalized == "BEGIN":
            raise ValueError("This reservation requires BEGIN IMMEDIATE.")
        if self.role in _ROLE_READS or self.execution and self.role.startswith("ex2-"):
            reads = _ROLE_READS.get(self.role, set())
            if self.execution:
                reads = reads | {sql for sql in ex2.SQL_READS.values() if ex2.compiled_read(self.role, sql)}
                # These existing internal probes validate this very Owner.
                reads = reads | _PROOF_READS
            queued = (self.role == "guard-proof" and getattr(_local,"diagnostic",None) is None
                      and normalized == _QUEUED_SELECT)
            controls = {"PRAGMA application_id", "PRAGMA user_version", "BEGIN", "BEGIN IMMEDIATE"}
            if self.execution and self.initialize and self.role == "ex2-test-initialize":
                from .operation_guard import APPLICATION_ID
                controls |= {f"PRAGMA application_id={APPLICATION_ID}", f"PRAGMA user_version={ex2.MARKER}"}
            writes = _ex2_writes(self.role) if self.execution else _ROLE_WRITES.get(self.role, set())
            if normalized not in reads | writes | controls and not queued:
                raise ValueError("This owner accepts only its registered role statements.")
        if normalized in {"BEGIN", "BEGIN IMMEDIATE"}:
            if self.readonly:
                if normalized != "BEGIN" or not self.__connection.in_transaction:
                    raise ValueError("Unsupported read transaction control.")
            else:
                self._control(normalized)
                self._rollback_attempted = False
                self._validate()
                self._immediate = normalized == "BEGIN IMMEDIATE"
            return Values()
        if (not self.readonly and not self.initialize and not self.memory
                and not self.__connection.in_transaction):
            self._control("BEGIN IMMEDIATE")
            self._rollback_attempted = False
            self._validate()
            self._immediate = True
        if normalized in _PRAGMAS:
            with self._cursor() as cursor:
                cursor.execute(sql, parameters)
                values = cursor.fetchmany(2)
                if len(values) != 1 or len(values[0]) != 1 or (isinstance(values[0][0],str) and len(values[0][0].encode()) > 128):
                    raise ReadBounds("Unsupported metadata result.")
                result = Values([Row([cursor.description[0][0]], values[0])])
            return result
        if re.fullmatch(r"PRAGMA (application_id|user_version)=\d+", normalized) and self.initialize:
            self._control(normalized)
            return Values()
        if normalized.startswith("SELECT ") and ";" not in normalized:
            return self._select(sql, parameters)
        if self.readonly or self.role in {"offline-freeze", "dispatch-reservation", "backup-destination"}:
            raise ValueError("This scope accepts reviewed observations only.")
        if not self.initialize and not self._validated:
            raise ValueError("Begin and validate the owned write transaction first.")
        if self.execution:
            if normalized not in _ex2_writes(self.role):
                raise ValueError("Unregistered EX2 mutation.")
        elif self.assessment:
            allowed = astate.GUARD_WRITES | astate.AUTH_WRITES | {astate.INSERT, astate.UPDATE}
            if self.role == "assessment-spawn":
                allowed = astate.GUARD_WRITES
            if self.initialize:
                allowed |= {"INSERT INTO projects VALUES(?,?)", "INSERT INTO members VALUES(?,?,?)",
                            "INSERT INTO operation_authority VALUES(?,0,?)"}
            if normalized not in allowed and not _schema_allowed(self.role, sql, script=False, assessment=True):
                raise ValueError("Unregistered assessment mutation.")
        elif not (re.match(r"(?:INSERT INTO|UPDATE|DELETE FROM) (?:users|sessions|projects|members|runs|operation_authority|operation_receipts)\b", normalized)
                or _schema_allowed(self.role,sql,script=False)):
            raise ValueError("Unregistered mutation statement.")
        with self._cursor() as cursor:
            try:
                cursor.execute(sql, parameters)
            except BaseException as failure:
                hold(self.root,self,failure)
                raise
            result = Values(rowcount=cursor.rowcount)
        return result

    def _select(self, sql, parameters):
        allowed = _SELECTS | _export_selects()
        if self.execution:
            allowed = set(ex2.SQL_READS.values()) | _PROOF_READS | {_RECEIPTS_SELECT}
        elif self.assessment:
            allowed = {q for q in _SELECTS if not re.search(r"\bruns\b", q) and not q.startswith("SELECT rowid,*")}
            allowed |= {astate.BY_ID, astate.BY_KEY, astate.ALL_META, astate.SUBJECT_META,
                        astate.COUNT, astate.SUBJECT_COUNT, astate.AUTH_SESSION, astate.LOGOUT_SUBJECT}
        if " ".join(sql.split()) not in allowed:
            raise ReadBounds("Unregistered observation projection or predicate.")
        if type(parameters) not in (tuple, list):
            raise ReadBounds("Observation parameters must be copied scalar bindings.")
        parameters = tuple(parameters)
        if any(type(value) not in (str, bytes, int, float, type(None))
               or type(value) is float and not math.isfinite(value) for value in parameters):
            raise ReadBounds("Observation parameters must be copied scalar bindings.")
        if self.execution and sql in ex2.READ_LIMITS and ("_PREFLIGHT" in next(
                name for name, text in ex2.SQL_READS.items() if text == sql)
                or sql in {ex2.SQL_READS[name] for name in ("PLAN_INVARIANTS", "SUBJECT_CAP",
                           "CROSS_ID_COLLISION", "AUTH_SHAPE")}
                or re.match(r"SELECT count\(\*\)", sql, re.I)):
            self._ex2_preflight()
            limit = ex2.READ_LIMITS[sql]
            with self._cursor() as cursor:
                cursor.execute(sql, parameters)
                rows = cursor.fetchmany(limit + 1)
                if len(rows) > limit or any(any(type(value) not in (int, str) or
                        type(value) is str and (not value.isascii() or len(value) > 80) or
                        type(value) is int and not 0 <= value <= ex2.M for value in row) for row in rows):
                    raise ReadBounds("EX2 scalar observation exceeds its bound.")
                names = [column[0] for column in cursor.description]
            _event(self.root, self.role, "materialized")
            return Values([Row(names, row) for row in rows])
        limit, table = _query_limit(sql, self.assessment, self.execution)
        if self.execution and sql == _RECEIPTS_SELECT:
            self._ex2_receipts_preflight()
        if self.execution and sql in ex2.READ_LIMITS:
            self._ex2_preflight()
            if sql in {ex2.SQL_READS["SCOPE_FULL_BY_ID"], ex2.SQL_READS["SLOT_FULL_BY_ID"]}:
                if self._ex2_full_read:
                    raise ReadBounds("Only one EX2 full row per snapshot.")
                self._ex2_full_read = True
        if self.assessment and table == "assessment_sessions":
            self._assessment_preflight()
        if self.archive:
            raise ValueError("Archive rejection only observes format markers.")
        with self._cursor() as cursor:
            cursor.execute(sql, parameters)
            names = [column[0] for column in cursor.description]
            if len({name.casefold() for name in names}) != len(names):
                raise ReadBounds("Ambiguous SQLite projection names are not permitted.")
            checks = []
            for name in names:
                kinds, maximum, nullable = _field(name, table)
                quoted = '"' + name.replace('"','""') + '"'
                check = "typeof(" + quoted + ") IN (" + ",".join("'"+kind+"'" for kind in kinds) + ")"
                if maximum is not None:
                    check += f" AND length(CAST({quoted} AS BLOB)) <= {maximum}"
                checks.append("(" + check + (f" OR {quoted} IS NULL)" if nullable else ")"))
            with self._cursor() as sizes:
                sizes.execute("SELECT " + " AND ".join(checks) + " FROM (" + sql + f") LIMIT {limit+1}", parameters)
                metadata = sizes.fetchmany(limit+1)
                if len(metadata)>limit or any(row[0] != 1 for row in metadata):
                    raise ReadBounds("SQLite observation exceeds its row, type or field bounds.")
            rows = cursor.fetchmany(limit+1)
            if len(rows)>limit:
                raise ReadBounds("SQLite observation exceeds its row bound.")
            for values in rows:
                for name,value in zip(names,values):
                    if isinstance(value,float) and not math.isfinite(value):
                        raise ReadBounds("Nonfinite SQLite observation.")
                    if name in {"active","essential","cancel_requested"} and value not in (0,1):
                        raise ReadBounds("Invalid SQLite flag.")
                    if name == "sequence" and not 0 <= value <= finite.COUNTER_LIMIT:
                        raise ReadBounds("Invalid SQLite sequence.")
            if self.assessment and table == "assessment_sessions" and names != ["count(*)"]:
                for values in rows:
                    astate.validate_row(dict(zip(names, values)), metadata="manifest_json" not in names)
            if self.execution and table in ex2.FIELDSETS and names and set(names) <= set(ex2.FIELDSETS[table]):
                for values in rows:
                    ex2.validate_row(table, dict(zip(names, values)), metadata=(
                        "manifest_json" not in names and "metrics_json" not in names))
            result = Values([Row(names,values) for values in rows])
        _event(self.root, self.role, "materialized")
        return result

    def _assessment_preflight(self):
        # Same owned snapshot, scalar metadata only; no manifest transfer.
        with self._cursor() as cursor:
            cursor.execute("SELECT count(*) FROM assessment_sessions")
            count = cursor.fetchone()[0]
            if not 0 <= count <= astate.LIMITS["rows"]:
                raise ReadBounds("Assessment row limit.")
            cursor.execute("SELECT count(*) FROM (SELECT subject FROM assessment_sessions GROUP BY subject HAVING count(*)>4)")
            if cursor.fetchone()[0]:
                raise ReadBounds("Assessment subject row limit.")
            checks, lengths = [], []
            for name in astate.FIELDS:
                kinds, maximum, nullable = _field(name, "assessment_sessions")
                check = "typeof("+name+")='"+kinds[0]+"'"
                if maximum is not None:
                    check += f" AND length(CAST({name} AS BLOB))<={maximum} AND length(CAST({name} AS BLOB))=length({name})"
                if name in astate.INTS:
                    check += f" AND {name}>=0 AND {name}<={astate.M}"
                checks.append("("+check+(f" OR {name} IS NULL)" if nullable else ")"))
                lengths.append(f"coalesce(length(CAST({name} AS BLOB)),0)")
            size = "+".join(lengths)
            cursor.execute("SELECT coalesce(max(CASE WHEN "+" AND ".join(checks)+" THEN 0 ELSE 1 END),0),"
                           +f"coalesce(max({size}),0),coalesce(sum({size}),0) FROM assessment_sessions")
            invalid, largest, total = cursor.fetchone()
            if invalid or largest > astate.LIMITS["row_fields"] or total > astate.LIMITS["aggregate_fields"]:
                raise ReadBounds("Assessment field or aggregate limit.")

    def _ex2_preflight(self):
        """Fixed aggregate probes remain in this Owner's current snapshot."""
        total_rows = total_fields = 0
        counts = {}
        for table in ex2.FIELDSETS:
            sql = ex2.SQL_READS[table + "_PREFLIGHT"]
            with self._cursor() as cursor:
                cursor.execute(sql)
                values = cursor.fetchmany(2)
            if (len(values) != 1 or len(values[0]) != 4
                    or any(type(item) is not int or item < 0 for item in values[0])):
                raise ReadBounds("EX2 aggregate preflight malformed.")
            count, invalid, largest, aggregate = values[0]
            if (invalid or count > ex2.CAPACITY[{"ex2_scopes": "scopes", "ex2_slots": "slots_total",
                    "ex2_commands": "commands_total", "ex2_security_facts": "security_facts"}[table]]
                    or largest > ex2.COMPACT["row_bounds"][table]["raw_fields_upper"]):
                raise ReadBounds("EX2 row or field bound.")
            total_rows += count
            total_fields += aggregate
            counts[table] = count
        if total_rows > 148 or total_fields > ex2.COMPACT["raw_profile_aggregate_upper"]:
            raise ReadBounds("EX2 profile aggregate bound.")
        if counts["ex2_security_facts"] != 12:
            raise ReadBounds("EX2 fixture security facts are incomplete.")
        with self._cursor() as cursor:
            cursor.execute(ex2.SQL_READS["PLAN_INVARIANTS"])
            plans = cursor.fetchmany(3)
            cursor.execute(ex2.SQL_READS["CROSS_ID_COLLISION"])
            collisions = cursor.fetchmany(2)
        if (len(plans) != counts["ex2_scopes"] or len(plans) > 2
                or any(len(row) != 7 or type(row[0]) is not str or
                       not re.fullmatch(r"[a-f0-9]{32}", row[0]) or
                       any(type(item) is not int for item in row[1:]) or
                       not 1 <= row[1] <= 12 or row[2] != 0 or row[3] != row[1] - 1 or
                       any(item != row[1] for item in row[4:]) for row in plans)
                or len(collisions) != 1 or collisions[0] != (0,)):
            raise ReadBounds("EX2 immutable plan invariant.")
        planned = {row[0]: row[1] for row in plans}
        with self._cursor() as cursor:
            cursor.execute("SELECT scope_id,sum(CASE WHEN lane='P' THEN 1 ELSE 0 END),"
                           "sum(CASE WHEN lane='S' THEN 1 ELSE 0 END) FROM ex2_commands "
                           "GROUP BY scope_id ORDER BY scope_id")
            commands = cursor.fetchmany(3)
            cursor.execute("SELECT count(*) FROM (SELECT scope_id,lane FROM ex2_commands "
                           "WHERE state IN ('pending','triggered') GROUP BY scope_id,lane HAVING count(*)>1)")
            pending = cursor.fetchmany(2)
        if (len(commands) != len(planned) or len(commands) > 2
                or any(row[0] not in planned or row[1] != 3 + planned[row[0]]
                       or row[2] != 4 + 3 * planned[row[0]] for row in commands)
                or pending != [(0,)]):
            raise ReadBounds("EX2 fixed command opportunity invariant.")
        with self._cursor() as cursor:
            cursor.execute(ex2.SQL_READS["AUTH_SHAPE"])
            shape = cursor.fetchmany(2)
        if (len(shape) != 1 or len(shape[0]) != 5
                or any(type(value) is not int or value < 0 for value in shape[0])
                or any(actual > maximum for actual, maximum in zip(shape[0], (2, 2, 4, 2, 0)))):
            raise ReadBounds("EX2 base authority row bound.")
        receipts = self._ex2_receipts_preflight()
        if total_rows + sum(shape[0]) + receipts + 1 > 256:
            raise ReadBounds("EX2 total data row bound.")

    def _ex2_receipts_preflight(self):
        with self._cursor() as cursor:
            cursor.execute("SELECT count(*),coalesce(max(length(CAST(id AS BLOB))+"
                           "length(CAST(sequence AS TEXT))+length(CAST(digest AS BLOB))+"
                           "length(CAST(outcome AS BLOB))+length(CAST(essential AS TEXT))),0),"
                           "coalesce(sum(length(CAST(id AS BLOB))+length(CAST(sequence AS TEXT))+"
                           "length(CAST(digest AS BLOB))+length(CAST(outcome AS BLOB))+"
                           "length(CAST(essential AS TEXT))),0) FROM operation_receipts")
            values = cursor.fetchmany(2)
        if (len(values) != 1 or len(values[0]) != 3
                or any(type(item) is not int or item < 0 for item in values[0])
                or values[0][0] > 97 or values[0][1] > 125 or values[0][2] > 12125):
            raise ReadBounds("EX2 retained receipt bound.")
        return values[0][0]

    def executescript(self, sql):
        sql = _statement(sql)
        self._check()
        if not _schema_allowed(self.role, sql, script=True, assessment=self.assessment,
                               execution=self.execution):
            raise ValueError("This owner accepts only its exact reviewed schema script.")
        with self._cursor() as cursor:
            try:
                cursor.executescript(sql)
            except BaseException as failure:
                hold(self.root,self,failure)
                raise
        return Values()

    def commit(self):
        self._check()
        if self.readonly or self.role in {"offline-freeze","dispatch-reservation","backup-destination"}:
            raise ValueError("Observation and reservation scopes do not commit writes.")
        try:
            self.__connection.commit()
            self._rollback_attempted = False
            self._immediate = False
            _event(self.root,self.role,"committed")
        except BaseException as failure:
            hold(self.root,self,failure)
            raise

    def rollback(self):
        # Cleanup must still run after a hold; do not consult _permit here.
        if not self._active or self._thread != threading.get_ident():
            raise RuntimeError("SQLite scope has ended.")
        if self._dependents():
            raise RuntimeError("End child SQLite scopes before releasing their parent reservation.")
        if self._rollback_attempted:
            raise OwnershipFailure()
        self._rollback_attempted = True
        if self.readonly:
            self._read_ended = True
        if self.role in {"offline-freeze","dispatch-reservation"}:
            self._reservation_ended = True
        self._immediate = False
        try:
            self.__connection.rollback()
            _event(self.root,self.role,"rolled-back")
        except BaseException as failure:
            hold(self.root,self,failure)
            raise

    def backup_to(self, destination):
        self._check(); destination._check()
        if self.assessment or destination.assessment:
            raise ValueError("Assessment backup is unsupported.")
        if (self.role != "backup-source" or destination.role != "backup-destination"
                or self.root != destination.root or self.guard is not destination.guard):
            raise ValueError("Backup requires distinct owned offline source and destination.")
        if self._freeze_owner is None or self._freeze_owner is not destination._freeze_owner:
            raise ValueError("Backup owners require the same parent freeze reservation.")
        self._freeze_owner._require_freeze()
        try:
            self.__connection.backup(destination.__connection)
            self._freeze_owner._require_freeze()
        except BaseException as failure:
            hold(self.root,self,failure)
            hold(destination.root,destination,failure)
            raise

    def __exit__(self, kind, error, tb):
        if self._thread != threading.get_ident() or not self._entered:
            raise RuntimeError("Only the owning thread may end an entered SQLite scope.")
        if self._dependents():
            raise RuntimeError("End child SQLite scopes before releasing their parent reservation.")
        original = error
        if error is not None:
            _end_diagnostic_attempt(self.root)
        if original is None:
            try:
                if self._uncertain:
                    raise self._cleanup_error or OwnershipFailure()
                _permit(self.root,self.role,self.guard)
            except BaseException as failure:
                original=failure
        if self._close_attempted:
            raise RuntimeError("SQLite owner cleanup was already attempted.")
        self._close_attempted = True
        try:
            if self.__connection is not None:
                try:
                    if self.__connection.in_transaction and not self._rollback_attempted:
                        self.rollback()
                except BaseException as failure:
                    hold(self.root,self)
                    if original is None: original=failure
                    else: original.add_note("SQLite transaction cleanup also failed: " + type(failure).__name__)
                try:
                    self.__connection.close()
                    _event(self.root,self.role,"closed")
                except BaseException as failure:
                    hold(self.root,self)
                    if original is None: original=failure
                    else: original.add_note("SQLite connection close also failed: " + type(failure).__name__)
                else:
                    if not self.__cursors:
                        self.__connection = None
        finally:
            self._active = False
            if self._stacked:
                _local.owners.remove(self)
                self._stacked=False
            if self._guard_scope is not None:
                try:
                    self._guard_scope.__exit__(type(original) if original else None,original,tb)
                except BaseException as failure:
                    hold(self.root,self)
                    if original is None: original=failure
                    else: original.add_note("Guard cleanup also failed: " + type(failure).__name__)
        if error is None and original is not None:
            raise original
        return False


def validate_schema(db):
    from .operation_guard import APPLICATION_ID, SCHEMA, GuardPaused
    from .storage_inventory import SCHEMA_SQL, _schema
    with Owner(db.root,"memory-reference",guard=db.guard,memory=True,initialize=True,validate=False) as expected:
        expected.executescript(";".join(ex2.schema() if db.execution else astate.schema()
                                       if db.assessment else SCHEMA_SQL+SCHEMA))
        if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID or _schema(db) != _schema(expected):
            raise GuardPaused("unsupported_schema")


class Mutation:
    """Business adapter; receipt completion uses its capability, never .raw."""
    def __init__(self, owner, operation, keep=False):
        self.__owner, self.operation, self.keep = owner, operation, keep

    def execute(self, sql, parameters=()):
        sql = _statement(sql)
        self.__owner._check()
        if not sql.startswith("SELECT "):
            self.operation.effect()
        return self.__owner.execute(sql,parameters)

    def commit(self):
        self.__owner._check()
        self.operation.check_publication()
        if not self.keep:
            self.operation.complete_sql(self.__owner)
        self.__owner.commit()

    def complete(self, token, *, authority=None):
        self.__owner._check()
        token.complete_sql(self.__owner,authority=authority)

    def effect(self):
        self.operation.effect()
