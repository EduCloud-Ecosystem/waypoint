# REQUIREMENTS.md: Platform requirements for the EduCloud hosting layer

Filled in during Phase 0 from the user's decisions. Conventions: LF endings,
no em dashes or en dashes. Settled architecture decisions live in
HOST_SESSION_BUILD.md and INTEGRATION.md and are not repeated here except where
a Phase 0 decision pins a specific value.

## Dev mode and domain scheme

This build runs in DEV MODE. No real domain is owned or required. Placeholder
domains come from magic wildcard DNS: sslip.io resolves any name with an IP
embedded in it to that IP, with no registration and no DNS records to create.

- Platform hostname: apps.<VM-IP-dashed>.sslip.io (the placeholder <HOSTNAME>).
- Per app: <app>.apps.<VM-IP-dashed>.sslip.io.
- Keycloak: auth.apps.<VM-IP-dashed>.sslip.io.
- Coolify dashboard: coolify.apps.<VM-IP-dashed>.sslip.io.

The concrete <HOSTNAME> is derived from the VM IP in Phase 1 and recorded in
server/NOTES.md. TLS is best effort in dev (HTTPS if Let's Encrypt issues for
the sslip.io names, plain HTTP otherwise); HTTP is acceptable for dev and is
not a checkpoint failure. Promotion to a real domain is configuration only and
is captured in docs/PROMOTION.md during Phase 9; no onboarding work is throwaway.

## 0. Scope: synthetic tenants only

This is a PROOF OF CONCEPT. Every tenant in docs/TENANTS.md is SYNTHETIC: a
minimal demo app created during the onboarding phases to exercise one specific
platform behavior (public access, internal auth gate, env/secret handling,
persistent write volume, static serving). No real or university-affiliated app
is onboarded here.

The real application portfolio is EXPLICITLY OUT OF SCOPE for this project and
would only be considered at promotion, on a real domain, after this dev
platform and its onboarding contract are proven. Demo apps are hello-world
level, but each must genuinely exercise its flagged behavior (a survey app
really writes to a volume, a secret app really reads a dummy key from Coolify's
env UI), so the path each real app would later take is actually validated.

## 1. Where the dev server lives

Decision: a small CLOUD VPS with a PUBLIC IP.

Rationale: sslip.io placeholder URLs then resolve and are testable from
anywhere, so routing, subdomain propagation, OIDC redirects, cookies, and
cross-app links can all be exercised off-LAN. Provider, region, size, and IP
are chosen and recorded in server/NOTES.md during Phase 1.

Topology is one VM for dev. The single point of failure is accepted in writing
for the dev build (settled decision 2). The control-plane / workload split is a
promotion-time concern, documented in docs/PROMOTION.md, not built here.

## 2. Capacity and scale (year one)

Expected portfolio: MEDIUM, roughly 10 to 25 apps, with a small publisher count.

Sizing implication: 16 GB RAM / 4 vCPU is the floor (Coolify plus Keycloak's
JVM plus R containers are memory-hungry, and R image builds are heavy). For a
10 to 25 app portfolio, provision on a clearly RESIZABLE plan and expect to sit
in the 16 to 32 GB range as tenants land. 80 GB disk minimum (R images are
bulky); watch disk and prune images on a schedule (docs/OPERATIONS.md).
Per-container memory limits are set per app during onboarding (Phases 4 and 5).

## 3. Identity

Decision: ONE Keycloak, LOCAL accounts only for the dev build (settled
decision 4). A realm is created with the team's first users; visitor auth for
protected apps is enforced by oauth2-proxy as Traefik forward-auth against this
realm. Coolify's own login protects only the Coolify dashboard and is never
conflated with visitor auth.

End state (not this plan): federation to MESA CI-Logon / Keycloak. No parallel
identity island is ever created.

Candidate post-Phase-8 enhancement: wiring an upstream IdP (Google or GitHub)
as an additional Keycloak identity provider, to be considered only AFTER the
auth chain is proven end to end (Checkpoints 3 and 7). Not in the year-one
critical path; recorded here so the option is not forgotten.

## 4. Backups

Dev decision: PROVIDER SNAPSHOTS. Use the VPS provider's volume / disk snapshot
feature on a schedule to cover the host, Coolify's database, app databases, and
persistent volumes. Coolify's own backup features for its database and any app
databases are also enabled (Phase 8). One test restore is performed and
documented before Checkpoint 8 closes.

Intended promotion target: S3-COMPATIBLE OBJECT STORAGE (for example AWS S3,
Backblaze B2, or Wasabi) as an off-host, provider-independent backup
destination. This is deferred to docs/PROMOTION.md; the provider-snapshot
approach is the dev-mode stand-in and is explicitly not the long-term target.

## 5. Summary of Phase 0 decisions (for Checkpoint 0 sign-off)

| Item            | Decision                                                        |
|-----------------|-----------------------------------------------------------------|
| Dev server      | Cloud VPS, public IP, sslip.io placeholder domains              |
| Topology        | One VM, SPOF accepted in writing; split deferred to promotion   |
| Year-one scale  | Medium, 10 to 25 apps, small publisher count                    |
| VM sizing       | 16 GB / 4 vCPU floor on a resizable plan; 80 GB+ disk           |
| Identity        | One Keycloak, local accounts only; MESA federation is end state |
| IdP federation  | Google/GitHub candidate, post-Phase-8 only, not critical path   |
| Backups (dev)   | Provider snapshots plus Coolify built-in DB backups             |
| Backups (target)| S3-compatible object storage, deferred to PROMOTION.md          |
