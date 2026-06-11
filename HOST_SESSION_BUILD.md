# HOST_SESSION_BUILD.md: Project memory for the EduCloud hosting build

Note for the user: Claude Code only auto-loads a file named CLAUDE.md. Since this
file is named HOST_SESSION_BUILD.md, either keep a one-line CLAUDE.md that says
"Read HOST_SESSION_BUILD.md and PLAN.1.md before doing anything," or start every
session with that instruction.

## What this project is

EduCloud's hosting layer. The goal is a hosting space for developers: any team
member can publish a web app (R Shiny, Shiny for Python, Dash, Streamlit, Flask,
FastAPI, Node, or static HTML) to a stable, secure URL through a documented
onboarding contract. A pre-existing portfolio of apps onboards as the first
tenants and proves the path.

Architecture decision (settled, do not relitigate): the hosting engine is
Coolify (open-source, Apache-2.0, self-hostable PaaS). We federate to mature
OSS instead of hand-building. Do NOT build or propose nginx + Shiny Server +
per-app systemd units; that earlier design is superseded. What we configure:

- Coolify: per-app containers, built-in reverse proxy, automatic Let's Encrypt
  TLS, env/secret management, git-push deploys, resource limits, teams/projects.
- Keycloak: identity. Visitor-facing auth for protected apps is enforced in
  front of those apps (oauth2-proxy / proxy forward-auth against Keycloak).
  Coolify's own login protects only the Coolify dashboard, never confuse the two.
- Static content: built from git via CI or served as a Coolify static app.

A separate, later workstream (not this plan) is the EduCloud Registry: a catalog
service with a register CLI and a thin Coolify adapter that drives Coolify's API
to deploy registered objects and records their deployed_url. This plan only
prepares its seam (a scoped Coolify API token and clean project structure).
The Registry never gates or delays anything in this plan.

## Dev mode (current state)

We do not control a real domain and do not need one yet. The build uses magic
wildcard DNS (sslip.io for a VPS public IP, nip.io for a local VM's private IP):
any name with the IP embedded resolves to that IP with no registration. The
placeholder platform domain is apps.<VM-IP-dashed>.sslip.io; apps live at
<app>.apps...; Keycloak at auth.apps... . This is real DNS, so routing,
subdomain propagation, OIDC redirects, cookies, and cross-app links are all
genuinely testable. TLS is best effort in dev: HTTPS if Let's Encrypt issues
for the sslip.io names, plain HTTP otherwise, and HTTP is acceptable for dev
only. The dev-to-production promotion (real domain, redirect URI and cookie
updates, cert re-issue) is configuration only and is captured in
docs/PROMOTION.md during Phase 9; no onboarding work is throwaway.

## Repo layout

- HOST_SESSION_BUILD.md   this file
- PLAN.1.md               phased implementation plan with checkpoints
- docs/REQUIREMENTS.md    platform requirements sheet (fill in Phase 0)
- docs/TENANTS.md         tenant intake manifest (fill in Phase 0)
- docs/ONBOARDING.md      one-page developer guide, written in Coolify terms
- docs/VERIFICATION.md    results of the Phase 8 checklist
- docs/OPERATIONS.md      steady-state runbook (Phase 9)
- docs/PROMOTION.md       dev-to-production checklist for a future real domain
- templates/shiny.Dockerfile     templated Dockerfile covering all R Shiny apps
- templates/py.Dockerfile        fallback Dockerfile for Python apps that the
                                 buildpack cannot handle
- templates/dot-env.example      commented env example, no real values
- server/NOTES.md         server facts: provider, size, IP, Coolify version,
                          dashboard URL, backup destination (no secrets)

## Hard rules

- Secrets (API keys, DB credentials, OIDC client secrets, Coolify API tokens)
  never enter this repo, app code, or the chat transcript. They are entered in
  Coolify's environment/secret UI or created directly on the server. Rotate any
  credential inherited from a previous hosting environment at onboarding time.
- Every app has a named owner and a declared sensitivity (public, internal,
  confidential) in docs/TENANTS.md before it is deployed.
- Internal and confidential apps must sit behind a working Keycloak-backed auth
  challenge before their URL is shared. Verify the challenge from a clean
  browser session; never assume it.
- Data files must not be directly downloadable through any app's public URL;
  test this per app during verification.
- The Coolify dashboard itself: registration disabled after the admin account
  exists, strong password, and access restricted (VPN/IP allowlist or at
  minimum never shared) since it can deploy arbitrary containers.
- Apps bind only inside the Docker network; the proxy is the sole public
  entrance. Never publish a container port directly to the host without asking.
- Ask before: destructive Coolify operations (deleting apps/projects), changing
  DNS, opening firewall ports, upgrading Coolify itself, or rebooting the host
  during business hours.
- Verify current install commands against official docs (coolify.io/docs,
  Keycloak docs) at execution time instead of trusting any command memorized
  from training or written in this repo.

## Conventions

- Work PLAN.1.md one phase at a time; stop at every CHECKPOINT and wait for the
  user's go-ahead.
- Commit after each completed phase step; message names the phase.
- Line endings LF (.gitattributes enforces this); the repo may be edited from
  Windows.
- Do not use em dashes or en dashes in any doc written in this repo.
