"""Reproduce known draft failures in disposable synthetic state, without providers.

Exit zero means the recorded defects were reproduced, not that admission works.
The accepted Git base is copied to a new temporary directory and the inert draft
patch is applied there. The caller's checkout and existing stores are not edited.
"""

import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
CSV = "group,value\na,1\na,2\na,3\na,4\na,100\n"


class StubBroker:
    def init_project(self, project_id, budget):
        return {"project_id": project_id, "budget_cpu_seconds": budget}

    def command(self, *args):
        return ["must-never-execute", *args]


def project_data():
    return {
        "name": "Synthetic blocked draft control",
        "question": "Does uncertainty stop new effects?",
        "success_criterion": "Retain the failure without new provider work.",
        "ends_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat(),
        "budget_cpu_seconds": 300,
    }


def new_store(root, with_project=True):
    from research_workspace.store import Store
    from research_workspace.storage import limits
    store = Store(root, create=True, storage_limits=limits())
    (root / "config.json").write_text('{"mode":"local-public"}\n')
    store.add_user("synthetic-owner")
    project = store.create_project("synthetic-owner", project_data(), StubBroker(), 600) if with_project else None
    return store, project


def submit(store, project):
    from research_workspace.catalog import list_environments
    environment = next(e for e in list_environments() if e["id"] == "summary-python-v1")
    return store.submit(project["id"], "synthetic-owner", {
        "recipe": "summary-python", "condition": "baseline", "csv": CSV,
    }, {"python": environment["image"]}, request_key="synthetic-request")


def rollback_probe(root, fail_rollback):
    from research_workspace.storage import Storage, _Lease
    from research_workspace.store import Store
    # Use the actual begin_db/end_db/held and transaction exception paths, with
    # in-memory authority persistence and a database that fails on rollback.
    authority = {"generation": 0, "pending_db": {}, "projects": {}, "runs": {}}
    storage = Storage.__new__(Storage)
    storage.lease = _Lease()
    storage.load = lambda: authority
    storage.save = lambda value: None
    storage.check = lambda *args, **kwargs: None

    class FailingDatabase:
        total_changes = 0
        storage_after_commit = []
        closed = False

        def execute(self, *args):
            return None

        def rollback(self):
            if fail_rollback:
                raise OSError("synthetic rollback I/O failure")

        def close(self):
            self.closed = True

    database = FailingDatabase()
    store = Store.__new__(Store)
    store.root, store.storage = root, storage
    store.connect = lambda: database
    try:
        with store.transaction():
            raise OSError("synthetic initial write failure")
    except OSError:
        pass
    observed = {
        "modeled_pending_intents": len(authority["pending_db"]),
        "active_intents_after_exit": len(storage.lease.active_db),
        "dispatch_held": storage.held(authority),
        "connection_closed": database.closed,
    }
    return observed


def rollback_failure(root):
    observed = rollback_probe(root, True)
    control = rollback_probe(root, False)
    assert observed == {"modeled_pending_intents": 1, "active_intents_after_exit": 1,
                        "dispatch_held": False, "connection_closed": True}, observed
    assert control == {"modeled_pending_intents": 1, "active_intents_after_exit": 0,
                       "dispatch_held": True, "connection_closed": True}, control
    return {"failure": observed, "successful_rollback_control": control,
            "scope": "in-memory coordinator persistence and database stub; injected rollback OSError"}


def claim_hold_race(root):
    from research_workspace.runtime import Worker
    store, project = new_store(root)
    submit(store, project)
    original = store.transaction

    @contextmanager
    def hold_between_check_and_transaction():
        # Deterministic schedule: another operation latches uncertainty after
        # claim's preliminary held check releases its lock, before BEGIN.
        store.storage.lease.broken = True
        with original() as db:
            yield db

    with patch.object(store, "transaction", hold_between_check_and_transaction):
        run = store.claim()
    assert run["status"] == "running"

    class LaunchIntercepted(Exception):
        pass

    with patch("research_workspace.runtime.subprocess.Popen", side_effect=LaunchIntercepted) as launch:
        try:
            Worker(store, StubBroker()).execute(run)
        except LaunchIntercepted:
            pass
        observed = {"dispatch_held": store.storage.held(store.storage.load()),
                    "claimed_status": run["status"], "intercepted_launch_attempts": launch.call_count,
                    "real_provider_processes": 0}
    assert observed == {"dispatch_held": True, "claimed_status": "running",
                        "intercepted_launch_attempts": 1, "real_provider_processes": 0}, observed
    clean, clean_project = new_store(root.parent / (root.name + "-control"))
    submit(clean, clean_project)
    clean_run = clean.claim()
    control = {"claimed_status": clean_run["status"], "dispatch_held": clean.storage.held(clean.storage.load())}
    assert control == {"claimed_status": "running", "dispatch_held": False}, control
    return {"failure": observed, "no_hold_control": control}


