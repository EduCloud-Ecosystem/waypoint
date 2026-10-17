"""Executable local Docker restore exercise. Publishes only synthetic evidence."""

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import platform
import time
from unittest.mock import patch

from research_workspace import recovery
from research_workspace.evidence import build_bundle, verify_bundle
from research_workspace.runtime import Broker, Worker, validate_images
from research_workspace.server import Application
from research_workspace.store import AccessError, Store, canonical, private_write


def exercise(broker, output):
    started = time.monotonic()
    output = Path(output).resolve()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    private = output / "state.private"
    private.mkdir(mode=0o700)
    source = private / "source"
    images = json.loads(Path("images.lock.json").read_text())
    validate_images(images)
    store = Store(source, create=True)
    config = {"version": 1, "mode": "local-public-data", "broker": str(Path(broker).resolve()), "images": images, "total_budget_cpu_seconds": 600}
    private_write(source / "config.json", canonical(config).encode())
    key = store.add_user("owner")
    revoked_key = store.add_user("revoked")
    store.add_user("later-removed")
    _, token = store.session(key=key)
    compute = Broker(broker, source / "broker")
    worker = Worker(store, compute)
    checks = []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    def refused(name, operation):
        try:
            operation()
        except (ValueError, AccessError):
            checks.append(name)
        else:
            raise AssertionError(name)

    def project(name, budget=120):
        return store.create_project("owner", {"name": name, "question": "Does local recovery preserve synthetic results and authority?", "success_criterion": "Exact results, budget charges and access restrictions survive replacement.", "ends_at": (dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=2)).isoformat(), "budget_cpu_seconds": budget}, compute, 600)

    study = project("Recovery study")
    closed = project("Closed project", 30)
    store.close_project(closed["id"], "owner")
    store.membership(study["id"], "owner", "revoked", "viewer")
    store.membership(study["id"], "owner", "later-removed", "viewer")
    store.revoke_user("revoked")
    csv = Path("examples/public-fixture.csv").read_text()
    completed = []
    for recipe in ("summary-python", "summary-r"):
        queued = store.submit(study["id"], "owner", {"recipe": recipe, "condition": "baseline", "csv": csv}, images)
        worker.execute(store.claim())
        run = store.run(study["id"], queued["id"], "owner")
        check(f"actual_{recipe}_result_cleanup", run["status"] == "succeeded" and run["cleanup_verified"] and run["compute"]["charged_cpu_seconds"] == 30)
        completed.append(run)
    queued = store.submit(study["id"], "owner", {"recipe": "summary-python", "condition": "alternative", "csv": csv}, images)
    before_bundle = build_bundle(*store.snapshot(study["id"], "owner"))
    verify_bundle(before_bundle)
    private_write(output / "before.zip", before_bundle)
    first = recovery.checkpoint(source, private / "stale-checkpoint")
    recovery.restore(private / "stale-checkpoint", private / "stale-destination", trusted_sha256=first["checkpoint_sha256"], broker=broker)
    store.membership(study["id"], "owner", "later-removed")
    refused("stale_membership_restore_cannot_activate", lambda: recovery.activate(private / "stale-destination", source, private / "stale-checkpoint", trusted_sha256=first["checkpoint_sha256"]))
    checkpoint = recovery.checkpoint(source, private / "checkpoint")
    destination = private / "replacement"
    restore_started = time.monotonic()
    restored = recovery.restore(private / "checkpoint", destination, trusted_sha256=checkpoint["checkpoint_sha256"], broker=broker)
    check("restore_is_held_with_original_charges", restored["mode"] == "recovery-held" and sum(p["charged_cpu_seconds"] for p in restored["broker"]["projects"]) == 60)
    refused("held_service_refused", lambda: Application(destination))
    original_call = Broker.call

    def interrupt(broker_instance, command, *args):
        if command == "activate":
            raise ValueError("Injected process loss after source seal, before broker cutover.")
        return original_call(broker_instance, command, *args)

    with patch.object(Broker, "call", interrupt):
        refused("injected_cutover_interruption", lambda: recovery.activate(destination, source, private / "checkpoint", trusted_sha256=checkpoint["checkpoint_sha256"]))
    refused("sealed_source_service_refused", lambda: Application(source))
    refused("interrupted_destination_still_held", lambda: Application(destination))
    check("same_pair_cutover_retries", recovery.activate(destination, source, private / "checkpoint", trusted_sha256=checkpoint["checkpoint_sha256"])["activated"])
    restore_seconds = time.monotonic() - restore_started
    dest = Store(destination)
    for name, credential in (("old_key", {"key": key}), ("old_session", {"token": token}), ("revoked_key", {"key": revoked_key})):
        refused(f"{name}_refused", lambda credential=credential: dest.session(**credential))
    refused("revoked_subject_cannot_rotate", lambda: dest.rotate_user("revoked"))
    refused("removed_member_cannot_read", lambda: dest.project(study["id"], "later-removed"))
    check("closed_project_stays_closed", dest.project(closed["id"], "owner")["project"]["status"] == "closed")
    fresh_key = dest.rotate_user("owner")
    check("new_key_authenticates_active_owner", dest.session(key=fresh_key)[0] == "owner")
    check("queued_work_cancelled_without_replay", dest.run(study["id"], queued["id"], "owner")["status"] == "cancelled")
    for run in completed:
        check(f"{run['recipe']}_completed_record_preserved", dest.run(study["id"], run["id"], "owner") == run)
    after_bundle = build_bundle(*dest.snapshot(study["id"], "owner"))
    check("restored_evidence_verifies", verify_bundle(after_bundle)["valid"])
    private_write(output / "after.zip", after_bundle)
    dest_broker = Broker(broker, destination / "broker")
    check("preserved_spending_and_remaining_allowance", dest_broker.usage(study["id"])["remaining_cpu_seconds"] == 60)
    refused("source_broker_new_admission_fenced", lambda: compute.init_project("must-not-admit", 30))
    new = dest.submit(study["id"], "owner", {"recipe": "summary-python", "condition": "baseline", "csv": csv}, images)
    Worker(dest, dest_broker).execute(dest.claim())
    check("replacement_new_run_bounded_and_clean", dest.run(study["id"], new["id"], "owner")["status"] == "succeeded")
    check("replacement_charges_accumulate_without_reset", dest_broker.usage(study["id"])["remaining_cpu_seconds"] == 30)
    final = recovery.recovery_status(destination)
    check("no_unresolved_provider_liability", not final["broker"]["unresolved"])
    report = {"format": "educloud-local-recovery-exercise-v1", "status": "PASS", "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(), "checks": checks, "checks_passed": len(checks),
              "evidence_scope": "local Docker plus explicitly injected application cutover interruption", "platform": platform.platform(), "images": images,
              "broker_binary_sha256": hashlib.sha256(Path(broker).read_bytes()).hexdigest(), "restore_elapsed_seconds": round(restore_seconds, 3), "exercise_elapsed_seconds": round(time.monotonic()-started, 3),
              "data_loss_at_frozen_recovery_point": "zero completed records or immutable artifacts; pending work explicitly cancelled without replay", "recovery_targets_approved": False,
              "independent_human_restore": "NOT RUN", "off_host_disaster_recovery": "NOT RUN", "live_cloud_provider": "NOT RUN", "private_data_approval": False,
              "retained_failure": "Simulated interruption after source access seal; both applications refused service until same-pair retry.",
              "artifacts": {"before.zip": hashlib.sha256(before_bundle).hexdigest(), "after.zip": hashlib.sha256(after_bundle).hexdigest()},
              "source_hashes": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path("research_workspace").glob("*.py"))}}
    private_write(output / "verification.json", (json.dumps(report, indent=2)+"\n").encode())
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(json.dumps(exercise(args.broker, args.output), indent=2))
