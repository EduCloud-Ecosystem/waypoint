"""Explicit v2 recovery. Interrupted cutover never retries provider effects."""

from contextlib import ExitStack, closing
from pathlib import Path
import secrets
import sqlite3

from . import recovery as r
from .operation_guard import GUARDED_MODES, GuardPaused, FILE, LOCK, TEMP, sync_directory
from .runtime import Broker, strict_json
from .store import Store, canonical, now, private_write, unknown_compute
from . import finite_authority as finite


def checkpoint(state, output, *, broker=None):
    with r.offline(state) as root:
        store = Store(root, resume=False)
        if not store.guard:
            raise ValueError("A v2 checkpoint requires guarded authority.")
        config_bytes = r.read_file(root / "config.json", 65536)
        config = strict_json(config_bytes)
        if config.get("mode") not in GUARDED_MODES:
            raise ValueError("Only an active source can create a recovery point.")
        if r.safe_root(output).is_relative_to(root):
            raise ValueError("Keep checkpoints outside the source authority directory.")
        if store.finite:
            finite.bounded_path(r.safe_root(output))
        target = r.fresh_directory(output, durable=True)
        with store.guard.locked(), r.frozen_db(root) as db:
            before = r.fingerprint(root, db)
            if store.profile_version == 4:
                from .sqlite_ownership import Owner
                with Owner(root,"backup-source",guard=store.guard,readonly=True) as reader:
                    with Owner(root,"backup-destination",guard=store.guard,path=target / "workspace.sqlite3",
                               initialize=True,validate=False) as backup:
                        reader.backup_to(backup)
            else:
                with closing(sqlite3.connect(root / "workspace.sqlite3")) as reader, closing(sqlite3.connect(target / "workspace.sqlite3")) as backup:
                    reader.backup(backup)
            (target / "workspace.sqlite3").chmod(0o600)
            private_write(target / "config.json", config_bytes)
            (target / "runs").mkdir(mode=0o700)
            r.copy_files(root / "runs", target / "runs", r.inventory(root / "runs"))
            r.copy_files(root, target, r.guard_files(root))
        # The lifecycle lock remains held. Any operator write during this wait
        # invalidates the snapshot in the second comparison below.
        report = Broker(broker or config["broker"], root / "broker").call("checkpoint", "--output", str(target / "broker"))
        if store.profile_version == 4:
            with store.guard.locked():
                with r.frozen_db(root) as db:
                    if before != r.fingerprint(root, db):
                        raise ValueError("Workspace authority changed during checkpoint; no manifest was published.")
                    r.sync_tree_directories(target)
                    manifest = {"format": r.GUARD_FORMAT, "created_at": now(), "source_state": str(root),
                                "source_fingerprint": before, "broker": report, "files": r.inventory(target)}
                    payload = store.serialized(manifest, "checkpoint_manifest").encode()
                # Cleanup is part of success. Lifecycle + guard still fence
                # cooperating writers; no claim about raw SQLite writers after
                # the frozen fingerprint comparison is introduced.
                private_write(target / "manifest.json", payload)
                sync_directory(target)
        else:
            with store.guard.locked(), r.frozen_db(root) as db:
                if before != r.fingerprint(root, db):
                    raise ValueError("Workspace authority changed during checkpoint; no manifest was published.")
                r.sync_tree_directories(target)
                manifest = {"format": r.GUARD_FORMAT, "created_at": now(), "source_state": str(root),
                            "source_fingerprint": before, "broker": report, "files": r.inventory(target)}
                payload = store.serialized(manifest, "checkpoint_manifest").encode()
                private_write(target / "manifest.json", payload)
                sync_directory(target)
        return {"format": r.GUARD_FORMAT, "checkpoint_sha256": r.sha(payload), "created_at": manifest["created_at"],
                "files": len(manifest["files"]), "broker": report, "quota_enforced": False,
                "warning": "Private backup contains credentials and research authority. Retain its digest separately."}


