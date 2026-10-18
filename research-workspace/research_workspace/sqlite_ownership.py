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
    if getattr(token, "_ownership_diagnostic_used", False) or getattr(_local, "diagnostic", None) is not None:
        raise OwnershipFailure()
    token._ownership_diagnostic_used = True
    _local.diagnostic = token
    try:
        yield
    finally:
        _local.diagnostic = None


def _permit(root, role, guard):
    token = getattr(_local, "diagnostic", None)
    if token is not None:
        if (token.guard is not guard or str(token.guard.root) != str(root)
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


def _schema_allowed(role, sql, *, script):
    from .storage_inventory import SCHEMA_SQL
    from .operation_guard import SCHEMA
    if not isinstance(sql,str):
        return False
    if script:
        allowed = ([";".join(SCHEMA_SQL)] if role == "initialize" else
                   [";".join(SCHEMA_SQL + SCHEMA)] if role == "memory-reference" else [])
    else:
        allowed = SCHEMA if role == "initialize" else ()
    key = lambda text: "".join(text.split()).upper().rstrip(";")
    return key(sql) in {key(statement) for statement in allowed}


def _query_limit(sql):
    match = re.search(r"\bFROM\s+([A-Za-z_]+)", sql, re.I)
    table = match.group(1).lower() if match else None
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
        self.__connection = None
        self.__cursors = []
        self._guard_scope = None
        self._active = False
        self._thread = threading.get_ident()
        self._entry_lock = threading.Lock()
        self._entered = False
        self._rollback_attempted = False
        self._close_attempted = False
        self._validated = False
        self._uncertain = False
        self._cleanup_error = None
        self._stacked = False
        readers = {"discovery", "online-read", "guard-proof", "backup-source", "archive-rejection"}
        writers = {"initialize", "mutation", "started-receipt", "barrier", "dispatch-reservation", "offline-freeze", "backup-destination"}
        if role not in readers | writers | {"memory-reference"}:
            raise ValueError("Unknown SQLite ownership role.")
        if readonly != (role in readers) or memory != (role == "memory-reference"):
            raise ValueError("SQLite role and opening mode differ.")
        if initialize != (role in {"initialize", "memory-reference", "backup-destination"}):
            raise ValueError("Only fresh owners may create a database.")
        if archive != (role == "archive-rejection") or (guard is None and not archive):
            raise ValueError("SQLite owner requires its bound guard or offline archive role.")

    def _check(self):
        if not self._active or self._thread != threading.get_ident():
            raise RuntimeError("SQLite scope has ended or belongs to another thread.")
        _permit(self.root, self.role, self.guard)

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
            if parents and self.role not in {"memory-reference", "guard-proof"}:
                offline = self.role in {"backup-source", "backup-destination"} and any(x.role == "offline-freeze" for x in parents)
                if not offline:
                    raise ValueError("End the current SQLite scope before opening another business or barrier owner.")
            _event(self.root, self.role, "before-open")
            try:
                if self.role == "backup-destination" and self.path.exists():
                    raise ValueError("Backup destination must be fresh.")
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
            self.__exit__(*sys.exc_info())
            raise

    def _validate(self):
        if self.validate and not self.initialize and not self.memory:
            finite.validate_binding(self.root, self.guard.binding, self)
            self.guard.load()
            validate_schema(self)
        self._validated = True

    def execute(self, sql, parameters=()):
        self._check()
        normalized = " ".join(sql.split())
        if self.role in _ROLE_READS:
            reads = _ROLE_READS[self.role]
            queued = (self.role == "guard-proof" and getattr(_local,"diagnostic",None) is None
                      and normalized == _QUEUED_SELECT)
            controls = {"PRAGMA application_id", "PRAGMA user_version", "BEGIN", "BEGIN IMMEDIATE"}
            if normalized not in reads | _ROLE_WRITES.get(self.role,set()) | controls and not queued:
                raise ValueError("This owner accepts only its registered role statements.")
        if normalized in {"BEGIN", "BEGIN IMMEDIATE"}:
            if self.readonly:
                if normalized != "BEGIN" or not self.__connection.in_transaction:
                    raise ValueError("Unsupported read transaction control.")
            else:
                self._control(normalized)
                self._rollback_attempted = False
                self._validate()
            return Values()
        if (not self.readonly and not self.initialize and not self.memory
                and not self.__connection.in_transaction):
            self._control("BEGIN IMMEDIATE")
            self._rollback_attempted = False
            self._validate()
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
        if not (re.match(r"(?:INSERT INTO|UPDATE|DELETE FROM) (?:users|sessions|projects|members|runs|operation_authority|operation_receipts)\b", normalized)
                or _schema_allowed(self.role,sql,script=False)):
            raise ValueError("Unregistered mutation statement.")
        with self._cursor() as cursor:
            cursor.execute(sql, parameters)
            result = Values(rowcount=cursor.rowcount)
        return result

    def _select(self, sql, parameters):
        limit, table = _query_limit(sql)
        if self.archive:
            raise ValueError("Archive rejection only observes format markers.")
        with self._cursor() as cursor:
            cursor.execute(sql, parameters)
            names = [column[0] for column in cursor.description]
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
            result = Values([Row(names,values) for values in rows])
        _event(self.root, self.role, "materialized")
        return result

    def executescript(self, sql):
        self._check()
        if not _schema_allowed(self.role, sql, script=True):
            raise ValueError("This owner accepts only its exact reviewed schema script.")
        with self._cursor() as cursor:
            cursor.executescript(sql)
        return Values()

    def commit(self):
        self._check()
        if self.readonly:
            raise ValueError("Read scopes do not commit writes.")
        self.__connection.commit()
        self._rollback_attempted = False
        _event(self.root,self.role,"committed")

    def rollback(self):
        # Cleanup must still run after a hold; do not consult _permit here.
        if not self._active or self._thread != threading.get_ident():
            raise RuntimeError("SQLite scope has ended.")
        if self._rollback_attempted:
            raise OwnershipFailure()
        self._rollback_attempted = True
        try:
            self.__connection.rollback()
            _event(self.root,self.role,"rolled-back")
        except BaseException:
            hold(self.root,self)
            raise

    def backup_to(self, destination):
        self._check(); destination._check()
        if (self.role != "backup-source" or destination.role != "backup-destination"
                or self.root != destination.root or self.guard is not destination.guard):
            raise ValueError("Backup requires distinct owned offline source and destination.")
        self.__connection.backup(destination.__connection)

    def __exit__(self, kind, error, tb):
        if self._thread != threading.get_ident() or not self._entered:
            raise RuntimeError("Only the owning thread may end an entered SQLite scope.")
        original = error
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
        expected.executescript(";".join(SCHEMA_SQL+SCHEMA))
        if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID or _schema(db) != _schema(expected):
            raise GuardPaused("unsupported_schema")


class Mutation:
    """Business adapter; receipt completion uses its capability, never .raw."""
    def __init__(self, owner, operation, keep=False):
        self.__owner, self.operation, self.keep = owner, operation, keep

    def execute(self, sql, parameters=()):
        if not sql.startswith("SELECT "):
            self.operation.effect()
        return self.__owner.execute(sql,parameters)

    def commit(self):
        self.operation.check_publication()
        if not self.keep:
            self.operation.complete_sql(self.__owner)
        self.__owner.commit()

    def complete(self, token, *, authority=None):
        token.complete_sql(self.__owner,authority=authority)

    def effect(self):
        self.operation.effect()
