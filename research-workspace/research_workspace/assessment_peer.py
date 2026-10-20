"""Bounded one-shot owned-data transport; never a worker or cleanup authority."""
import fcntl
import os
from pathlib import Path
import select
import signal
import time
import re
import threading
from datetime import datetime

from . import assessment_state as a
from .assessment import strict_document, opaque_id, number, timestamp

ERRORS = {"ASSESSMENT_INVALID", "ASSESSMENT_MISSING", "ASSESSMENT_UPLOAD_DISABLED",
          "ASSESSMENT_DENIED", "ASSESSMENT_COLLISION", "ASSESSMENT_STALE",
          "ASSESSMENT_UNRESOLVED", "ASSESSMENT_WRITER_ACTIVE"}
STATUS_KEYS = {"schema", "scope_id", "authority_id", "waypoint_authority_id", "project_id",
               "manifest_sha256", "declared_upload_ceiling_bytes", "admitting_owner_epoch",
               "generation", "state", "deny_latched", "expires_at", "writer_epoch", "watch_epoch",
               "code", "observations", "test_permission", "upload_authorized", "execution_authorized",
               "cleanup_trusted", "actual_resource_accepted", "liveness"}
OBS_KEYS = {"observation_id", "scope_id", "scope_generation", "observed_at", "method", "assurance",
            "new_admission", "writers_drained", "scope_access_revocation", "owned_removal", "reason",
            "cleanup_trusted", "actual_resource_accepted", "schema"}


class PeerFailure(ValueError):
    def __init__(self, code="ASSESSMENT_UNRESOLVED"):
        self.code = code if code in ERRORS else "ASSESSMENT_UNRESOLVED"
        super().__init__(self.code)


