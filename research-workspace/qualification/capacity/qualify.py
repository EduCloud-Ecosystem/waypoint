#!/usr/bin/env python3
"""Disposable production-transition witnesses, never runtime admission authority.

Run from any directory. No argument can select an existing state directory.
The only capacity arithmetic below is an explicitly labeled offline model.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import stat
import sys
import threading
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parents[1]
sys.path[:0] = [str(WORKSPACE), str(WORKSPACE / "tests")]
from research_workspace import operation_guard as g, store as storage, recovery as r
from test_operation_guard import GuardFixture
from test_guarded_recovery import GuardedRecoveryTests

OBSERVATIONS = []


def record(control, classification, **evidence):
    OBSERVATIONS.append(dict(control=control, classification=classification, **evidence))


def inventory(root):
    """Stable fixture boundaries only; includes root/directories, logical bytes."""
    result = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        assert stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
        data = path.read_bytes() if stat.S_ISREG(info.st_mode) else b""
        result[str(path.relative_to(root))] = {
            "bytes": len(data), "directory": stat.S_ISDIR(info.st_mode),
            "sha256": hashlib.sha256(data).hexdigest() if data else None}
    return result


def size(files):
    return [sum(x["bytes"] for x in files.values()), len(files)]


def fits(observed, remaining, limit):
    # Model only: UNKNOWN rejects; no state, lock, admission hook or persistence.
    return remaining is not None and all(o + r <= l for o, r, l in zip(observed, remaining, limit))


def modeled_append_remaining(envelope, expected, observed, owner, current_owner, terminal):
    """Offline proof rule, deliberately conservative after any mismatch."""
    if owner != current_owner or not terminal or expected != observed:
        return envelope.copy()
    return [0, 0]


class CapacityControls(GuardFixture):
    def test_append_owned_consumption_and_unknown_growth(self):
        before = inventory(self.root)
        run = self.submit()
        data = self.store.guard.load()
        oid, intent = next((oid, item) for oid, item in data["operations"].items()
                           if item["kind"] == "submit" and item["resource_id"] == run["id"])
        self.assertEqual(self.store.guard.proof(data)[oid][2], "committed")
        prefix = "runs/" + run["id"]
        owned = {name: meta for name, meta in inventory(self.root).items()
                 if name == prefix or name.startswith(prefix + "/")}
        self.assertEqual(len(owned), 5)  # Three directories and two immutable files.
        self.assertEqual(owned[prefix + "/input/data.csv"]["sha256"], run["input_sha256"])
        recipe = next(meta for name, meta in owned.items() if "/recipe/" in name)
        self.assertEqual(recipe["sha256"], run["recipe_sha256"])
        envelope = size(owned)
        self.assertTrue(fits([0, 0], envelope, envelope))
        self.assertFalse(fits([0, 0], envelope, [envelope[0] - 1, envelope[1]]))
        self.assertFalse(fits([0, 0], envelope, [envelope[0], envelope[1] - 1]))
        # Terminal proof, resource identity and both content hashes allow only
        # this append writer's liability to move to observed occupancy.
        owner = [data["authority_id"], oid, intent["sequence"], run["id"]]
        remaining = modeled_append_remaining(envelope, owned, owned, owner, owner, True)
        self.assertTrue(fits(envelope, remaining, envelope))
        (self.root / "unowned-orphan").write_bytes(b"unrelated")
        after = inventory(self.root)
        unchanged_owned = {name: after[name] for name in owned}
        self.assertEqual(unchanged_owned, owned)
        self.assertEqual(remaining, [0, 0])
        self.assertFalse(fits([envelope[0] + 9, envelope[1] + 1], remaining, envelope))
        # Unrelated bytes do not consume the next writer's remaining liability.
        next_remaining = modeled_append_remaining(envelope, owned, owned, owner,
                                                  [*owner[:2], intent["sequence"] + 1, run["id"]], True)
        self.assertEqual(next_remaining, envelope)
        self.assertEqual(modeled_append_remaining(envelope, owned, owned, owner, owner, False), envelope)
        changed = {**owned, prefix: {**owned[prefix], "bytes": 1}}
        self.assertEqual(modeled_append_remaining(envelope, owned, changed, owner, owner, True), envelope)
        self.assertFalse(fits([9, 1], next_remaining, envelope))
        record("append_exact_fit_and_unrelated_growth", "modeled invariant",
               production_transition="Store.submit", authority_id=data["authority_id"],
               operation_id=oid, sequence=intent["sequence"], terminal_receipt="committed",
               owned_entries=owned, observed_append=envelope, remaining_append=remaining,
               next_writer_remaining=next_remaining, exact_fit_bytes_and_entries=True,
               one_byte_short_refused=True, one_entry_short_refused=True,
               unknown_file_preserved=True, whole_root_before=size(before),
               whole_root_after=size(after), scope="Only this completed append; other writers UNKNOWN")

    def test_replacement_coexistence_and_repeated_safety_write(self):
        original = os.replace
        peaks = []
        def observe(src, dst):
            if Path(dst) == self.root / g.FILE:
                old = Path(dst).read_bytes()
                new = Path(src).read_bytes()
                peaks.append({"old_bytes": len(old), "temporary_bytes": len(new),
                              "entries": 2, "old_generation": json.loads(old)["generation"],
                              "new_generation": json.loads(new)["generation"]})
            return original(src, dst)
        with patch.object(g.os, "replace", side_effect=observe):
            self.store.logout("missing-token-first")
            self.store.logout("missing-token-retry")
        self.assertGreaterEqual(len(peaks), 2)
        self.assertTrue(all(p["new_generation"] > p["old_generation"] for p in peaks))
        final = peaks[-1]
        # A completed generation does not pay for the next replacement scratch.
        self.assertFalse(fits([final["old_bytes"], 1], [final["temporary_bytes"], 1],
                              [max(final["old_bytes"], final["temporary_bytes"]), 1]))
        record("replacement_and_safety_retry", "modeled invariant", peaks=peaks,
               production_transition="Store.logout -> Guard.save", future_scratch="UNKNOWN",
               previous_generation_cannot_discharge_next=True)

    def test_guard_file_fsync_failure_protects_temp(self):
        before = (self.root / g.FILE).read_bytes()
        with patch.object(g.os, "fsync", side_effect=OSError("injected file fsync failure")):
            with self.assertRaises(OSError):
                self.store.logout("failure")
        temps = list(self.root.glob(g.TEMP + "*"))
        self.assertEqual(len(temps), 1)
        self.assertEqual((self.root / g.FILE).read_bytes(), before)
        self.assertGreater(temps[0].stat().st_size, 0)
        record("file_fsync_failure", "current witnessed failure", old_preserved=True,
               protected_temporary_bytes=temps[0].stat().st_size,
               future_replacement_liability="UNKNOWN", reclaimed_bytes=0)

    def test_guard_directory_fsync_failure_keeps_uncertainty(self):
        original = os.fsync
        old_generation = self.store.guard.load()["generation"]
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("injected directory fsync failure after rename")
            return original(fd)
        with patch.object(g.os, "fsync", side_effect=fail_directory):
            with self.assertRaises(OSError):
                self.store.logout("failure-after-rename")
        self.assertGreater(self.store.guard.load()["generation"], old_generation)
        self.assertEqual(self.store.guard.lease.failure, "authority_write_failed")
        record("directory_fsync_after_rename", "current witnessed failure",
               renamed_generation_visible=True, durable_completion_proven=False,
               capacity_credit=0, process_latched=True)

    def test_partial_append_remains_owned_but_unproved(self):
        original = Path.open
        class Partial:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): self.stream.__enter__(); return self
            def __exit__(self, *args): return self.stream.__exit__(*args)
            def write(self, content):
                self.stream.write(content[:1])
                self.stream.flush()
                raise OSError("injected partial write")
        def intercept(path, *args, **kwargs):
            stream = original(path, *args, **kwargs)
            return Partial(stream) if path.name == "data.csv" and args == ("xb",) else stream
        with patch.object(Path, "open", intercept):
            with self.assertRaises(OSError): self.submit()
        files = list((self.root / "runs").glob("*/input/data.csv"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stat().st_size, 1)
        data = self.store.guard.load()
        oid, item = next((oid, item) for oid, item in data["operations"].items() if item["kind"] == "submit")
        self.assertEqual(item["phase"], "uncertain")
        self.assertEqual(self.store.guard.proof(data)[oid][2], "started")
        record("partial_append", "current witnessed failure", operation_id=oid,
               sequence=item["sequence"], retained_bytes=1, terminal_receipt="started",
               consumption_credit=0, remaining_liability="UNKNOWN", file_preserved=True)

    def test_signin_rows_and_held_reader_wal(self):
        data = self.store.guard.load()
        ordinary = sum(not x["essential"] for x in data["operations"].values())
        # Actual production connections/settings; hold a read snapshot while
        # Store.session repeatedly writes. No PRAGMA is changed by the harness.
        with closing(self.store.connect()) as reader:
            reader.execute("BEGIN")
            initial = reader.execute("SELECT count(*) FROM sessions").fetchone()[0]
            wal = self.root / "workspace.sqlite3-wal"
            before = wal.stat().st_size if wal.exists() else 0
            for _ in range(24): self.store.session(key=self.key)
            after = wal.stat().st_size
            with closing(self.store.connect()) as observer:
                current = observer.execute("SELECT count(*) FROM sessions").fetchone()[0]
                settings = {key: observer.execute("PRAGMA " + key).fetchone()[0]
                            for key in ("page_size", "journal_mode", "synchronous", "wal_autocheckpoint", "max_page_count")}
            self.assertEqual(reader.execute("SELECT count(*) FROM sessions").fetchone()[0], initial)
        data = self.store.guard.load()
        self.assertEqual(current - initial, 24)
        self.assertEqual(sum(not x["essential"] for x in data["operations"].values()), ordinary)
        self.assertEqual(sum(x["essential"] for x in data["operations"].values()), 1)
        self.assertGreater(after, before)
        record("signin_and_reader_growth", "current witnessed failure", added_sessions=24,
               ordinary_operations_before_and_after=ordinary, essential_slots=1,
               wal_before=before, wal_after=after, settings=settings,
               finite_session_bound="UNKNOWN", finite_wal_peak="UNKNOWN")

    def test_competing_final_guard_slot(self):
        ordinary = sum(not x["essential"] for x in self.store.guard.load()["operations"].values())
        barrier = threading.Barrier(2)
        outcomes = []
        def contender(subject):
            barrier.wait(timeout=3)
            try:
                self.store.add_user(subject)
                outcomes.append("committed")
            except g.GuardPaused as exc:
                outcomes.append(exc.code)
        # Existing operation authority is used unchanged. Only its metadata slot
        # constant is narrowed for a finite boundary control, never a byte quota.
        with patch.object(g, "MAX_OPERATIONS", ordinary + 1):
            threads = [threading.Thread(target=contender, args=(s,)) for s in ("bob", "carol")]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=5); self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(outcomes), ["committed", "intent_limit"])
        record("competing_final_metadata_slot", "enforced bound", outcomes=sorted(outcomes),
               fixture_max_operations=ordinary + 1, production_default=g.MAX_OPERATIONS,
               whole_root_capacity_proven=False)


class DestinationControl(GuardedRecoveryTests):
    # Do not inherit/run the parent's unrelated test methods in this script.
    def test_destination_peak(self):
        self.submit()
        report = r.checkpoint(self.root, self.backup)
        original = r.copy_files
        copied = []
        class StopBeforeCopy(Exception): pass
        def inspect(source, destination, files):
            if Path(destination) == self.destination:
                copied.append({"already_present": size(inventory(self.destination)),
                               "copy_bytes": sum(m["bytes"] for m in files.values()),
                               "copy_files": len(files)})
                raise StopBeforeCopy()
            return original(source, destination, files)
        with patch.object(r, "copy_files", side_effect=inspect):
            with self.assertRaises(StopBeforeCopy):
                r.restore(self.backup, self.destination, trusted_sha256=report["checkpoint_sha256"], broker="unused")
        self.assertEqual(len(copied), 1)
        self.assertTrue((self.destination / "config.json").exists())
        self.assertFalse((self.destination / "workspace.sqlite3").exists())
        self.assertEqual(json.loads((self.root / "config.json").read_text())["mode"], "local-guarded-public-data-v1")
        # Even fitting the measured copy bytes is insufficient: post-copy SQLite,
        # guard, broker and cutover replacement peaks are still UNKNOWN.
        self.assertFalse(fits(copied[0]["already_present"], None, [2**63 - 1, 2**63 - 1]))
        record("destination_pre_copy_peak", "current witnessed failure", observed=copied[0],
               production_capacity_preflight=False, harness_stopped_before_copy=True,
               source_sealed=False, complete_peak="UNKNOWN",
               modeled_unknown_peak_refusal=True, enforced_peak_refusal="NOT RUN",
               provider="on-disk simulated broker; no provider process")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    contract = json.loads((HERE / "contract-v1.json").read_text())
    checked = {}
    for name, expected in contract["sources"]["waypoint"]["sha256"].items():
        actual = hashlib.sha256((WORKSPACE.parent / name).read_bytes()).hexdigest()
        if actual != expected: raise ValueError("Pinned production source changed: " + name)
        checked[name] = actual
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(CapacityControls)
    suite.addTest(DestinationControl("test_destination_peak"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    payload = {"format": "waypoint-capacity-qualification-v1", "quota_enforced": False,
               "source_sha256": checked, "python": platform.python_version(),
               "sqlite": sqlite3.sqlite_version, "platform": platform.system(),
               "tests_run": result.testsRun, "tests_passed": result.wasSuccessful(),
               "observations": OBSERVATIONS,
               "go_no_go": {"authority_growth_implementation": "GO only for separately reviewed finite-authority slice",
                            "admission_wiring": "NO-GO"},
               "not_run": ["physical disk exhaustion", "host crash/power loss", "portable SQLite/VFS ceiling",
                           "real broker/watchdog process peak", "destination enforced peak refusal",
                           "whole-root quota", "accepted-store migration"]}
    with args.output.open("x") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True); stream.write("\n")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
