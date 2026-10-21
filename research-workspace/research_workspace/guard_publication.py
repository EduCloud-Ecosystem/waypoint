"""Fresh v3 guard publication: fixed scratch, preserved residue, no quota."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import os
import re
import secrets
import stat

from . import operation_guard as g

ORDINARY = ".operation-guard-ordinary.pending"
SAFETY = ".operation-guard-safety.pending"
SLOTS = {ORDINARY, SAFETY}


@contextmanager
def descriptor(path, flags, mode=0o600, *, dir_fd=None):
    """Close each owned descriptor once, preserving an earlier error."""
    fd = os.open(path, flags, mode, dir_fd=dir_fd)
    try:
        yield fd
    except BaseException as original:
        try:
            os.close(fd)
        except BaseException:
            original.add_note("A guard descriptor close also failed.")
        raise
    else:
        os.close(fd)


def validate(data):
    p = data.get("publication")
    if (not isinstance(p, dict) or set(p) != {"id", "owner", "role", "operation_id", "stage",
                                             "previous_generation", "previous_sha256", "generation"}
            or any(not isinstance(p[k], str) or not re.fullmatch(r"[a-f0-9]{32}", p[k]) for k in ("id", "owner"))
            or p["role"] not in {"ordinary", "safety"}
            or p["stage"] not in {"bootstrap", "prepare", "effect", "uncertainty-note"}
            or type(p["generation"]) is not int or p["generation"] != data["generation"]):
        raise ValueError("publication")
    if p["stage"] == "bootstrap":
        if (p["generation"] != 1 or p["role"] != "ordinary" or data["sequence"] or data["operations"]
                or any(p[k] is not None for k in ("operation_id", "previous_generation", "previous_sha256"))):
            raise ValueError("bootstrap publication")
    elif (type(p["previous_generation"]) is not int or not 1 <= p["previous_generation"] < 2**63 - 1
          or p["generation"] != p["previous_generation"] + 1
          or not isinstance(p["previous_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", p["previous_sha256"])
          or not isinstance(p["operation_id"], str) or p["operation_id"] not in data["operations"]
          or data["operations"][p["operation_id"]]["essential"] != (p["role"] == "safety")):
        raise ValueError("publication predecessor")


class Publication:
    def __init__(self, guard):
        self.guard = guard
        self.initializing = False

    def _root(self):
        return descriptor(self.guard.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def _locked(self):
        if not self.guard.lease.depth:
            raise g.GuardPaused("publication_lock_required")

    def namespace(self, root):
        self._locked()
        occupied = set()
        for name in os.listdir(root):
            if not name.startswith(g.TEMP):
                continue
            if name not in SLOTS:
                raise g.GuardPaused("unsupported_guard_family")
            info = os.stat(name, dir_fd=root, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > self.guard.max_bytes:
                raise g.GuardPaused("unsupported_guard_family")
            occupied.add(name)
        return occupied

    def _read(self, root, name):
        with descriptor(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root) as fd:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > self.guard.max_bytes:
                raise g.GuardPaused("invalid_authority")
            content = bytearray()
            while len(content) <= self.guard.max_bytes:
                chunk = os.read(fd, min(65536, self.guard.max_bytes + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
            after = os.fstat(fd)
            path = os.stat(name, dir_fd=root, follow_symlinks=False)
            if ((before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_nlink) !=
                    (len(content), after.st_mtime_ns, after.st_ctime_ns, after.st_nlink)
                    or (path.st_dev, path.st_ino, path.st_nlink) != (before.st_dev, before.st_ino, 1)):
                raise g.GuardPaused("changing_authority")
            payload = bytes(content)
            return (before.st_dev, before.st_ino, len(payload), hashlib.sha256(payload).hexdigest()), payload

    def current(self):
        self._locked()
        with self._root() as root:
            return self._read(root, g.FILE)

    def _slots(self, essential):
        with self._root() as root:
            occupied = self.namespace(root)
        if SAFETY in occupied:
            raise g.GuardPaused("safety_slot_exhausted")
        if not essential and ORDINARY in occupied:
            raise g.GuardPaused("ordinary_slot_exhausted")
        return occupied

    def qualify(self):
        """Cache durability only. Permission, receipts and namespace are never cached."""
        self._locked()
        lease = self.guard.lease
        if lease.publication_failed:
            raise g.GuardPaused("publication_unverified")
        self._slots(True)
        data = self.guard.load()
        self.guard.proof(data)
        original, _ = self.current()
        try:
            with self._root() as root:
                with descriptor(g.FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root) as fd:
                    info = os.fstat(fd)
                    if (info.st_dev, info.st_ino) != original[:2]:
                        raise g.GuardPaused("changing_authority")
                    os.fsync(fd)
                os.fsync(root)
            self.guard.barrier()
            if self.current()[0] != original:
                raise g.GuardPaused("changing_authority")
            self.guard.proof(self.guard.load())
            self._slots(True)
        except BaseException:
            lease.publication_failed = True
            lease.failure = "publication_unverified"
            raise
        lease.durable_incarnation = original

    def gate(self, *, essential=False, operation=None, diagnostic=False, takeover=True):
        self._locked()
        lease = self.guard.lease
        if lease.publication_failed:
            raise g.GuardPaused("publication_unverified")
        # These observations MUST precede the durability-cache shortcut.
        self._slots(essential)
        data = self.guard.load()
        if self.initializing:
            return data
        receipts = self.guard.proof(data)
        own = operation.identity if operation is not None and lease.active.get(operation.identity) is operation else None
        if operation is not None and (own is None or operation.guard is not self.guard):
            raise g.GuardPaused("missing_operation")
        for identity, item in data["operations"].items():
            live = item["owner"] == g.BOOT and identity in lease.active
            unresolved = item["phase"] == "uncertain" or receipts[identity][2] not in g.TERMINAL
            if identity == own and item["phase"] == "uncertain" and not diagnostic:
                raise g.GuardPaused("unfinished_operation")
            if item["essential"] and unresolved and identity != own:
                if not (diagnostic and live and item["phase"] != "uncertain"):
                    raise g.GuardPaused("essential_intent_unresolved")
            if not essential and not diagnostic and (item["phase"] == "uncertain" or (unresolved and not live)):
                raise g.GuardPaused("unfinished_operation")
        if not essential and not diagnostic and lease.failure:
            raise g.GuardPaused(lease.failure)
        if self.current()[0] != lease.durable_incarnation:
            if not takeover:
                raise g.GuardPaused("durability_unverified")
            self.qualify()
        return data

    def registration(self, identity):
        """Only the just-published prepare may register its expected missing receipt."""
        self._locked()
        data = self.guard.load()
        item = data["operations"][identity]
        self._slots(item["essential"])
        if (self.guard.lease.publication_failed or item["phase"] != "prepared"
                or identity not in self.guard.lease.active
                or item["owner"] != g.BOOT or data["publication"]["operation_id"] != identity
                or data["publication"]["stage"] != "prepare"
                or self.current()[0] != self.guard.lease.durable_incarnation):
            raise g.GuardPaused("publication_unverified")

    def save(self, data, *, identity=None, stage=None):
        self._locked()
        if stage not in {"bootstrap", "prepare", "effect", "uncertainty-note"}:
            raise g.GuardPaused("publication_context_required")
        initial = stage == "bootstrap"
        operation = None
        if initial:
            if not self.initializing or data["generation"] or identity is not None:
                raise g.GuardPaused("invalid_bootstrap")
            essential, previous, predecessor = False, None, None
        else:
            essential = data["operations"][identity]["essential"]
            operation = self.guard.lease.active.get(identity)
            if stage in {"effect", "uncertainty-note"} and operation is None:
                raise g.GuardPaused("missing_operation")
            previous = self.gate(essential=essential, operation=operation,
                                 diagnostic=stage == "uncertainty-note", takeover=False)
            predecessor = self.current()
            if data["generation"] != previous["generation"]:
                raise g.GuardPaused("changing_authority")
        if data["generation"] >= g.finite.COUNTER_LIMIT or data["sequence"] > g.finite.COUNTER_LIMIT:
            raise g.GuardPaused("intent_limit")
        candidate = deepcopy(data)
        candidate["generation"] += 1
        candidate["publication"] = {"id": secrets.token_hex(16), "owner": g.BOOT,
            "role": "safety" if essential else "ordinary", "operation_id": identity, "stage": stage,
            "previous_generation": previous["generation"] if previous else None,
            "previous_sha256": predecessor[0][3] if predecessor else None,
            "generation": candidate["generation"]}
        validate(candidate)
        if self.guard.assessment:
            (g.ex2.guard_shape if self.guard.assessment is g.ex2.EX2 else g.astate.guard_shape)(candidate)
            if operation is not None and operation._publications >= 4:
                raise g.GuardPaused("intent_limit")
        payload = g.encode(candidate)
        if len(payload) > self.guard.max_bytes:
            raise g.GuardPaused("intent_limit")
        name = SAFETY if essential else ORDINARY
        try:
            with self._root() as root:
                occupied = self.namespace(root)
                if name in occupied or SAFETY in occupied:
                    raise g.GuardPaused("safety_slot_exhausted" if SAFETY in occupied else "ordinary_slot_exhausted")
                if initial:
                    try:
                        os.stat(g.FILE, dir_fd=root, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise g.GuardPaused("invalid_bootstrap")
                with descriptor(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, dir_fd=root) as fd:
                    created = os.fstat(fd)
                    if not stat.S_ISREG(created.st_mode) or created.st_nlink != 1:
                        raise g.GuardPaused("invalid_candidate")
                    offset = 0
                    while offset < len(payload):
                        written = os.write(fd, payload[offset:])
                        if written <= 0 or written > len(payload) - offset:
                            raise OSError("Guard candidate write did not complete.")
                        offset += written
                    os.fsync(fd)
                observed, content = self._read(root, name)
                if observed[:2] != (created.st_dev, created.st_ino) or content != payload:
                    raise g.GuardPaused("changing_candidate")
                occupied = self.namespace(root)
                if not essential and SAFETY in occupied:
                    raise g.GuardPaused("safety_slot_exhausted")
                if predecessor and self._read(root, g.FILE)[0] != predecessor[0]:
                    raise g.GuardPaused("changing_authority")
                os.replace(name, g.FILE, src_dir_fd=root, dst_dir_fd=root)
                os.fsync(root)
                current, content = self._read(root, g.FILE)
                if current != observed or content != payload:
                    raise g.GuardPaused("changing_authority")
                try:
                    os.stat(name, dir_fd=root, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise g.GuardPaused("publication_unverified")
            self.guard.lease.durable_incarnation = current
            data.clear()
            data.update(candidate)
            if self.guard.assessment and operation is not None:
                operation._publications += 1
        except BaseException:
            self.guard.lease.publication_failed = True
            self.guard.lease.failure = "publication_unverified"
            raise
