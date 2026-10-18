"""Seven-attempt, disposable two-binary qualification; never changes active pins."""
import argparse
from contextlib import closing
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

from research_workspace import finite_authority as finite, recovery
from research_workspace.bounded_process import collect
from research_workspace.evidence import build_bundle, verify_bundle
from research_workspace.runtime import Broker, Worker, strict_json, validate_images, validate_receipt
from research_workspace.store import AccessError, Store, canonical, private_write

HERE = Path(__file__).resolve().parent
PINS = HERE / "qualification/broker-upgrade/pins.json"
MAX_ATTEMPTS = 7
CSV = "group,value\n001,1\n001,3\nNA,5\n"
BRIDGE_CHECKS = (
    "baseline_result", "candidate_read_identity", "idempotent_init_identity",
    "stale_activation_refused", "stale_source_not_sealed", "restore_held",
    "held_admission_refused", "cutover", "fenced_admission_refused",
    "completed_and_queued_preserved", "credentials_and_revocation_preserved",
    "replacement_result", "charges_accumulate", "export_verifies",
)
OTHER_CHECKS = (
    "unknown_both_binaries", "unknown_held_restore", "unknown_activation_refused",
    "missing_diagnostic_status", "missing_diagnostic_checkpoint_refused",
    "native_candidate_preserves", "native_baseline_refuses_without_write",
    "consumer_candidate_preserves", "consumer_baseline_refuses_without_write",
    "export_python_result", "export_r_result", "bounded_export_verifies",
    "finite_restore_refused", "diagnostic_failure_accounting", "diagnostic_failure_export",
    "strict_receipt_rejects_mutation", "owned_cleanup_verified",
)
REQUIRED = {f"{kind}_{name}" for kind in ("ordinary", "guarded") for name in BRIDGE_CHECKS} | set(OTHER_CHECKS)


def digest(content):
    return hashlib.sha256(content).hexdigest()


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def new_private_directory(path):
    # Resolve only the existing parent. Resolving the final component would
    # follow a dangling symlink and create a different, unrequested destination.
    raw = Path(path).absolute()
    root = raw.parent.resolve(strict=True) / raw.name
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    return root


def observed_command(argv, timeout=20):
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = collect(process, timeout=timeout)
    return process.returncode, stdout, stderr


def binary_identity(path, pin):
    path = Path(path).resolve(strict=True)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("Supply an executable broker build.")
    code, stdout, _ = observed_command(["go", "version", "-m", str(path)])
    if code:
        raise ValueError("Broker Go build provenance is unavailable.")
    metadata = stdout.decode().splitlines()[1:]
    attestation = strict_json(path.with_name(path.name + ".provenance.json").read_bytes())
    if (attestation.get("format") != "waypoint-broker-build-v1" or attestation.get("pin") != pin
            or attestation.get("binary_sha256") != file_hash(path) or attestation.get("build_metadata") != metadata
            or attestation.get("pins_sha256") != file_hash(PINS)
            or attestation.get("builder_sha256") != file_hash(HERE / "build_upgrade_brokers.py")
            or "\tpath\tgithub.com/EduCloud-Ecosystem/outfitter/cmd/outfitter" not in metadata):
        raise ValueError("Broker build does not match the pinned preparation manifest.")
    return {"sha256": file_hash(path), "build_manifest_sha256": file_hash(path.with_name(path.name + ".provenance.json")),
            "source_commit": pin["commit"], "source_tree": pin["tree"],
            "source_sha256": pin["source_sha256"], "build_metadata": metadata}


def accepted(report):
    checks = report["checks"]
    names = [entry["name"] for entry in checks]
    return (len(names) == len(REQUIRED) and set(names) == REQUIRED
            and all(entry["passed"] is True for entry in checks)
            and len(report["attempts"]) == MAX_ATTEMPTS
            and all(entry.get("cleanup_verified") is True for entry in report["attempts"]))


