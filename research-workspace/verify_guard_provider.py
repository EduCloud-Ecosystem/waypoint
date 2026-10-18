"""Fresh local Docker acceptance for guarded dispatch and v2 replacement."""
import argparse
from contextlib import closing
import datetime as dt
import json
from pathlib import Path

from research_workspace import recovery
from research_workspace.runtime import Broker, Worker, validate_images
from research_workspace.operation_guard import sync_directory
from research_workspace.store import Store, canonical, private_write


def exercise(broker, output):
    output = Path(output).resolve()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    sync_directory(output.parent)
    source, destination = output / "source.private", output / "replacement.private"
    images = json.loads((Path(__file__).parent / "images.lock.json").read_bytes())
    validate_images(images)
    store = Store(source, create=True, guarded=True, config={"version": 1, "mode": "local-guarded-public-data-v1",
                  "broker": str(Path(broker).resolve()), "images": images, "total_budget_cpu_seconds": 300}, subject="owner")
    compute = Broker(broker, source / "broker")
    worker = Worker(store, compute)
    checks = []
    def check(name, condition):
        checks.append({"name": name, "status": "PASS" if condition else "FAIL"})
        print(name + ": " + checks[-1]["status"], flush=True)
        if not condition:
            raise AssertionError(name)
    try:
        data = {"name": "Guarded synthetic execution", "question": "Does dispatch and replacement preserve authority?",
                "success_criterion": "Results, charges, revocations and pending work remain consistent.", "budget_cpu_seconds": 120,
                "ends_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).isoformat()}
        project = store.create_project("owner", data, compute, 300, "guard-provider-project")
        check("project_retry_same_allocation", store.create_project("owner", data, compute, 300, "guard-provider-project") == project)
        store.add_user("revoked"); store.revoke_user("revoked")
        csv = (Path(__file__).parent / "examples/public-fixture.csv").read_text()
        for recipe in ("summary-python", "summary-r"):
            run = store.submit(project["id"], "owner", {"recipe": recipe, "condition": "baseline", "csv": csv}, images, recipe)
            worker.execute(store.claim())
            saved = store.run(project["id"], run["id"], "owner")
            check(recipe + "_real_docker_result_cleanup_charge", saved["status"] == "succeeded" and saved["cleanup_verified"] and saved["compute"]["charged_cpu_seconds"] == 30)
            check(recipe + "_terminal_joint_proof", store.operation_status()["state"] == "ready")
        queued = store.submit(project["id"], "owner", {"recipe": "summary-python", "condition": "alternative", "csv": csv}, images, "never-replayed")
        backup = output / "checkpoint.private"
        checkpoint = recovery.checkpoint(source, backup)
        restored = recovery.restore(backup, destination, trusted_sha256=checkpoint["checkpoint_sha256"], broker=broker)
        check("v2_restore_held_and_queued_cancelled", restored["mode"] == "recovery-held" and restored["run_counts"]["cancelled"] == 1)
        check("v2_provider_cutover", recovery.activate(destination, source, backup, trusted_sha256=checkpoint["checkpoint_sha256"])["activated"])
        check("v2_idempotent_observation_without_replay", recovery.activate(destination, source, backup, trusted_sha256=checkpoint["checkpoint_sha256"])["already_active"])
        replacement = Store(destination)
        check("replacement_ready_after_durability_barrier", replacement.operation_status()["state"] == "ready")
        check("queued_run_never_replayed", replacement.run(project["id"], queued["id"], "owner")["status"] == "cancelled" and replacement.claim() is None)
        with closing(replacement.connect()) as db:
            check("revocation_preserved", db.execute("SELECT active FROM users WHERE subject='revoked'").fetchone()[0] == 0)
        usage = Broker(broker, destination / "broker").usage(project["id"])
        check("charges_preserved", usage["charged_cpu_seconds"] == 60)
    finally:
        private_write(output / "verification.json", canonical({"format": "educloud-operation-guard-provider-v1",
            "status": "PASS" if len(checks) == 12 and all(c["status"] == "PASS" for c in checks) else "INCOMPLETE", "checks": checks,
            "quota_enforced": False, "evidence_boundary": "Fresh local synthetic Docker runs and coordinated v2 replacement using reviewed legacy runtimes. Source candidates remain disabled. No hosted or real-data readiness claim."}).encode())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    exercise(args.broker, args.output)
