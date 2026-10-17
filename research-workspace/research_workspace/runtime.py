"""Outfitter integration; the web service never executes submitted code itself."""

import json
from pathlib import Path
import re
import subprocess
import threading
import time

from .recipes import IDS, digest, validate_result
from .store import canonical, private_write


def strict_json(content):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key.")
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError("Non-finite JSON value.")
    return json.loads(content, object_pairs_hook=pairs, parse_constant=nonfinite)


def validate_receipt(receipt, run):
    """Accept the whole execution/accounting assertion, never just a status label."""
    if not isinstance(receipt, dict) or type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1:
        raise ValueError("Unsupported broker receipt schema.")
    if receipt.get("request_id") != run["id"] or receipt.get("project_id") != run["project_id"] or receipt.get("image") != run["environment"]["image"]:
        raise ValueError("Mismatched broker receipt identity or runtime.")
    for key in ("admitted", "execution_started", "cleanup_verified", "unresolved_liability"):
        if type(receipt.get(key)) is not bool:
            raise ValueError("Missing broker execution or accounting assertion.")
    for key in ("stdout", "stderr", "error"):
        if not isinstance(receipt.get(key), str) or len(receipt[key].encode()) > 65536:
            raise ValueError("Invalid bounded broker diagnostics.")
    exit_code = receipt.get("exit_code")
    if exit_code is not None and type(exit_code) is not int:
        raise ValueError("Invalid process exit code.")
    status = receipt.get("status")
    if status not in ("succeeded", "failed", "cancelled", "unknown", "reserved", "running"):
        raise ValueError("Unrecognized broker status.")
    compute = receipt.get("compute")
    if not isinstance(compute, dict) or set(compute) != {"reserved_cpu_seconds", "charged_cpu_seconds", "provenance"}:
        raise ValueError("Incomplete compute accounting.")
    reserved, charged = compute["reserved_cpu_seconds"], compute["charged_cpu_seconds"]
    if type(reserved) is not int or type(charged) is not int or not 0 <= charged <= reserved <= 30:
        raise ValueError("Invalid finite compute accounting.")
    if not receipt["admitted"]:
        if status != "failed" or receipt["execution_started"] or not receipt["cleanup_verified"] or receipt["unresolved_liability"] or reserved != 0 or charged != 0 or compute["provenance"] != "not_admitted":
            raise ValueError("Contradictory pre-admission refusal.")
    elif receipt["unresolved_liability"]:
        if reserved != 30 or charged != 0 or compute["provenance"] != "reserved":
            raise ValueError("Uncertain work must retain its full known reservation.")
    elif not receipt["cleanup_verified"] or reserved != 30 or charged != 30 or compute["provenance"] != "estimated_upper_bound":
        raise ValueError("Settled work requires verified cleanup and conservative charging.")
    if status == "succeeded" and (not receipt["admitted"] or not receipt["execution_started"] or exit_code != 0 or not receipt["cleanup_verified"] or receipt["unresolved_liability"]):
        raise ValueError("Successful execution was not established.")
    return receipt


class Broker:
    def __init__(self, executable, state):
        self.executable = str(Path(executable).resolve())
        self.state = str(Path(state).resolve())
        if not Path(self.executable).is_file():
            raise ValueError("Build the Outfitter broker and supply its executable path.")

    def command(self, *args):
        return [self.executable, "--state-dir", self.state, *args]

    def call(self, *args):
        completed = subprocess.run(self.command(*args), capture_output=True, timeout=20)
        if len(completed.stdout) > 262144 or len(completed.stderr) > 65536:
            raise ValueError("The broker returned an oversized response.")
        try:
            result = strict_json(completed.stdout)
        except (ValueError, UnicodeError) as exc:
            raise ValueError("The compute broker is unavailable or returned an invalid receipt.") from exc
        if completed.returncode != 0:
            raise ValueError("The compute broker refused the request; check its private operator diagnostics.")
        return result

    def init_project(self, project_id, budget):
        return self.call("project-init", "--project-id", project_id, "--budget-cpu-seconds", str(budget))

    def usage(self, project_id):
        return self.call("project-status", "--project-id", project_id)


