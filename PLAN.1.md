# PLAN.1.md: Build the EduCloud Hosting Layer on Coolify

Instructions for Claude Code: read HOST_SESSION_BUILD.md first. Work one phase at
a time, in order. Each phase ends with a CHECKPOINT; stop there, summarize what
was done and verified, and wait for the user's go-ahead. Phase 0 runs on the
LOCAL machine. Phases 2 and later run against the SERVER (SSH from local, or a
Claude Code session on the server). Never start server phases until Phase 1 is
confirmed complete by the user.

DEV MODE (current state): we do not control a real domain, and that is fine.
This build runs end to end on magic wildcard DNS: sslip.io / nip.io resolve any
name with an IP embedded in it to that IP (e.g. anything.203-0-113-10.sslip.io
resolves to 203.0.113.10) with no registration and no DNS records to create.
Coolify itself defaults new apps to sslip.io names, so routing, subdomain
propagation, auth round trips, and cross-app links are all genuinely testable.
Conventions for this plan:

- <HOSTNAME> means apps.<VM-IP-dashed>.sslip.io (a placeholder platform domain)
- <APP>.<HOSTNAME> is each app's name; Keycloak lives at auth.<HOSTNAME>
- TLS is best effort in dev: Let's Encrypt can issue for sslip.io names, but if
  issuance is blocked or rate limited, plain HTTP is acceptable for dev and is
  NOT a checkpoint failure. Record which scheme is in use in server/NOTES.md.
- Promotion to a real domain later is configuration only: add real DNS records,
  change the domain fields in Coolify and the Keycloak redirect URIs, re-issue
  certificates. Nothing in the app onboarding work is throwaway.

<APP> is an app name throughout.

---

## Phase 0 (LOCAL): Requirements, tenant intake, onboarding guide, templates

1. Interview the user to fill in docs/REQUIREMENTS.md: where the dev server
   will live (a small cloud VPS with a public IP is simplest because sslip.io
   then works from anywhere; a local VM or WSL2 Docker host also works using
   nip.io with its private IP, reachable only from the local network); upstream
   identity for Keycloak (local accounts only at first, or federate Google/
   GitHub/ORCID/institutional SSO later); expected app and publisher counts for
   year one; backup destination. Record DEV MODE: placeholder sslip.io domains,
   no owned domain, promotion path documented. Do not guess; ask.
2. Build docs/TENANTS.md: columns app name, framework, status (production /
   superseded / retired), sensitivity (public / internal / confidential),
   audience, data refresh cadence, owner. Only status=production onboards.
3. Write docs/ONBOARDING.md (one page) as the contract in Coolify terms. To
   publish, a developer provides: (a) a git repo (or directory) that is
   self-contained with relative paths; (b) a declared environment
   (requirements.txt / renv or package list / package.json), or a Dockerfile;
   (c) secrets listed by NAME only, values entered in Coolify's env UI;
   (d) desired subdomain, sensitivity, audience, and a named owner.
4. Create templates/:
   - templates/shiny.Dockerfile: rocker/shiny-verse (or rocker/r2u for fast
     binary package installs) base; install the app's declared packages; COPY
     the app; EXPOSE 3838; healthcheck. Parameterized so one template covers
     the whole R portfolio.
   - templates/py.Dockerfile: slim Python base, venv, requirements.txt,
     uvicorn/gunicorn CMD. Fallback for apps Coolify's buildpack (nixpacks)
     cannot autodetect.
   - templates/dot-env.example: commented variable names, no values.
5. Add .gitattributes (LF). Commit.

CHECKPOINT 0: Docs complete, templates drafted, the Decisions confirmed by the
user (where the dev server lives, identity approach; domain is settled as
sslip.io placeholders per DEV MODE).

---

## Phase 1 (USER + LOCAL): Provision

Claude Code assists; the user executes provider-side clicks.

