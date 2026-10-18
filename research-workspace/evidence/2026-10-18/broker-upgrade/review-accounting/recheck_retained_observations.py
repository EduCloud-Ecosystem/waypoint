"""Read saved receipts/requests only. Never executes a broker or changes state."""
import argparse
import copy
import json
from pathlib import Path
import sys


def recheck(q, private_run, historical_path):
    historical = q.strict_json(historical_path.read_bytes())
    if (historical["format"] != "waypoint-broker-upgrade-qualification-v1" or historical["status"] != "PASS"
            or q.file_hash(private_run / "verification.json") != q.file_hash(historical_path)
            or "failure" in historical or len(historical["attempts"]) != q.MAX_ATTEMPTS):
        raise ValueError("Retained run does not match the historical report.")
    private = (private_run / "state.private").resolve(strict=True)
    images = q.strict_json((q.HERE / "images.lock.json").read_bytes())
    if historical["provenance"]["images_lock_sha256"] != q.file_hash(q.HERE / "images.lock.json"):
        raise ValueError("Historical reviewed images changed.")
    corrected = copy.deepcopy(historical)
    corrected.update(format="waypoint-broker-upgrade-qualification-v2", status="INCOMPLETE", expected_attempts=[])
    observations = []
    for index, (entry, (slot, state_role, broker, image)) in enumerate(zip(corrected["attempts"], q.ATTEMPT_PLAN)):
        state = private / state_role
        request_path = state.parent / "runs" / entry["request_id"] / "request.private.json"
        request = q.strict_json(request_path.read_bytes())
        config = q.strict_json((state.parent / "config.json").read_bytes())
        # The config identifies the actual build used, including fenced sources.
        if (q.file_hash(config["broker"]) != entry["broker_sha256"]
                or entry["broker_sha256"] != historical["provenance"][broker]["sha256"]
                or q.file_hash(request_path) != entry["request_sha256"]
                or request["request_id"] != entry["request_id"] or request["project_id"] != entry["project_id"]
                or request["image"] != images[image]):
            raise ValueError("Retained request/state/build binding changed.")
        ledger_path = state / "ledger.json"
        ledger = q.strict_json(ledger_path.read_bytes())
        recorded = ledger["runs"][request["request_id"]]
        if any(recorded["request"][key] != request[key] for key in ("request_id", "project_id", "image")):
            raise ValueError("Retained ledger/request identity differs.")
        # The two old-binary fixture refusals precede these seven status calls.
        prefix = private / f"cli-{index + 3:03d}"
        stdout, stderr, metadata = (prefix.with_suffix(suffix) for suffix in (".stdout", ".stderr", ".json"))
        if q.strict_json(metadata.read_bytes()) != {"command": "status", "exit_code": 0} or stderr.read_bytes():
            raise ValueError("Retained observation was not a successful status call.")
        receipt = q.strict_json(stdout.read_bytes())
        final = q.final_receipt(receipt, request)
        if (entry["compute"] != final["compute"] or entry["outcome"] != final["status"]
                or entry["cleanup_verified"] is not True):
            raise ValueError("Historical public summary differs from retained final observation.")
        binding = {key: entry[key] for key in ("request_id", "project_id", "request_sha256", "broker_sha256")}
        binding.update(slot=slot, state_role=state_role, state_sha256=q.digest(str(state).encode()), image=request["image"])
        corrected["expected_attempts"].append(copy.deepcopy(binding))
        entry.update(binding, final_receipt=final, final_receipt_sha256=q.digest(q.canonical(receipt).encode()))
        observations.append({"slot": slot, "request_id": request["request_id"], "state_role": state_role,
                             "request_sha256": q.file_hash(request_path), "ledger_sha256": q.file_hash(ledger_path),
                             "stdout_sha256": q.file_hash(stdout), "stderr_sha256": q.file_hash(stderr),
                             "metadata_sha256": q.file_hash(metadata), "binding": binding,
                             "final_receipt": final, "final_receipt_sha256": entry["final_receipt_sha256"]})
    if not q.accepted(corrected):
        raise ValueError("Retained observations fail the corrected report oracle.")
    return {"format": "waypoint-broker-upgrade-retained-observations-v1", "status": "PASS",
            "historical_report_sha256": q.file_hash(historical_path), "verifier_sha256": q.file_hash(q.HERE / "verify_broker_upgrade.py"),
            "reader_sha256": q.file_hash(Path(__file__)), "checked_receipts": len(observations),
            "charged_cpu_seconds": sum(o["final_receipt"]["compute"]["charged_cpu_seconds"] for o in observations),
            "corrected_oracle_accepts_reconstructed_report": True,
            "observations": observations, "new_docker_attempts": 0, "new_broker_processes": 0,
            "limitations": ["Read-only retrospective validation of seven retained actual final observations; no workload rerun.",
                            "Bindings were reconstructed from retained requests/config/ledger and the original report, not recorded by a v2 prelaunch run.",
                            "The historical v1 report and its failed acceptance oracle remain unchanged; this is supplemental evidence.",
                            "No native/consumer execution, provider truth, all-writer finality or active-pin adoption is established."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--private-run", required=True)
    parser.add_argument("--historical-report", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.workspace).resolve(strict=True)))
    import verify_broker_upgrade as q
    report = recheck(q, Path(args.private_run).resolve(strict=True), Path(args.historical_report).resolve(strict=True))
    with Path(args.output).open("x") as stream:
        json.dump(report, stream, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps({key: report[key] for key in ("status", "checked_receipts", "charged_cpu_seconds", "new_docker_attempts", "new_broker_processes")}))


if __name__ == "__main__":
    main()