class Worker:
    def __init__(self, store, broker):
        self.store = store
        self.broker = broker
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True, name="research-worker")

    def start(self):
        self.store.recover()
        self.thread.start()

    def stop(self):
        self.stopped.set()
        self.thread.join(timeout=40)

    def loop(self):
        while not self.stopped.is_set():
            run = self.store.claim()
            if run is None:
                self.stopped.wait(0.25)
                continue
            try:
                self.execute(run)
            except Exception:
                # Keep failures visible without leaking local paths or raw broker logs.
                run.update(status="interrupted", error="Execution could not be reconciled. The operator must inspect the broker receipt; no automatic retry was made.")
                self.store.finish(run)

    def execute(self, run):
        directory = self.store.root / "runs" / run["id"]
        runtime, filename = IDS[run["recipe"]]
        content = (directory / "input/data.csv").read_bytes()
        source = (directory / "recipe" / filename).read_bytes()
        if digest(content) != run["input_sha256"] or digest(source) != run["recipe_sha256"]:
            run.update(status="failed", error="Immutable input or recipe integrity failed before dispatch.", cleanup_verified=True)
            self.store.finish(run)
            return
        from .catalog import resolve_environment, recipe_source
        trusted = resolve_environment(run["recipe"], run["recipe_sha256"], run["environment"]["image"])
        if source != recipe_source(trusted):
            raise ValueError("Queued source no longer matches the trusted environment catalog.")
        if self.store.should_cancel(run["id"]):
            run.update(status="cancelled", error="Cancelled before dispatch.", cleanup_verified=True)
            self.store.finish(run)
            return
        command = (["python", f"/recipe/{filename}"] if runtime == "python" else ["Rscript", "--vanilla", f"/recipe/{filename}"])
        command += ["/input/data.csv", run["condition"]]
        request = {"schema_version": 1, "request_id": run["id"], "project_id": run["project_id"],
                   "image": run["environment"]["image"], "command": command,
                   "input_dir": str(directory / "input"), "recipe_dir": str(directory / "recipe"),
                   "cpus": 1, "memory_mb": 256, "pids_limit": 64, "timeout_seconds": 30,
                   "max_output_bytes": 65536, "isolation": "reviewed-local", "allow_supervised_reaper": True}
        request_path = directory / "request.private.json"
        private_write(request_path, canonical(request).encode())
        process = subprocess.Popen(self.broker.command("run", "--request", str(request_path)), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        cancellation_sent = False
        deadline = time.monotonic() + 70
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() > deadline:
                    process.kill()
                    process.communicate(timeout=5)
                    raise ValueError("Broker response deadline exceeded; retained reservation needs reconciliation.")
                if not cancellation_sent and (self.stopped.is_set() or self.store.should_cancel(run["id"])):
                    try:
                        self.broker.call("cancel", "--request-id", run["id"])
                    except (ValueError, subprocess.TimeoutExpired):
                        # The independent watchdog still owns the original finite deadline.
                        # Do not kill or refund a possibly running resource on an uncertain response.
                        pass
                    cancellation_sent = True
        if len(stdout) > 262144 or len(stderr) > 65536:
            raise ValueError("Oversized broker receipt.")
        if process.returncode != 0:
            raise ValueError("The broker process failed; its receipt is not an accepted outcome.")
        receipt = validate_receipt(strict_json(stdout), run)
        private_write(directory / "receipt.private.json", canonical(receipt).encode())
        compute = receipt.get("compute", {})
        run["compute"] = {key: compute[key] for key in ("reserved_cpu_seconds", "charged_cpu_seconds", "provenance") if key in compute}
        run["compute"]["unresolved_liability"] = bool(receipt.get("unresolved_liability", True))
        run["cleanup_verified"] = receipt.get("cleanup_verified") is True
        run["status"] = receipt.get("status") if receipt.get("status") in ("succeeded", "failed", "cancelled") else "interrupted"
        result = None
        if run["status"] == "succeeded":
            if not run["cleanup_verified"] or run["compute"]["unresolved_liability"]:
                run.update(status="interrupted", error="Execution completed but cleanup or accounting remains unresolved.")
            else:
                try:
                    result = strict_json(receipt.get("stdout", ""))
                    validate_result(result, content.decode(), run["condition"])
                    run["metrics"] = result
                except (ValueError, TypeError, UnicodeError):
                    run.update(status="failed", error="The output failed the independent result check.")
                    result = None
        else:
            run["error"] = "The bounded compute run did not complete. Its failure and reservation are retained; inspect private operator diagnostics."
        self.store.finish(run, result)


def validate_images(images, *, require_active=True):
    if not isinstance(images, dict) or set(images) - {"python", "r"}:
        raise ValueError("Configure only the reviewed Python and R runtimes.")
    for runtime, image in images.items():
        if not isinstance(image, str) or not re.fullmatch(r"(?:[A-Za-z0-9./_:-]+@)?sha256:[0-9a-f]{64}", image):
            raise ValueError("Runtime images must be pinned by immutable SHA-256 digest.")
        from .catalog import list_environments
        if require_active and not any(entry["available"] and entry["runtime"]["id"] == runtime for entry in list_environments(images)):
            raise ValueError(f"The configured {runtime} image has no active reviewed environment.")
        check = subprocess.run(["docker", "image", "inspect", image, "--format", "{{.Id}}"], capture_output=True, timeout=20)
        if check.returncode != 0:
            raise ValueError(f"The pinned {runtime} image is not available locally.")
    if not images:
        raise ValueError("Configure at least one reviewed runtime.")
    return images
