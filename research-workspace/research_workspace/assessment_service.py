"""Disabled production entrance and the connected AS1 metadata lifecycle.

Application always selects DisabledAssessmentService. Positive construction
belongs to test fixtures; no configuration selects the connected service.
"""
import re
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import secrets
import select
import threading
import time

from . import assessment_state as a
from . import operation_guard as g
from . import sqlite_ownership as owned
from .assessment import validate_manifest, canonical_bytes, timestamp, opaque_id, number, strict_document
from .assessment_peer import Lifeline, PeerFailure
from .store import AccessError

HTTP_BODY_SECONDS = 20.0
HTTP_READ_POLL_SECONDS = 0.1


@dataclass(frozen=True)
class Authorization:
    subject: str
    token: str
    expires: float
    context: object = None
    browser_cookie: str = ""


@dataclass
class LiveScope:
    pipes: Lifeline
    initiating_token: str
    status: dict = field(default_factory=dict)
    lock: object = field(default_factory=threading.Lock)
    upload_started: bool = False
    upload_complete: bool = False
    sealed: bool = False


class BoundedRequestBody:
    """Exact Content-Length reads with an absolute deadline and live check."""
    def __init__(self, handler):
        self.handler = handler
        self.deadline = None
        self.check = None

    def bind(self, deadline, check):
        self.deadline, self.check = deadline, check

    def read(self, length):
        if type(length) is not int or length < 0 or self.deadline is None or self.check is None:
            raise PeerFailure("ASSESSMENT_INVALID")
        if length == 0:
            return b""
        connection = self.handler.connection
        previous_timeout = connection.gettimeout()
        result = bytearray()
        try:
            # Socket timeouts poison SocketIO's buffered reader after the first
            # quiet poll. Nonblocking peek preserves header read-ahead and lets
            # readiness distinguish would-block from an actual EOF.
            connection.setblocking(False)
            while len(result) < length:
                self.check()
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise PeerFailure()
                try:
                    buffered = self.handler.rfile.peek(1)
                    if not buffered:
                        ready, _, _ = select.select([connection], [], [], min(HTTP_READ_POLL_SECONDS, remaining))
                        if not ready:
                            continue
                        self.check()
                        if time.monotonic() >= self.deadline:
                            raise PeerFailure()
                    chunk = self.handler.rfile.read1(min(length - len(result), a.LIMITS["chunk"]))
                except BlockingIOError:
                    continue
                except OSError:
                    raise PeerFailure() from None
                if not chunk:
                    raise PeerFailure("ASSESSMENT_INVALID")
                result.extend(chunk)
            self.check()
            if time.monotonic() >= self.deadline:
                raise PeerFailure()
            return bytes(result)
        finally:
            try:
                connection.settimeout(previous_timeout)
            except OSError:
                pass


@dataclass(frozen=True)
class PublishedResponse:
    payload: bytes
    sha256: str


def assessment_route(path):
    return re.fullmatch(r"/api/projects/([a-f0-9]{32})/assessments(?:/.*)?", path)


class DisabledAssessmentService:
    def handle_http(self, handler, path):
        handler.close_connection = True
        handler.respond(503, {"code": "ASSESSMENT_UPLOAD_DISABLED",
                              "error": "Assessment uploads are disabled."})


def _finite_epoch(value):
    if type(value) not in (int, float):
        a.invalid()
    try:
        result = float(value)
    except (OverflowError, ValueError):
        a.invalid()
    if not math.isfinite(result):
        a.invalid()
    return result