def reply(raw, status):
    try:
        value = strict_document(raw, limit=a.LIMITS["reply"])
        if raw != a.encode(value) + b"\n":
            raise PeerFailure()
        if status == 2:
            if set(value) != {"schema", "code"} or value["schema"] != "outfitter.owned-data-error/v1" or value["code"] not in ERRORS:
                raise PeerFailure()
            raise PeerFailure(value["code"])
        if (status != 0 or set(value) != STATUS_KEYS or value["schema"] != "outfitter.owned-data-status/v1"
                or len(raw) > a.LIMITS["status"] or value["code"] not in {"OK", "DENIED", "UNRESOLVED"}
                or value["state"] not in {"preparing", "accepting", "sealed", "cleaning", "cleanup_unresolved"}
                or value["test_permission"] not in {"none", "synthetic_ingest_only"}
                or value["liveness"] not in {"not_observed", "observed_current"}
                or type(value["deny_latched"]) is not bool):
            raise PeerFailure()
        for key in ("upload_authorized", "execution_authorized", "cleanup_trusted", "actual_resource_accepted"):
            if value[key] is not False:
                raise PeerFailure()
        for key in ("scope_id", "authority_id", "waypoint_authority_id", "project_id",
                    "admitting_owner_epoch", "writer_epoch"):
            opaque_id(value[key])
        if value["watch_epoch"] is not None:
            opaque_id(value["watch_epoch"])
        if type(value["manifest_sha256"]) is not str or re.fullmatch("[a-f0-9]{64}", value["manifest_sha256"]) is None:
            raise PeerFailure()
        number(value["generation"], 1)
        number(value["declared_upload_ceiling_bytes"], 1)
        timestamp(value["expires_at"])
        denied = value["state"] in {"cleaning", "cleanup_unresolved"}
        expected_code = ("UNRESOLVED" if value["state"] == "cleanup_unresolved"
                         else "DENIED" if denied else "OK")
        if (value["deny_latched"] != denied or value["code"] != expected_code
                or value["test_permission"] == "synthetic_ingest_only"
                and (value["liveness"] != "observed_current" or value["state"] != "accepting"
                     or value["watch_epoch"] is None)):
            raise PeerFailure()
        if type(value["observations"]) is not list or len(value["observations"]) > 8:
            raise PeerFailure()
        observed_ids, previous_generation = set(), 0
        for observation in value["observations"]:
            if (type(observation) is not dict or set(observation) != OBS_KEYS
                    or len(a.encode(observation)) > 1024 or observation["schema"] != "outfitter.owned-data-observation/v1"
                    or observation["method"] != "synthetic_local_scope_v1"
                    or observation["assurance"] != "synthetic" or observation["cleanup_trusted"] is not False
                    or observation["actual_resource_accepted"] is not False):
                raise PeerFailure()
            opaque_id(observation["observation_id"])
            number(observation["scope_generation"], 1, value["generation"])
            if (observation["observation_id"] in observed_ids or observation["scope_id"] != value["scope_id"]
                    or observation["scope_generation"] < previous_generation
                    or observation["new_admission"] not in {"denied", "unknown"}
                    or observation["writers_drained"] not in {"observed", "failed", "unknown"}
                    or observation["scope_access_revocation"] not in {"observed", "failed", "unknown"}
                    or observation["owned_removal"] not in {"observed_absent", "failed", "unknown"}
                    or observation["reason"] not in {"none", "writer_active", "owner_mismatch", "io_uncertain",
                                                   "lease_uncertain", "record_uncertain", "clock_uncertain"}
                    or observation["owned_removal"] == "observed_absent"
                    and (observation["new_admission"] != "denied" or observation["writers_drained"] != "observed"
                         or observation["scope_access_revocation"] != "observed")
                    or type(observation["observed_at"]) is not str
                    or not denied
                    or re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", observation["observed_at"]) is None):
                raise PeerFailure()
            datetime.fromisoformat(observation["observed_at"].replace("Z", "+00:00"))
            observed_ids.add(observation["observation_id"])
            previous_generation = observation["scope_generation"]
        return value
    except PeerFailure:
        raise
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise PeerFailure() from None


class Lifeline:
    """Actual pipe pairs, owned by this HTTP process; never reconstructed."""
    def __init__(self):
        self.lock = threading.RLock()
        self.owner_read = self.owner_write = self.watch_read = self.watch_write = None
        self.uncertain = False
        try:
            self.owner_read, self.owner_write = os.pipe()
            self.watch_read, self.watch_write = os.pipe()
            for fd in (self.owner_read, self.owner_write, self.watch_read, self.watch_write):
                os.set_inheritable(fd, False)
        except BaseException as original:
            try:
                self.close()
            except PeerFailure:
                # Construction never returned an owner to the service. Carry
                # the failed single close explicitly so it still latches the
                # process hold; a missing LiveScope is not a drain proof.
                failure = PeerFailure()
                failure.ownership_uncertain = True
                raise failure from original
            raise

    def _close_locked(self, name):
        fd = getattr(self, name)
        if fd is None:
            return
        setattr(self, name, None)  # one close attempt; no ambiguous FD reuse
        try:
            os.close(fd)
        except OSError:
            self.uncertain = True
            raise PeerFailure() from None

    def _close(self, name):
        with self.lock:
            self._close_locked(name)

    def launched_prepare(self):
        with self.lock:
            try:
                self._close_locked("watch_write")
            except PeerFailure as failure:
                failure.ownership_uncertain = True
                raise

    def _duplicate_descriptors_locked(self, operation):
        if self.uncertain or self.owner_write is None:
            raise PeerFailure("ASSESSMENT_DENIED")
        if operation == "prepare":
            values = (self.owner_read, self.watch_read, self.watch_write)
        elif operation in {"ingest", "seal"}:
            values = (self.owner_read, self.watch_read)
        else:
            values = ()
        if any(fd is None for fd in values):
            raise PeerFailure("ASSESSMENT_DENIED")
        duplicates = []
        try:
            for fd in values:
                duplicates.append(fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 20))
        except BaseException as original:
            while duplicates:
                duplicate = duplicates.pop()
                try:
                    os.close(duplicate)
                except OSError:
                    self.uncertain = True
            failure = PeerFailure()
            if self.uncertain:
                failure.ownership_uncertain = True
            raise failure from original
        return tuple(duplicates)

    def duplicate_descriptors(self, operation):
        """Return independently owned CLOEXEC copies under the close lock."""
        with self.lock:
            return self._duplicate_descriptors_locked(operation)

    def current(self):
        with self.lock:
            if self.uncertain or self.owner_write is None or self.watch_read is None:
                raise PeerFailure("ASSESSMENT_DENIED")
            # Once prepare has consumed its challenge, readable means EOF or an
            # unsupported extra message. Neither can grant current liveness.
            if select.select([self.watch_read], [], [], 0)[0]:
                raise PeerFailure("ASSESSMENT_DENIED")

    def close(self):
        with self.lock:
            failure = self.uncertain
            for name in ("owner_write", "watch_write", "owner_read", "watch_read"):
                try:
                    self._close_locked(name)
                except PeerFailure:
                    failure = True
            if failure:
                raise PeerFailure()


