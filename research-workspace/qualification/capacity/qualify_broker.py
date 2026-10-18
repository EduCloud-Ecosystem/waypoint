#!/usr/bin/env python3
"""Run selected pinned production broker transitions with simulated providers.

Reads a repository's Git object database only. Exports into a new disposable
directory; never builds in or changes the supplied checkout. No Docker calls.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
TESTS = ["TestUnknownCreateRetainsLiabilityAcrossReopen",
         "TestCleanupFailureNeverClaimsSuccess", "TestNoWatchdogNoProviderEffect",
         "TestDiagnosticWriteFailureCannotBlockTerminalAccounting",
         "TestReviewAmbiguousCreationCannotSettleBeforeLateProviderCompletion",
         "TestRecoveryRetainsUnknownReservationsWithoutReplayAndRequiresReconciliation",
         "TestRecoveryCrashAfterFenceLeavesBothNonAdmittingAndRetryable"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    pinned = json.loads((HERE / "contract-v1.json").read_text())["sources"]["outfitter"]
    archive = subprocess.check_output(["git", "-C", str(args.repository), "archive", pinned["commit"]])
    with tempfile.TemporaryDirectory(prefix="waypoint-capacity-broker-") as directory:
        root = Path(directory)
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            source.extractall(root, filter="data")
        for name, expected in pinned["sha256"].items():
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
                raise ValueError("Pinned broker source mismatch: " + name)
        # Disposable caches keep repository and existing runtime artifacts intact.
        env = dict(os.environ, GOCACHE=str(root / ".qualification-go-cache"))
        result = subprocess.run(["go", "test", "-json", "-count=1", "-timeout=45s",
                                 "./internal/broker", "./internal/ledger", "-run", "^(" + "|".join(TESTS) + ")$"],
                                cwd=root, env=env, capture_output=True, text=True, timeout=60)
        events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
        passed = sorted(e["Test"] for e in events if e.get("Action") == "pass" and e.get("Test") in TESTS)
        payload = {"format": "waypoint-capacity-broker-qualification-v1", "quota_enforced": False,
                   "commit": pinned["commit"], "source_sha256": pinned["sha256"],
                   "git_archive_sha256": hashlib.sha256(archive).hexdigest(),
                   "command_exit_code": result.returncode, "expected_tests": TESTS, "passed_tests": passed,
                   "boundary": "Actual pinned ledger/snapshot/recovery writers; fake provider and watchdog readiness callback. No detached watchdog or Docker process.",
                   "classification": "enforced bound on uncertainty/compute settlement; no storage envelope proved",
                   "real_watchdog_ownership_and_peak": "NOT RUN", "whole_root_capacity": "UNKNOWN"}
        if result.returncode or sorted(TESTS) != passed:
            payload["diagnostic"] = (result.stdout + result.stderr)[-12000:]
        with args.output.open("x") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True); stream.write("\n")
        return 0 if result.returncode == 0 and passed == sorted(TESTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