1. VM: Ubuntu 24.04 LTS, 4 vCPU, 16 GB RAM recommended (containers cost more
   memory than shared processes; 8 GB is a floor, pick a resizable plan),
   80 GB disk (images are bulky, especially R), SSH key auth, inbound 22/80/443.
2. No DNS records to create in dev mode. Derive the placeholder domain from the
   VM's IP and verify the magic wildcard resolves from the LOCAL machine:
   nslookup test.apps.<VM-IP-dashed>.sslip.io   (must return the VM IP)
   nslookup whatever.auth.<VM-IP-dashed>.sslip.io
   Write the chosen <HOSTNAME> into server/NOTES.md. If using a local VM
   instead of a VPS, use nip.io with the VM's private IP and note that URLs
   work only on the local network.
3. Confirm ssh <user>@<VM-IP> works.
4. Record provider, region, size, IP in server/NOTES.md. Commit.

CHECKPOINT 1: SSH works; the sslip.io wildcard test names resolve to the VM IP.
User says go.

---

## Phase 1.5 (LOCAL): Pre-build before any server exists

Inserted by amendment (CHANGELOG Release 6) because provisioning is deferred
pending budget approval. Goal: when the server arrives, Phases 2 through 7
become transplanting proven pieces rather than first-time integration. All work
here is local and throws nothing away; the same Dockerfiles and Keycloak recipe
deploy unchanged on the server (only hostnames and the dev scheme change).

1. Write the working source code for all five demo apps from docs/TENANTS.md
   into apps/ in this repo, one directory per app. Each app is hello-world level
   but genuinely exercises its flagged behavior (public load with interactive
   control; internal app behind the gate; env/secret read; persistent write to
   DATA_DIR; static serving). Apps bind 0.0.0.0 on their EXPOSEd port.
2. Instantiate each app's final Dockerfile from the templates (R_PKGS baked for
   Shiny; START_CMD set per framework for Python; an nginx static image for the
   report). These are the artifacts that later ship to Coolify unchanged.
3. If Docker Desktop is available on this machine (ASK THE USER FIRST), build
   all images locally and write docker-compose.dev.yml that runs Traefik,
   Keycloak (with its Postgres), oauth2-proxy, and the demo apps on localhost,
   proving the Phase 3 auth round trip locally: a clean browser hitting an
   internal app is challenged by Keycloak, a realm user passes, a non-user does
   not. If Docker Desktop is NOT available, stop at build-ready: Dockerfiles and
   compose file written and reviewed, build deferred to the server.
4. Pre-write the Keycloak realm and OIDC client configuration as a documented
   recipe (realm name, client id, client type, redirect URIs, group/role
   mapping, oauth2-proxy settings), with both the local-compose values and the
   placeholder-domain server values, so Phase 3 on the server is configuration
   transfer, not research. Fold the recipe into docs/ONBOARDING.md admin section.

CHECKPOINT 1.5: All five demo apps written and their Dockerfiles instantiated.
If Docker Desktop was available, all images build and the local auth round trip
is demonstrated end to end; otherwise everything is build-ready with the build
deferred to the server, recorded as such. Keycloak realm/OIDC recipe written.
Secrets (the demo API key, any local Keycloak admin password) stay out of the
repo; local-only values live in an untracked .env that .gitignore excludes.

---

## Phase 2 (SERVER): Install and secure Coolify

1. Check the current official installation command at https://coolify.io/docs
   (self-hosted installation page) before running anything; do not trust a
   memorized command. Historically of the form:
   curl -fsSL https://cdn.coollabs.io/coolify/install.sh | bash
   Run it as root per the docs.
2. First-run setup at http://<VM-IP>:8000: create the admin account
   immediately, then disable open registration in settings.
3. Set the instance's domain to coolify.<HOSTNAME> and the wildcard app domain
   to <HOSTNAME> (both sslip.io placeholders). Attempt Let's Encrypt issuance;
   if it succeeds, dev runs HTTPS, and if issuance fails or rate limits on
   sslip.io, proceed on HTTP and record the scheme in server/NOTES.md (per DEV
   MODE this is not a failure).