class Audit:
    def __init__(self, output, provenance):
        self.root = new_private_directory(output)
        self.private = self.root / "state.private"
        self.private.mkdir(mode=0o700)
        self.report = {"format": "waypoint-broker-upgrade-qualification-v1", "status": "INCOMPLETE",
                       "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "provenance": provenance,
                       "attempt_limit": MAX_ATTEMPTS, "reserved_execution_ceiling_cpu_seconds": 210,
                       "checks": [], "attempts": [], "artifacts": {}, "fixture_boundaries": {},
                       "quota_enforced": False, "active_pin_changed": False,
                       "limitations": ["Local synthetic qualification only; no active-pin adoption or accepted-state change.",
                           "Build metadata and hashes do not independently authenticate a supplied executable.",
                           "No physical capacity, all-writer finality, native execution, hosted identity or cloud acceptance."]}
        self.targets = []
        self.log_sequence = 0
        self.save()

    def save(self):
        recovery.atomic_json(self.root / "verification.json", self.report)

    def check(self, name, condition):
        if name not in REQUIRED or name in {v["name"] for v in self.report["checks"]}:
            raise ValueError("Unexpected or duplicate qualification control: " + name)
        passed = condition is True
        self.report["checks"].append({"name": name, "passed": passed})
        self.save()
        if not passed:
            raise ValueError("Qualification control failed: " + name)

    def refused(self, name, callback):
        try:
            callback()
        except (ValueError, AccessError):
            self.check(name, True)
        else:
            self.check(name, False)

    def artifact(self, name, content):
        private_write(self.root / name, content)
        self.report["artifacts"][name] = {"sha256": digest(content), "bytes": len(content)}
        self.save()

    def attempt(self, executable, state, request_path):
        if len(self.report["attempts"]) >= MAX_ATTEMPTS:
            raise ValueError("The qualification execution-attempt ceiling is exhausted.")
        request = strict_json(Path(request_path).read_bytes())
        if (any(type(request[k]) is not int for k in ("memory_mb", "pids_limit", "timeout_seconds", "max_output_bytes"))
                or type(request["cpus"]) not in (int, float)
                or (request["cpus"], request["memory_mb"], request["pids_limit"], request["timeout_seconds"], request["max_output_bytes"]) != (1, 256, 64, 30, 65536)
                or request["image"] not in strict_json((HERE / "images.lock.json").read_bytes()).values()):
            raise ValueError("Execution exceeds the fixed qualification profile.")
        entry = {"request_id": request["request_id"], "project_id": request["project_id"],
                 "request_sha256": file_hash(request_path), "broker_sha256": file_hash(executable),
                 "cleanup_verified": False, "outcome": "unknown_before_launch"}
        self.report["attempts"].append(entry)
        self.targets.append((str(executable), str(state), request, entry))
        self.save()  # Before Popen; a crash never erases this attempt or authorizes retry.

    def call(self, executable, state, *args, allow_failure=False):
        self.log_sequence += 1
        code, stdout, stderr = observed_command(Broker(executable, state).command(*args))
        prefix = self.private / f"cli-{self.log_sequence:03d}"
        private_write(prefix.with_suffix(".stdout"), stdout)
        private_write(prefix.with_suffix(".stderr"), stderr)
        private_write(prefix.with_suffix(".json"), canonical({"command": args[0], "exit_code": code}).encode())
        if code and not allow_failure:
            raise ValueError("Qualification broker command refused: " + args[0])
        return code, strict_json(stdout)

    def cleanup(self):
        complete = True
        for executable, state, request, entry in self.targets:
            try:
                receipt = self.call(executable, state, "status", "--request-id", request["request_id"])[1]
                if not receipt.get("cleanup_verified") or receipt.get("unresolved_liability"):
                    self.call(executable, state, "cancel", "--request-id", request["request_id"])
                    receipt = self.call(executable, state, "reconcile", "--request-id", request["request_id"])[1]
                validate_receipt(receipt, {"id": request["request_id"], "project_id": request["project_id"],
                                         "environment": {"image": request["image"]}})
                entry.update(cleanup_verified=receipt["cleanup_verified"] and not receipt["unresolved_liability"],
                             outcome=receipt["status"], compute=receipt["compute"])
            except Exception as exc:
                entry.update(cleanup_verified=False, cleanup_error_type=type(exc).__name__)
            complete = complete and entry["cleanup_verified"] is True
            self.save()
        return complete


