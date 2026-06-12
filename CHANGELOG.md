# CHANGELOG.md: Document history for the EduCloud hosting layer repo

Records every substantive change to HOST_SESSION_BUILD.md, PLAN.1.md, and
INTEGRATION.md, newest first within each release. Conventions: LF endings, no
em dashes or en dashes. Maintain this file going forward: every commit that
changes a planning document adds an entry here.

---

## Release 6 (June 2026): Phase 1.5 pre-build inserted (LOCAL)

Trigger: provisioning deferred pending budget approval. Rather than idle, the
user directed a local pre-build so that the server phases become transplanting
proven pieces rather than first-time integration.

### PLAN.1.md
- New Phase 1.5 (LOCAL) inserted between Phase 1 and Phase 2: (1) write working
  source for all five demo apps into apps/; (2) instantiate each app's final
  Dockerfile from the templates; (3) if Docker Desktop is available, build all
  images locally and run Traefik plus Keycloak plus oauth2-proxy plus the demo
  apps via docker-compose.dev.yml to prove the Phase 3 auth round trip on
  localhost, otherwise stop at build-ready; (4) pre-write the Keycloak realm and
  OIDC client recipe with both local-compose and server placeholder values.
- CHECKPOINT 1.5 added. Nothing in Phases 2 through 9 changes; this phase
  de-risks them. All Phase 1.5 output is reused unchanged on the server.

### Note
- Phase 1 (provisioning) remains open and is unblocked when budget is approved;
  Phase 1.5 does not depend on it and runs entirely locally.

---

## Release 5 (June 2026): Provider change Hetzner to DigitalOcean (Phase 1)

Trigger: during Phase 1 provisioning, Hetzner required government ID
verification at signup, which the user declined; that account is being deleted.
No server had been created yet, so this is a clean swap with no rebuild. The
provider is not architecturally significant: any cloud VPS with a public IPv4
satisfies the requirement, and DigitalOcean meets the same intent (Ubuntu 24.04,
4 vCPU / 8 GB resizable, public IPv4, existing SSH key, provider firewall with
22 restricted to the user IP and 80/443 open, backups enabled).

### docs/REQUIREMENTS.md
- Section 1 now names DigitalOcean as the chosen provider and records the
  Hetzner-to-DigitalOcean reason, with a note that the provider choice is not
  architecturally significant.

### server/NOTES.md
- Provider section rewritten for DigitalOcean (Basic Droplet 4 vCPU / 8 GB /
  ~160 GB SSD, resizable; resize-up remedy to 8 vCPU / 16 GB; DO resize
  semantics noted). SSH user root (DO default). DigitalOcean backups enabled.
- Firewall section records the implemented rules (22 to home IP, 80/443 open,
  8000 closed).

### Plan
- Nothing else changes. Phase 1 procedure and all later phases are unaffected.

---

## Release 4 (June 2026): Phase 0 deliverables created (LOCAL)

Trigger: Phase 0 of PLAN.1.md executed on the local machine. Requirements and
tenant intake interviewed from the user; onboarding contract and build
templates drafted. No planning document changed; this entry records the new
deliverables for history.

### docs/REQUIREMENTS.md (new)
- Section 0 records that ALL tenants are synthetic for this proof of concept;
  the real portfolio is explicitly out of scope and is a promotion-time concern.
- Decisions pinned: dev server is a cloud VPS with a public IP (sslip.io
  placeholder domains); year-one scale is medium (10 to 25 apps, small
  publisher count) on a resizable 16 to 32 GB VM, 80 GB+ disk; identity is one
  Keycloak with local accounts only (MESA federation is the end state, an
  upstream Google/GitHub IdP is a candidate post-Phase-8 enhancement); backups
  in dev are provider snapshots plus Coolify built-in DB backups, with
  S3-compatible object storage recorded as the intended promotion target.

### docs/TENANTS.md (new)
- Five synthetic production tenants, each exercising one platform behavior:
  demo-public-shiny (R Shiny, public, no gate), demo-internal-shiny (R Shiny,
  internal auth gate), demo-dash-secret (Dash, internal, dummy env secret),
  demo-survey (Shiny for Python, internal, persistent write volume),
  demo-static-report (static HTML, public). Owner Ben on all rows.