def result_before_commit(root):
    store, project = new_store(root)
    submit(store, project)
    run = store.claim()
    # These values are synthetic settlement inputs, not a provider receipt.
    run.update(status="succeeded", cleanup_verified=True, error="x" * 262144,
               compute={"reserved_cpu_seconds": 30, "charged_cpu_seconds": 30,
                        "provenance": "estimated_upper_bound", "unresolved_liability": False})
    failed = False
    try:
        store.finish(run, result={"synthetic": True})
    except ValueError:
        failed = True
    data = store.storage.load()
    result_path = root / "runs" / run["id"] / "result.json"
    observed = {"oversized_record_rejected": failed, "result_file_retained": result_path.is_file(),
                "pending_intents_after_failure": len(data["pending_db"]),
                "dispatch_held_after_failure": store.storage.held(data)}
    run.update(status="interrupted", metrics={}, error="Synthetic fallback settlement.")
    store.finish(run)
    data = store.storage.load()
    observed.update(fallback_phase=data["runs"][run["id"]]["phase"],
                    dispatch_held_after_fallback=store.storage.held(data))
    assert observed == {"oversized_record_rejected": True, "result_file_retained": True,
                        "pending_intents_after_failure": 0, "dispatch_held_after_failure": False,
                        "fallback_phase": "settled", "dispatch_held_after_fallback": False}, observed
    contrast_root = root.parent / (root.name + "-io-control")
    contrast, contrast_project = new_store(contrast_root)
    submit(contrast, contrast_project)
    contrast_run = contrast.claim()
    original = contrast._payload

    def late_io_failure(value, maximum):
        if value is contrast_run:
            raise OSError("synthetic post-result I/O failure")
        return original(value, maximum)

    with patch.object(contrast, "_payload", late_io_failure):
        try:
            contrast.finish(contrast_run, result={"synthetic": True})
        except OSError:
            pass
    contrast_data = contrast.storage.load()
    control = {"result_file_retained": (contrast_root / "runs" / contrast_run["id"] / "result.json").is_file(),
               "pending_intents_after_failure": len(contrast_data["pending_db"]),
               "dispatch_held_after_failure": contrast.storage.held(contrast_data)}
    assert control == {"result_file_retained": True, "pending_intents_after_failure": 1,
                       "dispatch_held_after_failure": True}, control
    return {"failure": observed, "post_result_oserror_control": control,
            "scope": "real disposable files/SQLite; injected 262144-byte internal error outside normal broker validation; ordinary recipe reachability not established"}


def project_initialization_hold(root):
    from research_workspace.runtime import Worker
    store, _ = new_store(root, with_project=False)
    worker = Worker(store, StubBroker())

    class ConcurrentBroker(StubBroker):
        def init_project(self, project_id, budget):
            # Run the worker loop at the unlocked, preparing project intent.
            # The draft exits immediately on held(); no background thread runs.
            worker.loop()
            return super().init_project(project_id, budget)

    project = store.create_project("synthetic-owner", project_data(), ConcurrentBroker(), 600)
    observed = {"project_committed": project["status"] == "active",
                "worker_stopped_with_failure": worker.failure is not None,
                "dispatch_held_after_init": store.storage.held(store.storage.load())}
    assert observed == {"project_committed": True, "worker_stopped_with_failure": True,
                        "dispatch_held_after_init": True}, observed
    clean, _ = new_store(root.parent / (root.name + "-control"))
    control = {"dispatch_held_after_init": clean.storage.held(clean.storage.load())}
    assert control == {"dispatch_held_after_init": False}, control
    return {"failure": observed, "no_worker_overlap_control": control}


