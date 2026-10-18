"""Replay the rejected candidate from an immutable Git export in fresh state."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

HEAD = "cf8befca9cef418d7e1238945872bed45d7fc6f2"
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--repository", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--full-review", action="store_true")
args = parser.parse_args()
output = args.output.resolve()
output.mkdir(parents=True, exist_ok=False)
checkout = output / "checkout"
checkout.mkdir()
archive = subprocess.check_output(["git", "archive", HEAD], cwd=args.repository)
with tarfile.open(fileobj=io.BytesIO(archive)) as source:
    source.extractall(checkout, filter="data")
retained = Path(__file__).resolve().parent / "independent"
scripts = ["claim_refusal_witness.py"]
if args.full_review:
    scripts += ["run_candidate.py", "adversarial_controls.py", "negative_controls.py"]
for name in scripts:
    shutil.copyfile(retained / name, output / name)
results = []
for name in scripts:
    command = [sys.executable, "-W", "error::ResourceWarning", str(output / name)]
    with (output / (name + ".replay.log")).open("xb") as log:
        result = subprocess.run(command, cwd=output,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                stdout=log, stderr=subprocess.STDOUT, timeout=240)
    results.append({"script": name, "exit_code": result.returncode})
    if result.returncode:
        raise SystemExit(f"Replay script failed: {name}; inspect its retained log.")
witness = json.loads((output / "claim-refusal-witness.json").read_bytes())
assert len(witness["cases"]) == 4
for case in witness["cases"]:
    assert case["claim"]["code"] == "missing_operation", case
    assert case["latest_receipt"] == "committed", case
    assert case["later_admission"]["code"] == "operation_failed", case
root_probe = json.loads((retained.parent / "root-crossprocess-takeover.json").read_bytes())
assert root_probe["revision"] == HEAD
for name, expected in root_probe["source_sha256"].items():
    assert hashlib.sha256((checkout / name).read_bytes()).hexdigest() == expected, name
probe_path = output / "root_crossprocess_probe.py"
probe_path.write_text(root_probe["probe_source"])
source = checkout / "research-workspace"
probe = subprocess.run([sys.executable, str(probe_path)], cwd=source,
                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                            "PYTHONPATH": str(source) + os.pathsep + str(source / "tests")},
                       capture_output=True, text=True, timeout=30)
(output / "root-crossprocess-probe.log").write_text(probe.stdout + probe.stderr)
assert probe.returncode == 0, probe.stderr
observation = json.loads(probe.stdout)
assert observation == root_probe["observation"], observation
report = {"head": HEAD, "original_failure_reproduced": True, "scripts": results,
          "coordinating_failure_reproduced": observation,
          "store_sha256": hashlib.sha256((checkout / "research-workspace/research_workspace/store.py").read_bytes()).hexdigest(),
          "script_sha256": {name: hashlib.sha256((output / name).read_bytes()).hexdigest() for name in scripts}}
with (output / "replay.json").open("x") as stream:
    json.dump(report, stream, indent=2)
    stream.write("\n")
print(json.dumps(report, indent=2))
