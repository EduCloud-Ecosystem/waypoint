"""Real browser/HTTP guard control; simulated compute and fresh private state."""
import argparse
from contextlib import closing
import datetime as dt
import json
from pathlib import Path
import secrets
import sys
import threading

from research_workspace.server import Application, Handler, Server
from research_workspace.operation_guard import sync_directory
from research_workspace.store import Store, canonical, private_write
from verify_oidc import Pilot, CLI


class SimulatedBroker:
    wait = None
    entered = None

    def init_project(self, pid, budget, *, launch=None):
        if launch:
            launch(lambda: None)
        if self.entered is not None:
            self.entered.set()
        if self.wait is not None and not self.wait.wait(30):
            raise RuntimeError("Synthetic initialization did not resume.")

    def usage(self, pid):
        return {"budget_cpu_seconds": 120, "remaining_cpu_seconds": 120}


def exercise(output):
    output = Path(output).resolve()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    sync_directory(output.parent)
    images = json.loads((Path(__file__).parent / "images.lock.json").read_bytes())
    store = Store(output / "state.private", create=True, guarded=True, config={"version": 1,
        "mode": "local-guarded-public-data-v1", "broker": sys.executable, "images": images,
        "total_budget_cpu_seconds": 600}, subject="synthetic-researcher")
    broker = SimulatedBroker()
    project = store.create_project("synthetic-researcher", {"name": "Guard browser control",
        "question": "Does a pause preserve a draft?", "success_criterion": "Drafts stay editable and safety actions work.",
        "budget_cpu_seconds": 120, "ends_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7)).isoformat()},
        broker, 600, "browser-project")
    csv = "group,value\na,1\na,2\na,3\n"
    queued = store.submit(project["id"], "synthetic-researcher", {"recipe": "summary-python", "condition": "baseline", "csv": csv}, images, "queued-control")
    app = Application(store.root)
    app.broker = broker
    project_keys = []
    pause_submission = [False]
    dispatch = app.dispatch
    def observed_dispatch(method, path, subject, data, request_key, context=None):
        if method == "POST" and path == "/api/projects":
            project_keys.append(request_key)
        if method == "POST" and path.endswith("/runs") and pause_submission[0]:
            pause_submission[0] = False
            token = app.store.guard.begin("mutation", request={"synthetic_control": "hold-before-submit"})
            token.fail()
        return dispatch(method, path, subject, data, request_key, context)
    app.dispatch = observed_dispatch
    server = Server(("127.0.0.1", 0), Handler)
    server.app = app
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    pilot = Pilot.__new__(Pilot)
    pilot.root, pilot.cli, pilot.session = output, CLI, "guard-" + secrets.token_hex(5)
    pilot.operator = {"users": {"synthetic": {"password": store.initial_key}}}
    pilot.checks = []
    browser, check = pilot.browser, pilot.check
    try:
        pilot.cli_call("open", "http://127.0.0.1:" + str(server.server_port))
        pilot.cli_call("snapshot")
        browser('await page.locator("#access-key").fill(' + json.dumps(store.initial_key) + '); await page.locator("#sign-in-button").click(); await page.locator("#project-title").waitFor({state:"visible"});')
        pilot.cli_call("snapshot")
        broker.wait, broker.entered = threading.Event(), threading.Event()
        overlap_errors = []
        def overlap():
            try:
                app.store.create_project("synthetic-researcher", {"name": "Overlapping initialization", "question": "Synthetic overlap", "success_criterion": "No permanent hold", "budget_cpu_seconds": 120, "ends_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7)).isoformat()}, broker, 600, "overlap-control")
            except BaseException as exc:
                overlap_errors.append(type(exc).__name__)
        initializer = threading.Thread(target=overlap)
        initializer.start()
        if not broker.entered.wait(3):
            raise RuntimeError("Synthetic initialization did not start.")
        browser('await page.locator("#new-project").click(); await page.locator("#create-name").fill("Retry project draft"); await page.locator("#create-question").fill("Synthetic question"); await page.locator("#create-success").fill("One project for repeated request"); await page.locator("#create-submit").click(); await page.locator("#create-error").waitFor({state:"visible"});')
        check("busy_initialization_preserves_project_draft", browser('return (await page.locator("#create-name").inputValue()) === "Retry project draft" && await page.locator("#create-name").isEditable();'))
        broker.wait.set(); initializer.join(3); broker.wait = None; broker.entered = None
        browser('await page.locator("#create-submit").click(); await page.locator("#create-dialog").waitFor({state:"hidden"});')
        check("project_retry_reuses_idempotency_key", not overlap_errors and len(project_keys) == 2 and bool(project_keys[0]) and project_keys[0] == project_keys[1] and len(app.store.projects("synthetic-researcher")) == 3)
        browser('await page.locator(".project-item").filter({hasText:"Guard browser control"}).click(); await page.locator("#project-title").waitFor({state:"visible"});')
        browser('await page.locator("#csv-input").fill(' + json.dumps(csv) + '); await page.locator("#condition").selectOption("alternative");')
        check("real_browser_ready_before_fault", browser('return await page.locator("#submit-run").isEnabled();'))
        pause_submission[0] = True
        browser('await page.locator("#submit-run").click(); await page.locator("#operation-status").waitFor({state:"visible"});')
        pilot.cli_call("snapshot")
        check("server_pause_preserves_csv_and_condition", browser('return (await page.locator("#csv-input").inputValue()) === ' + json.dumps(csv) + ' && (await page.locator("#condition").inputValue()) === "alternative";'))
        check("draft_editable_submission_disabled", browser('return await page.locator("#csv-input").isEditable() && await page.locator("#submit-run").isDisabled();'))
        status = browser('return await page.evaluate(async () => { const r=await fetch("/api/operation-status"); return {status:r.status,body:await r.json()}; });')
        check("authenticated_http_pause_and_no_quota_claim", status["status"] == 200 and status["body"]["state"] == "paused" and status["body"]["quota_enforced"] is False)
        browser('await page.locator("#runs-list details summary").first().click(); await page.getByRole("button", {name:"Cancel run",exact:true}).click();')
        with closing(app.store.connect()) as db:
            check("cancellation_write_remains_available", db.execute("SELECT cancel_requested FROM runs WHERE id=?", (queued["id"],)).fetchone()[0] == 1)
        browser('await page.locator("#new-project").click(); await page.locator("#create-name").fill("Preserved project draft"); await page.locator("#create-question").fill("Synthetic question");')
        check("project_draft_editable_creation_disabled", browser('return await page.locator("#create-name").isEditable() && await page.locator("#create-submit").isDisabled() && (await page.locator("#create-name").inputValue()) === "Preserved project draft";'))
        browser('await page.locator("#dismiss-create").click(); await page.locator("#runs-list details summary").first().click(); await page.screenshot({path:' + json.dumps(str(output / "paused-workspace.png")) + ',fullPage:true});')
        browser('await page.locator("#sign-out").click(); await page.locator("#sign-in").waitFor({state:"visible"});')
        check("logout_available_during_pause", browser('return await page.locator("#sign-in").isVisible();'))
    finally:
        if broker.wait is not None:
            broker.wait.set()
        server.shutdown(); server.server_close(); thread.join(3)
        try:
            pilot.cli_call("close")
        finally:
            private_write(output / "verification.json", canonical({"format": "educloud-operation-guard-browser-v1",
                "status": "PASS" if len(pilot.checks) == 9 and all(c["status"] == "PASS" for c in pilot.checks) else "INCOMPLETE",
                "checks": pilot.checks, "quota_enforced": False,
                "evidence_boundary": "Real local browser and HTTP; simulated provider; fresh disposable guarded state; no compute execution or hosted readiness claim."}).encode())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    exercise(parser.parse_args().output)