def restore(checkpoint_path, output, *, trusted_sha256, broker):
    archive, manifest = r.verified_checkpoint(checkpoint_path, trusted_sha256)
    finite.refuse_unqualified_recovery(archive)
    destination = r.safe_root(output)
    if destination.is_relative_to(archive) or destination.is_relative_to(r.safe_root(manifest["source_state"])):
        raise ValueError("Restore outside the checkpoint and original source.")
    Broker(broker, destination / "broker")
    config = strict_json(r.read_file(archive / "config.json", 65536))
    if config.get("mode") not in GUARDED_MODES or not config.get("operation_guard"):
        raise ValueError("A v2 checkpoint requires explicit guard binding.")
    root = r.fresh_directory(output, durable=True)
    config.update(mode="recovery-held", broker=str(Path(broker).resolve()))
    # The durable held configuration precedes every copied authority/effect.
    private_write(root / "config.json", canonical(config).encode())
    files = {name: value for name, value in manifest["files"].items()
             if name == "workspace.sqlite3" or name.startswith("runs/") or name in {FILE, LOCK} or name.startswith(TEMP)}
    r.copy_files(archive, root, files)
    (root / "runs").mkdir(exist_ok=True, mode=0o700)
    r.sync_tree_directories(root)
    store = Store(root, resume=False)
    token = store.guard.begin("recover", request={"phase": "restore", "checkpoint": trusted_sha256}, essential=True)
    try:
        with store.guard.locked(), closing(store._connect()) as db:
            r.database_digest(db)
            token.effect()
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM sessions")
            for row in db.execute("SELECT subject FROM users").fetchall():
                db.execute("UPDATE users SET key_hash=? WHERE subject=?", (r.sha(secrets.token_bytes(32)), row["subject"]))
            for row in db.execute("SELECT id,payload,status FROM runs WHERE status IN ('queued','running')").fetchall():
                run = strict_json(row["payload"])
                queued = row["status"] == "queued"
                run.update(status="cancelled" if queued else "interrupted", finished_at=now(),
                           error="Restored without replay. Queued work was cancelled." if queued else "Restored unfinished work requires operator reconciliation; no retry was issued.",
                           cleanup_verified=queued, compute=run["compute"] if queued else unknown_compute())
                db.execute("UPDATE runs SET status=?,payload=?,cancel_requested=1 WHERE id=?", (run["status"], canonical(run), row["id"]))
            db.commit()
        # Restore is an essential operation on a held destination. Its own intent
        # stays pending through the provider wait even when the source is paused.
        report = Broker(broker, root / "broker").call("restore", "--checkpoint", str(archive / "broker"),
                    "--checkpoint-sha256", manifest["broker"]["checkpoint_sha256"])
        if report.get("admission") != "held":
            raise ValueError("The restored compute authority did not remain held.")
        r.sync_tree_directories(root)
        with store.guard.locked(), closing(store._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            token.complete_sql(db)
            db.commit()
        token.release()
        with r.frozen_db(root) as db:
            restored_fingerprint = r.fingerprint(root, db)
        r.atomic_json(root / "recovery.json", {"format": r.GUARD_FORMAT, "phase": "held", "checkpoint_sha256": trusted_sha256,
                      "source_state": manifest["source_state"], "source_fingerprint": manifest["source_fingerprint"],
                      "restored_fingerprint": restored_fingerprint, "restored_at": now()})
        return r.recovery_status(root)
    except BaseException:
        token.fail()
        raise
    finally:
        token.release()


def _finish(token, store):
    with store.guard.locked(), closing(store._connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        token.complete_sql(db)
        db.commit()
    token.release()


def activate(state, source_state, checkpoint_path, *, trusted_sha256):
    archive, manifest = r.verified_checkpoint(checkpoint_path, trusted_sha256)
    finite.refuse_unqualified_recovery(archive)
    destination, source = r.safe_root(state), r.safe_root(source_state)
    if destination == source or str(source) != manifest["source_state"]:
        raise ValueError("Activation requires the original source and a distinct replacement.")
    with ExitStack() as lifecycle:
        for path in sorted((destination, source)):
            lifecycle.enter_context(r.offline(path))
        stores = {path: Store(path, resume=False) for path in (source, destination)}
        if any(store.guard is None for store in stores.values()):
            raise GuardPaused("missing_binding")
        origin, replacement = stores[source], stores[destination]
        record = strict_json(r.read_file(destination / "recovery.json", 65536))
        source_config = strict_json(r.read_file(source / "config.json", 65536))
        dest_config = strict_json(r.read_file(destination / "config.json", 65536))
        active_mode = strict_json(r.read_file(archive / "config.json", 65536))["mode"]
        if active_mode not in GUARDED_MODES or record.get("format") != r.GUARD_FORMAT:
            raise ValueError("Unsupported guarded recovery profile.")
        if record.get("checkpoint_sha256") != trusted_sha256 or record.get("source_state") != str(source):
            raise ValueError("The destination belongs to a different recovery point.")
        retired = {"destination": str(destination), "checkpoint_sha256": trusted_sha256}
        compute = Broker(dest_config["broker"], destination / "broker")
        # Provider observation is outside guard/DB locks; broker cutover itself
        # also verifies the bound source checkpoint before moving authority.
        source_broker = Broker(dest_config["broker"], source / "broker").call("recovery-status")
        destination_broker = compute.call("recovery-status")
        tokens = []
        try:
            with ExitStack() as guards:
                for path in sorted(stores):
                    guards.enter_context(stores[path].guard.locked())
                    stores[path].guard.check()
                if record.get("phase") == "active":
                    # No provider replay, including on the already-active path.
                    if dest_config.get("mode") != active_mode or source_config.get("mode") != "retired" or source_config.get("recovery_retired") != retired:
                        raise ValueError("The cutover publication is inconsistent.")
                    ids = record.get("cutover_operations", {})
                    for label, store in (("source", origin), ("destination", replacement)):
                        data = store.guard.load()
                        oid = ids.get(label)
                        item = data["operations"].get(oid)
                        if not item or item["kind"] != "recover" or item["request_hash"] != record.get("cutover_request_sha256") or store.guard.proof(data)[oid][2] != "committed":
                            raise GuardPaused("unfinished_operation")
                    if source_broker.get("admission") != "fenced" or destination_broker.get("admission") != "active":
                        raise ValueError("Provider cutover is not confirmed.")
                    origin.guard.resume()
                    replacement.guard.resume()
                    return {"activated": True, "already_active": True, "automatic_replay": False}
                if dest_config.get("mode") != "recovery-held" or source_config.get("mode") != active_mode:
                    raise ValueError("Incomplete cutover requires explicit reconciliation; it is never retried.")
                with r.frozen_db(source) as source_db, r.frozen_db(destination) as dest_db:
                    source_fp = r.fingerprint(source, source_db)
                    dest_fp = r.fingerprint(destination, dest_db)
                    if source_fp != manifest["source_fingerprint"] or dest_fp != record["restored_fingerprint"]:
                        raise ValueError("Authority changed since checkpoint or held restore. Reconcile and create a fresh checkpoint.")
                if source_broker.get("unresolved") or destination_broker.get("unresolved"):
                    raise ValueError("Provider liabilities require source reconciliation.")
                if source_broker.get("admission") != "active" or source_broker.get("authority_sha256") != manifest["broker"]["authority_sha256"] or destination_broker.get("admission") != "held":
                    raise ValueError("The provider authority changed after checkpoint.")
                request = {"checkpoint": trusted_sha256, "source": str(source), "destination": str(destination),
                           "source_authority": origin.guard.binding, "destination_authority": replacement.guard.binding,
                           "source_fingerprint": source_fp, "destination_fingerprint": dest_fp}
                for store in (origin, replacement):
                    tokens.append(store.guard.begin("recover", request=request))
                for token in tokens:
                    token.effect()
                source_config.update(mode="retired", recovery_retired=retired)
                r.atomic_json(source / "config.json", source_config)
            def launch(callback):
                with ExitStack() as guards:
                    for path in sorted(stores):
                        guards.enter_context(stores[path].guard.locked())
                        own = tokens[0] if path == source else tokens[1]
                        stores[path].guard.check(admitted=own)
                    return callback()
            result = compute.call("activate", "--source-state-dir", str(source / "broker"),
                                  "--checkpoint", str(archive / "broker"), "--checkpoint-sha256",
                                  manifest["broker"]["checkpoint_sha256"], launch=launch)
            if result.get("admission") != "active":
                raise ValueError("Provider cutover is unresolved; both applications remain held.")
            with ExitStack() as guards:
                for path in sorted(stores):
                    guards.enter_context(stores[path].guard.locked())
                dest_config["mode"] = active_mode
                record.update(phase="active", activated_at=now(),
                              cutover_operations={"source": tokens[0].identity, "destination": tokens[1].identity},
                              cutover_request_sha256=r.sha(canonical(request).encode()))
                r.atomic_json(destination / "recovery.json", record)
                r.atomic_json(destination / "config.json", dest_config)
                _finish(tokens[0], origin)
                _finish(tokens[1], replacement)  # last authority publication
            return {"activated": True, "automatic_replay": False, "broker": result,
                    "credentials": "Issue fresh keys or sign in again; transient authorization was discarded."}
        except BaseException:
            for token in tokens:
                token.fail()
            raise
        finally:
            for token in tokens:
                token.release()
