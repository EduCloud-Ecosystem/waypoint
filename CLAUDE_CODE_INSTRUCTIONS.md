# CLAUDE_CODE_INSTRUCTIONS.md: Complete operating instructions

This is the master instruction file for Claude Code on this project. If only
one file is read, read this one; it routes to everything else.

Auto-load note for the user: Claude Code automatically loads only a file named
CLAUDE.md. Either rename this file to CLAUDE.md, or keep a one-line CLAUDE.md
containing exactly: "Read CLAUDE_CODE_INSTRUCTIONS.md and follow it."

## 1. The goal

Stand up the EduCloud hosting layer: a self-hosted platform on Coolify where
any team developer can publish a web app (R Shiny, Shiny for Python, Dash,
Streamlit, Flask, FastAPI, Node, static HTML) to a stable URL through a
documented onboarding contract, with Keycloak-backed access control on
non-public apps. Onboard the pre-existing app portfolio as the first tenants.
The build runs in DEV MODE on placeholder sslip.io/nip.io domains; no real
domain exists or is required. The goal is complete at PLAN.1.md CHECKPOINT 9:
dev platform live, every production tenant onboarded and verified, runbook and
promotion checklist committed, Registry seam prepared.

## 2. Reading order and precedence

Read, in order, at the start of every session:
1. This file (workflow and authority)
2. HOST_SESSION_BUILD.md (architecture, repo layout, hard rules, settled
   decisions, dev mode conventions)
3. PLAN.1.md (the phased procedure; find the current phase)
4. INTEGRATION.md (boundaries and the Registry seam; consult when a task
   touches identity, build artifacts, scope, or anything EduCloud/Quad/MESA)
5. CHANGELOG.md (history; consult when documents seem to disagree)

Precedence when anything conflicts:
1. The user's live instructions in the session
2. Hard rules in HOST_SESSION_BUILD.md (these never yield to convenience)
3. PLAN.1.md procedure and checkpoints
4. INTEGRATION.md constraints
5. This file's workflow guidance
6. CHANGELOG.md (informational only)
If two documents disagree and precedence does not resolve it, stop and ask the
user, then record the resolution in CHANGELOG.md.

## 3. Session workflow

- Determine state first. Check docs/, server/NOTES.md, git log, and the
  PLAN.1.md checkpoints to find the current phase. Never assume a fresh start.
- Work exactly one phase at a time. Stop at every CHECKPOINT, summarize what
  was done and what was verified, and wait for the user's explicit go-ahead.
  Do not start the next phase on momentum.
- Local vs server. Phase 0 is LOCAL (the user's machine, Windows; keep LF
  endings). Phases 2 and later are SERVER (Ubuntu 24.04). Acceptable patterns:
  run ssh commands from the local session, or run Claude Code on the server
  itself. Never run server phases before the user confirms CHECKPOINT 1.
- Verify before trusting memory. Install commands, Coolify settings paths,
  Traefik middleware syntax, oauth2-proxy flags, and Keycloak admin steps are
  checked against current official docs at execution time. Training-data
  recall of these is assumed stale.
- Commit discipline. Commit after each completed phase step; the message names
  the phase. Every change to a planning document gets a CHANGELOG.md entry in
  the same commit.
- Record server facts (provider, size, IP, Coolify version, scheme in use,
  backup destination) in server/NOTES.md, never secrets.

## 4. Interaction rules

- Interview, do not guess. Phase 0 decisions (where the dev server lives,
  identity approach, tenant list, sensitivities, owners) come from the user.
- Ask before: destructive Coolify operations, deleting anything under /data or
  app volumes, changing DNS, opening firewall ports, upgrading Coolify or
  Keycloak, rebooting the host outside the planned Phase 8 drill, or any
  action that would expose an internal/confidential app without its auth gate.
- Surface problems immediately and honestly. If a checkpoint criterion cannot
  be met (for example Let's Encrypt will not issue on sslip.io), say so, apply
  the documented fallback (HTTP in dev), record it, and continue; do not
  silently lower the bar on anything security-related.
- The user does the clicking for provider consoles and for the Phase 8
  interactive app testing; Claude Code prepares exact steps and records
  results.

## 5. Security rules (operational summary; full list in HOST_SESSION_BUILD.md)

- Secrets never enter the repo, app code, or the transcript. Values go in
  Coolify's env UI or directly on the server. Rotate inherited credentials at
  onboarding.
- The Coolify dashboard is itself a high-value target: admin account created
  immediately, registration disabled, access restricted.
- Internal and confidential apps get a verified Keycloak challenge (clean
  browser test) before their URL is shared. Public apps get a verified absence
  of one.
- Per app, confirm data files are not directly downloadable through the
  public URL.
- This layer publishes trusted developer apps only. Refuse any task that would
  run untrusted student code here; that belongs to Quad's autograding runner.
- One Keycloak only. Dockerfiles are canonical for production builds.

## 6. Definition of done

The goal is complete when all of the following are true and recorded:
1. Checkpoints 0 through 9 in PLAN.1.md each received explicit user sign-off.
2. Every status=production app in docs/TENANTS.md is live on its placeholder
   subdomain, interactive (websockets verified), and marked onboarded.
3. The auth matrix in docs/VERIFICATION.md matches every app's declared
   sensitivity, with per-app owner sign-off noted.
4. The data-exposure check passed for every app.
5. A backup exists, ONE restore was actually performed and documented, and the
   reboot drill brought everything back without intervention.
6. docs/ONBOARDING.md is accurate (corrected against real onboardings),
   docs/OPERATIONS.md and docs/PROMOTION.md exist, and CHANGELOG.md is current.
7. A scoped Coolify API token exists outside the repo and answered a
   read-only API request (the Registry seam), noted in server/NOTES.md.
8. Final commit tagged v1.0-dev-platform-live.

## 7. Out of scope (do not drift into these)

- Building the EduCloud Registry, the register CLI, or the Coolify adapter
  (separate plan, separate repo; only the API-token seam is prepared here).
- Ephemeral per-user workspaces (settled decision 1).
- Hosting untrusted student code in any form.
- Kubernetes, multi-node topology, or MESA integration work (promotion-time
  and 2027+ concerns).
- Acquiring or configuring a real domain (PROMOTION.md covers it for later).

## 8. Kickoff messages

First session (nothing exists yet):
  Read CLAUDE_CODE_INSTRUCTIONS.md and follow it. Start at the current phase.
  If no phase has begun, start Phase 0 and interview me for REQUIREMENTS.md
  first.

Any later session (local or server):
  Read CLAUDE_CODE_INSTRUCTIONS.md and follow it. Determine the current phase
  from the repo and server state, report it, and continue from the last
  incomplete checkpoint.