class Child:
    """An owned child plus bounded pipe I/O; no retained diagnostics/body."""
    def __init__(self, argv, env, extra=()):
        self.pid, self.status = None, None
        self.input = self.output = self.error = None
        self.raw, self.has_error = bytearray(), False
        self.close_uncertain = False
        self.deadline = time.monotonic() + 20
        opened, safe = list(extra), []
        original_error = None
        try:
            ir, iw = os.pipe(); opened += [ir, iw]
            or_, ow = os.pipe(); opened += [or_, ow]
            er, ew = os.pipe(); opened += [er, ew]
            for fd in opened:
                os.set_inheritable(fd, False)
            originals = (ir, ow, ew) + tuple(extra)
            for fd in originals:
                safe.append(fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 20))
            for fd in (iw, or_, er):
                os.set_blocking(fd, False)
            actions = [(os.POSIX_SPAWN_DUP2, fd, target) for target, fd in enumerate(safe)]
            self.pid = os.posix_spawn(str(argv[0]), [str(x) for x in argv], env, file_actions=actions)
            self.input, self.output, self.error = iw, or_, er
            for fd in (iw, or_, er):
                opened.remove(fd)  # ownership transferred to Child exactly once
            for fd in (ir, ow, ew):
                opened.remove(fd)  # detach before the single ambiguous close attempt
                try:
                    os.close(fd)
                except OSError:
                    self.close_uncertain = True
        except BaseException as original:
            original_error = original
        finally:
            while safe:
                fd = safe.pop()
                try:
                    os.close(fd)
                except OSError:
                    self.close_uncertain = True
            while opened:
                fd = opened.pop()
                try:
                    os.close(fd)
                except OSError:
                    self.close_uncertain = True
        if self.close_uncertain:
            failure = PeerFailure()
            failure.child = self if self.pid is not None else None
            failure.ownership_uncertain = True
            if original_error is not None:
                raise failure from original_error
            raise failure
        if original_error is not None:
            # A caller may be inside the final spawn fence. Transfer a launched
            # child for cleanup AFTER that fence, never wait here.
            failure = PeerFailure()
            failure.child = self if self.pid is not None else None
            raise failure from original_error

    def _poll(self, *, writing=False, check=None):
        if check is not None:
            check()
        if time.monotonic() >= self.deadline:
            raise PeerFailure()
        reads = [x for x in (self.output, self.error) if x is not None]
        readable, writable, _ = select.select(reads, [self.input] if writing and self.input is not None else [], [], .05)
        for fd in readable:
            chunk = os.read(fd, min(65536, a.LIMITS["reply"] + 1 - len(self.raw)) if fd == self.output else 1024)
            if fd == self.output:
                self.raw.extend(chunk)
                if len(self.raw) > a.LIMITS["reply"]:
                    raise PeerFailure()
            elif chunk:
                self.has_error = True
                raise PeerFailure()
            if not chunk:
                name = "output" if fd == self.output else "error"
                self._close(name)
        if self.pid is not None and self.status is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.status = os.waitstatus_to_exitcode(status)
                self.pid = None
        return bool(writable)

    def write(self, payload, check=None):
        view = memoryview(payload)
        while view:
            if self.input is None or self.status is not None:
                raise PeerFailure()
            if self._poll(writing=True, check=check):
                count = os.write(self.input, view[:65536])
                if count <= 0:
                    raise PeerFailure()
                view = view[count:]

    def finish(self, check=None):
        if self.input is not None:
            self._close("input")  # exact HTTP frame supplies peer EOF, never socket EOF
        while self.output is not None or self.error is not None or self.status is None:
            self._poll(check=check)
        if self.has_error:
            raise PeerFailure()
        return reply(bytes(self.raw), self.status)

    def _close(self, name):
        fd = getattr(self, name)
        if fd is None:
            return
        setattr(self, name, None)  # one close attempt; no ambiguous FD reuse
        try:
            os.close(fd)
        except OSError:
            self.close_uncertain = True
            raise PeerFailure() from None

    def abort(self):
        uncertain = self.close_uncertain
        for name in ("input", "output", "error"):
            fd = getattr(self, name)
            setattr(self, name, None)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    uncertain = True
        if self.pid is not None:
            # Only the unreaped child from this launch is eligible, never a PID
            # recovered from a record. Its exit is not content-cleanup proof.
            try:
                os.kill(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                uncertain = True
            deadline = time.monotonic() + 2
            while self.pid is not None and time.monotonic() < deadline:
                try:
                    pid, status = os.waitpid(self.pid, os.WNOHANG)
                except InterruptedError:
                    continue
                except OSError:
                    uncertain = True
                    break
                if pid:
                    self.status, self.pid = os.waitstatus_to_exitcode(status), None
                else:
                    time.sleep(.01)
            uncertain |= self.pid is not None
        self.close_uncertain = uncertain
        return not uncertain


class OwnedDataPeer:
    """Production argv always targets the ordinary, disabled executable.

    Tests override command/environment only, using the actual Go test peer.
    They do not replace this transport or the lifecycle implementation.
    """
    def __init__(self, executable, state, authority_id):
        self.executable = str(Path(executable).resolve())
        self.state = str(Path(state).resolve())
        self.authority_id = authority_id

    def command(self, operation):
        return [self.executable, "--state-dir", self.state, "owned-data", operation]

    def environment(self):
        return {k: v for k, v in os.environ.items() if not k.startswith("OUTFITTER_OWNED_TEST_")}

    def start(self, operation, lifeline=None):
        if operation not in {"prepare", "status", "ingest", "seal", "cancel", "reconcile"}:
            raise PeerFailure("ASSESSMENT_INVALID")
        if lifeline:
            # Denial/shutdown cannot close the original capability between its
            # duplication and the one-shot spawn. This fence ends before any
            # child IPC or response wait begins.
            with lifeline.lock:
                extra = lifeline._duplicate_descriptors_locked(operation)
                child = Child(self.command(operation), self.environment(), extra)
        else:
            child = Child(self.command(operation), self.environment())
        if operation == "prepare":
            try:
                lifeline.launched_prepare()
            except BaseException as original:
                failure = PeerFailure()
                failure.child = child
                failure.ownership_uncertain = lifeline.uncertain
                raise failure from original
        return child

    def call(self, operation, control, lifeline=None):
        raw = a.encode(control)
        if len(raw) > a.LIMITS["control"]:
            raise PeerFailure("ASSESSMENT_INVALID")
        child = None
        try:
            child = self.start(operation, lifeline)
            child.write(raw + b"\n")
            return child.finish()
        except PeerFailure as failure:
            child = child or getattr(failure, "child", None)
            raise
        finally:
            if child is not None and not child.abort():
                failure = PeerFailure()
                failure.ownership_uncertain = True
                raise failure
