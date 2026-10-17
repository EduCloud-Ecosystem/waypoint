#!/usr/bin/env python3
"""Exercise the actual HTTP -> Outfitter -> Docker -> evidence workflow."""

import argparse
import datetime as dt
import hashlib
import http.cookiejar
import json
from pathlib import Path
import tempfile
import threading
import time
import urllib.error
import urllib.request

from research_workspace.evidence import verify_bundle
from research_workspace.recipes import expected_result
from research_workspace.runtime import validate_images
from research_workspace.server import Application, Handler, Server
from research_workspace.store import Store, canonical, private_write


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker", required=True)
    parser.add_argument("--python-image", required=True)
    parser.add_argument("--r-image", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    images = validate_images({"python": args.python_image, "r": args.r_image})
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    root = output / "state.private"
    store = Store(root, create=True)
    private_write(root / "config.json", canonical({"version": 1, "mode": "local-public-data", "broker": str(Path(args.broker).resolve()),
                  "images": images, "total_budget_cpu_seconds": 600}).encode())
    owner_key = store.add_user("owner")
    viewer_key = store.add_user("viewer")
    outsider_key = store.add_user("outsider")
    app = Application(root)
    server = Server(("127.0.0.1", 0), Handler)
    server.app = app
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    app.worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    checks = []
    recorded_runs = []
    report = {"scope": "local-public-data-http-docker-research-pilot", "status": "FAIL", "checks": checks,
              "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "broker_binary_sha256": hashlib.sha256(Path(args.broker).read_bytes()).hexdigest(),
              "images": images, "production_identity": False, "scientific_validity": False,
              "human_independent_reproduction": False, "cloud_resources": False}

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    def client():
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(connection, method, path, data=None, extra=None):
        payload = canonical(data).encode() if data is not None else None
        headers = {"Content-Type": "application/json", "X-Research-Request": "1"}
        headers.update(extra or {})
        req = urllib.request.Request(base+path, data=payload, method=method, headers=headers)
        try:
            response = connection.open(req, timeout=30)
        except urllib.error.HTTPError as exc:
            response = exc
        raw = response.read()
        return response.code, raw if response.headers.get_content_type() == "application/zip" else json.loads(raw)

    owner, viewer, outsider = client(), client(), client()
    def new_project(label, budget):
        status, project = request(owner, "POST", "/api/projects", {"name": label, "question": "How do declared summaries differ?",
            "success_criterion": "Independently verify numeric outputs and preserve every attempted run.",
            "ends_at": (dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=7)).isoformat(), "budget_cpu_seconds": budget})
        check(f"create_{label}", status == 201)
        return project["id"]

    def submit(pid, recipe, condition, csv, key):
        status, run = request(owner, "POST", f"/api/projects/{pid}/runs", {"recipe": recipe, "condition": condition, "csv": csv, "parameters": {}}, {"Idempotency-Key": key})
        check(f"admit_{key}", status == 202)
        recorded_runs.append((pid, run["id"]))
        return run

    def wait(pid, rid):
        for _ in range(200):
            status, run = request(owner, "GET", f"/api/projects/{pid}/runs/{rid}")
            if status != 200:
                raise AssertionError("run read failed")
            if run["status"] not in ("queued", "running"):
                return run
            time.sleep(0.25)
        raise AssertionError("bounded run deadline exceeded")

    try:
        check("anonymous_refused", request(owner, "GET", "/api/projects")[0] == 401)
        for connection, key in ((owner, owner_key), (viewer, viewer_key), (outsider, outsider_key)):
            check("opaque_key_signin", request(connection, "POST", "/api/session", {"access_key": key})[0] == 200)
        check("cross_origin_refused", request(owner, "POST", "/api/projects", {}, {"Origin": "https://untrusted.example"})[0] == 403)
        check("dns_rebinding_host_refused", request(owner, "GET", "/api/projects", extra={"Host": "untrusted.example"})[0] == 403)
        first = new_project("first-project", 120)
        check("outsider_read_refused", request(outsider, "GET", f"/api/projects/{first}")[0] == 403)
        check("viewer_enrolled", request(owner, "POST", f"/api/projects/{first}/members", {"subject": "viewer", "role": "viewer"})[0] == 200)
        check("viewer_run_refused", request(viewer, "POST", f"/api/projects/{first}/runs", {"recipe": "summary-python", "condition": "baseline", "csv": "group,value\nx,1\n"})[0] == 403)
        csv = (Path(__file__).parent / "examples/public-fixture.csv").read_text()
        a = submit(first, "summary-python", "baseline", csv, "first-python")
        same = submit(first, "summary-python", "baseline", csv, "first-python")
        check("request_deduplicated", a["id"] == same["id"])
        a = wait(first, a["id"])
        check("python_actual_result_and_cleanup", a["status"] == "succeeded" and a["cleanup_verified"] and a["metrics"] == expected_result(csv, "baseline"))
        b = wait(first, submit(first, "summary-r", "alternative", csv, "first-r")["id"])
        check("r_actual_result_and_cleanup", b["status"] == "succeeded" and b["cleanup_verified"] and b["metrics"] == expected_result(csv, "alternative"))
        check("conservative_accounting_labelled", a["compute"]["provenance"] == "estimated_upper_bound" and a["compute"]["charged_cpu_seconds"] == 30)
        status, comparison = request(owner, "GET", f"/api/projects/{first}/comparison")
        check("comparison_retains_conditions_and_limitations", status == 200 and len(comparison["runs"]) == 2 and bool(comparison["warning"]))
        status, archive = request(viewer, "GET", f"/api/projects/{first}/export")
        verified = verify_bundle(archive)
        check("viewer_export_integrity", status == 200 and verified["valid"] and verified["run_count"] == 2)
        (output / "first-project.zip").write_bytes(archive)
        check("member_removed", request(owner, "DELETE", f"/api/projects/{first}/members/viewer")[0] == 200)
        check("removed_member_export_refused", request(viewer, "GET", f"/api/projects/{first}/export")[0] == 403)
        second = new_project("second-project", 120)
        second_csv = (Path(__file__).parent / "examples/second-project.csv").read_text()
        for recipe, condition in (("summary-r", "baseline"), ("summary-python", "alternative")):
            run = wait(second, submit(second, recipe, condition, second_csv, "second-"+condition)["id"])
            check("second_project_"+condition, run["status"] == "succeeded" and run["metrics"] == expected_result(second_csv, condition) and run["cleanup_verified"])
        status, archive = request(owner, "GET", f"/api/projects/{second}/export")
        check("second_project_export", status == 200 and verify_bundle(archive)["valid"])
        (output / "second-project.zip").write_bytes(archive)
        labels = new_project("label-control", 60)
        label_csv = 'group,value\n001,+1.0\n002,.5\nNA,2e1\nNULL,-1.\n001, 3E0 \n'
        for recipe in ("summary-python", "summary-r"):
            run = wait(labels, submit(labels, recipe, "baseline", label_csv, "labels-"+recipe)["id"])
            check("exact_group_labels_"+recipe, run["status"] == "succeeded" and run["metrics"] == expected_result(label_csv, "baseline") and run["cleanup_verified"])
        check("nonportable_number_refused_before_dispatch", request(owner, "POST", f"/api/projects/{labels}/runs", {"recipe": "summary-r", "condition": "baseline", "csv": "group,value\nNA,1_000\n"})[0] == 400)
        capped = new_project("budget-control", 30)
        successful = wait(capped, submit(capped, "summary-python", "baseline", csv, "budget-first")["id"])
        denied = wait(capped, submit(capped, "summary-python", "alternative", csv, "budget-second")["id"])
        check("budget_refusal_without_dispatch_or_refund", successful["status"] == "succeeded" and denied["status"] == "failed" and denied["cleanup_verified"] and denied["compute"]["charged_cpu_seconds"] == 0)
        check("project_closed", request(owner, "POST", f"/api/projects/{first}/close", {})[0] == 200)
        check("closed_project_rejects_new_work", request(owner, "POST", f"/api/projects/{first}/runs", {"recipe": "summary-python", "condition": "baseline", "csv": csv})[0] == 400)
        check("closed_project_export_retained", request(owner, "GET", f"/api/projects/{first}/export")[0] == 200)
        restored_view = Store(root)
        check("independent_reopen_preserves_recorded_runs", len(restored_view.project(second, "owner")["runs"]) == 2)
        report["status"] = "PASS"
    except Exception as exc:
        report["failure"] = str(exc)[:250]
        raise
    finally:
        server.shutdown()
        app.worker.stop()
        server.server_close()
        thread.join(timeout=3)
        report["checks_passed"] = len(checks)
        report["all_recorded_runs_cleanup_verified"] = all(store.run(pid, rid, "owner")["cleanup_verified"] for pid, rid in recorded_runs)
        source_root = Path(__file__).parent
        sources = {path for pattern in ("research_workspace/*.py", "tests/*.py", "recipes/*.py", "recipes/*.R", "static/*", "verify.py", "images.lock.json") for path in source_root.glob(pattern) if path.is_file()}
        report["source_hashes"] = {str(path.relative_to(source_root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(sources)}
        (output / "verification.json").write_text(json.dumps(report, indent=2)+"\n")
        print(json.dumps({"status": report["status"], "checks_passed": len(checks), "cleanup_verified": report["all_recorded_runs_cleanup_verified"]}))


if __name__ == "__main__":
    main()
