"""Durable uncertainty authority, deliberately independent of storage capacity."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import threading
from . import finite_authority as finite


FORMAT = "educloud-operation-guard-v1"
FILE = "operation-guard.json"
LOCK = "operation-guard.lock"
TEMP = ".operation-guard-"
APPLICATION_ID = 0x45574F31
MAX_BYTES = 4 * 1024 * 1024
MAX_OPERATIONS = 4096
KINDS = {"initialize", "mutation", "submit", "project", "claim", "run", "settle", "recover"}
TERMINAL = {"committed", "refused"}
PHASES = {"prepared", "effect", "uncertain"}
GUARDED_MODES = {"local-guarded-public-data-v1", "local-guarded-synthetic-oidc-v1"} | finite.MODES
SCHEMA = (
    "CREATE TABLE operation_authority(authority_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL, barrier TEXT NOT NULL)",
    "CREATE TABLE operation_receipts(id TEXT PRIMARY KEY, sequence INTEGER UNIQUE NOT NULL, digest TEXT NOT NULL, outcome TEXT NOT NULL, essential INTEGER NOT NULL)",
)
IMMUTABLE = ("sequence", "kind", "resource_id", "subject", "request_key", "request_hash", "essential")
BOOT = secrets.token_hex(16)
_registry = {}
_registry_lock = threading.Lock()


class GuardPaused(RuntimeError):
    def __init__(self, code="unfinished_operation"):
        self.code = code
        if code in {"safety_slot_exhausted", "essential_intent_unresolved"}:
            message = "This action was not completed because a required safety step is unavailable or remains unverified. Existing records and obligations are preserved; ask the operator."
        elif code == "publication_unverified":
            message = "This action could not be verified. The workspace is held and existing records and obligations are preserved; ask the operator."
        else:
            message = "New work is paused because an earlier operation could not be verified. Your draft is preserved; ask the operator."
        super().__init__(message)


class GuardBusy(RuntimeError):
    def __init__(self):
        super().__init__("Another project is being initialized. Your draft is preserved; retry the same request shortly.")


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def request_hash(value):
    return hashlib.sha256(encode(value)).hexdigest()


def read(path, limit=MAX_BYTES):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise GuardPaused("invalid_authority")
        content = bytearray()
        while len(content) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (len(content), after.st_mtime_ns, after.st_ctime_ns):
            raise GuardPaused("changing_authority")
        return bytes(content)
    finally:
        os.close(fd)


def sync_directory(root):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Lease:
    def __init__(self):
        self.lock = threading.RLock()
        self.depth = 0
        self.fd = None
        self.active = {}
        self.failure = None
        self.publication_failed = False
        self.durable_incarnation = None


class Guard:
    def __init__(self, root, binding, connect=None):
        self.root = Path(root).resolve()
        self.finite = isinstance(binding, dict) and binding.get("format") in finite.GUARD_FORMATS
        self.profile_version = finite.binding_version(binding) if self.finite else 0
        keys = {"format", "authority_id"} | ({"logical_authority_sha256"} if self.finite else set())
        if (not isinstance(binding, dict) or set(binding) != keys
                or binding["format"] not in (finite.GUARD_FORMATS if self.finite else {FORMAT})
                or not re.fullmatch(r"[a-f0-9]{32}", str(binding["authority_id"]))
                or (self.finite and binding["logical_authority_sha256"] != finite.profile_digest(self.profile_version))):
            raise GuardPaused("invalid_binding")
        self.binding = dict(binding)
        self.connect = connect
        with _registry_lock:
            self.lease = _registry.setdefault(str(self.root), Lease())
        self.publication = None
        if self.profile_version == 3:
            from .guard_publication import Publication
            self.publication = Publication(self)

    @classmethod
    def create(cls, root, connect=None, *, finite_profile=False, profile_version=1):
        guard_format = finite.PROFILE_GUARDS[profile_version]
        binding = {"format": guard_format if finite_profile else FORMAT, "authority_id": secrets.token_hex(16)}
        if finite_profile:
            binding["logical_authority_sha256"] = finite.profile_digest(profile_version)
        guard = cls(root, binding, connect)
        if guard.publication:
            guard.publication.initializing = True
        if guard.publication:
            from .guard_publication import descriptor
            with guard.publication._root() as directory:
                with descriptor(LOCK, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, dir_fd=directory) as fd:
                    os.fsync(fd)
                os.fsync(directory)
        else:
            fd = os.open(guard.root / LOCK, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            sync_directory(guard.root)
        with guard.locked():
            guard.publish({**binding, "generation": 0, "sequence": 0, "operations": {}}, stage="bootstrap")
        return guard

    @contextmanager
    def locked(self):
        with self.lease.lock:
            outer = self.lease.depth == 0
            if outer:
                fd = os.open(self.root / LOCK, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    lock_info = os.fstat(fd)
                    if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1 or lock_info.st_size:
                        raise GuardPaused("invalid_lock")
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    self.lease.fd = fd
                except BaseException as original:
                    self.lease.failure = "lock_failure"
                    if self.publication:
                        self.lease.publication_failed = True
                    try:
                        os.close(fd)
                    except BaseException:
                        if not self.publication:
                            raise
                        original.add_note("The guard lock close also failed.")
                    raise
            self.lease.depth += 1
            try:
                yield
            finally:
                body_error = sys.exception()
                self.lease.depth -= 1
                if outer:
                    fd, self.lease.fd = self.lease.fd, None
                    try:
                        current = (self.root / LOCK).lstat()
                        if (current.st_dev, current.st_ino, current.st_nlink, current.st_size) != (lock_info.st_dev, lock_info.st_ino, 1, 0):
                            self.lease.failure = "lock_replaced"
                            raise GuardPaused("lock_replaced")
                    except BaseException:
                        self.lease.failure = "lock_failure"
                        self.lease.active.clear()
                        if self.publication:
                            self.lease.publication_failed = True
                        if self.publication and body_error is not None:
                            body_error.add_note("The guard lock identity could not be verified.")
                        else:
                            raise
                    finally:
                        pending = sys.exception() or body_error
                        try:
                            os.close(fd)
                        except BaseException:
                            self.lease.failure = "lock_failure"
                            self.lease.active.clear()
                            if self.publication:
                                self.lease.publication_failed = True
                            if self.publication and pending is not None:
                                pending.add_note("The guard lock close also failed.")
                            else:
                                raise

    def load(self):
        if self.publication:
            with self.locked():
                return self._load()
        return self._load()

    def _load(self):
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate")
                result[key] = value
            return result
        try:
            content = self.publication.current()[1] if self.publication else read(self.root / FILE)
            data = json.loads(content, object_pairs_hook=pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
            if (set(data) != set(self.binding) | {"generation", "sequence", "operations"} | ({"publication"} if self.publication else set())
                    or any(data[k] != v for k, v in self.binding.items())
                    or type(data["generation"]) is not int or not 0 <= data["generation"] < 2**63
                    or type(data["sequence"]) is not int or not 0 <= data["sequence"] < 2**63
                    or not isinstance(data["operations"], dict) or len(data["operations"]) > MAX_OPERATIONS + 1):
                raise ValueError("shape")
            for identity, item in data["operations"].items():
                if (not re.fullmatch(r"[a-f0-9]{32}", identity) or not isinstance(item, dict)
                        or set(item) != {"kind", "phase", "resource_id", "subject", "request_key", "request_hash", "owner", "reason", "sequence", "essential"}
                        or type(item["sequence"]) is not int or not 1 <= item["sequence"] <= data["sequence"]
                        or type(item["essential"]) is not bool
                        or item["kind"] not in KINDS or item["phase"] not in PHASES
                        or not re.fullmatch(r"[a-f0-9]{32}", item["owner"])
                        or not re.fullmatch(r"[a-f0-9]{64}", item["request_hash"])
                        or any(not isinstance(item[k], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", item[k]) for k in ("resource_id", "subject"))
                        or (item["request_key"] is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", item["request_key"]))
                        or item["reason"] not in {None, "operation_failed", "worker_failed", "unresolved_compute"}):
                    raise ValueError("operation")
            if (len({x["sequence"] for x in data["operations"].values()}) != len(data["operations"])
                    or sum(x["essential"] for x in data["operations"].values()) > 1):
                raise ValueError("sequence")
            if self.publication:
                from .guard_publication import validate
                validate(data)
            return data
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise GuardPaused("invalid_authority") from exc

    def publish(self, data, *, identity=None, stage=None):
        if self.publication:
            return self.save(data, identity=identity, stage=stage)
        return self.save(data)

    def save(self, data, *, identity=None, stage=None):
        if self.publication:
            with self.locked():
                return self.publication.save(data, identity=identity, stage=stage)
        if self.finite and (data["generation"] >= finite.COUNTER_LIMIT or data["sequence"] > finite.COUNTER_LIMIT):
            raise GuardPaused("intent_limit")
        data["generation"] += 1
        payload = encode(data)
        if len(payload) > MAX_BYTES:
            raise GuardPaused("intent_limit")
        temporary = self.root / (TEMP + secrets.token_hex(16))
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as target:
                target.write(payload)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.root / FILE)
            sync_directory(self.root)
        except BaseException:
            self.lease.failure = "authority_write_failed"
            raise

    def intent_digest(self, identity, item):
        return request_hash({**self.binding, "operation_id": identity,
                             **{key: item[key] for key in IMMUTABLE}})

    @staticmethod
    def validate_schema(db):
        import sqlite3
        from .storage_inventory import SCHEMA_SQL, _schema
        expected = sqlite3.connect(":memory:")
        try:
            expected.executescript(";".join(SCHEMA_SQL + SCHEMA))
            if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID or _schema(db) != _schema(expected):
                raise GuardPaused("unsupported_schema")
        finally:
            expected.close()

    def receipts(self):
        if self.publication:
            with self.locked():
                return self._receipts()
        return self._receipts()

    def _receipts(self):
        # Pure observation: no journal-mode changes, barrier or provider calls.
        import sqlite3
        db = sqlite3.connect((self.root / "workspace.sqlite3").as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            db.execute("PRAGMA query_only=ON")
            self.validate_schema(db)
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if self.finite:
                finite.validate_binding(self.root, self.binding, db)
            elif version != 0:
                raise GuardPaused("unsupported_schema")
            authority = db.execute("SELECT authority_id,sequence FROM operation_authority").fetchmany(2)
            rows = db.execute("SELECT id,sequence,digest,outcome,essential FROM operation_receipts").fetchmany(MAX_OPERATIONS + 2)
            if len(rows) > MAX_OPERATIONS + 1 or len({row[0] for row in rows}) != len(rows):
                raise GuardPaused("invalid_receipts")
            for oid, sequence, digest, outcome, essential in rows:
                if (not isinstance(oid, str) or not re.fullmatch(r"[a-f0-9]{32}", oid)
                        or type(sequence) is not int or not 1 <= sequence < 2**63
                        or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                        or outcome not in TERMINAL | {"started"} or type(essential) is not int or essential not in (0, 1)):
                    raise GuardPaused("invalid_receipts")
            return authority, {row[0]: row[1:] for row in rows}
        finally:
            db.close()

    def proof(self, data):
        authority, receipts = self.receipts()
        if authority != [(self.binding["authority_id"], data["sequence"])]:
            raise GuardPaused("authority_mismatch")
        if set(receipts) != set(data["operations"]):
            raise GuardPaused("intent_mismatch")
        for identity, item in data["operations"].items():
            sequence, digest, outcome, essential = receipts[identity]
            if (sequence != item["sequence"] or digest != self.intent_digest(identity, item)
                    or essential != int(item["essential"]) or outcome not in TERMINAL | {"started"}):
                raise GuardPaused("receipt_mismatch")
        return receipts

    def resume(self):
        """Establish durability of observed proof before a fresh process admits work."""
        with self.locked():
            if self.publication:
                return self.publication.qualify()
            data = self.load()
            self.proof(data)
            db = None
            try:
                db = self.connect()
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE operation_authority SET barrier=? WHERE authority_id=?",
                           (secrets.token_hex(16), self.binding["authority_id"]))
                db.commit()  # real FULL-synchronous write; a fresh SELECT is insufficient
                db.close()
                db = None
            except BaseException:
                self.lease.failure = "durability_unverified"
                raise
            finally:
                if db is not None:
                    try:
                        db.close()
                    except BaseException:
                        self.lease.failure = "durability_unverified"

    def barrier(self):
        """New-profile takeover barrier; never inside a business transaction."""
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE operation_authority SET barrier=? WHERE authority_id=?",
                       (secrets.token_hex(16), self.binding["authority_id"]))
            db.commit()
        except BaseException as original:
            try:
                db.close()
            except BaseException:
                original.add_note("The guard barrier connection close also failed.")
            raise
        else:
            db.close()

    def queued_ids(self):
        import sqlite3
        db = sqlite3.connect((self.root / "workspace.sqlite3").as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            db.execute("PRAGMA query_only=ON")
            rows = db.execute("SELECT id FROM runs WHERE status='queued' LIMIT 21").fetchall()
            if len(rows) > 20:
                raise GuardPaused("invalid_queue")
            return {row[0] for row in rows}
        finally:
            db.close()

    def status_locked(self, data=None):
        data = self.load() if data is None else data
        reason = self.lease.failure
        if self.publication:
            try:
                self.publication._slots(False)
            except GuardPaused as exc:
                reason = reason or exc.code
        elif any(self.root.glob(TEMP + "*")):
            reason = reason or "unfinished_authority_write"
        try:
            receipts = self.proof(data)
        except Exception:
            receipts = {}
            reason = reason or "authority_mismatch"
        for identity, operation in data["operations"].items():
            terminal = receipts.get(identity, (None, None, None, None))[2] in TERMINAL
            live = operation["owner"] == BOOT and identity in self.lease.active
            if operation["phase"] == "uncertain" or (not terminal and not live):
                reason = reason or "unfinished_operation"
        ordinary = sum(not item["essential"] for item in data["operations"].values())
        reserved = len(self.queued_ids())
        can_project = ordinary + reserved + 1 <= MAX_OPERATIONS
        can_submit = ordinary + reserved + 2 <= MAX_OPERATIONS
        if not can_project:
            reason = reason or "intent_limit"
        if self.finite and (data["generation"] >= finite.COUNTER_LIMIT or data["sequence"] >= finite.COUNTER_LIMIT):
            reason = reason or "intent_limit"
        return {"guard_enabled": True, "state": "paused" if reason else "ready",
                "reason": reason, "quota_enforced": False,
                "can_create_project": can_project and not reason,
                "can_submit_run": can_submit and not reason}

    def status(self):
        try:
            with self.locked():
                return self.status_locked()
        except Exception:
            return {"guard_enabled": True, "state": "paused", "reason": "invalid_authority", "quota_enforced": False}

    def check(self, *, essential=False, kind=None, admitted=None):
        data = self.publication.gate(essential=essential, operation=admitted) if self.publication else self.load()
        if essential:
            # Safety controls may bypass a valid uncertainty hold, never an
            # unsupported schema or malformed receipt representation.
            self.receipts()
        state = self.status_locked(data)
        owns_slot = (admitted is not None and self.lease.active.get(admitted.identity) is admitted
                     and admitted.identity in data["operations"] and data["operations"][admitted.identity]["owner"] == BOOT)
        reserved_claim = kind in {"claim", "run"} and bool(self.queued_ids()) and sum(not item["essential"] for item in data["operations"].values()) < MAX_OPERATIONS
        capacity_only = state["reason"] == "intent_limit" and (owns_slot or reserved_claim)
        if not essential and state["state"] == "paused" and not capacity_only:
            raise GuardPaused(state["reason"])
        if not essential and any(token.kind == "project" for token in self.lease.active.values()):
            # Normal overlap is a retryable conflict, never a permanent failure.
            if kind in {"project", "claim"} or (self.finite and not owns_slot):
                raise GuardBusy()
        return data

    def lookup(self, kind, subject, key):
        if key is None:
            return None
        with self.locked():
            for identity, operation in self.load()["operations"].items():
                if (operation["kind"], operation["subject"], operation["request_key"]) == (kind, subject, key):
                    return identity, dict(operation)
        return None

    def begin(self, kind, *, resource_id="workspace", subject="operator", request=None,
              request_key=None, essential=False, bootstrap=False):
        if kind not in KINDS:
            raise ValueError("Unsupported operation kind.")
        for value in (resource_id, subject):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
                raise ValueError("Use a bounded operation identity.")
        if request_key is not None and (not isinstance(request_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_key)):
            raise ValueError("Use a bounded request identity.")
        digest = request_hash(request)
        with self.locked():
            if self.publication and bootstrap and not self.publication.initializing:
                raise GuardPaused("invalid_bootstrap")
            data = self.load() if bootstrap else self.check(essential=essential, kind=kind)
            if not essential and not bootstrap:
                queued = self.queued_ids()
                ordinary = sum(not x["essential"] for x in data["operations"].values())
                reserved = len(queued) - int(kind == "run" and resource_id in queued)
                required = 2 if kind == "submit" else 1
                if ordinary + reserved + required > MAX_OPERATIONS:
                    raise GuardPaused("intent_limit")
            if request_key and self.lookup(kind, subject, request_key):
                raise GuardPaused("existing_intent")
            previous = next(((oid, item) for oid, item in data["operations"].items() if item["essential"]), None)
            if essential and previous:
                oid, item = previous
                try:
                    terminal = self.proof(data)[oid][2] in TERMINAL and item["phase"] != "uncertain"
                except Exception:
                    terminal = False
                if not terminal:
                    if self.publication:
                        raise GuardPaused("essential_intent_unresolved")
                    # The earlier durable dirty marker covers subsequent safety writes.
                    # Never replace it or let a later success certify it.
                    return Operation(self, oid, kind, borrowed=True)
                del data["operations"][oid]
            identity = secrets.token_hex(16)
            if self.finite and data["sequence"] >= finite.COUNTER_LIMIT:
                raise GuardPaused("intent_limit")
            data["sequence"] += 1
            data["operations"][identity] = {"kind": kind, "phase": "prepared", "resource_id": resource_id,
                "subject": subject, "request_key": request_key, "request_hash": digest, "owner": BOOT, "reason": None,
                "sequence": data["sequence"], "essential": essential}
            self.publish(data, identity=identity, stage="prepare")  # before business effects
            token = Operation(self, identity, kind)
            self.lease.active[identity] = token
            if not bootstrap:
                db = None
                try:
                    db = self.connect()
                    db.execute("BEGIN IMMEDIATE")
                    token.register(db, data)
                    db.commit()
                    if self.publication:
                        closing, db = db, None
                        closing.close()
                    else:
                        db.close()
                        db = None
                except BaseException:
                    token.fail()
                    raise
                finally:
                    if db is not None:
                        try:
                            db.close()
                        except BaseException:
                            token.fail()
            return token

    def running(self, resource_id):
        with self.locked():
            data = self.load()
            for identity, item in data["operations"].items():
                if item["kind"] == "run" and item["resource_id"] == resource_id:
                    return self.lease.active.get(identity)
        return None

    def fail(self, reason="worker_failed", *, origin=None, operation=None, error=None):
        if self.publication:
            if origin not in {"ordinary", "safety"}:
                raise ValueError("An explicit failure origin is required.")
            with self.locked():
                try:
                    token = operation or self.begin("mutation", essential=origin == "safety")
                    item = self.load()["operations"][token.identity]
                    if item["essential"] != (origin == "safety"):
                        raise ValueError("Failure origin differs from its operation.")
                    token.fail(reason, error=error)
                except BaseException:
                    if error is None:
                        raise
                    error.add_note("The required guard failure note could not be published.")
                finally:
                    self.lease.failure = self.lease.failure or reason
            return
        with self.lease.lock:
            self.lease.failure = reason
            for token in list(self.lease.active.values()):
                token.fail(reason)
            # An idle claim failure also needs a persistent hold. Failure of
            # this initial marker permits no business effect in this process.
            try:
                with self.locked():
                    token = self.begin("mutation", essential=True)
                    token.fail(reason)
            except BaseException:
                pass


class Operation:
    def __init__(self, guard, identity, kind, borrowed=False):
        self.guard, self.identity, self.kind = guard, identity, kind
        self.effects = False
        self.borrowed = borrowed

    def register(self, db, data=None):
        if self.guard.publication:
            self.guard.publication.registration(self.identity)
        data = self.guard.load() if data is None else data
        item = data["operations"][self.identity]
        if item["essential"]:
            db.execute("DELETE FROM operation_receipts WHERE essential=1")
        db.execute("INSERT INTO operation_receipts VALUES(?,?,?,?,?)",
                   (self.identity, item["sequence"], self.guard.intent_digest(self.identity, item), "started", int(item["essential"])))
        db.execute("UPDATE operation_authority SET sequence=? WHERE authority_id=?",
                   (data["sequence"], self.guard.binding["authority_id"]))

    def effect(self):
        with self.guard.locked():
            data = self.guard.load()
            item = data["operations"][self.identity]
            if self.guard.publication:
                self.guard.publication.gate(essential=item["essential"], operation=self, takeover=False)
            if item["phase"] == "prepared":
                item["phase"] = "effect"
                self.guard.publish(data, identity=self.identity, stage="effect")
            self.effects = True

    def check_publication(self):
        with self.guard.locked():
            item = self.guard.load()["operations"][self.identity]
            self.guard.publication.gate(essential=item["essential"], operation=self, takeover=False)

    def complete_sql(self, db, *, refused=False, authority=None):
        if self.borrowed:
            return
        if refused and self.effects:
            raise GuardPaused("effect_refusal")
        item = self.guard.load()["operations"][self.identity]
        if self.guard.publication:
            token = authority or self
            authorizing = self.guard.load()["operations"][token.identity]
            self.guard.publication.gate(essential=authorizing["essential"], operation=token, takeover=False)
        if item["phase"] == "uncertain":
            return
        db.execute("UPDATE operation_receipts SET outcome=? WHERE id=? AND digest=? AND outcome='started'",
                   ("refused" if refused else "committed", self.identity, self.guard.intent_digest(self.identity, item)))

    def fail(self, reason="operation_failed", *, error=None):
        original = error or sys.exception()
        with self.guard.lease.lock:
            self.guard.lease.failure = reason
            try:
                with self.guard.locked():
                    data = self.guard.load()
                    data["operations"][self.identity].update(phase="uncertain", reason=reason)
                    self.guard.publish(data, identity=self.identity, stage="uncertainty-note")
            except BaseException:
                if self.guard.publication:
                    if original is None:
                        raise
                    original.add_note("The required guard failure note could not be published.")
            finally:
                self.release()

    def release(self):
        with self.guard.lease.lock:
            if not self.borrowed:
                self.guard.lease.active.pop(self.identity, None)

    def launch(self, callback):
        with self.guard.locked():
            self.guard.check(admitted=self)
            self.effect()
            try:
                return callback()
            except BaseException:
                self.fail()
                raise