### docs/ONBOARDING.md (new)
- One-page publishing contract in Coolify terms: self-contained source,
  Dockerfile from templates as the canonical artifact (nixpacks preview-only),
  secrets by name only, and publication details (subdomain, sensitivity,
  audience, owner, write path). Admin auth-wiring recipe stubbed for Phase 3.
  Post-review edits: added the 0.0.0.0 container networking rule, and stated
  that publication details become the app's row in docs/TENANTS.md (registry
  of record). py.Dockerfile gained an explicit 0.0.0.0 bind rule comment.

### templates/ (new)
- shiny.Dockerfile: rocker/r2u (Ubuntu 24.04 noble) base, install.r for CRAN
  binaries, parameterized R_PKGS and APP_DIR, runs shiny::runApp on 3838,
  curl healthcheck. Verified r2u guidance against rocker-project.org at build.
- py.Dockerfile: python:3.12-slim, venv, requirements.txt, parameterized
  START_CMD with per-framework examples (Dash, Flask, FastAPI, Streamlit,
  Shiny for Python), DATA_DIR for persistent write paths, curl healthcheck.
- dot-env.example: variable NAMES only, no values; documents the demo secret,
  the persistent write path, and the oauth2-proxy variables for Phase 3.

### Repo
- .gitattributes added enforcing LF endings (the repo may be edited from Windows).
- Git repository initialized; first commit names Phase 0.

---

## Release 3 (June 2026): INTEGRATION.md adopted; decisions settled; fold-in edits applied

Trigger: INTEGRATION.md introduced to define the relationship between this
hosting layer and the wider EduCloud/Quad and MESA work. Its prescribed edits
were applied so the repo does not self-contradict.

