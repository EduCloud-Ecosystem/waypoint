#!/usr/bin/env python3
"""Source-pinned, local-only PR37 dispatch witness. No broker process is created."""
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "source" / "research-workspace"
for relative, expected in json.loads((ROOT / "source-sha256.json").read_text()).items():
    actual = hashlib.sha256((ROOT / "source" / relative).read_bytes()).hexdigest()
    if actual != expected:
        raise SystemExit("Source digest mismatch; the pinned witness was not executed.")
sys.path[:0] = [str(SOURCE), str(SOURCE / "tests")]
from test_operation_guard import GuardFixture
from research_workspace import runtime

class LaunchIntercepted(RuntimeError):
    pass

def deny(fixture, run, action):
    if action == "cancel":
        fixture.store.cancel(fixture.pid, run["id"], "alice")
    elif action == "revoke":
        fixture.store.revoke_user("alice")
    elif action == "close":
        fixture.store.close_project(fixture.pid, "alice")
    else:
        raise AssertionError("unsupported action")
    if not fixture.store.should_cancel(run["id"]):
        raise AssertionError("local denial did not commit")

def observe(action, phase):
    fixture = GuardFixture()
    fixture.setUp()
    record = {"action": action, "phase": phase, "local_denial_committed": False,
              "intercepted_launch_attempts": 0, "real_provider_processes": 0}
    try:
        fixture.submit()
        run = fixture.store.claim()
        actual_write = runtime.private_write
        def write_then_deny(path, payload):
            actual_write(path, payload)
            if Path(path).name == "request.private.json":
                deny(fixture, run, action)
                record["local_denial_committed"] = True
        if phase == "before_execute":
            deny(fixture, run, action)
            record["local_denial_committed"] = True
        with patch.object(runtime, "private_write", side_effect=write_then_deny if phase == "after_request_write" else actual_write), patch.object(runtime.subprocess, "Popen", side_effect=LaunchIntercepted("Intercepted; no process created")) as process:
            try:
                runtime.Worker(fixture.store, fixture.broker).execute(run)
            except LaunchIntercepted:
                pass
            record["intercepted_launch_attempts"] = process.call_count
        record["invariant"] = "No broker launch after local authority denial committed."
        record["status"] = "PASS" if record["intercepted_launch_attempts"] == 0 else "FAIL"
        return record
    finally:
        fixture.tearDown()

controls = [observe(action, phase) for phase in ("before_execute", "after_request_write") for action in ("cancel", "revoke", "close")]
confirmed = all(r["local_denial_committed"] and r["intercepted_launch_attempts"] == (0 if r["phase"] == "before_execute" else 1) for r in controls)
result = {"schema_version": 1, "source_head": "68fe54960d9d3e56b39bd6aa51f21728f532b4c9",
          "source_merge": "a58001b2731fa0dc9a1150b65c8940bf656625ec",
          "status": "FAIL", "witness": "REPRODUCED" if confirmed else "CHANGED_OR_INCOMPLETE",
          "controls": controls, "real_provider_processes": 0,
          "boundary": "Temporary synthetic local stores; fake project broker and intercepted Popen. No HTTP/browser/Docker/cloud execution.",
          "interpretation": "FAIL is the product dispatch invariant. Exit 0 means all three exact-boundary failures and all three earlier-denial controls were reproduced, not product acceptance."}
print(json.dumps(result, sort_keys=True, indent=2))
raise SystemExit(0 if confirmed else 1)
