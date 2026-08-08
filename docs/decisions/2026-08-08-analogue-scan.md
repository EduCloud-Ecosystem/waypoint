# Analogue scan: hosting, identity, and tenancy

2026-08-08. Surveyed the self-hosted PaaS field, the open-source identity field,
the education-federation stack, and what universities actually run today, to find
anything Waypoint is about to build that already exists, and anything Waypoint is
about to omit that a university will require. Full scan with sources:
`outfitter-waypoint-landscape-2026-08-08.md` (Cowork project).

Status: decision record. Changes no code. Adds one must-have to the plan (LTI 1.3,
section 2), one structural reframing (section 1), and a triage table (section 5).

Convention note: this file follows the repo rule of no em dashes and no en dashes.

---

## 1. The structural finding: the tenancy layer must live above Coolify

Every tool surveyed either has real multi-tenancy under a bad license, or a good
license with no multi-tenancy. Nothing in the open-source PaaS field gives enforced
per-tenant quotas plus RBAC plus backup under a permissive license.

- **Dokploy** relicensed in January 2026. SSO, RBAC, and audit logs moved to a
  Source Available License. Disqualified on principle.
- **Portainer CE** gates RBAC and teams to the Business tier. **Nomad** is BUSL and
  gates resource quotas to Enterprise. In both cases the feature we need is the
  paid one.
- **Dokku**, **CapRover**, and **Kamal** are single-admin: no RBAC, no tenancy.
- **Cloudron** has the best per-app isolation and backup story in the survey, but
  is source-available and paid above two apps.

**Coolify** (Apache-2.0) reached v4.0.0 stable in April 2026 and has teams with
roles, but its isolation is logical, not enforced: all tenants share one Docker
daemon, one proxy, and one Postgres, and there is **no per-team CPU, RAM, or disk
quota primitive**. Multi-server is experimental, with manual SSH registration, no
scheduler, and no rescheduling on node failure. **v5 is an unreleased ground-up
rewrite** for multi-node with no announced date. Eleven critical CVEs were
disclosed in January 2026 and patched in v4.0.0, so the dashboard port must not be
exposed.

Conclusion, and it is a reframing rather than a tool swap: Waypoint's
differentiator is the **tenancy control plane** (course to tenant to quota to
lifecycle to LTI identity binding), with Coolify as a swappable execution backend
behind an interface, not as the tenancy boundary. Coolify remains a reasonable v1
backend for a single institution. But the plan's "each scale step is a config diff"
promise currently rests on a platform whose scaling story is an unreleased rewrite,
and that risk belongs in the interface rather than being discovered later. Keep a
Kubernetes backend option open.

## 2. LTI 1.3 is make-or-break, and it is not in the plan

**A classroom tool that cannot LTI-launch from Canvas, Moodle, or Blackboard is
dead on arrival at most universities.** Cairn's roadmap has NRPS roster sync at
Phase 4 ("stretch"). This scan moves the underlying LTI capability to a must-have
for Waypoint, because it is an identity concern before it is a roster concern and
therefore belongs to this layer.

What implementing the Tool side actually requires:

1. **Registration.** Exchange `client_id`, `deployment_id`, the tool public JWKS
   URL, the OIDC login-initiation URL, redirect URIs, and `target_link_uri`.
   Support Dynamic Registration; Canvas and Moodle both do.
2. **Launch.** Third-party-initiated OIDC login, the platform posts an RS256-signed
   `id_token`, and the tool validates it against the platform JWKS and checks
   `nonce`, `iss`, `aud`, and `deployment_id`.
3. **Deep Linking.** Return a signed `LtiDeepLinkingResponse` JWT of content items.
   This is how an instructor picks which assignment from inside Canvas.
4. **NRPS** (Names and Role Provisioning Service). GET the memberships endpoint for
   the roster. **This replaces GitHub Classroom's roster import entirely**, which
   matters directly for the August 28 migration story.
5. **AGS** (Assignment and Grade Services). Create line items and POST scores;
   grades land in the Canvas gradebook.

Services 4 and 5 authenticate with OAuth2 client_credentials using a
`private_key_jwt` assertion and scoped tokens.

