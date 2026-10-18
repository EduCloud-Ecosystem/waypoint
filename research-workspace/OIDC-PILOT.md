# Local synthetic identity pilot

This slice of [issue 29](https://github.com/EduCloud-Ecosystem/waypoint/issues/29)
adds a disposable browser identity boundary to the existing research workspace.
It does not authorize a hosted service, private research/student records, paid
capacity, inference, or source-environment promotion. The four source-built
Python/R environments remain disabled candidates.

## Authority and failure behavior

The separate `local-synthetic-oidc` mode uses the existing Waypoint conventions
in `keycloak/realm-export.json`, `docker-compose.dev.yml`,
`traefik/dynamic.yml`, and the stable-subject/current-authority approach in
`course-workspace/hub/jupyterhub_config.py`. It never starts that general stack.
The existing loopback collaborator-key mode remains independently usable.
OIDC mode rejects collaborator keys, browser identity headers, emails, and
browser-provided roles as authority.

OAuth2 Proxy owns the browser authorization-code protocol with PKCE S256 and
nonce verification. A private `/oauth2/auth` call supplies an access token to
the backend; that endpoint is never public. The backend performs authenticated
Keycloak introspection for each API access and before dispatch/publication,
checking active status, exact issuer, audience, expiry, stable subject,
session ID, issued-at and authentication time. The configured issuer plus
subject maps to an operator-provisioned opaque user. Current SQLite project
membership and active-user status remain authoritative for every project
operation. No JWT cryptography is implemented in this adapter.

Only the ingress publishes a port, bound to `127.0.0.1`. It strips incoming
identity/authorization headers and overwrites the private boundary secret.
The backend requires exactly one matching boundary header and the exact
configured public origin. The private network contains the backend, proxy,
and Keycloak; the ingress alone also joins a bridge for its loopback port.
Only the realm's browser authentication/logout, login-action and static-resource
paths are public. Proxy authentication/logout shortcuts, Keycloak admin/master,
health, metrics, token, introspection and userinfo endpoints stay private.

Application sessions and queued token contexts exist only in process memory.
Tokens never enter runs, broker requests, evidence exports, application URLs
or request logs. Each private call has a six-second caller deadline, a bounded
response and at most four concurrent I/O tasks. Slow or failed providers close
access; exhausted transport slots require recovery or an application restart.
There is no offline authority fallback. Ordinary authorization-code redirects
remain part of the provider protocol; raw browser traces are not public evidence.

The worker rechecks identity before launch, during execution observation, and
before publication. A denial stays latched for that run even if identity
recovers. Current membership checks remain in Store. Local logout and broker
launch/publication use a consistent lock order; publication commits before
logout can acknowledge. Denied work has no published metrics or result file;
actual receipts, conservative charges and unresolved cleanup remain visible.
Terminal queued cancellations and failed submissions release transient slots.

Local sign-out invalidates the session and its queued contexts even during an
IdP outage, and clears the owned application/proxy cookies. The UI explicitly
states that the IdP session may remain open and offers a separate provider
logout path. A signed-out session ID cannot establish a new workspace session.
Restart loses all transient authority and terminally cancels queued work.
Tokens must have both immutable `iat` and `auth_time` after the new startup
cutoff. Keycloak refresh-token issuance and proxy cookie refresh are disabled;
old proxy cookies cannot refresh into new startup authority.

Coordinated recovery retains the source identity mode and opaque mappings,
keeps the source-fingerprint and broker-cutover checks, clears sessions, and
requires fresh identity-provider sign-in. It never activates OIDC state in
key mode. Keycloak's own persistence is a separate dependency; off-host IdP and
workspace recovery is not qualified by this local replacement exercise.

## Disposable operation

Requirements: Docker with Compose, Python 3, a Playwright CLI (or Codex's
Playwright skill wrapper), and Outfitter built from approved commit
`5827c84e9952cc67b85e91ceff4282e0ead6923b`. Build one native host executable
for operator provisioning/recovery and one static Linux executable matching
the Docker engine architecture. Do not use an unrelated broker checkout.

From `research-workspace`, with operator-selected paths:

```sh
python3 pilot_oidc.py prepare --root /tmp/waypoint-oidc-UNIQUE \
  --broker /absolute/path/to/native-outfitter \
  --linux-broker /absolute/path/to/linux-outfitter
python3 pilot_oidc.py start --root /tmp/waypoint-oidc-UNIQUE
python3 verify_oidc.py --root /tmp/waypoint-oidc-UNIQUE \
  --cli /absolute/path/to/playwright-cli-or-wrapper
python3 pilot_oidc.py down --root /tmp/waypoint-oidc-UNIQUE
```

Preparation requires a fresh directory below `/tmp`, creates four synthetic
identities and two projects, and generates credentials under a private 0700
parent. Exact file mounts readable by non-root containers are 0644 within
that inaccessible parent; operator/configuration files are 0600. Never commit
or share the private directory, cookies, admin credentials or raw CLI output.
The Linux backend alone uses the Docker socket for the accepted local broker.
It requires a trusted local operator; this is not a hosted security boundary.

Images are pinned by digest in `pilot_oidc.py`: Keycloak 26.6.4, OAuth2 Proxy
7.15.3, Traefik 3.6.2, Docker CLI 28.5.1 and the existing approved Python base.
The image versions are observed identities, not a production-support claim.
The generator records broker and application-source hashes. Runtime services
have finite CPU, memory, process and log bounds. The two projects share a
600 CPU-second maximum allocation. Each admitted reviewed run retains the
existing 30 CPU-second bound and conservative charge.

Each prepared stack has a one-hour lifetime including startup. A detached
watcher owns its deadline. Startup and teardown serialize; stopped or previously
started directories cannot restart. Late/failed startup tears down owned
services. Teardown removes only that Compose project's services/network/volume
and preserves private operator state and public observations. Images and private
state remain for explicit operator retention decisions; there is no broad prune.
The watcher cannot guarantee teardown if the host/Docker daemon is unavailable;
inspect exact owned resources before claiming cleanup.

The acceptance harness temporarily changes only the generated realm's audience
mapper/token lifetime, restoring explicit values afterward. It also uses a
private test broker wrapper with finite synchronization gates. Receipt-gated
checks prove post-execution publication denial; a distinct exact-owned-container
pause/cancellation control tests active compute. Neither changes a reviewed
recipe, runtime image, source candidate or parent pilot worktree.

## Readiness and retained attempts

The [acceptance record](evidence/2026-10-17/oidc-pilot/acceptance.json)
records 74 passing real-browser/service checks and 158 offline controls.
The full browser run used application and harness revision `b818d02`.
A later harness-only observer cleanup fix has two targeted failure-path
regressions; it did not change the 18 prepared application files. Desktop/mobile
inspection and a separate provider-logout control supplement the full run.
The initial attempt omitted Keycloak's `basic` scope; current identity validation
correctly denied the resulting token without `sub`/`auth_time`. A browser-harness
attempt sent an empty JSON body, and a later short-token control initially
failed to restore the lifespan because Keycloak preserves omitted attributes.
Those attempts remain failures. The corrected generator includes `basic`; the
harness sends JSON data and explicitly restores the 600-second lifetime.
Two cold-start attempts reached the application before Keycloak was ready.
They remain failed attempts; the corrected harness waits for the exact provider
issuer and private proxy readiness before opening the browser. A screenshot
supplement initially used the wrong browser execution context; the corrected
read-only layout check passed separately.
The separate provider sign-out initially failed at Keycloak's confirmation
POST. The generator now exposes that exact `logout/logout-confirm` path. A
supplemental real-browser control verifies removal of the current browser's
exact provider session ID and continued denial of private provider endpoints.

| Gate | Scope / status |
| --- | --- |
| Local synthetic identity and authorization | Final measured results in the acceptance record |
| Existing key mode, Python/R and recovery regression controls | Automated controls and ordinary CI |
| Source candidate promotion | NOT RUN; four disabled candidates |
| Manual screen reader, complete focus/tab order and new human walkthrough | NOT RUN |
| Store-wide byte/inode/output limits and retention/deletion | OPEN in issue 29 |
| Intended hosted cohort/concurrency and overload envelope | NOT RUN |
| Service owner, support, recovery targets, off-host backups | OPEN |
| Approved private-data classes/destinations or institutional deployment | NOT APPROVED |

The original source-build failed replay and cleanup gate stay byte-for-byte
preserved. Its separate later resource/accounting observation remains in
[the supplemental reconciliation](evidence/2026-10-17/replay-cleanup-reconciliation/README.md).

## Primary references checked during implementation

- [Keycloak OIDC endpoints and confidential-client introspection](https://www.keycloak.org/securing-apps/oidc-layers)
- [Keycloak fixed hostname and backchannels](https://www.keycloak.org/server/hostname)
- [Keycloak reverse-proxy path exposure](https://www.keycloak.org/server/reverseproxy)
- [Keycloak logout confirmation implementation](https://github.com/keycloak/keycloak/blob/main/services/src/main/java/org/keycloak/protocol/oidc/endpoints/LogoutEndpoint.java)
- [OAuth2 Proxy configuration, nonce/PKCE, cookies and logging](https://oauth2-proxy.github.io/oauth2-proxy/configuration/overview/)
- [OAuth2 Proxy endpoint and sign-out behavior](https://oauth2-proxy.github.io/oauth2-proxy/features/endpoints/)

The provider owns protocol behavior; the recorded real-service controls are
separate evidence of this particular generated configuration.
