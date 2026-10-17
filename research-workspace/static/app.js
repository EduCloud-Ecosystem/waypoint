"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const MAX_CSV_BYTES = 64 * 1024;
  const EXAMPLE_CSV = "group,value\nA,10\nA,11\nA,12\nA,13\nA,40\nB,20\nB,21\nB,22\nB,23\nB,60\n";
  const state = {
    subject: null,
    lastSubject: null,
    projects: [],
    recipes: [],
    environments: [],
    environmentError: "",
    selected: null,
    detail: null,
    drafts: new Map(),
    submissions: new Map(),
    pollTimer: null,
    requestVersion: 0,
    runFingerprint: "",
    memberFingerprint: "",
    comparisonFingerprint: "",
    projectListFingerprint: "",
    refreshInFlight: false,
    runBusy: false,
    confirmation: null,
  };

  class RequestError extends Error {
    constructor(message, status = 0) { super(message); this.status = status; }
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function announce(message) { $("announcement").textContent = message; }
  function errorMessage(error) { return error instanceof Error ? error.message : "Something went wrong. Please try again."; }
  function showError(message) {
    $("global-error-text").textContent = message;
    $("global-error").hidden = false;
    $("global-success").hidden = true;
  }
  function clearError() { $("global-error").hidden = true; }
  function showSuccess(message) {
    $("global-success").textContent = message;
    $("global-success").hidden = false;
    clearError();
  }
  function formError(id, message) { $(id).textContent = message; $(id).hidden = !message; }

  async function api(path, { method = "GET", body, headers = {}, download = false } = {}) {
    const requestHeaders = { Accept: download ? "application/zip" : "application/json", ...headers };
    if (method !== "GET") requestHeaders["X-Research-Request"] = "1";
    if (body !== undefined) requestHeaders["Content-Type"] = "application/json";
    let response;
    try {
      response = await fetch(path, { method, headers: requestHeaders, credentials: "same-origin", cache: "no-store", ...(body !== undefined ? { body: JSON.stringify(body) } : {}) });
    } catch (_) {
      throw new RequestError("The workspace could not be reached. Check that the local service is running, then retry.");
    }
    if (!response.ok) {
      let message = `The request could not be completed (${response.status}).`;
      try { const payload = await response.json(); if (typeof payload.error === "string") message = payload.error; } catch (_) { /* No untrusted HTML error bodies are displayed. */ }
      if (response.status === 401 && state.subject) {
        showSignIn("Your session has ended. Sign in again to continue. Your open run drafts are kept for this page session.");
      }
      throw new RequestError(message, response.status);
    }
    if (download) return response.blob();
    if (response.status === 204) return null;
    const text = await response.text();
    if (!text) return null;
    try { return JSON.parse(text); } catch (_) { throw new RequestError("The service returned an unreadable response. Please retry.", 502); }
  }

  function pathFor(projectId, suffix = "") { return `/api/projects/${encodeURIComponent(projectId)}${suffix}`; }
  function readableDate(value, withTime = false) {
    if (!value) return "Not recorded";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "Not recorded";
    return new Intl.DateTimeFormat(undefined, withTime ? { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" } : { month: "short", day: "numeric", year: "numeric" }).format(date);
  }
  function number(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) return String(value ?? "Not recorded");
    return new Intl.NumberFormat(undefined, { maximumFractionDigits: 5 }).format(value);
  }
  function label(value) { return String(value).replace(/_/g, " ").replace(/\b\w/g, (char) => char.toUpperCase()).replace(/Cpu\b/g, "CPU").replace(/Id\b/g, "ID"); }
  function scalar(value) {
    if (value === null || value === undefined) return "Not recorded";
    if (typeof value === "boolean") return value ? "Yes" : "No";
    if (typeof value === "number") return number(value);
    return String(value);
  }
  function byteLength(value) { return new TextEncoder().encode(value).length; }
  function bytes(value) { return value < 1024 ? `${value} B` : `${(value / 1024).toFixed(1)} KiB`; }
  function recipeName(id) { return state.recipes.find((item) => item.id === id)?.name || (id === "summary-python" ? "Group summary · Python" : id === "summary-r" ? "Group summary · R" : String(id)); }
  function conditionName(condition) { return condition === "baseline" ? "Baseline" : condition === "alternative" ? "Alternative" : label(condition); }
  function projectExpired(project) { return new Date(project.ends_at).getTime() <= Date.now(); }
  function canRun() { return !!state.detail && ["owner", "editor"].includes(state.detail.role) && state.detail.project.status === "active" && !projectExpired(state.detail.project); }
  function statusBadge(status) {
    const classes = status === "failed" ? "failure" : ["queued", "running"].includes(status) ? "active" : ["cancelled", "interrupted"].includes(status) ? "warning" : "";
    return element("span", `badge ${classes}`, label(status));
  }

  function stopPolling() { if (state.pollTimer) clearTimeout(state.pollTimer); state.pollTimer = null; }
  function schedulePoll() {
    stopPolling();
    if (!state.subject || !state.selected || document.hidden) return;
    const active = state.detail?.runs?.some((run) => ["queued", "running"].includes(run.status));
    state.pollTimer = setTimeout(async () => {
      await refreshProject({ quiet: true });
      schedulePoll();
    }, active ? 3500 : 12000);
  }

  function saveDraft() {
    if (!state.selected) return;
    state.drafts.set(state.selected, { recipe: $("recipe").value, condition: $("condition").value, csv: $("csv-input").value, note: $("input-note").textContent });
  }
  function loadDraft() {
    const draft = state.drafts.get(state.selected);
    $("condition").value = draft?.condition || "baseline";
    $("csv-input").value = draft?.csv || "";
    $("input-note").textContent = draft?.note || "";
    $("csv-file").value = "";
    renderRecipes(draft?.recipe);
    updateInputSize();
    updateCondition();
  }
  function renderRecipes(preferred = $("recipe").value) {
    const select = $("recipe");
    select.replaceChildren();
    for (const recipe of state.recipes) {
      const option = element("option", "", `${recipe.name}${recipe.available ? "" : " · unavailable"}`);
      option.value = recipe.id;
      option.disabled = !recipe.available;
      select.append(option);
    }
    if (!state.recipes.length) {
      const option = element("option", "", "No reviewed recipes are configured"); option.value = ""; option.disabled = true; select.append(option);
    }
    const choice = state.recipes.find((item) => item.id === preferred && item.available) || state.recipes.find((item) => item.available);
    select.value = choice?.id || "";
    updateRecipe();
  }
  function updateRecipe() {
    const recipe = state.recipes.find((item) => item.id === $("recipe").value);
    $("recipe-description").textContent = recipe?.description || "Ask the operator to configure a reviewed, pinned execution environment before running an analysis.";
    renderEnvironment();
    updateRunPermissions();
  }
  function selectedEnvironment() {
    return state.environments.find((entry) => entry.recipe === $("recipe").value && entry.status === "active");
  }
  function environmentRecord(entry) {
    const section = element("section", "environment-record");
    section.append(element("h3", "", entry.name));
    section.append(element("span", `badge ${entry.status === "archived" ? "warning" : "neutral"}`, entry.status === "candidate" ? "Candidate: awaiting promotion review" : entry.status === "archived" ? "Historical replay only" : entry.available ? "Configured for new runs" : "Not configured here"));
    const definitions = element("dl", "environment-facts");
    const add = (title, value, mono = false) => { definitions.append(element("dt", "", title), element("dd", mono ? "mono" : "", value)); };
    add("Environment version", entry.id, true);
    add("Runtime", `${entry.runtime?.id === "r" ? "R" : "Python"} ${entry.runtime?.version || "version not recorded"}`);
    add("Version evidence", entry.runtime?.version_evidence);
    add("Dependencies", entry.dependencies?.statement);
    add("Included libraries", (entry.dependencies?.standard_library || []).join(", "));
    add("Additional packages", entry.dependencies?.additional_packages?.length ? entry.dependencies.additional_packages.join(", ") : "None beyond the runtime standard library");
    for (const platform of entry.platforms || []) add("Recorded platform", `${platform.os}/${platform.architecture} · ${platform.evidence_status}. ${platform.evidence_boundary}`);
    const limits = entry.limits || {};
    add("Run limits", `${limits.cpus} CPU · ${limits.memory_mb} MiB memory · ${limits.pids_limit} processes · ${limits.timeout_seconds} seconds maximum`);
    add("Input & output limits", `${bytes(limits.max_input_bytes)} input, ${number(limits.max_input_rows)} rows; ${bytes(limits.max_output_bytes)} output`);
    add("Baseline method", entry.conditions?.baseline);
    add("Alternative method", entry.conditions?.alternative);
    add("Result checks", `Mean tolerance: relative ${entry.validation?.relative_tolerance}, absolute ${entry.validation?.absolute_tolerance}. ${entry.validation?.independent_check || ""}`);
    add("Pinned runtime image", entry.image, true);
    add("Packaged recipe", entry.source, true);
    add("Recipe SHA-256", entry.recipe_sha256, true);
    add("Recipe source revision", entry.provenance?.source_revision, true);
    add("Recipe repository", entry.provenance?.repository, true);
    add("Image provenance", entry.provenance?.image_origin);
    const evidence = entry.provenance?.execution_evidence;
    add("Recorded execution", `${evidence?.status || "NOT RUN"}. ${evidence?.boundary || "No execution evidence recorded."}`);
    add("Evidence record", evidence?.reference, true);
    section.append(definitions);
    const rebuild = entry.provenance?.clean_rebuild;
    section.append(element("p", "environment-boundary", `Clean image rebuild: ${rebuild?.status || "NOT RUN"}. ${rebuild?.boundary || "No source build reproduction is recorded."}`));
    if (entry.limitations?.length) {
      const limitations = element("ul", "environment-limitations");
      for (const item of entry.limitations) limitations.append(element("li", "", item));
      section.append(limitations);
    }
    return section;
  }
  function renderEnvironment() {
    const target = $("environment-content");
    const expanded = new Set([...target.querySelectorAll("details[open]")].map((node) => node.dataset.environmentId));
    target.replaceChildren();
    const current = selectedEnvironment();
    $("environment-summary").textContent = state.environmentError || (!state.environments.length ? "Reviewed environment details are unavailable. Refresh before starting a new run." : current ? `${current.runtime.id === "r" ? "R" : "Python"} ${current.runtime.version} · version ${current.version} · no additional packages. ${current.available ? "Ready on this service." : "This exact environment is not configured here."}` : "Inspect the reviewed versions below. No environment is selected for a new run.");
    if (current) target.append(environmentRecord(current));
    const others = state.environments.filter((entry) => entry.id !== current?.id);
    if (others.length) target.append(element("p", "environment-others", "Other reviewed versions"));
    for (const entry of others) {
      const details = element("details", "environment-version");
      details.dataset.environmentId = entry.id;
      details.open = expanded.has(entry.id);
      details.append(element("summary", "", `${entry.name}${entry.status === "archived" ? " · archived" : entry.status === "candidate" ? " · candidate" : ""}`), environmentRecord(entry));
      target.append(details);
    }
  }
  function updateCondition() {
    $("condition-help").textContent = $("condition").value === "alternative" ? "For each group with at least five rows, remove one smallest and one largest value before summarizing. Smaller groups keep all rows." : "Baseline summarizes all values in each group.";
  }
  function updateInputSize() {
    const size = byteLength($("csv-input").value);
    $("csv-size").textContent = `${bytes(size)} / 64 KiB`;
    $("csv-input").setCustomValidity(size > MAX_CSV_BYTES ? "Input data must be 64 KiB or smaller." : "");
    $("csv-size").style.color = size > MAX_CSV_BYTES ? "var(--danger)" : "";
  }
  function updateRunPermissions() {
    const permitted = canRun();
    $("run-fields").disabled = !permitted || state.runBusy;
    $("submit-run").disabled = !permitted || state.runBusy || !$("recipe").value || !selectedEnvironment()?.available;
    const readOnly = $("read-only-message");
    readOnly.hidden = permitted || !state.detail;
    if (state.detail && !permitted) readOnly.textContent = state.detail.role === "viewer" ? "You have view access. You can inspect and export this project's evidence; ask the owner for editor access to submit a run." : "This project is closed or has reached its end date. Existing evidence remains available below.";
  }

  function showSignIn(message = "") {
    saveDraft();
    stopPolling();
    state.subject = null;
    state.requestVersion += 1;
    state.refreshInFlight = false;
    document.title = "Research workspace · EduCloud";
    announce("");
    $("app").hidden = true;
    $("project-view").hidden = true;
    $("welcome").hidden = true;
    $("global-success").hidden = true;
    clearError();
    $("boot").hidden = true;
    $("sign-in").hidden = false;
    $("access-key").value = "";
    for (const id of ["create-dialog", "confirm-dialog"]) if ($(id).open) $(id).close();
    formError("sign-in-error", message);
    $("access-key").focus();
  }

  async function enterWorkspace(subject) {
    if (state.lastSubject && state.lastSubject !== subject) {
      state.drafts.clear(); state.submissions.clear(); state.selected = null; state.detail = null;
      $("create-form").reset();
    }
    state.lastSubject = subject;
    state.subject = subject;
    state.detail = null;
    state.projects = [];
    state.environments = [];
    state.environmentError = "";
    renderProjectList();
    $("project-view").hidden = true;
    $("welcome").hidden = true;
    $("global-success").hidden = true;
    $("sign-in").hidden = true;
    $("boot").hidden = true;
    $("app").hidden = false;
    $("account-subject").textContent = subject;
    $("account-subject").title = subject;
    clearError();
    $("project-loading").hidden = false;
    const results = await Promise.allSettled([api("/api/projects"), api("/api/recipes"), api("/api/environments")]);
    if (!state.subject) return;
    if (results[0].status !== "fulfilled") {
      $("project-loading").hidden = true;
      showError(errorMessage(results[0].reason));
      $("welcome").hidden = false;
      return;
    }
    state.projects = results[0].value.projects || [];
    if (results[1].status === "fulfilled") state.recipes = results[1].value.recipes || [];
    else { state.recipes = []; showError(`Recipes could not be loaded. ${errorMessage(results[1].reason)}`); }
    if (results[2].status === "fulfilled") state.environments = results[2].value.environments || [];
    else state.environmentError = "Environment details could not be loaded. Refresh before starting a new run.";
    renderProjectList();
    const selected = state.projects.find((project) => project.id === state.selected) || state.projects[0];
    state.selected = null;
    if (selected) await selectProject(selected.id);
    else showWelcome();
  }

  function showWelcome() {
    state.selected = null; state.detail = null;
    $("welcome").hidden = false;
    $("project-view").hidden = true;
    $("project-loading").hidden = true;
    document.title = "Research workspace · EduCloud";
    stopPolling();
  }

  function renderProjectList() {
    const fingerprint = JSON.stringify([state.selected, state.projects]);
    if (fingerprint === state.projectListFingerprint) return;
    state.projectListFingerprint = fingerprint;
    const list = $("project-list"); list.replaceChildren();
    $("project-count").textContent = state.projects.length;
    $("project-list-empty").hidden = state.projects.length > 0;
    for (const project of state.projects) {
      const button = element("button", "project-item"); button.type = "button";
      button.setAttribute("aria-current", String(project.id === state.selected));
      const icon = element("span", "project-icon", "↗"); icon.setAttribute("aria-hidden", "true");
      const copy = element("span");
      copy.append(element("span", "project-item-name", project.name));
      copy.append(element("span", "project-item-state", project.status === "closed" ? "Closed · evidence available" : projectExpired(project) ? "Ended · evidence available" : "Active project"));
      button.append(icon, copy); button.addEventListener("click", () => selectProject(project.id)); list.append(button);
    }
  }

  async function selectProject(id) {
    if (state.selected === id && state.detail) return;
    saveDraft(); stopPolling();
    state.selected = id; state.detail = null;
    state.runFingerprint = ""; state.memberFingerprint = ""; state.comparisonFingerprint = "";
    state.refreshInFlight = false;
    state.requestVersion += 1;
    $("project-view").hidden = true; $("welcome").hidden = true; $("project-loading").hidden = false;
    $("global-success").hidden = true;
    clearError(); renderProjectList(); loadDraft(); setTab("workspace");
    await refreshProject();
    schedulePoll();
  }

  async function refreshProject({ quiet = false } = {}) {
    if (!state.selected || !state.subject || state.refreshInFlight) return;
    const id = state.selected;
    const version = state.requestVersion;
    state.refreshInFlight = true;
    $("refresh-project").disabled = true;
    try {
      const detail = await api(pathFor(id));
      if (id !== state.selected || version !== state.requestVersion || !state.subject) return;
      state.detail = detail;
      const index = state.projects.findIndex((project) => project.id === id);
      if (index >= 0) state.projects[index] = detail.project;
      renderProject();
      $("project-loading").hidden = true; $("project-view").hidden = false;
      $("poll-status").textContent = `Updated ${new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(new Date())}. This project refreshes automatically while the page is visible.`;
      try {
        const comparison = await api(pathFor(id, "/comparison"));
        if (id === state.selected && version === state.requestVersion && state.subject) renderComparison(comparison);
      } catch (error) {
        if (id !== state.selected || version !== state.requestVersion) return;
        $("comparison-empty").hidden = false;
        $("comparison-empty").textContent = `Comparison could not be refreshed. ${errorMessage(error)}`;
      }
    } catch (error) {
      if (id !== state.selected || version !== state.requestVersion || !state.subject) return;
      $("project-loading").hidden = true;
      if ([403, 404].includes(error.status)) {
        state.projects = state.projects.filter((project) => project.id !== id);
        showWelcome(); renderProjectList();
        showError("This project is no longer available to your account. Your permissions may have changed.");
      } else if (quiet) $("poll-status").textContent = `Automatic refresh paused after an error. ${errorMessage(error)} The next refresh will retry.`;
      else showError(errorMessage(error));
    } finally {
      if (version === state.requestVersion) state.refreshInFlight = false;
      $("refresh-project").disabled = false;
    }
  }

  function renderProject() {
    const { project, role, runs = [], members = [], usage = {} } = state.detail;
    document.title = `${project.name} · EduCloud research`;
    $("project-title").textContent = project.name;
    $("project-question").textContent = project.question;
    $("project-state").textContent = project.status === "closed" ? "Closed" : projectExpired(project) ? "Ended" : "Active";
    $("project-state").className = `badge${project.status === "closed" || projectExpired(project) ? " neutral" : ""}`;
    $("project-role").textContent = `${label(role)} access`;
    $("project-budget").textContent = `${number(project.budget_cpu_seconds)} CPU seconds`;
    $("project-run-count").textContent = runs.length;
    const completed = runs.filter((run) => run.status === "succeeded").length;
    const active = runs.filter((run) => ["queued", "running"].includes(run.status)).length;
    $("project-run-summary").textContent = `${completed} completed${active ? ` · ${active} in progress` : ""}`;
    $("project-end").textContent = readableDate(project.ends_at);
    $("project-end").title = readableDate(project.ends_at, true);
    $("member-count").textContent = members.length;
    const unavailable = project.status !== "active" || projectExpired(project);
    $("project-unavailable").hidden = !unavailable;
    $("project-unavailable").textContent = project.status === "closed" ? "This project is closed. New runs are disabled; its recorded evidence can still be inspected and exported." : "This project's end date has passed. New runs are disabled; its recorded evidence can still be inspected and exported.";
    for (const [id, value] of [["details-question", project.question], ["details-success", project.success_criterion], ["details-owner", project.owner], ["details-created", readableDate(project.created_at, true)], ["details-id", project.id]]) $(id).textContent = value;
    $("usage-details").replaceChildren(renderMetrics(usage));
    $("close-section").hidden = role !== "owner" || project.status !== "active";
    $("member-form").hidden = role !== "owner";
    $("membership-readonly").hidden = role === "owner";
    updateRunPermissions(); renderRuns(runs); renderMembers(members, role, project.owner); renderProjectList();
  }

  function appendMeta(list, title, value, mono = false) {
    list.append(element("dt", "", title));
    list.append(element("dd", mono ? "mono" : "", scalar(value)));
  }

  function renderMetrics(metrics, depth = 0) {
    const wrapper = element("div");
    if (!metrics || typeof metrics !== "object" || !Object.keys(metrics).length) {
      wrapper.append(element("p", "metrics-empty", "No metrics recorded yet.")); return wrapper;
    }
    const flat = element("dl", "metrics");
    for (const [key, value] of Object.entries(metrics)) {
      if (value !== null && typeof value === "object") continue;
      flat.append(element("dt", "", label(key)), element("dd", "", scalar(value)));
    }
    if (flat.childElementCount) wrapper.append(flat);
    for (const [key, value] of Object.entries(metrics)) {
      if (value === null || typeof value !== "object") continue;
      const section = element("section", "metrics-section"); section.append(element("h4", "", label(key)));
      if (Array.isArray(value) && value.length && value.every((row) => row && typeof row === "object" && !Array.isArray(row) && Object.values(row).every((cell) => cell === null || typeof cell !== "object"))) {
        const keys = [...new Set(value.flatMap((row) => Object.keys(row)))];
        if (keys.includes("group")) keys.splice(0, 0, ...keys.splice(keys.indexOf("group"), 1));
        const wrap = element("div", "metrics-table-wrap"); const table = element("table", "metrics-table");
        const caption = element("caption", "sr-only", `${label(key)} recorded metrics`); table.append(caption);
        const head = element("thead"); const headRow = element("tr");
        for (const item of keys) { const cell = element("th", "", label(item)); cell.scope = "col"; headRow.append(cell); } head.append(headRow); table.append(head);
        const body = element("tbody");
        for (const row of value) { const tr = element("tr"); for (const item of keys) tr.append(element("td", "", scalar(row[item]))); body.append(tr); } table.append(body); wrap.append(table); section.append(wrap);
      } else if (depth < 3 && !Array.isArray(value)) section.append(renderMetrics(value, depth + 1));
      else section.append(element("p", "mono", JSON.stringify(value)));
      wrapper.append(section);
    }
    return wrapper;
  }

  function renderRuns(runs) {
    const fingerprint = JSON.stringify([runs, canRun()]);
    if (fingerprint === state.runFingerprint) return;
    state.runFingerprint = fingerprint;
    const list = $("runs-list");
    const expanded = new Set([...list.querySelectorAll("details[open]")].map((node) => node.dataset.runId));
    const focused = document.activeElement?.closest("details[data-run-id]");
    const focusedId = focused?.dataset.runId;
    const focusCancel = document.activeElement?.classList.contains("cancel-run");
    list.replaceChildren(); $("runs-empty").hidden = runs.length > 0;
    const sorted = [...runs].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
    for (const run of sorted) {
      const item = element("details", "run-item"); item.dataset.runId = run.id; item.open = expanded.has(run.id);
      const summary = element("summary");
      const heading = element("span", "run-item-heading");
      heading.append(element("span", "run-title", `${conditionName(run.condition)} · ${recipeName(run.recipe)}`), element("span", "run-date", readableDate(run.created_at, true)));
      const disclosure = element("span", "run-disclosure", item.open ? "−" : "+"); disclosure.setAttribute("aria-hidden", "true");
      item.addEventListener("toggle", () => { disclosure.textContent = item.open ? "−" : "+"; });
      summary.append(heading, statusBadge(run.status), disclosure); item.append(summary);
      const body = element("div", "run-body");
      if (run.error) body.append(element("p", "run-error", run.error));
      if (["queued", "running"].includes(run.status)) body.append(element("p", "run-queued", run.status === "queued" ? "Waiting for the worker. You can close the browser; the service keeps the run record." : "The reviewed recipe is running. Results will appear when execution and recording finish."));
      if (run.status === "interrupted") body.append(element("p", "run-error", "Execution was interrupted. This run will not be silently repeated. Ask the operator to review any unresolved compute reservation."));
      const meta = element("dl", "run-meta");
      appendMeta(meta, "Run", run.id, true); appendMeta(meta, "Finished", readableDate(run.finished_at, true));
      appendMeta(meta, "Cleanup verified", run.cleanup_verified === true ? "Yes" : run.cleanup_verified === false ? "Not verified" : "Not recorded");
      appendMeta(meta, "Input SHA-256", run.input_sha256, true); appendMeta(meta, "Recipe SHA-256", run.recipe_sha256, true);
      if (typeof run.environment === "string") appendMeta(meta, "Environment", run.environment, true);
      body.append(meta);
      if (run.metrics && Object.keys(run.metrics).length) { body.append(element("h3", "subheading", "Recorded results"), renderMetrics(run.metrics)); body.append(element("p", "field-help", "Provisional descriptive results. Independent validation is required.")); }
      if (run.compute && Object.keys(run.compute).length) body.append(element("h3", "subheading", "Compute record"), renderMetrics(run.compute));
      if (run.environment && typeof run.environment === "object") body.append(element("h3", "subheading", "Execution environment"), renderMetrics(run.environment));
      if (run.parameters && Object.keys(run.parameters).length) body.append(element("h3", "subheading", "Parameters"), renderMetrics(run.parameters));
      if (canRun() && ["queued", "running"].includes(run.status)) {
        const actions = element("div", "run-actions"); const cancel = element("button", "button secondary cancel-run", "Cancel run"); cancel.type = "button";
        cancel.addEventListener("click", () => cancelRun(run, cancel)); actions.append(cancel); body.append(actions);
      }
      item.append(body); list.append(item);
      if (focusedId === run.id) (focusCancel && item.querySelector(".cancel-run") ? item.querySelector(".cancel-run") : summary).focus({ preventScroll: true });
    }
  }

  function renderComparison(comparison) {
    const fingerprint = JSON.stringify(comparison);
    const runs = (comparison.runs || []).filter((run) => run.status === "succeeded");
    $("comparison-warning").textContent = comparison.warning || "These summaries describe the supplied data. They do not establish scientific validity or statistical significance.";
    $("comparison-empty").hidden = runs.length > 0;
    $("comparison-empty").textContent = "Completed runs will appear here. For a useful comparison, use the same input data and recipe for both conditions.";
    if (fingerprint === state.comparisonFingerprint) return;
    state.comparisonFingerprint = fingerprint;
    const content = $("comparison-content"); content.replaceChildren();
    const signatures = new Set(runs.map((run) => `${run.input_sha256}:${run.recipe_sha256}`));
    if (signatures.size > 1) content.append(element("p", "comparison-mismatch", "These runs use different inputs or recipe versions. Inspect their recorded identifiers before comparing results; a difference may reflect more than the method condition."));
    for (const run of [...runs].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))) {
      const card = element("article", "comparison-run");
      const heading = element("h3", "", conditionName(run.condition)); heading.append(statusBadge(run.status));
      card.append(heading, element("p", "comparison-meta", `${recipeName(run.recipe)} · ${readableDate(run.finished_at || run.created_at, true)}`), renderMetrics(run.metrics));
      card.append(element("p", "comparison-lineage", `Run ${run.id} · Input ${run.input_sha256 || "not recorded"}`));
      content.append(card);
    }
  }

  function renderMembers(members, role, owner) {
    const fingerprint = JSON.stringify([members, role, owner]);
    if (fingerprint === state.memberFingerprint) return;
    state.memberFingerprint = fingerprint;
    const list = $("members-list"); list.replaceChildren();
    for (const member of members) {
      const row = element("li", "member-item"); const copy = element("div");
      copy.append(element("span", "member-subject", `${member.subject}${member.subject === state.subject ? " (you)" : ""}`), element("span", "member-role", label(member.role)));
      row.append(copy);
      if (role === "owner" && member.subject !== owner && member.role !== "owner") {
        const remove = element("button", "text-button", "Remove"); remove.type = "button"; remove.setAttribute("aria-label", `Remove ${member.subject} from project`);
        remove.addEventListener("click", () => confirmAction({ title: "Remove project access?", description: `${member.subject} will lose access to this project's runs and exports. Their operator-provisioned account is not deleted.`, action: "Remove access", callback: () => removeMember(member.subject) })); row.append(remove);
      }
      list.append(row);
    }
  }

  function setTab(name, focus = false) {
    for (const tabName of ["workspace", "details"]) {
      const active = name === tabName;
      $(`${tabName}-tab`).setAttribute("aria-selected", String(active));
      $(`${tabName}-tab`).tabIndex = active ? 0 : -1;
      $(`${tabName}-panel`).hidden = !active;
    }
    if (focus) $(`${name}-tab`).focus();
  }

  function openCreate() {
    formError("create-error", "");
    if (!$("create-end").value) {
      const date = new Date(); date.setDate(date.getDate() + 7);
      $("create-end").value = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
    }
    const now = new Date(); $("create-end").min = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
    const latest = new Date(); latest.setDate(latest.getDate() + 365);
    $("create-end").max = `${latest.getFullYear()}-${String(latest.getMonth() + 1).padStart(2, "0")}-${String(latest.getDate()).padStart(2, "0")}`;
    $("create-dialog").showModal(); $("create-name").focus();
  }

  function confirmAction({ title, description, action, callback }) {
    state.confirmation = callback;
    $("confirm-title").textContent = title;
    $("confirm-description").textContent = description;
    $("confirm-action").textContent = action;
    $("confirm-dialog").showModal(); $("confirm-cancel").focus();
  }

  async function removeMember(subject) {
    const id = state.selected;
    await api(pathFor(id, `/members/${encodeURIComponent(subject)}`), { method: "DELETE" });
    if (id === state.selected) { showSuccess("Project access removed."); await refreshProject(); }
  }

  async function cancelRun(run, button) {
    const id = state.selected; button.disabled = true; button.textContent = "Cancelling…";
    try {
      await api(pathFor(id, `/runs/${encodeURIComponent(run.id)}/cancel`), { method: "POST", body: {} });
      if (id === state.selected) { showSuccess("Cancellation requested. The run record will show the confirmed outcome."); await refreshProject(); }
    } catch (error) { if (state.subject) showError(errorMessage(error)); }
    finally { button.disabled = false; button.textContent = "Cancel run"; schedulePoll(); }
  }

  $("sign-in-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const button = $("sign-in-button"); button.disabled = true; button.textContent = "Opening workspace…"; formError("sign-in-error", "");
    const key = $("access-key").value; $("access-key").value = "";
    try { const session = await api("/api/session", { method: "POST", body: { access_key: key } }); await enterWorkspace(session.subject); }
    catch (error) { formError("sign-in-error", errorMessage(error)); }
    finally { button.disabled = false; button.textContent = "Open workspace"; }
  });
  $("sign-out").addEventListener("click", async () => {
    $("sign-out").disabled = true;
    try { await api("/api/session", { method: "DELETE" }); showSignIn(); state.drafts.clear(); state.submissions.clear(); state.selected = null; state.detail = null; $("create-form").reset(); }
    catch (error) { if (state.subject) showError(`Sign-out was not confirmed. ${errorMessage(error)}`); }
    finally { $("sign-out").disabled = false; }
  });
  $("dismiss-error").addEventListener("click", clearError);
  $("refresh-list").addEventListener("click", async () => {
    const button = $("refresh-list"); button.disabled = true;
    try {
      const payload = await api("/api/projects"); state.projects = payload.projects || []; renderProjectList(); clearError();
      if (state.selected && !state.projects.some((project) => project.id === state.selected)) { saveDraft(); showWelcome(); renderProjectList(); }
      if (!state.selected && state.projects.length) await selectProject(state.projects[0].id);
      else if (!state.selected) showWelcome();
      announce("Project list refreshed.");
    } catch (error) { if (state.subject) showError(errorMessage(error)); }
    finally { button.disabled = false; }
  });
  $("new-project").addEventListener("click", openCreate);
  $("welcome-create").addEventListener("click", openCreate);
  $("dismiss-create").addEventListener("click", () => $("create-dialog").close());
  $("cancel-create").addEventListener("click", () => $("create-dialog").close());
  $("confirm-cancel").addEventListener("click", () => $("confirm-dialog").close());
  $("confirm-dialog").addEventListener("close", () => { state.confirmation = null; });
  $("confirm-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const callback = state.confirmation; if (!callback) return;
    $("confirm-action").disabled = true;
    try { await callback(); $("confirm-dialog").close(); }
    catch (error) { if (state.subject) { $("confirm-dialog").close(); showError(errorMessage(error)); } }
    finally { $("confirm-action").disabled = false; }
  });
  $("create-form").addEventListener("submit", async (event) => {
    event.preventDefault(); formError("create-error", "");
    const end = new Date(`${$("create-end").value}T23:59:59`);
    if (!Number.isFinite(end.getTime()) || end.getTime() <= Date.now()) { formError("create-error", "Choose an end date in the future."); return; }
    const body = { name: $("create-name").value.trim(), question: $("create-question").value.trim(), success_criterion: $("create-success").value.trim(), ends_at: end.toISOString(), budget_cpu_seconds: Number($("create-budget").value) };
    $("create-submit").disabled = true; $("create-submit").textContent = "Creating…";
    try {
      const project = await api("/api/projects", { method: "POST", body });
      state.projects.unshift(project); $("create-dialog").close(); $("create-form").reset();
      await selectProject(project.id); showSuccess("Project created. Add public or synthetic data to start your first run."); $("project-title").scrollIntoView({ block: "start" }); announce("Project created.");
    } catch (error) { formError("create-error", errorMessage(error)); }
    finally { $("create-submit").disabled = false; $("create-submit").textContent = "Create project"; }
  });

  for (const name of ["workspace", "details"]) {
    $(`${name}-tab`).addEventListener("click", () => setTab(name));
    $(`${name}-tab`).addEventListener("keydown", (event) => {
      if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) { event.preventDefault(); setTab(event.key === "Home" ? "workspace" : event.key === "End" ? "details" : name === "workspace" ? "details" : "workspace", true); }
    });
  }
  $("recipe").addEventListener("change", () => { updateRecipe(); saveDraft(); });
  $("condition").addEventListener("change", () => { updateCondition(); saveDraft(); });
  $("csv-input").addEventListener("input", () => { updateInputSize(); $("input-note").textContent = ""; saveDraft(); });
  $("use-example").addEventListener("click", () => {
    $("csv-input").value = EXAMPLE_CSV;
    $("input-note").textContent = "Synthetic example loaded: two groups of five values, each with a high extreme value. It is demonstration data, not a research finding.";
    updateInputSize(); saveDraft(); announce("Synthetic example data loaded.");
  });
  $("csv-file").addEventListener("change", async (event) => {
    const file = event.target.files?.[0]; if (!file) return;
    if (file.size > MAX_CSV_BYTES) { showError("That file is larger than 64 KiB. Choose a smaller CSV file; your existing input has been kept."); event.target.value = ""; return; }
    const id = state.selected;
    try {
      const text = await file.text();
      if (id !== state.selected) return;
      if (byteLength(text) > MAX_CSV_BYTES) throw new Error("That input is larger than 64 KiB after reading. Your existing input has been kept.");
      $("csv-input").value = text; $("input-note").textContent = `Loaded ${file.name}. Check that it contains public or synthetic data before submitting.`;
      updateInputSize(); saveDraft(); clearError(); announce("CSV loaded. Review it before starting a run.");
    } catch (error) { showError(errorMessage(error)); }
    event.target.value = "";
  });
  $("run-form").addEventListener("submit", async (event) => {
    event.preventDefault(); if (!canRun() || state.runBusy || !selectedEnvironment()?.available) return;
    updateInputSize(); if (!$("run-form").reportValidity()) return;
    const id = state.selected;
    const body = { recipe: $("recipe").value, condition: $("condition").value, csv: $("csv-input").value, parameters: {} };
    const fingerprint = JSON.stringify(body);
    let submission = state.submissions.get(id);
    if (!submission || submission.fingerprint !== fingerprint) submission = { fingerprint, key: crypto.randomUUID() };
    state.submissions.set(id, submission);
    state.runBusy = true; updateRunPermissions(); $("submit-run").textContent = "Submitting…"; clearError(); saveDraft();
    try {
      await api(pathFor(id, "/runs"), { method: "POST", body, headers: { "Idempotency-Key": submission.key } });
      state.submissions.delete(id);
      if (state.selected === id) { showSuccess(`${conditionName(body.condition)} run submitted. Its progress will appear in Project runs.`); await refreshProject(); announce("Run submitted."); }
    } catch (error) {
      if (error.status > 0 && error.status < 500) state.submissions.delete(id);
      if (state.subject && state.selected === id) showError(`${errorMessage(error)}${!error.status || error.status >= 500 ? " The submission outcome is uncertain. Retry with the same recipe, condition, and data to reuse this request safely." : ""}`);
    } finally {
      state.runBusy = false; $("submit-run").textContent = "Start run"; updateRunPermissions(); schedulePoll();
    }
  });
  $("refresh-project").addEventListener("click", async () => {
    clearError();
    const results = await Promise.allSettled([api("/api/recipes"), api("/api/environments")]);
    if (!state.subject) return;
    state.recipes = results[0].status === "fulfilled" ? results[0].value.recipes || [] : [];
    state.environments = results[1].status === "fulfilled" ? results[1].value.environments || [] : [];
    state.environmentError = results[1].status === "fulfilled" ? "" : "Environment details could not be loaded. Refresh before starting a new run.";
    if (results[0].status === "rejected") showError(errorMessage(results[0].reason));
    renderRecipes();
    await refreshProject(); schedulePoll();
  });
  $("member-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const button = event.target.querySelector('button[type="submit"]'); const id = state.selected;
    const subject = $("member-subject").value.trim();
    if (!subject) { $("member-subject").focus(); return; }
    if (subject === state.detail?.project.owner) { showError("The owner already has full access and cannot be reassigned through this form."); return; }
    button.disabled = true;
    try { await api(pathFor(id, "/members"), { method: "POST", body: { subject, role: $("member-role").value } }); if (id === state.selected) { $("member-subject").value = ""; showSuccess("Collaborator access updated."); await refreshProject(); } }
    catch (error) { if (state.subject) showError(errorMessage(error)); }
    finally { button.disabled = false; }
  });
  $("close-project").addEventListener("click", () => {
    const id = state.selected;
    confirmAction({ title: "Close this project?", description: "New runs will stop and queued work will be cancelled. Existing evidence remains available to current project members. This workspace does not offer reopening.", action: "Close project", callback: async () => {
      await api(pathFor(id, "/close"), { method: "POST", body: {} });
      if (id === state.selected) { showSuccess("Project closed. Its recorded evidence remains available for inspection and export."); await refreshProject(); }
    } });
  });
  $("export-project").addEventListener("click", async () => {
    const id = state.selected; const name = state.detail?.project.name || "research-project"; const button = $("export-project"); button.disabled = true; button.textContent = "Preparing export…";
    try {
      const blob = await api(pathFor(id, "/export"), { download: true });
      const url = URL.createObjectURL(blob); const link = element("a"); link.href = url; link.download = `${name.replace(/[^a-z0-9-]/gi, "-").replace(/-+/g, "-").slice(0, 70)}-evidence.zip`; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 60000);
      if (state.selected === id) showSuccess("Evidence bundle prepared and download started. The export records provenance and integrity; it does not establish scientific validity.");
    } catch (error) { if (state.subject) showError(errorMessage(error)); }
    finally { button.disabled = false; button.textContent = "↓ Export evidence"; }
  });
  document.addEventListener("visibilitychange", () => { if (document.hidden) stopPolling(); else { refreshProject({ quiet: true }).then(schedulePoll); } });
  window.addEventListener("pagehide", stopPolling);

  api("/api/session").then((session) => enterWorkspace(session.subject)).catch((error) => showSignIn(error.status === 401 ? "" : errorMessage(error)));
})();
