# HOST

> Settled name: **Waypoint** (see `../educloud/SYSTEM.md`). The repo keeps the
> working name until the org-level rename.

Waypoint is EduCloud's publishing and federation layer: the hosting engine
(Coolify), the identity provider (Keycloak), the tenant onboarding contract,
and, as a separate workstream, the registry (catalog, register CLI, and the
federation metadata standard).

## Status (July 2026)

Phase 0 and Phase 1.5 are done: requirements, the tenant intake and
onboarding contract, templates, and a full local pre-build (Coolify stack,
Keycloak realm export, the visitor-auth round trip proven on demo apps).
Phase 1 (server provisioning, DigitalOcean) is pending purchase. Phases 2
through 9 run against the server and are configuration by design. The
registry standard is in proposal stage
(`../educloud/architecture/EduCloud-registry-federation-proposal.md`) and is
collaborator-led.

## Read in this order

| | |
|---|---|
| `PLAN.1.md` | The phased build plan on Coolify. Phases, gates, and acceptance. |
| `INTEGRATION.md` | How the hosting layer relates to EduCloud, Cairn, and MESA. |
| `docs/ONBOARDING.md` | The tenant contract: a Coolify application is the contract. |
| `docs/REQUIREMENTS.md`, `docs/TENANTS.md` | Platform requirements and the tenant registry of record. |
| `CHANGELOG.md` | Document history. |
| `HOST_SESSION_BUILD.md`, `CLAUDE_CODE_INSTRUCTIONS.md` | Build-session operating instructions. |

## Role in EduCloud

Waypoint is the deployment substrate for the other modules (each ships a
Coolify-conforming container) and the ecosystem identity provider (every
module's OIDC seam points here when deployed together). The scale path is in
`../educloud/SYSTEM.md` §7: one host at MVP, Coolify multi-tenant with
Keycloak at institutional scale, CILogon and per-site provider registries at
federation.

## License and contributions

Code is intended Apache-2.0 with DCO sign-off; see `LICENSING.md` and `DCO`.
The full LICENSE text lands at the publish-step rename, in the same commit as
the name.
