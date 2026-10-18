"""Replay the pre-fix final-charge false PASS. No process/provider is invoked."""
import argparse
import copy
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()
    sys.path.insert(0, str(workspace))
    source = Path(__file__).with_name("verifier-before-accounting-fix.py.txt")
    loader = importlib.machinery.SourceFileLoader("before_accounting_fix", str(source))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    q = importlib.util.module_from_spec(spec)
    loader.exec_module(q)
    q.HERE = workspace
    original = json.loads((source.parent.parent / "verification.json").read_bytes())
    if original["status"] != "PASS" or sum(a["compute"]["charged_cpu_seconds"] for a in original["attempts"]) != 210:
        raise ValueError("Historical report is not the expected seven-attempt observation.")
    images = json.loads((workspace / "images.lock.json").read_bytes())
    with tempfile.TemporaryDirectory(prefix="waypoint44-owned-charge-probe-") as temporary:
        audit = q.Audit(Path(temporary) / "audit", {})
        audit.report = copy.deepcopy(original)
        audit.targets = [(sys.executable, temporary, {"request_id": a["request_id"], "project_id": a["project_id"],
                          "image": images["r" if index in (2, 5) else "python"]}, a)
                         for index, a in enumerate(audit.report["attempts"])]
        requests = {r["request_id"]: r for _, _, r, _ in audit.targets}

        def changed_status(executable, state, command, flag, request_id):
            if command != "status":
                raise ValueError("Unexpected broker operation.")
            request = requests[request_id]
            return 0, {"schema_version": 1, **request, "admitted": False, "execution_started": False,
                       "cleanup_verified": True, "unresolved_liability": False, "stdout": "", "stderr": "",
                       "error": "simulated refusal", "exit_code": None, "status": "failed",
                       "compute": {"reserved_cpu_seconds": 0, "charged_cpu_seconds": 0, "provenance": "not_admitted"}}

        with patch.object(audit, "call", side_effect=changed_status):
            cleanup = audit.cleanup()
        observed = {"original_charged_cpu_seconds": 210,
                    "final_charged_cpu_seconds": sum(a["compute"]["charged_cpu_seconds"] for a in audit.report["attempts"]),
                    "cleanup_returns": cleanup, "qualification_accepted": q.accepted(audit.report),
                    "final_outcomes": [a["outcome"] for a in audit.report["attempts"]],
                    "docker_attempts_in_probe": 0, "broker_processes_in_probe": 0,
                    "verifier_sha256": q.file_hash(source), "historical_report_sha256": q.file_hash(source.parent.parent / "verification.json")}
        print(json.dumps(observed, sort_keys=True))
        return 1 if cleanup is True and q.accepted(audit.report) is True else 0


if __name__ == "__main__":
    sys.exit(main())