def consumed_headroom_model(root):
    from research_workspace import storage as module
    # Arithmetic only: model an existing 128 KiB database, 64 KiB protected
    # unknown file, no runs, and one consumed byte in a protected temporary file.
    storage = module.Storage.__new__(module.Storage)
    storage.root, storage.lease = root, module._Lease()
    authority = {"pending_db": {}, "projects": {}, "runs": {}, "policy": module.limits()}
    entries = {"workspace.sqlite3": 131072, "protected-unknown": 65536}
    consumed = sum(entries.values())
    storage.measure = lambda: (consumed, 7 + len(entries), entries)
    growth, _ = storage.control_remaining(authority, entries)
    authority["policy"] = module.limits(logical_bytes=consumed + growth)
    initial_policy, initial_observed = authority["policy"]["logical_bytes"], consumed
    rejected = False
    with patch.object(module, "physical_preflight", return_value=None):
        storage.check(authority)
        consumed += 1
        entries[".storage-" + "0" * 32] = 1
        try:
            storage.check(authority, essential=True)
        except module.CapacityError:
            rejected = True
        authority["policy"]["logical_bytes"] += 1
        storage.check(authority, essential=True)
    assert rejected
    return {"failure": {"initial_capacity_check_accepted": True, "consumed_reserved_bytes": 1,
                        "essential_check_rejected": True, "policy_bytes": initial_policy,
                        "observed_bytes_before": initial_observed, "reserved_bytes_before": growth,
                        "observed_bytes_after": consumed, "reserved_bytes_after": growth,
                        "temporary_name": ".storage-" + "0" * 32},
            "one_extra_policy_byte_control": {"essential_check_accepted": True},
            "scope": "inventory arithmetic model; physical free-space preflight mocked"}


def reconstruct(repo, target, manifest):
    requested = ["research-workspace/research_workspace", "research-workspace/recipes",
                 "research-workspace/environments.json"]
    names = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", manifest["base_commit"],
                                     "--", *requested], cwd=repo).decode().splitlines()
    assert 1 <= len(names) <= 128
    size = 0
    for name in names:
        relative = Path(name)
        assert not relative.is_absolute() and ".." not in relative.parts
        value = subprocess.check_output(["git", "show", manifest["base_commit"] + ":" + name], cwd=repo)
        size += len(value)
        assert size <= 16 * 1024 * 1024
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(value)
    patch_bytes = (HERE / "draft.patch").read_bytes()
    assert hashlib.sha256(patch_bytes).hexdigest() == manifest["patch_sha256"]
    subprocess.run(["git", "apply", "--check", str(HERE / "draft.patch")], cwd=target, check=True)
    subprocess.run(["git", "apply", str(HERE / "draft.patch")], cwd=target, check=True)
    for name, expected in manifest["draft_files"].items():
        assert hashlib.sha256((target / name).read_bytes()).hexdigest() == expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="Local Waypoint repository with the accepted base commit")
    args = parser.parse_args()
    manifest = json.loads((HERE / "draft.json").read_text())
    with tempfile.TemporaryDirectory(prefix="waypoint-blocked-draft-") as temporary:
        target = Path(temporary)
        reconstruct(args.repo.resolve(), target, manifest)
        sys.path.insert(0, str(target / "research-workspace"))
        cases = {}
        # Reconstruction above uses Git only. All subsequent process launches
        # are forbidden; the race case intercepts its specific launch attempt.
        with patch("subprocess.Popen", side_effect=AssertionError("Unexpected process launch")):
            for case in (rollback_failure, claim_hold_race, result_before_commit,
                         project_initialization_hold, consumed_headroom_model):
                root = target / case.__name__
                cases[case.__name__] = {"outcome": "BLOCKER_REPRODUCED", "observed": case(root)}
        report = {"format": "waypoint-storage-blocked-observation-v1", "base_commit": manifest["base_commit"],
                  "draft_patch_sha256": manifest["patch_sha256"], "quota_enforced": False,
                  "product_acceptance": "BLOCKED", "real_provider_processes": 0, "cases": cases}
        print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
