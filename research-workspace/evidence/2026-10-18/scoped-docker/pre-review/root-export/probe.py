"""Synthetic export congruence counterexample. No HTTP, Docker, provider or markers.
Run: python3 -B probe.py. Writes only to stdout and disposable temporary extraction.
"""
from pathlib import Path, PurePosixPath
import copy, hashlib, io, json, sys, tarfile, tempfile, zipfile
ROOT=Path(__file__).resolve().parent
SOURCE_COMMIT="5fd7d445855b191fd2f20eee218a4860961cf38e"
HARNESS_SHA="0c18d10fb9fe148f2560d8e6cfb1b586ec459b415b05a5e2815bf627b264f30a"
ARCHIVE_SHA="038fc4ebf21acdb2f6142a374e93662005a69b4ce6e633820eb361c3183b5300"
FIXTURE_SHA="3b2d63b1b396b16014125c0ced32d40f836c8fc142ebe72484339cd30efb2b20"
def sha(payload):return hashlib.sha256(payload).hexdigest()
archive=(ROOT/"source.tar.gz").read_bytes(); original=(ROOT/"fixture.zip").read_bytes()
if sha(archive)!=ARCHIVE_SHA or sha(original)!=FIXTURE_SHA:raise ValueError("Input hash mismatch")
with tempfile.TemporaryDirectory(prefix="waypoint-export-congruence-") as tmp:
    target=Path(tmp)
    with tarfile.open(fileobj=io.BytesIO(archive),mode="r:gz") as tar:
        seen=set()
        for item in tar.getmembers():
            path=PurePosixPath(item.name)
            if not item.isfile() or path.is_absolute() or ".." in path.parts or item.name in seen:raise ValueError("Unsafe member")
            seen.add(item.name); dest=target/item.name
            dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(tar.extractfile(item).read())
    if sha((target/"qualify_scoped_docker.py").read_bytes())!=HARNESS_SHA:raise ValueError("Harness hash mismatch")
    sys.path.insert(0,tmp)
    from research_workspace.evidence import build_bundle,verify_bundle
    with zipfile.ZipFile(io.BytesIO(original)) as zipped:
        project=json.loads(zipped.read("project.json"));runs=json.loads(zipped.read("runs.json"))
        files={n:zipped.read(n) for n in zipped.namelist() if n.startswith("runs/")}
    ids={run["id"] for run in runs};successful=[r for r in runs if r["status"]=="succeeded"]
    outcomes=[]
    for mutation in ("wrong_numeric_result","unknown_accounting_and_failed_runs"):
        altered=copy.deepcopy(runs);selected=dict(files)
        for run in altered:
            if run["status"]!="succeeded":continue
            if mutation=="wrong_numeric_result":
                run["metrics"]["mean"]+=1
                for group in run["metrics"]["groups"]:group["mean"]+=1
                selected["runs/"+run["id"]+"/result.json"]=json.dumps(run["metrics"],sort_keys=True,separators=(",",":"),allow_nan=False).encode()
            else:
                run["status"]="failed";run["metrics"]={};run["cleanup_verified"]=False
                run["compute"]={"reserved_cpu_seconds":None,"charged_cpu_seconds":None,"provenance":"unknown","unresolved_liability":True}
                selected.pop("runs/"+run["id"]+"/result.json",None)
        payload=build_bundle(project,altered,selected); verified=verify_bundle(payload)
        with zipfile.ZipFile(io.BytesIO(payload)) as zipped:exported=json.loads(zipped.read("runs.json"))
        # Exact export predicates at immutable harness lines 512 and 514.
        passed=({run["id"] for run in exported}==ids and verified["valid"] is True and verified["run_count"]==len(ids))
        outcomes.append(dict(mutation=mutation,bundle_valid=verified["valid"],harness_export_checks_pass=passed,
            run_count=verified["run_count"],original_succeeded=len(successful),
            exported_succeeded=sum(run["status"]=="succeeded" for run in exported),bundle_sha256=sha(payload)))
    result={"source_commit":SOURCE_COMMIT,"harness_sha256":HARNESS_SHA,"fixture_sha256":sha(original),
        "mode":"synthetic in-memory export oracle only; no HTTP, Docker, provider, exercise marker or accepted state","outcomes":outcomes}
    print(json.dumps(result,indent=2))
