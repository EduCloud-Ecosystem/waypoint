# Research workspace v1 integration contract

This build is a local, loopback-only research proof of concept using public or
synthetic tabular inputs and reviewed recipes. It is not an institutional
identity deployment or a scientific-validity claim. Existing course and AI
pilots remain independent.

## Ownership

Waypoint owns project membership, study configuration, runs, input/output
lineage, the browser interface and portable export. Outfitter owns Docker
execution, finite compute reservations, lifetime, containment and cleanup.
Portage remains the inference authority, but this first non-AI recipe does not
call it. Cairn and Belay are optional consumers, not prerequisites.

## Python layout and shared types

`research-workspace/research_workspace/` is an importable Python package.
The service uses Python's standard library, local SQLite transactions, and
private local files; no database on a shared/network filesystem. Static files
live in `research-workspace/static/`. Runtime state is ignored and private.

Project JSON: `id`, `name`, `question`, `success_criterion`, `owner`,
`created_at`, `ends_at` (UTC ISO-8601), `budget_cpu_seconds`, `status`
(`active` or `closed`). Membership is server-authoritative: owner, editor,
viewer; opaque subjects never grant themselves access.

Run JSON: `id`, `project_id`, `recipe` (`summary-python` or `summary-r`),
`condition` (`baseline` or `alternative`), `status` (`queued`, `running`,
`succeeded`, `failed`, `cancelled`, `interrupted`), `created_at`, `started_at`,
`finished_at`, `input_sha256`, `recipe_sha256`, `environment`, `parameters`,
`metrics` (object), `error` (bounded safe text), `cleanup_verified`,
`compute` (object). Uncertain execution is never silently rerun or refunded.
All results remain provisional until independent validation of the output.

## Browser/API

JSON errors use `{"error":"human-readable message"}`. Mutation requests use
same-origin JSON and `X-Research-Request: 1`. No cross-origin access. Login
uses an operator-issued opaque access key; cookies are HttpOnly/SameSite=Strict.
The bootstrap and keys never appear in committed evidence. This local identity
adapter refuses network-wide binding and is not production SSO.

- `POST /api/session` `{access_key}` → `{subject}` plus session cookie.
- `DELETE /api/session`; `GET /api/session` → `{subject}`.
- `GET /api/projects` → `{projects:[...]}`.
- `POST /api/projects` `{name,question,success_criterion,ends_at,budget_cpu_seconds}` → project (201).
- `GET /api/projects/{id}` → `{project,role,members:[{subject,role}],runs:[...],usage:{...}}`.
- `POST /api/projects/{id}/members` `{subject,role}` (owner only).
- `DELETE /api/projects/{id}/members/{subject}` (owner only, cannot remove owner).
- `POST /api/projects/{id}/close` → project, refuses new runs and cancels queued work.
- `POST /api/projects/{id}/runs` `{recipe,condition,csv,parameters:{}}` → run (202).
  CSV is capped at 64 KiB and must have columns `group,value`; no private data.
  Two reviewed conditions summarize values: baseline retains all rows;
  alternative trims one smallest/largest value per group with at least 5 rows.
- `GET /api/projects/{id}/runs/{run_id}` → run.
- `POST /api/projects/{id}/runs/{run_id}/cancel` → run.
- `GET /api/projects/{id}/comparison` → `{runs:[...],warning:"..."}`;
  this reports descriptive results, not statistical/scientific acceptance.
- `GET /api/projects/{id}/export` → downloadable application/zip evidence bundle.
- `GET /api/recipes` → `{recipes:[{id,name,description,available}]}`.

Only owners/editors can submit or cancel runs. Viewers can inspect/export project
evidence. Every operation rechecks active membership; submission, queued dispatch
and publication also recheck project status and end date. Authorized members can
still read and export retained evidence after closure or expiry. Removed users
cannot read runs or exports. Keys
belong to local subjects provisioned by an operator CLI; browser input cannot
create or switch subjects.

## Export seam

`research_workspace/evidence.py` supplies
`build_bundle(project: dict, runs: list[dict], files: dict[str, bytes]) -> bytes`
and `verify_bundle(payload: bytes) -> dict`. `files` contains only explicitly
selected relative input/output/recipe paths. Reject traversal, symlinks supplied
as files, duplicates, unsafe/oversized archive members, non-finite JSON metrics,
secrets and unexpected metadata fields. The server constructs a consistent
snapshot; members, access keys, sessions, raw logs and local paths are excluded.
Bundle contains project/run manifests, checksums, a methods/readme document,
comparison JSON and RO-Crate metadata. Verification checks membership, hashes,
record lineage and declared sizes; it does not claim scientific reproducibility.

## Acceptance

Meaningful automated tests cover authorization, cross-project access, durable
state, budget refusal, cancelled/interrupted work, immutable input lineage,
export integrity and unsafe archive rejection. Actual Docker verification must
exercise the Python and R recipes with independent expected results and verified
cleanup. A second public/synthetic project uses different inputs without core
code changes. Browser acceptance covers sign-in, project creation, submission,
comparison and export. No unexecuted control is reported as passed.