class CountedBroker(Broker):
    def __init__(self, executable, state, audit):
        super().__init__(executable, state)
        self.audit = audit

    def command(self, *args):
        if args and args[0] == "run":
            self.audit.attempt(self.executable, self.state, args[2])
        return super().command(*args)


def workspace(audit, name, executable, images, *, guarded=False, bounded_export=False, budget=60):
    root = audit.private / name
    mode = finite.EXPORT_MODE if bounded_export else "local-guarded-public-data-v1" if guarded else "local-public-data"
    config = {"version": 1, "mode": mode, "broker": str(Path(executable).resolve()), "images": images,
              "total_budget_cpu_seconds": budget}
    if bounded_export:
        config["logical_authority"] = finite.profile(2)
    if guarded or bounded_export:
        store = Store(root, create=True, guarded=True, config=config, subject="owner")
        key = store.initial_key
    else:
        store = Store(root, create=True)
        private_write(root / "config.json", canonical(config).encode())
        key = store.add_user("owner")
    compute = CountedBroker(executable, root / "broker", audit)
    data = {"name": name, "question": "Does upgrading preserve bounded synthetic authority?",
            "success_criterion": "Identical history, no refund or replay, independently checked output.",
            "budget_cpu_seconds": budget, "ends_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()}
    project = store.create_project("owner", data, compute, budget, name)
    return store, compute, project, key


def run(store, compute, project, images, recipe, key):
    queued = store.submit(project["id"], "owner", {"recipe": recipe, "condition": "baseline", "csv": CSV}, images, key)
    claimed = store.claim()
    if claimed is None or claimed["id"] != queued["id"]:
        raise ValueError("Qualification claimed an unexpected run.")
    Worker(store, compute).execute(claimed)
    saved = store.run(project["id"], queued["id"], "owner")
    receipt = strict_json((store.root / "runs" / saved["id"] / "receipt.private.json").read_bytes())
    validate_receipt(receipt, saved)
    return saved, receipt


def succeeded(saved):
    return (saved["status"] == "succeeded" and saved["cleanup_verified"] is True
            and saved["compute"]["charged_cpu_seconds"] == 30 and not saved["compute"]["unresolved_liability"]
            and bool(saved["metrics"]))


def bridge(audit, kind, baseline, candidate, images):
    store, old, project, key = workspace(audit, kind, baseline, images, guarded=kind == "guarded")
    prefix = lambda name: kind + "_" + name
    revoked = store.add_user("revoked")
    _, session = store.session(key=key)
    completed, _ = run(store, old, project, images, "summary-python" if kind == "ordinary" else "summary-r", "original")
    audit.check(prefix("baseline_result"), succeeded(completed))
    new = CountedBroker(candidate, store.root / "broker", audit)
    before = old.call("recovery-status")
    ledger_before = (store.root / "broker/ledger.json").read_bytes()
    audit.check(prefix("candidate_read_identity"), before == new.call("recovery-status")
                and old.call("status", "--request-id", completed["id"]) == new.call("status", "--request-id", completed["id"]))
    new.init_project(project["id"], 60)
    audit.check(prefix("idempotent_init_identity"), before == new.call("recovery-status")
                and ledger_before == (store.root / "broker/ledger.json").read_bytes())
    stale = audit.private / (kind + "-stale-checkpoint")
    stale_report = recovery.checkpoint(store.root, stale)  # Config still selects the baseline.
    stale_dest = audit.private / (kind + "-stale-replacement")
    recovery.restore(stale, stale_dest, trusted_sha256=stale_report["checkpoint_sha256"], broker=candidate)
    store.revoke_user("revoked")
    audit.refused(prefix("stale_activation_refused"), lambda: recovery.activate(stale_dest, store.root, stale, trusted_sha256=stale_report["checkpoint_sha256"]))
    audit.check(prefix("stale_source_not_sealed"), strict_json((store.root / "config.json").read_bytes())["mode"] != "retired"
                and new.call("recovery-status")["admission"] == "active")
    queued = store.submit(project["id"], "owner", {"recipe": "summary-python", "condition": "alternative", "csv": CSV}, images, "never-replayed")
    checkpoint, destination = audit.private / (kind + "-checkpoint"), audit.private / (kind + "-replacement")
    report = recovery.checkpoint(store.root, checkpoint)
    restored = recovery.restore(checkpoint, destination, trusted_sha256=report["checkpoint_sha256"], broker=candidate)
    audit.report["artifacts"][kind + "_private_checkpoint"] = {"sha256": report["checkpoint_sha256"], "authority_sha256": report["broker"]["authority_sha256"]}
    audit.check(prefix("restore_held"), restored["broker"]["admission"] == "held" and restored["run_counts"]["cancelled"] == 1)
    replacement = Store(destination, resume=False)
    current = CountedBroker(candidate, destination / "broker", audit)
    audit.refused(prefix("held_admission_refused"), lambda: current.init_project("forbidden", 30))
    audit.check(prefix("cutover"), recovery.activate(destination, store.root, checkpoint, trusted_sha256=report["checkpoint_sha256"])["activated"] is True)
    audit.refused(prefix("fenced_admission_refused"), lambda: old.init_project("forbidden", 30))
    replacement = Store(destination)
    audit.check(prefix("completed_and_queued_preserved"), replacement.run(project["id"], completed["id"], "owner") == completed
                and replacement.run(project["id"], queued["id"], "owner")["status"] == "cancelled" and replacement.claim() is None)
    old_denied = 0
    for credentials in ({"key": key}, {"key": revoked}, {"token": session}):
        try:
            replacement.session(**credentials)
        except AccessError:
            old_denied += 1
    with closing(replacement.connect()) as db:
        inactive = db.execute("SELECT active FROM users WHERE subject='revoked'").fetchone()[0] == 0
    audit.check(prefix("credentials_and_revocation_preserved"), old_denied == 3 and inactive)
    after, _ = run(replacement, current, project, images, "summary-python", "replacement")
    audit.check(prefix("replacement_result"), succeeded(after))
    audit.check(prefix("charges_accumulate"), current.usage(project["id"])["charged_cpu_seconds"] == 60
                and current.usage(project["id"])["remaining_cpu_seconds"] == 0)
    bundle = build_bundle(*replacement.snapshot(project["id"], "owner"))
    audit.check(prefix("export_verifies"), verify_bundle(bundle)["valid"] is True)
    audit.artifact(kind + "-after.zip", bundle)
    return strict_json(ledger_before), completed


def seeded_state(audit, name, baseline, template, *, kind="unknown"):
    """Synthetic decoder fixtures only. Never dispatched or reconciled with Docker."""
    root = audit.private / name
    project_id = next(iter(template["projects"]))
    Broker(baseline, root).init_project(project_id, 30)
    ledger = strict_json((root / "ledger.json").read_bytes())
    record = copy.deepcopy(next(iter(template["runs"].values())))
    record.update(owner=ledger["owner"], status="unknown", cleanup_verified=False, unresolved_liability=True,
                  create_ambiguous=True, output_error="", compute={"reserved_cpu_seconds": 30, "charged_cpu_seconds": 0, "provenance": "reserved"})
    record.pop("output_sha256", None); record.pop("output_bytes", None)
    if kind == "consumer":
        request = record["request"]
        rid = "consumer-" + digest(json.dumps(["consumer-key-v1", "cairn-synthetic-v1", project_id, "fixture", "1"], separators=(",", ":")).encode())
        binding = {"consumer": "cairn-synthetic-v1", "job_id": "fixture", "attempt": 1,
                   "submission_commit": "a"*40, "policy_commit": "b"*40, "policy_manifest_sha256": "c"*64,
                   "input_files": [], "recipe_files": []}
        request = {"consumer_binding": binding, **request}
        request["request_id"] = rid
        record.update(request_id=rid, request=request)
        hashed = {**request, "input_dir": "", "recipe_dir": ""}
        record["payload_hash"] = digest(json.dumps(hashed, separators=(",", ":"), ensure_ascii=False).encode())
        ledger["version"] = 4
    record["container_name"] = "outfitter-" + digest((ledger["owner"] + ":" + record["request_id"]).encode())[:32]
    ledger["runs"] = {record["request_id"]: record}
    if kind == "native":
        ledger["version"] = 3
        ledger["native_accounts"] = {"native": {"id": "native", "provider": "fixture", "unit": "milli_su", "scope": "synthetic", "budget": 1, "policy": {}}}
        ledger["native_runs"] = {"native-run": {"request_id": "native-run", "account_id": "native", "owner": ledger["owner"],
              "reservation_id": "a"*32, "payload_hash": "b"*64, "created_at": record["created_at"], "deadline": record["deadline"],
              "reserved": 1, "cleanup_verified": False, "ambiguous": True, "state": {}, "revision": 1}}
    recovery.atomic_json(root / "ledger.json", ledger)
    audit.report["fixture_boundaries"][name] = "Synthetic valid ledger seeded directly after CLI initialization; no provider dispatch or settlement."
    audit.save()
    return root, record["request_id"]


def offline_controls(audit, baseline, candidate, template, run_record):
    root, rid = seeded_state(audit, "unknown-fixture", baseline, template)
    old, new = Broker(baseline, root), Broker(candidate, root)
    before = (root / "ledger.json").read_bytes()
    receipt = new.call("status", "--request-id", rid)
    validate_receipt(receipt, run_record)
    audit.check("unknown_both_binaries", old.call("recovery-status") == new.call("recovery-status")
                and old.call("status", "--request-id", rid) == receipt and receipt["compute"]["reserved_cpu_seconds"] == 30
                and receipt["compute"]["charged_cpu_seconds"] == 0 and receipt["unresolved_liability"] is True
                and before == (root / "ledger.json").read_bytes())
    cp, dest = audit.private / "unknown-checkpoint", audit.private / "unknown-held"
    report = old.call("checkpoint", "--output", str(cp))
    held = Broker(candidate, dest).call("restore", "--checkpoint", str(cp), "--checkpoint-sha256", report["checkpoint_sha256"])
    audit.check("unknown_held_restore", held["admission"] == "held" and len(held["unresolved"]) == 1
                and held["projects"][0]["reserved_cpu_seconds"] == 30)
    audit.refused("unknown_activation_refused", lambda: Broker(candidate, dest).call("activate", "--source-state-dir", str(root),
                  "--checkpoint", str(cp), "--checkpoint-sha256", report["checkpoint_sha256"]))
    for kind in ("native", "consumer"):
        state, request_id = seeded_state(audit, kind + "-fixture", baseline, template, kind=kind)
        original = (state / "ledger.json").read_bytes()
        observed = Broker(candidate, state).call("recovery-status")
        Broker(candidate, state).call("status", "--request-id", request_id)
        audit.check(kind + "_candidate_preserves", original == (state / "ledger.json").read_bytes()
                    and observed["admission"] == "active" and bool(observed["unresolved"])
                    and (bool(observed.get("native_unresolved")) if kind == "native" else True))
        code, _ = audit.call(baseline, state, "recovery-status", allow_failure=True)
        audit.check(kind + "_baseline_refuses_without_write", code != 0 and original == (state / "ledger.json").read_bytes())
    # A synthetic missing-sidecar fixture preserves the original settled charge.
    state, _ = seeded_state(audit, "missing-output-fixture", baseline, template)
    ledger = strict_json((state / "ledger.json").read_bytes())
    complete = copy.deepcopy(next(iter(template["runs"].values())))
    complete["owner"] = ledger["owner"]
    complete["container_name"] = "outfitter-" + digest((ledger["owner"] + ":" + complete["request_id"]).encode())[:32]
    ledger["runs"] = {complete["request_id"]: complete}
    recovery.atomic_json(state / "ledger.json", ledger)
    missing = Broker(candidate, state).call("status", "--request-id", complete["request_id"])
    audit.check("missing_diagnostic_status", bool(missing.get("output_error")) and missing["compute"]["charged_cpu_seconds"] == 30)
    audit.refused("missing_diagnostic_checkpoint_refused", lambda: Broker(candidate, state).call("checkpoint", "--output", str(audit.private / "incomplete-checkpoint")))


def export_controls(audit, candidate, images):
    store, compute, project, key = workspace(audit, "bounded-export", candidate, images, bounded_export=True, budget=90)
    token = store.session(key=key)[1]
    for recipe, label in (("summary-python", "python"), ("summary-r", "r")):
        saved, receipt = run(store, compute, project, images, recipe, label)
        audit.check("export_" + label + "_result", succeeded(saved))
    payload = store.export_bundle(project["id"], "owner", token)
    audit.check("bounded_export_verifies", verify_bundle(payload)["valid"] is True)
    audit.artifact("bounded-export.zip", payload)
    cp = audit.private / "finite-checkpoint"
    checkpoint = recovery.checkpoint(store.root, cp)
    audit.refused("finite_restore_refused", lambda: recovery.restore(cp, audit.private / "finite-destination",
                  trusted_sha256=checkpoint["checkpoint_sha256"], broker=candidate))
    mutated = copy.deepcopy(receipt)
    mutated["compute"]["charged_cpu_seconds"] = 0
    audit.refused("strict_receipt_rejects_mutation", lambda: validate_receipt(mutated, saved))
    # Obstruct only this disposable broker's output directory. Existing outputs
    # are retained verbatim, and old run receipts/results remain in Waypoint.
    outputs = store.root / "broker/outputs"
    outputs.rename(store.root / "broker/retained-outputs")
    private_write(outputs, b"owned diagnostic-failure fixture")
    failed, receipt = run(store, compute, project, images, "summary-python", "diagnostic-failure")
    audit.check("diagnostic_failure_accounting", failed["status"] == "failed" and not failed["metrics"]
                and failed["compute"]["charged_cpu_seconds"] == 30 and failed["cleanup_verified"] is True
                and receipt.get("output_error") == "execution diagnostics could not be persisted; authority accounting remains intact"
                and compute.usage(project["id"])["remaining_cpu_seconds"] == 0)
    payload = store.export_bundle(project["id"], "owner", token)
    audit.check("diagnostic_failure_export", verify_bundle(payload)["valid"] is True
                and not (store.root / "runs" / failed["id"] / "result.json").exists())
    audit.artifact("bounded-export-with-diagnostic-failure.zip", payload)


def exercise(baseline, candidate, output):
    pins = strict_json(PINS.read_bytes())
    provenance = {"baseline": binary_identity(baseline, pins["baseline"]), "candidate": binary_identity(candidate, pins["candidate"]),
                  "pins_sha256": file_hash(PINS), "platform": platform.platform(),
                  "source_sha256": {str(p.relative_to(HERE)): file_hash(p) for p in sorted([Path(__file__).resolve(), * (HERE / "research_workspace").glob("*.py")])},
                  "images_lock_sha256": file_hash(HERE / "images.lock.json"), "catalog_sha256": file_hash(HERE / "environments.json")}
    images = strict_json((HERE / "images.lock.json").read_bytes())
    validate_images(images)
    audit = Audit(output, provenance)
    try:
        template, original = bridge(audit, "ordinary", baseline, candidate, images)
        bridge(audit, "guarded", baseline, candidate, images)
        offline_controls(audit, baseline, candidate, template, original)
        export_controls(audit, candidate, images)
    except BaseException as exc:
        # A terminal interrupt between the last check and artifact publication
        # must never become PASS merely because every earlier check succeeded.
        audit.report["failure"] = {"type": type(exc).__name__, "message": "Qualification stopped; inspect private failure details."}
        private_write(audit.private / "failure.txt", (type(exc).__name__ + ": " + str(exc)).encode())
    finally:
        try:
            audit.check("owned_cleanup_verified", audit.cleanup())
        except ValueError:
            pass  # The failed cleanup control is retained; final status must be FAIL.
        audit.report["status"] = "PASS" if accepted(audit.report) and "failure" not in audit.report else "FAIL"
        audit.save()
    return audit.report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-broker", required=True)
    parser.add_argument("--candidate-broker", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    report = exercise(args.baseline_broker, args.candidate_broker, args.output)
    print(json.dumps({"status": report["status"], "checks": len(report["checks"]), "attempts": len(report["attempts"])}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
