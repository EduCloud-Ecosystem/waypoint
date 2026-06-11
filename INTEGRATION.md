# INTEGRATION.md: How the hosting layer relates to EduCloud/Quad and MESA

Companion to HOST_SESSION_BUILD.md and PLAN.1.md. This file answers one
question: does the Coolify hosting layer integrate with the wider EduCloud
work, and if so, where the seam is. It constrains the later Registry workstream
without building it. Same conventions as the rest of the repo: LF endings, no
em dashes or en dashes.

## What this answers

The hosting layer integrates as a sibling layer, not as part of the Quad
classroom platform. It joins Quad through exactly one seam: the EduCloud
Registry and its Coolify adapter (deferred to a later plan). Coolify is a
deployment backend behind that adapter, never a dependency that Quad imports.
This keeps faith with two settled Quad commitments: adapter discipline (every
optional capability composes through a thin seam) and no lock-in (commitment 5).

Coolify is Apache-2.0 (verified against the current v4 tree). That makes the
future Coolify adapter a clean Apache-2.0 interop primitive that sits beside
pkg/adapter, with no license friction against Quad's AGPL-3.0 control plane.

## The seam

- The Registry holds the catalog and the register CLI. A thin Coolify adapter
  drives Coolify's API to deploy a registered object and records its
  deployed_url. A second adapter (Kubernetes, Nomad, or plain Docker) can target
  a different backend without the Registry knowing the difference.
- Define the adapter interface now, in writing, even though the Registry is
  built later. The interface is the thing that prevents Coolify lock-in. The
  test: could the entire tenant portfolio move to a K8s adapter later with no
  change to app source. If yes, the seam is real.
- This plan only prepares the seam: a scoped, read-tested Coolify API token
  stored outside the repo (PLAN.1.md Phase 9). Nothing in this plan waits on the
  Registry, and the Registry never gates anything here.

## Tiers (why Coolify does not compete with MESA's Kubernetes)

- Single-VM / small-operator tier: Coolify. One VM, Docker, built-in proxy and
  TLS, low ops overhead. This is the low-activation-energy path the architecture
  needs so that small operators (a department, a community college serving
  transfer students) are not filtered out by a Kubernetes requirement.
- Institutional tier: MESA's CyVerse Kubernetes, Jetstream2, ACCESS-CI. Higher
  capability, higher operating cost, a platform team assumed.
- The Registry targets both tiers through different adapters. Coolify therefore
  fills a real gap in the deployment story rather than duplicating K8s. The
  condition is that apps stay portable across tiers (see Boundaries).

## Boundaries that must hold

1. Trusted publishing, not untrusted execution. This layer publishes trusted
   developer apps. It is not, and must not become, the place untrusted student
   code runs. Student submissions execute only in Quad's autograding runner
   (hardened OCI containers, gVisor or Kata as a documented runtime swap).
   Coolify's app containers are not a student-code sandbox. Keep the sandbox
   isolation honesty principle intact: do not let the two execution contexts
   blur.
2. Portable artifacts. The Dockerfile is the canonical, portable build artifact.
   Prefer it over Coolify's buildpack (nixpacks) for any production tenant.
   Nixpacks is acceptable only for throwaway or preview deployments. A
   buildpack-built app ties its reproducibility to Coolify and weakens the
   no-lock-in claim.
3. One identity, federated. The hosting layer runs one Keycloak, federated to
   MESA's CI-Logon/Keycloak rather than standing up a parallel identity island.
   Visitor accounts for protected apps and MESA accounts must not fork. Note
   that this identity model (real accounts, possible institutional SSO) is for
   publishers and visitors and is deliberately distinct from Quad's
   privacy-minimal classroom model (Git usernames only). The two models do not
   leak into each other.
4. Names disambiguated. "EduCloud hosting layer" (this repo) and "EduCloud/Quad
   classroom platform" are different systems that share a brand. Say which one
   is meant in any shared doc, URL scheme, or grant narrative.

## Licensing placement

- Coolify: Apache-2.0, used as-is, not vendored into Quad.
- Future Coolify adapter and Registry interop primitives: Apache-2.0, beside
  pkg/adapter. Consistent with Quad's split-license posture (DESIGN.md section
  11).
- Quad control plane: AGPL-3.0, unaffected. The adapter pattern is exactly what
  keeps the AGPL boundary clean while integrating Apache-2.0 backends.

## Open decisions: RESOLVED (June 2026, recorded in HOST_SESSION_BUILD.md
## under Settled decisions)

1. Relationship to Quad section 12 Interactive Workspaces. RESOLVED: this layer
   is a portfolio of finished, long-lived published apps. It is not a
   pkg/workspace backend; ephemeral per-user environments are out of scope and
   would require a different security posture.
2. Control plane and workload topology. RESOLVED for dev: one VM, SPOF accepted
   in writing (server/NOTES.md). The control-plane/workload split is deferred
   to promotion and captured in docs/PROMOTION.md.
3. Reverse proxy. RESOLVED: Traefik (Coolify's v4 default). Forward-auth with
   oauth2-proxy is wired as Traefik middleware; syntax verified against current
   docs at execution time.
4. Keycloak relationship to MESA CI-Logon/Keycloak. RESOLVED: standalone with
   local accounts for year one; federation to MESA CI-Logon/Keycloak is the
   stated end state. No parallel identity island is ever created.

## Edits to fold into the existing docs: APPLIED (June 2026)

All of the following are now reflected in PLAN.1.md and HOST_SESSION_BUILD.md;
this list remains as the record of what changed and why.

- PLAN.1.md Phase 1: add a capacity and topology line. 16 GB holding Coolify
  plus Keycloak (JVM) plus R containers is a floor and a SPOF; prefer a separate
  control-plane node or a larger resizable plan, and record the choice in
  server/NOTES.md.
- PLAN.1.md Phase 3: name the reverse proxy and verify the forward-auth
  middleware mechanism for that proxy against current docs.
- PLAN.1.md Phase 4: add a note that any R app scaled past one replica needs
  sticky sessions for Shiny websockets.
- PLAN.1.md Phase 5: flip the build default. Dockerfile is canonical for
  production tenants; nixpacks for preview or throwaway only.
- HOST_SESSION_BUILD.md Hard rules: add the trusted-publishing-not-untrusted-
  execution boundary, and the one-Keycloak-federated rule.
- HOST_SESSION_BUILD.md: state the section 12 decision once it is made, and add
  one line naming this layer as a Registry deployment backend behind a future
  adapter (not a Quad dependency).