class AssessmentService:
    """Real pre-execution service, constructed only by fresh test fixtures.

    The Store remains the sole local authority. LiveScope holds only this
    process's non-reconstructable pipe capabilities and current credentials.
    """
    def __init__(self, store, peer, *, clock, max_upload_bytes, policy_expires, identity=None):
        if store._assessment is not a.AS1 or store._assessment_service is not None:
            a.invalid()
        number(max_upload_bytes, 1)
        # Validate both time boundaries before reading prior rows or performing
        # restart denials. NaN/inf must never weaken the policy ceiling or lose
        # the initial monotonic wall-clock baseline.
        policy_expires = _finite_epoch(policy_expires)
        initial_clock = _finite_epoch(clock())
        self.store, self.peer, self.identity = store, peer, identity
        self.clock, self.max_upload_bytes, self.policy_expires = clock, max_upload_bytes, policy_expires
        self.lock, self.scopes = threading.RLock(), {}
        self.high_water = initial_clock
        self.held = False
        with store.read() as db:
            prior = db.execute(a.ALL_META).fetchall()
        # A new service cannot recover earlier in-memory pipe capabilities,
        # including a second construction in the same BOOT.
        if prior:
            with store._safety_transaction(essential=True, request={"action": "assessment-restart"}) as (db, affected):
                store._deny_assessments(db, affected, reason="restart")
        store._assessment_service = self

    def now(self):
        with self.lock:
            try:
                sampled = _finite_epoch(self.clock())
            except (TypeError, ValueError, OverflowError):
                self.held = True
                raise PeerFailure("ASSESSMENT_DENIED") from None
            if self.held or not math.isfinite(sampled) or sampled < self.high_water:
                self.held = True
                raise PeerFailure("ASSESSMENT_DENIED")
            self.high_water = sampled
            return sampled

    def authenticate(self, token, browser_cookie=""):
        sampled = self.now()
        if not isinstance(token, str) or not token or len(token) > 256:
            raise AccessError("Current authorization is required.")
        if self.identity:
            context = self.identity.authenticate(token, browser_cookie)
            return Authorization(context.subject, token, context.expires, context, browser_cookie)
        with self.store.read() as db:
            row = db.execute(a.AUTH_SESSION, (hashlib.sha256(token.encode()).hexdigest(), sampled)).fetchone()
        if row is None:
            raise AccessError("Current authorization is required.")
        return Authorization(row["subject"], token, row["expires"])

    @contextlib.contextmanager
    def _authorized(self, db, auth, project_id, *, active=True):
        sampled = self.now()
        if auth.expires <= sampled:
            raise AccessError("Current authorization is required.")
        with self.identity.context_publication(auth.context) if self.identity else contextlib.nullcontext(True) as permitted:
            if not permitted:
                raise AccessError("Current authorization is required.")
            if not self.identity:
                row = db.execute(a.AUTH_SESSION, (hashlib.sha256(auth.token.encode()).hexdigest(), sampled)).fetchone()
                if row is None or row["subject"] != auth.subject:
                    raise AccessError("Current authorization is required.")
            project, _ = self.store._access(db, project_id, auth.subject, {"owner", "editor"}, active=active)
            if active and datetime.fromisoformat(project["ends_at"].replace("Z", "+00:00")).timestamp() <= sampled:
                raise AccessError("Current project authorization is required.")
            yield project

    def _current(self, db, auth, project_id, *, active=True):
        with self._authorized(db, auth, project_id, active=active) as project:
            return project

    def _row(self, db, scope_id, project_id=None, auth=None):
        row = db.execute(a.BY_ID, (scope_id,)).fetchone()
        if row is None:
            raise PeerFailure("ASSESSMENT_MISSING")
        value = a.validate_row(row)
        if project_id is not None and value["project_id"] != project_id or auth is not None and value["subject"] != auth.subject:
            raise AccessError("Current authorization is required.")
        return value

    def _summary(self, row):
        result = {k: row[k] for k in a.META_FIELDS}
        result.update(cleanup_trusted=False, upload_authorized=False, execution_authorized=False,
                      actual_resource_accepted=False)
        return result

    def _control(self, operation, row):
        return {"schema": "outfitter.owned-data-control/v1", "operation": operation,
                "scope_id": row["id"], "waypoint_authority_id": self.store.guard.binding["authority_id"],
                "expected_broker_authority_id": row["outfitter_authority_id"]}

    def _binding(self, response, row, live=None):
        expected = {"scope_id": row["id"], "authority_id": row["outfitter_authority_id"],
                    "waypoint_authority_id": self.store.guard.binding["authority_id"],
                    "project_id": row["project_id"], "manifest_sha256": row["manifest_sha256"],
                    "declared_upload_ceiling_bytes": row["declared_upload_ceiling_bytes"],
                    "expires_at": row["expires_at"], "admitting_owner_epoch": row["admitting_owner_epoch"]}
        if any(response[k] != value for k, value in expected.items()):
            raise PeerFailure()
        if row["last_scope_generation"] is not None and response["generation"] < row["last_scope_generation"]:
            raise PeerFailure("ASSESSMENT_STALE")
        if live is not None and live.status:
            if (response["writer_epoch"], response["watch_epoch"]) != (live.status["writer_epoch"], live.status["watch_epoch"]):
                raise PeerFailure()
            if response["generation"] < live.status["generation"]:
                raise PeerFailure("ASSESSMENT_STALE")
        return response

    def _advance(self, row, response, *, bind=False, auth=None):
        """A fresh short transaction, never a retained token across IPC."""
        self._binding(response, row)
        if response["deny_latched"] and row["local_phase"] != "denied":
            self.deny(row["id"], "ipc_uncertain")
        with self.store.read() as db:
            current = self._row(db, row["id"])
        if current["last_scope_generation"] == response["generation"] and not bind:
            return current, True
        if current["version"] == a.M:
            return current, False
        essential = current["local_phase"] == "denied"
        with self.store.transaction(essential=essential, request={"action": "assessment-observation", "scope_id": row["id"]}) as db:
            current = self._row(db, row["id"])
            self._binding(response, current)
            if current["last_scope_generation"] == response["generation"] and not bind:
                return current, True
            phase = "bound" if bind else current["local_phase"]
            if bind and current["local_phase"] != "pending":
                raise PeerFailure("ASSESSMENT_DENIED")
            if bind:
                self._current(db, auth, current["project_id"])
                if timestamp(current["expires_at"]).timestamp() <= self.now():
                    raise PeerFailure("ASSESSMENT_DENIED")
            next_version = current["version"] + 1
            if next_version > a.M - (phase != "denied"):
                raise PeerFailure()
            changed = db.execute(a.UPDATE, (phase, current["deny_reason"], next_version, response["generation"],
                                current["id"], current["version"], current["local_phase"]))
            if changed.rowcount != 1:
                raise PeerFailure()
            current.update(local_phase=phase, version=next_version, last_scope_generation=response["generation"])
        return current, True

    def _spawn(self, operation, row, auth, live):
        child = None
        try:
            with self.store.transaction(request={"action": "spawn", "scope_id": row["id"]}) as db:
                current = self._row(db, row["id"], row["project_id"], auth)
                with self._authorized(db, auth, row["project_id"]):
                    if (current["local_phase"] != ("pending" if operation == "prepare" else "bound")
                            or current["admitting_owner_epoch"] != g.BOOT
                            or timestamp(current["expires_at"]).timestamp() <= self.now()):
                        raise PeerFailure("ASSESSMENT_DENIED")
                    if operation != "prepare":
                        live.pipes.current()
                    # Only the actual spawn is inside this fence. SQL completion,
                    # Owner close and token release precede all pipe/body waits.
                    db.effect()
                    child = self.peer.start(operation, live.pipes)
            return child
        except BaseException as failure:
            child = child or getattr(failure, "child", None)
            if (child is not None and not child.abort()) or getattr(failure, "ownership_uncertain", False):
                self.held = True
                owned.hold(self.store.root)
            raise

    def _live(self, scope_id, auth):
        with self.lock:
            live = self.scopes.get(scope_id)
        if live is None or not secrets.compare_digest(live.initiating_token, auth.token):
            raise PeerFailure("ASSESSMENT_DENIED")
        return live

    def _check_stream(self, row, auth, live):
        # Remote OIDC validation is outside the local database/publication fence.
        fresh = self.authenticate(auth.token, auth.browser_cookie)
        with self.store.read() as db:
            self._current(db, fresh, row["project_id"])
            current = self._row(db, row["id"], row["project_id"], fresh)
            if (current["local_phase"] != "bound" or current["admitting_owner_epoch"] != g.BOOT
                    or timestamp(current["expires_at"]).timestamp() <= self.now()):
                raise PeerFailure("ASSESSMENT_DENIED")
        live.pipes.current()

    def _check_request(self, auth, project_id, *, active=True):
        with self.store.read() as db:
            self._current(db, auth, project_id, active=active)

    def _publication_response(self, project_id, auth, value, *, active, limit, scope_id=None):
        """Copy/hash a bounded response at the final local authorization point."""
        with self.store.guard.locked():
            if self.store.guard.publication is not None:
                self.store.guard.publication.gate()
            with self.store.read() as db, self._authorized(db, auth, project_id, active=active):
                body = strict_document(a.encode(value), limit=limit)
                row_id = scope_id
                if row_id is None and type(body.get("session")) is dict:
                    row_id = body["session"].get("id")
                if row_id is not None:
                    current = self._row(db, row_id, project_id, auth)
                    if current["local_phase"] == "denied" and current["deny_reason"] != "cancelled":
                        raise PeerFailure("ASSESSMENT_DENIED")
                    if "session" in body and body["session"] != self._summary(current):
                        raise PeerFailure("ASSESSMENT_STALE")
                elif "sessions" in body:
                    rows = db.execute(a.SUBJECT_META, (auth.subject,)).fetchall()
                    body["sessions"] = [self._summary(a.validate_row(row, metadata=True))
                                         for row in rows if row["project_id"] == project_id]
                payload = a.encode(body)
                if len(payload) > limit:
                    raise PeerFailure()
                digest = hashlib.sha256(payload).hexdigest()
        # The authority/Owner/identity fences have all been released. The exact
        # immutable bytes that were checked are sent by the caller afterward.
        return PublishedResponse(payload, digest)

    def prepare(self, project_id, auth, request_key, manifest, ceiling):
        opaque_id(project_id); opaque_id(request_key)
        validate_manifest(manifest)
        number(ceiling, 1, min(manifest["ceilings"]["scratch_bytes"], self.max_upload_bytes))
        if manifest["project_id"] != project_id:
            raise PeerFailure("ASSESSMENT_INVALID")
        raw = canonical_bytes(manifest).decode("ascii")
        digest = hashlib.sha256(raw.encode("ascii")).hexdigest()
        def preflight(db):
            project = self._current(db, auth, project_id)
            prior = db.execute(a.BY_KEY, (project_id, auth.subject, request_key)).fetchone()
            if prior is not None:
                old = a.validate_row(prior)
                if (old["manifest_sha256"], old["manifest_json"], old["declared_upload_ceiling_bytes"]) != (digest, raw, ceiling):
                    raise PeerFailure("ASSESSMENT_COLLISION")
                return project, old
            if db.execute(a.COUNT).fetchone()[0] >= 16 or db.execute(a.SUBJECT_COUNT, (auth.subject,)).fetchone()[0] >= 4:
                raise PeerFailure("ASSESSMENT_DENIED")
            return project, None
        with self.store.guard.locked():
            with self.store.read() as db:
                project, prior = preflight(db)
            if prior is not None:
                return {"session": self._summary(prior), "duplicate": True}
            expiry = math.floor(min(timestamp(manifest["expires_at"]).timestamp(),
                datetime.fromisoformat(project["ends_at"].replace("Z", "+00:00")).timestamp(), auth.expires, self.policy_expires))
            if expiry <= self.now():
                raise PeerFailure("ASSESSMENT_DENIED")
            row = {"id": secrets.token_hex(16), "project_id": project_id, "subject": auth.subject,
                   "request_key": request_key, "manifest_json": raw, "manifest_sha256": digest,
                   "outfitter_authority_id": self.peer.authority_id,
                   "created_at": datetime.fromtimestamp(self.now(), timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                   "expires_at": datetime.fromtimestamp(expiry, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "local_phase": "pending", "deny_reason": "none", "version": 1, "last_scope_generation": None,
                   "declared_upload_ceiling_bytes": ceiling, "admitting_owner_epoch": g.BOOT}
            a.validate_row(row)
            with self.store.transaction(subject=auth.subject, request={"action": "assessment-prepare", "scope_id": row["id"],
                    "manifest_sha256": digest, "declared_upload_ceiling_bytes": ceiling}, preflight=preflight) as db:
                _, prior = preflight(db)
                if prior is not None:
                    return {"session": self._summary(prior), "duplicate": True}
                db.execute(a.INSERT, tuple(row[k] for k in a.FIELDS))
        child = None
        try:
            live = LiveScope(Lifeline(), auth.token)
            with self.lock:
                self.scopes[row["id"]] = live
            control = self._control("prepare", row)
            control.update(project_id=project_id, manifest=manifest, declared_upload_ceiling_bytes=ceiling,
                           admitting_owner_epoch=g.BOOT, expires_at=row["expires_at"])
            child = self._spawn("prepare", row, auth, live)
            child.write(a.encode(control) + b"\n")
            response = self._binding(child.finish(), row)
            if (response["state"] != "accepting" or response["deny_latched"]
                    or response["liveness"] != "observed_current" or response["test_permission"] != "synthetic_ingest_only"):
                raise PeerFailure("ASSESSMENT_DENIED")
            live.pipes.current()
            live.status = response
            row, persisted = self._advance(row, response, bind=True, auth=auth)
            return {"session": self._summary(row), "broker": response, "cache_persisted": persisted, "duplicate": False}
        except BaseException as failure:
            self._failed(row["id"], ownership_uncertain=getattr(failure, "ownership_uncertain", False))
            raise
        finally:
            if child is not None and not child.abort():
                self._failed(row["id"], ownership_uncertain=True)
                raise PeerFailure()

    def status(self, project_id, scope_id, auth, *, reconcile=False):
        with self.store.read() as db:
            self._current(db, auth, project_id, active=False)
            row = self._row(db, scope_id, project_id, auth)
        if reconcile and row["local_phase"] != "denied":
            self.deny(scope_id, "cancelled")
            with self.store.read() as db:
                row = self._row(db, scope_id, project_id, auth)
        operation = "reconcile" if reconcile else "status"
        try:
            response = self._binding(self.peer.call(operation, self._control(operation, row)), row)
            current, persisted = self._advance(row, response)
            return {"session": self._summary(current), "broker": response, "cache_persisted": persisted}
        except BaseException as failure:
            self._failed(scope_id, ownership_uncertain=getattr(failure, "ownership_uncertain", False))
            raise

    def upload(self, project_id, scope_id, auth, source, length):
        live = self._live(scope_id, auth)
        if not live.lock.acquire(blocking=False):
            raise PeerFailure("ASSESSMENT_WRITER_ACTIVE")
        child = None
        try:
            with self.store.read() as db:
                row = self._row(db, scope_id, project_id, auth)
            number(length, 1, row["declared_upload_ceiling_bytes"])
            self._check_stream(row, auth, live)
            if live.upload_started:
                raise PeerFailure("ASSESSMENT_DENIED")
            response = self._binding(self.peer.call("status", self._control("status", row)), row, live)
            if response["deny_latched"] or response["state"] != "accepting":
                raise PeerFailure("ASSESSMENT_DENIED")
            live.upload_started = True
            control = self._control("ingest", row)
            control.update(expected_generation=response["generation"], writer_epoch=response["writer_epoch"],
                           admitting_owner_epoch=g.BOOT, content_length=length)
            child = self._spawn("ingest", row, auth, live)
            check = lambda: self._check_stream(row, auth, live)
            if isinstance(source, BoundedRequestBody):
                source.bind(min(child.deadline, time.monotonic() + HTTP_BODY_SECONDS), check)
            child.write(a.encode(control) + b"\n", check)
            remaining = length
            while remaining:
                check()
                chunk = source.read(min(a.LIMITS["chunk"], remaining))
                if type(chunk) is not bytes or not chunk or len(chunk) > remaining:
                    raise PeerFailure("ASSESSMENT_DENIED")
                check()
                child.write(chunk, check)
                remaining -= len(chunk)
            response = self._binding(child.finish(check), row, live)
            if response["deny_latched"] or response["state"] != "accepting":
                raise PeerFailure("ASSESSMENT_DENIED")
            live.status, live.upload_complete = response, True
            current, persisted = self._advance(row, response)
            return {"session": self._summary(current), "broker": response, "cache_persisted": persisted}
        except BaseException as failure:
            self._failed(scope_id, ownership_uncertain=getattr(failure, "ownership_uncertain", False))
            raise
        finally:
            uncertain = False
            if child is not None and not child.abort():
                self._failed(scope_id, ownership_uncertain=True)
                uncertain = True
            live.lock.release()
            if uncertain:
                raise PeerFailure()

    def seal(self, project_id, scope_id, auth):
        live = self._live(scope_id, auth)
        if not live.lock.acquire(blocking=False):
            raise PeerFailure("ASSESSMENT_WRITER_ACTIVE")
        child = None
        try:
            with self.store.read() as db:
                row = self._row(db, scope_id, project_id, auth)
            self._check_stream(row, auth, live)
            if not live.upload_complete or live.sealed:
                raise PeerFailure("ASSESSMENT_DENIED")
            response = self._binding(self.peer.call("status", self._control("status", row)), row, live)
            control = self._control("seal", row)
            control.update(expected_generation=response["generation"], writer_epoch=response["writer_epoch"], admitting_owner_epoch=g.BOOT)
            child = self._spawn("seal", row, auth, live)
            child.write(a.encode(control) + b"\n")
            response = self._binding(child.finish(lambda: self._check_stream(row, auth, live)), row, live)
            if response["deny_latched"] or response["state"] != "sealed":
                raise PeerFailure("ASSESSMENT_DENIED")
            live.status, live.sealed = response, True
            current, persisted = self._advance(row, response)
            return {"session": self._summary(current), "broker": response, "cache_persisted": persisted}
        except BaseException as failure:
            self._failed(scope_id, ownership_uncertain=getattr(failure, "ownership_uncertain", False))
            raise
        finally:
            uncertain = False
            if child is not None and not child.abort():
                self._failed(scope_id, ownership_uncertain=True)
                uncertain = True
            live.lock.release()
            if uncertain:
                raise PeerFailure()

    def deny(self, scope_id, reason):
        affected = []
        with self.store.transaction(essential=True, request={"action": "assessment-deny", "scope_id": scope_id, "reason": reason}) as db:
            row = self._row(db, scope_id)
            if row["local_phase"] != "denied":
                if db.execute(a.UPDATE, ("denied", reason, row["version"] + 1, row["last_scope_generation"],
                              scope_id, row["version"], row["local_phase"])).rowcount != 1:
                    raise PeerFailure()
                row.update(local_phase="denied", deny_reason=reason, version=row["version"] + 1)
            affected.append(row)
        self.denied(affected)

    def denied(self, rows):
        # Called only after Store safety transaction completion and lock exit.
        # End every local capability before attempting metadata IPC. A failed
        # close or child acknowledgement holds the process and forbids another
        # launch, but must not leave later rows' lifelines open.
        for row in rows:
            with self.lock:
                live = self.scopes.get(row["id"])
            if live is not None:
                try:
                    live.pipes.close()
                except PeerFailure:
                    self.held = True
                    owned.hold(self.store.root)
        if self.held:
            return
        for row in rows:
            control = self._control("cancel", row)
            reason = row["deny_reason"]
            control["reason"] = reason if reason in {"cancelled", "access_ended", "expired", "restart", "clock_rollback", "ipc_uncertain"} else "ipc_uncertain"
            try:
                self._binding(self.peer.call("cancel", control), row)
            except PeerFailure as failure:
                if getattr(failure, "ownership_uncertain", False):
                    self.held = True
                    owned.hold(self.store.root)
                    return
            except (ValueError, OSError):
                pass  # denied remains durable; no successful cleanup claim

    def _failed(self, scope_id, *, ownership_uncertain=False):
        if ownership_uncertain:
            self.held = True
            owned.hold(self.store.root)
        with self.lock:
            live = self.scopes.get(scope_id)
        if live is not None:
            try:
                live.pipes.close()
            except PeerFailure:
                self.held = True
                owned.hold(self.store.root)
        try:
            self.deny(scope_id, "ipc_uncertain")
        except (ValueError, OSError, g.GuardPaused, owned.OwnershipFailure):
            self.held = True
            owned.hold(self.store.root)

    def close_lifelines(self):
        with self.lock:
            self.held = True
            entries = list(self.scopes.values())
        for live in entries:
            try:
                live.pipes.close()
            except PeerFailure:
                owned.hold(self.store.root)

    def logout_subject(self, subject):
        with self.store._safety_transaction(essential=True, subject=subject,
                request={"action": "assessment-logout", "subject": subject}) as (db, affected):
            self.store._deny_assessments(db, affected, subject=subject)

    @staticmethod
    def _length(handler, maximum, *, required=False):
        if handler.headers.get_all("Transfer-Encoding"):
            raise PeerFailure("ASSESSMENT_INVALID")
        values = handler.headers.get_all("Content-Length", [])
        if not values and not required:
            return 0
        if len(values) != 1 or re.fullmatch(r"[0-9]{1,10}", values[0]) is None:
            raise PeerFailure("ASSESSMENT_INVALID")
        value = int(values[0])
        if value > maximum:
            raise PeerFailure("ASSESSMENT_INVALID")
        return value

    def handle_http(self, handler, path):
        handler.close_connection = True
        try:
            match = re.fullmatch(r"/api/projects/([a-f0-9]{32})/assessments(?:/([a-f0-9]{32})(?:/(upload|seal|cancel|reconcile|retention))?)?", path)
            if match is None or handler.command not in {"GET", "POST", "DELETE"}:
                raise PeerFailure("ASSESSMENT_INVALID")
            project_id, scope_id, action = match.groups()
            if handler.command != "GET" and handler.headers.get_all("X-Research-Request") != ["1"]:
                raise PeerFailure("ASSESSMENT_INVALID")
            cookie_name = self.identity.config["cookie_name"] if self.identity else "research_session"
            auth = self.authenticate(handler.session_token(cookie_name), handler.headers.get("Cookie", ""))
            with self.store.read() as db:
                self._current(db, auth, project_id, active=handler.command == "POST" and action not in {"cancel", "reconcile"})
            if scope_id is None and handler.command == "POST":
                length = self._length(handler, a.LIMITS["control"], required=True)
                if handler.headers.get_content_type() != "application/json":
                    raise PeerFailure("ASSESSMENT_INVALID")
                keys = handler.headers.get_all("Idempotency-Key", [])
                if len(keys) != 1:
                    raise PeerFailure("ASSESSMENT_INVALID")
                opaque_id(keys[0])
                source = BoundedRequestBody(handler)
                source.bind(time.monotonic() + HTTP_BODY_SECONDS,
                            lambda: self._check_request(auth, project_id, active=True))
                raw = source.read(length)
                control = strict_document(raw, limit=a.LIMITS["control"])
                if set(control) != {"manifest", "declared_upload_ceiling_bytes"}:
                    raise PeerFailure("ASSESSMENT_INVALID")
                value = self.prepare(project_id, auth, keys[0], control["manifest"], control["declared_upload_ceiling_bytes"])
                code = 200 if value["duplicate"] else 201
            elif scope_id is not None and action == "upload" and handler.command == "POST":
                with self.store.read() as db:
                    row = self._row(db, scope_id, project_id, auth)
                length = self._length(handler, row["declared_upload_ceiling_bytes"], required=True)
                if handler.headers.get_content_type() != "application/octet-stream":
                    raise PeerFailure("ASSESSMENT_INVALID")
                value, code = self.upload(project_id, scope_id, auth, BoundedRequestBody(handler), length), 200
            else:
                if self._length(handler, 0):
                    raise PeerFailure("ASSESSMENT_INVALID")
                if scope_id is None and handler.command == "GET":
                    with self.store.read() as db:
                        self._current(db, auth, project_id, active=False)
                        rows = db.execute(a.SUBJECT_META, (auth.subject,)).fetchall()
                    value = {"sessions": [self._summary(a.validate_row(r, metadata=True)) for r in rows if r["project_id"] == project_id]}
                elif handler.command == "GET" and action in {None, "retention"}:
                    value = self.status(project_id, scope_id, auth)
                elif handler.command == "POST" and action == "seal":
                    value = self.seal(project_id, scope_id, auth)
                elif (handler.command == "POST" and action == "cancel") or (handler.command == "DELETE" and action is None):
                    with self.store.read() as db:
                        self._row(db, scope_id, project_id, auth)
                    self.deny(scope_id, "cancelled")
                    value = self.status(project_id, scope_id, auth)
                elif handler.command == "POST" and action == "reconcile":
                    value = self.status(project_id, scope_id, auth, reconcile=True)
                else:
                    raise PeerFailure("ASSESSMENT_INVALID")
                code = 200
            limit = a.LIMITS["summary"] if scope_id is None and handler.command == "GET" else a.LIMITS["status"]
            if len(a.encode(value)) > limit:
                raise PeerFailure()
            published = self._publication_response(project_id, auth, value,
                active=handler.command == "POST" and action not in {"cancel", "reconcile"},
                limit=limit, scope_id=scope_id)
            if hashlib.sha256(published.payload).hexdigest() != published.sha256:
                raise PeerFailure()
            handler.respond(code, published.payload, content_type="application/json; charset=utf-8")
        except AccessError:
            handler.respond(403, {"code": "ASSESSMENT_DENIED", "error": "Current authorization is required."})
        except PeerFailure as failure:
            status = 400 if failure.code == "ASSESSMENT_INVALID" else 409 if failure.code in {"ASSESSMENT_COLLISION", "ASSESSMENT_STALE", "ASSESSMENT_WRITER_ACTIVE"} else 503
            handler.respond(status, {"code": failure.code, "error": "Assessment operation was not confirmed."})
        except Exception:
            handler.respond(503, {"code": "ASSESSMENT_UNRESOLVED", "error": "Assessment operation was not confirmed."})
