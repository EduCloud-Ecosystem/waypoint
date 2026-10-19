"""Generate invented counters for calculation tests; never execute a workload."""

import argparse
import copy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from research_workspace.assessment import canonical_bytes, manifest_digest
from research_workspace.catalog import list_environments


def scenario():
    value = {"schema": "waypoint.assessment-scenario/v1", "scenario_id": "a"*32,
            "target_rows": 25, "stage_executions": 1, "experiments": 2, "repetitions": 3,
            "retry_attempts_per_execution": 0,
            "overheads": {"setup": {"scope": "once", "cpu_nanoseconds": 100, "duration_nanoseconds": 200},
                          "transfer": {"scope": "per_attempt", "cpu_nanoseconds": 0, "duration_nanoseconds": 0},
                          "idle": {"scope": "per_attempt", "cpu_nanoseconds": 0, "duration_nanoseconds": 0}},
            "held_out_tolerance": {"relative_parts_per_million": 100000, "absolute_nanoseconds": 0}}
    for overhead in value["overheads"].values():
        overhead["basis"] = "additional_outside_measured_execution"
    return value


def series(*, alternative=False):
    fixtures = ROOT / "assessment/fixtures"
    manifest = json.loads((fixtures / "research-default.json").read_text())
    manifest["assessment_id"] = ("b" if alternative else "a")*32
    manifest["reservations"] = []
    if alternative:
        entry = next(e for e in list_environments() if e["recipe"] == "summary-r" and e["status"] == "active")
        manifest["workload"].update(recipe_id=entry["recipe"], recipe_sha256=entry["recipe_sha256"],
                                     environment_id=entry["id"], image=entry["image"])
    for role, sizes in (("fitting", [10, 20, 30]), ("held_out", [40])):
        for size in sizes:
            for repetition in (1, 2, 3):
                identity = f'{len(manifest["reservations"]) + (101 if alternative else 1):032x}'
                manifest["reservations"].append({"attempt_id": identity, "reservation_id": identity,
                    "stage_id": "analysis", "sample_role": role, "sample_size": size, "repetition": repetition})
    receipts = []
    for slot in manifest["reservations"]:
        receipt = json.loads((fixtures / "legacy-receipts.json").read_text())
        binding = {**slot, "manifest_sha256": manifest_digest(manifest),
                   "assessment_id": manifest["assessment_id"], "project_id": manifest["project_id"],
                   "environment_id": manifest["workload"]["environment_id"], "image": manifest["workload"]["image"],
                   "hardware": manifest["hardware"], "measurement_method": manifest["measurement"]}
        for kind in ("measurement", "cleanup"):
            receipt[kind]["binding"] = copy.deepcopy(binding)
            receipt[kind]["observation"]["source_kind"] = "synthetic_fixture"
        cleanup = receipt["cleanup"]
        cleanup["receipt_id"] = "synthetic-" + slot["attempt_id"]
        cleanup["method"]["name"] = "unverified_scope_observation"
        for i, scope in enumerate(cleanup["observed_scopes"]):
            scope["scope_id"] = slot["attempt_id"] if i == 0 else manifest["assessment_id"]
        measurement = receipt["measurement"]
        measurement["cleanup_receipt_id"] = cleanup["receipt_id"]
        measurement["cleanup_receipt_sha256"] = manifest_digest(cleanup)
        measurement["quality"] = {"status": "passed", "source": "unverified_checker"}
        size, repeat = slot["sample_size"], slot["repetition"]
        multiplier = 2 if alternative else 1
        # Deliberately invented nanosecond counters with symmetric variation.
        values = {"cpu_consumed": multiplier*(1000 + 50*size + (repeat - 2)*10),
                  "execution_duration": multiplier*(500 + 20*size + (repeat - 2)*5),
                  "peak_memory": 131072 + size*64, "scratch_peak": size*32, "output_peak": size*16}
        for name, value in values.items():
            metric = measurement["measured_consumption"][name]
            metric.update(availability="available", value=value, source="unverified_collector",
                          scope="whole_attempt" if name == "execution_duration" else "process")
        receipts.append(receipt)
    return {"schema": "waypoint.assessment-series/v1", "series_id": ("5" if alternative else "4")*32,
            "manifest": manifest, "receipts": receipts}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    output = Path(parser.parse_args().output)
    output.mkdir(parents=True, exist_ok=True)
    for name, value in (("scenario.json", scenario()), ("python-series.json", series()),
                        ("r-series.json", series(alternative=True))):
        (output / name).write_bytes(canonical_bytes(value))