### INTEGRATION.md
- Added to the repo as the third planning document: the Registry seam, the
  adapter-interface discipline and portability test, the two-tier deployment
  story (Coolify for single-VM operators, MESA Kubernetes for institutional
  scale), the four boundaries, and licensing placement (Coolify Apache-2.0
  beside Quad's AGPL-3.0 control plane).
- Open decisions section marked RESOLVED with the four answers below.
- "Edits to fold into the existing docs" section marked APPLIED, retained as
  the record of what changed and why.

### Decisions settled (recorded in HOST_SESSION_BUILD.md, mirrored here)
1. Quad section 12 relationship: this layer is a portfolio of long-lived
   PUBLISHED apps, not a pkg/workspace backend for ephemeral environments.
2. Topology: one VM for dev with the single point of failure accepted in
   writing; control-plane/workload split deferred to promotion.
3. Reverse proxy: Traefik (Coolify's v4 default); forward-auth wired as
   Traefik middleware.
4. Identity: one Keycloak, standalone with local accounts for year one;
   federation to MESA CI-Logon/Keycloak is the stated end state.

### HOST_SESSION_BUILD.md
- Added "Settled decisions" section with the four resolutions and a
  do-not-relitigate marker.
- Added three hard rules: trusted publishing never untrusted student
  execution; exactly one Keycloak, federated end state, no parallel identity
  island; Dockerfile is the canonical build artifact for production tenants
  (nixpacks for preview/throwaway only).
- Reframed the Registry paragraph: this layer is a Registry deployment backend
  behind a future adapter, never a dependency Quad imports; pointer to
  INTEGRATION.md added.
- Repo layout: INTEGRATION.md added; templates/py.Dockerfile description
  changed from "fallback" to "canonical for production Python tenants."

### PLAN.1.md
- Phase 0: onboarding contract now requires a Dockerfile (from the repo
  templates) as the canonical artifact for production; py.Dockerfile template
  description updated to match.
- Phase 1: capacity and topology line added (16 GB is a floor with Coolify
  plus Keycloak's JVM plus R containers; one-VM SPOF accepted in writing for
  dev; split is a promotion-time decision in docs/PROMOTION.md).
- Phase 3: reverse proxy named explicitly (Traefik); forward-auth middleware
  syntax must be verified against current Traefik and oauth2-proxy docs at
  execution time.
- Phase 4: one replica per app by default; any Shiny app scaled past one
  replica requires sticky sessions (websocket session state).
- Phase 5: build default flipped. Dockerfile canonical for production
  tenants; nixpacks demoted to preview or throwaway deployments only
  (previously "try nixpacks first, Dockerfile as fallback").

---

## Release 2 (June 2026): Dev mode, placeholder domains

Trigger: no real domain is controlled, and none is needed to validate that
information propagates correctly. The build was reframed to run end to end on
magic wildcard DNS.

### PLAN.1.md
- DEV MODE block added to the header: <HOSTNAME> means
  apps.<VM-IP-dashed>.sslip.io (nip.io with a private IP for a local VM);
  Keycloak at auth.<HOSTNAME>; TLS best effort (HTTP acceptable in dev and not
  a checkpoint failure); promotion to a real domain is configuration only.
- Phase 0: domain-ownership question removed; asks where the dev server lives
  (VPS with public IP vs local VM) and records DEV MODE.
- Phase 1: DNS record creation replaced with nslookup verification that the
  sslip.io wildcard names resolve to the VM IP.
- Phase 2: instance domain set to coolify.<HOSTNAME>; Let's Encrypt attempted,
  HTTP fallback recorded in server/NOTES.md if issuance fails or rate limits.
- Phase 3: redirect URIs use placeholder names; cookie-secure off documented
  as a dev-only oauth2-proxy setting to flip at promotion.
- Phase 8: per-app check reworded to "over the dev scheme (HTTPS if issued,
  otherwise HTTP)."
- Phase 9: new deliverable docs/PROMOTION.md (the dev-to-production
  checklist: real DNS, domain fields, redirect URIs, cookie settings, cert
  re-issue, re-run auth matrix and data-exposure checks); final tag renamed
  v1.0-dev-platform-live.

### HOST_SESSION_BUILD.md
- "Dev mode (current state)" section added mirroring the PLAN.1.md DEV MODE
  conventions.
- docs/PROMOTION.md added to the repo layout.

---

## Release 1 (June 2026): Coolify architecture rewrite; files renamed

Trigger: the EduCloud reconciliation documents settled the architecture
question: adopt Coolify as the hosting engine rather than hand-building
nginx + Shiny Server OSS + per-app systemd services. Files renamed from
CLAUDE.md to HOST_SESSION_BUILD.md and from PLAN.md to PLAN.1.md.

### HOST_SESSION_BUILD.md (replaced CLAUDE.md)
- Architecture decision stated as settled: Coolify (per-app containers,
  built-in proxy, automatic TLS, env/secret UI, git-push deploys) plus
  Keycloak for identity. The hand-rolled stack is superseded; do not propose
  it.
- The EduCloud Registry named as a separate later workstream; this plan only
  prepares its seam (a scoped Coolify API token).
- Hard rules updated for the new attack surface: the Coolify dashboard can
  deploy arbitrary containers, so it gets its own lockdown rule (registration
  disabled, restricted access); containers bind only inside the Docker
  network; install commands verified against official docs at execution time.
- Note added that Claude Code auto-loads only a file named CLAUDE.md, with the
  workaround (one-line pointer file or explicit read instruction).

### PLAN.1.md (replaced PLAN.md)
- Old Phases 2 through 6 (Shiny Server install, per-app systemd units,
  PORTS.md port registry, hand-written nginx blocks and certbot) removed
  entirely; they become Coolify configuration.
- New phase structure: 0 intake and templates; 1 provision (16 GB RAM
  recommended for containers, 80 GB disk); 2 install and secure Coolify;
  3 Keycloak plus the full visitor-auth round trip on a dummy app (gated hard
  as the highest-integration-risk phase); 4 templated Shiny Dockerfile proven
  on one app, then R tenants onboarded; 5 Python tenants; 6 static content;
  7 auth matrix against declared sensitivities; 8 verification, backups, and a
  tested restore; 9 launch, operations runbook, and the Registry seam (scoped
  API token, read-tested).
- Privacy discipline carried over unchanged from the original proposal: no
  direct data-file downloads, auth challenge verification from clean browser
  sessions, owner sign-off before URLs are shared.

### Background (pre-repo)
- The original Word proposal ("Team Application Hosting Platform") reframed a
  fixed-portfolio migration into a general developer hosting platform with an
  onboarding contract deliberately shaped like a container contract. That
  contract is why the Coolify pivot required no rework of the onboarding
  concept: a Coolify application IS the contract, enforced by software.