Mature libraries exist, so the JWT handling should not be written by hand.
**PyLTI1p3** (MIT, Python, with Django, Flask, and FastAPI adapters, full Advantage
support; Blackboard's own tutorials use it) and **ltijs** (Apache-2.0, Node) are
both viable. 1EdTech conformance certification requires paid membership. Most
universities accept uncertified tools, so certify later when procurement asks.

## 3. Keycloak stays, with one configuration decision to make now

Keycloak 26.7.0 (July 2026) is a materially better position than when this was
chosen. **Organizations** gained fine-grained delegation roles and per-organization
permissions, organization groups now support role inheritance, realm discovery by
display name landed for multi-tenant setups, SCIM provisioning is in preview, and
**multi-cluster v2 drops the external Infinispan requirement** in favor of
DB-backed sync, which removes a real chunk of operational burden.

The decision: **Organizations, not realms, for departments and courses.** Realms
give hard isolation but no cross-realm SSO and N times the admin overhead. Use one
realm plus Organizations for units within an institution, and reserve realms for
genuinely separate institutions. That keeps scaling a config diff.

Alternatives were checked and rejected. **Authelia** has no SAML, which is
disqualifying for InCommon. **Zitadel** relicensed to AGPL-3.0 in March 2025 and
has no LDAP. **Ory** requires building every UI. **Authentik** (MIT) is worth
keeping as a fallback only because it uniquely ships an LDAP server outpost,
relevant if a campus ever needs Waypoint to serve LDAP rather than consume it.

Federation is a config entry, by design. **CILogon** brokers InCommon and eduGAIN
(over 5000 IdPs) plus ORCID, Google, and GitHub, with a free tier for academic
research projects, and is deployable as open source. Configure it as an **OIDC
identity provider inside Keycloak**, and then cross-institution federation is one
Keycloak IdP entry, satisfying the config-diff contract. Do not put a Shibboleth SP
in the request path; Keycloak's SAML IdP support handles campus Shibboleth IdPs
directly.

## 4. Open OnDemand: integrate, do not replace

**Open OnDemand** (MIT, NSF-funded) runs at over 2,100 organizations including
Harvard, Georgia Tech, Columbia, and Texas A&M. It is the incumbent web portal for
campus HPC and it is not a competitor. It authenticates via `mod_auth_openidc`, so
**pointing it at Waypoint's Keycloak makes Open OnDemand a Waypoint tenant**.

Its isolation mechanism is worth studying as a model even where it is not adopted:
the **Per-User NGINX (PUN)**. Running `nginx_stage pun -u <user>` launches an NGINX
process as that Unix user, with per-user temp dirs at mode 0700, per-user logs and
sockets, and Apache `mod_ood_proxy` routing `/pun/sys/<app>` to the right socket.
The result is kernel-enforced per-user isolation with no container runtime, and
quotas come free from the OS and the batch scheduler. Where Waypoint needs per-user
isolation and a container is overkill, this is the cheaper primitive.

**JupyterHub and Zero-to-JupyterHub** (BSD-3) is likewise a complement. Do not
rebuild it. Ship a Waypoint template that deploys it with `ltiauthenticator` (which
already supports LTI 1.1 and 1.3), `jupyterhub-idle-culler`, and `profile_list` for
instructor-chosen resource tiers.

## 5. Triage

| Item | Verdict | Mechanism |
|---|---|---|
| **LTI 1.3 plus Advantage** (Deep Linking, NRPS, AGS) | **Must-have** | Tool side via PyLTI1p3 (MIT). NRPS replaces GHC roster import. Without this, dead on arrival at most universities. |
| **Keycloak: one realm plus Organizations** | **Must-have** | Organizations for departments and courses; realms only for separate institutions. Pin the version, upgrade quarterly, use the Operator. |
| **CILogon as a Keycloak OIDC IdP** | **Must-have (design now, enable later)** | Federation becomes one IdP config entry, a true config diff. Free tier for academic projects. |
| **SAML IdP support** (campus Shibboleth) | **Must-have** | Keycloak SAML IdP. Rules out Authelia entirely. |
| **Per-tenant labels and cost attribution from day one** | **Must-have** | `course_id`, `term`, `owner` on every container and volume. Retrofitting attribution is painful. Pairs with Outfitter's ledger. |
| **Tenancy and quota layer above Coolify** | **Must-have** | Coolify has no per-team quota primitive. Enforce cgroup and disk quota in Waypoint's own control plane; treat Coolify as an execution backend. |
| **Open OnDemand integration** (SSO'd, not replaced) | **Must-have if targeting R1s** | Point its `mod_auth_openidc` at Waypoint's Keycloak. Borrow the PUN model where a container is overkill. |
| **Declarative env template plus TTL reaper** | **Must-have** | The Heroku `app.json` and Northflank preview-blueprint pattern: assignment template to per-student stack, auto-destroyed at term end. Pairs with Outfitter specs/03 section 3. |
| **Coolify as the v1 backend** | **Nice-to-have, hedge** | Fine for a single institution v1, but abstract it behind a deploy interface. v5 is an unreleased rewrite and multi-server has no scheduler today. |
| **Warm pool plus scale-to-zero for student envs** | **Nice-to-have, high ROI** | Fly's per-user blueprint: pre-provisioned warm pool, wildcard router with transparent replay, autostop on idle, tmpfs reset for "reset my environment." |
| **JupyterHub as a provisionable tenant app** | **Nice-to-have** | Ship a Zero-to-JupyterHub template with `ltiauthenticator`, idle-culler, and `profile_list`. |
| **1EdTech LTI certification** | **Nice-to-have, later** | Requires paid membership. Uncertified tools work in Canvas and Moodle; certify when procurement demands it. |
| Dokploy, Portainer, Nomad | **Skip** | The exact features needed are the paid or relicensed ones. |
| Cloudron | **Skip, but study** | Source-available and paid. Its per-app versioned backup-to-S3 model is worth imitating. |
| Dokku, CapRover, Kamal | **Skip** | Single-admin, no tenancy. |
| Zitadel, Ory, Authelia, Casdoor, Logto | **Skip** | AGPL plus no LDAP; custom-UI burden; no SAML. Keycloak wins the education requirement set. |
| Galaxy, CyVerse | **Skip** | Domain-specific science gateways, no overlap. |