4. Restrict dashboard exposure per HOST_SESSION_BUILD.md hard rules (IP
   allowlist / VPN if feasible; at minimum strong password and registration
   disabled). Confirm ports 80/443 (and 8000 only if still needed) are the
   only public listeners: ss -tlnp.
5. In Coolify, create the project/team structure: one project for the platform
   (e.g. "tenants"), environments as needed. Record Coolify version and
   dashboard URL in server/NOTES.md. Commit.

CHECKPOINT 2: Coolify dashboard secured and reachable at its placeholder
domain; wildcard routing proven (deploy Coolify's sample/placeholder app to a
test subdomain, load it at test.<HOSTNAME>, then remove it). Scheme in use
(HTTPS or HTTP) recorded.

---

## Phase 3 (SERVER): Keycloak identity

1. Deploy Keycloak as a Coolify one-click service (with its Postgres) onto
   auth.<HOSTNAME>. Admin credentials entered in Coolify's env UI only.
2. Create a realm (e.g. educloud), the team's first users (or wire the upstream
   identity provider chosen in Phase 0), and an OIDC client for visitor auth
   (confidential client; redirect URIs use the placeholder names, e.g.
   https?://<app>.<HOSTNAME>/oauth2/callback). sslip.io names are ordinary
   public DNS names, so OIDC redirects and cookies behave normally; if running
   HTTP-only dev, set cookie-secure off in oauth2-proxy and note it as a
   dev-only setting to flip at promotion.
3. Stand up the visitor-auth gate: oauth2-proxy (deployable in Coolify) pointed
   at the Keycloak realm, wired as forward-auth in front of protected app
   domains via the proxy's middleware/labels. Protect ONE dummy app end to end
   first: clean browser gets a Keycloak login challenge; a realm user gets
   through; a non-user does not.
4. Document the exact wiring steps in docs/ONBOARDING.md (admin section) so
   protecting the next app is a recipe, not research. Commit.

CHECKPOINT 3: Dummy protected app demonstrates the full auth round trip.
This phase has the most integration risk; do not proceed past it half-working.

---

## Phase 4 (SERVER + LOCAL): Templated Shiny image, onboard the R tenants

1. Take the simplest production R app from docs/TENANTS.md. Instantiate
   templates/shiny.Dockerfile for it, build, and deploy it as a Coolify
   application on <app>.<HOSTNAME>. Iterate the template until: app loads,
   controls are interactive (WebSockets work through the proxy), container
   restarts cleanly, build time is tolerable.
2. Expect this first image to be the slow one (R package compilation; prefer a
   binary-package base like rocker/r2u to cut build times). Record the working
   pattern back into templates/shiny.Dockerfile.
3. Onboard the remaining production R apps: per app, repo (or directory) +
   instantiated Dockerfile + Coolify app + subdomain + resource limits
   (memory limit per container). Fix hardcoded URLs/absolute paths; cross-links
   use the new subdomains.
4. Sensitivity gate: any internal/confidential R app gets the Phase 3
   forward-auth wiring BEFORE its URL is recorded in docs/TENANTS.md as live.
5. Mark each app onboarded in docs/TENANTS.md; commit as you go.

CHECKPOINT 4: All production R apps live on their subdomains, interactive,
auth-gated where required.

---

## Phase 5 (SERVER): Onboard the Python tenants

Per production Python app (Shiny for Python, Dash, Streamlit, Flask, FastAPI):
1. Try Coolify's buildpack (nixpacks) first with the app's requirements.txt
   and a Procfile/start command (uvicorn app:app for ASGI; gunicorn app:server
   for Dash). If autodetection fights you, fall back to templates/py.Dockerfile.
2. Secrets: enter values in Coolify's env UI for that app; names match
   templates/dot-env.example documentation. Rotate anything inherited from the
   previous hosting environment. Never echo values into the transcript.
3. Apps that write data (surveys/forms): mount a Coolify persistent volume for
   the write path and confirm the volume is included in backups (Phase 8).
4. Verify per app: loads on its subdomain, interactive, restart clean,
   container logs clean (docker logs via Coolify UI). Sensitivity gate as in Phase 4.
5. Update docs/TENANTS.md; commit.

CHECKPOINT 5: All production Python apps live, secrets in Coolify only,
persistent write paths on volumes.

---

## Phase 6 (SERVER): Static content

1. Self-contained HTML reports: deploy as a single Coolify static app (one
   small nginx/static container serving a reports directory or repo) at
   reports.<HOSTNAME>, or follow the org's existing git -> CI -> static
   pattern if docs/REQUIREMENTS.md says so.
2. Verify each report loads; no app processes involved.

CHECKPOINT 6: Static reports reachable.

---

## Phase 7 (SERVER): Auth matrix completion

1. Walk docs/TENANTS.md top to bottom. For every internal/confidential app,
   verify from a clean browser session: challenge appears, authorized user
   passes, unauthorized does not. For every public app, verify no challenge.
2. Record the matrix outcome per app in docs/VERIFICATION.md.
3. Owner sign-off per app (the user relays or collects this) before each URL
   is shared beyond the team.

CHECKPOINT 7: Auth matrix matches declared sensitivities, with sign-offs.

---

## Phase 8 (SERVER + LOCAL): Verification and backups

Write all results into docs/VERIFICATION.md:
- Per app: loads at its placeholder URL over the dev scheme (HTTPS if issued,
  otherwise HTTP), every filter/control/tab/download exercised (the
  user clicks; Claude Code records), reconnect after idle.
- Data exposure: request known data-file paths through each app's public URL
  and confirm they are NOT served.
- Cross-links between apps resolve on the new subdomains.
- Write-path test: submit a test record end to end for each collecting app;
  confirm it lands on a persistent volume.
- Logs clean across containers.
- Backups: enable Coolify's backup features for its own database and any app
  databases; add provider snapshots or a cron rsync of persistent volumes and
  /data/coolify to the destination from REQUIREMENTS.md. Perform ONE test
  restore and document it.
- Restart drill: reboot the VM; confirm Coolify, the proxy, Keycloak, and
  every app return without intervention.

CHECKPOINT 8: VERIFICATION.md complete, restore tested, no open failures.

---

## Phase 9 (LOCAL + SERVER): Launch and operations

1. Share the placeholder URLs and docs/ONBOARDING.md with the team for dev use.
2. Write docs/PROMOTION.md, the dev-to-production checklist for when a real
   domain exists: create A + wildcard records; update Coolify instance and
   wildcard domains; update each app's domain (or rely on the wildcard);
   update Keycloak redirect URIs and oauth2-proxy cookie/secure settings;
   re-issue certificates; re-run the Phase 7 auth matrix and the Phase 8
   data-exposure checks against the new names. Estimated effort: hours, not
   days; no app is rebuilt.
3. Write docs/OPERATIONS.md: monthly routine (host apt patching, Coolify
   upgrade cadence per its release notes, Keycloak updates, cert spot-check,
   log review, disk/image pruning with docker system prune schedules), the
   redeploy one-liners (git push or Coolify redeploy button), and the
   twice-yearly tenant lifecycle review (ownerless or unused apps retired).
4. Prepare the Registry seam (do NOT build the Registry here): create a scoped
   Coolify API token, store it outside the repo, and note in server/NOTES.md
   that it exists and where; confirm the Coolify API answers a read-only
   request with it.
5. Final commit; tag v1.0-dev-platform-live.

CHECKPOINT 9: Dev platform live on placeholder domains, runbook and promotion checklist committed, Registry seam ready. The
Registry MVP (catalog + register CLI + Coolify adapter) proceeds as its own
plan in its own repo.
