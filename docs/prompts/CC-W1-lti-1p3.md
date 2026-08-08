# CC-W1 — LTI 1.3 Tool implementation (the university entry requirement)

*Claude Code prompt. Authored in Cowork, 2026-08-08, from an analogue scan
recorded at `docs/decisions/2026-08-08-analogue-scan.md` (section 2), full scan in
the Cowork project as `outfitter-waypoint-landscape-2026-08-08.md`.

The finding, stated plainly: **a classroom tool that cannot LTI-launch from
Canvas, Moodle, or Blackboard is dead on arrival at most universities**, and LTI
was absent from the plan. Cairn carries NRPS roster sync at Phase 4 ("stretch");
the scan concluded the underlying LTI capability is an identity concern before it
is a roster concern, and therefore belongs to this layer, at must-have priority.

There is a second, more concrete reason to do this now: **NRPS replaces GitHub
Classroom's roster import outright.** With GHC retiring August 28, the migration
story currently depends on Cairn's `cairn import ghc` snapshot. LTI gives every
future course a roster path that does not depend on GitHub at all.

**Do not implement the JWT handling by hand.** Use **PyLTI1p3** (MIT, with Django,
Flask, and FastAPI adapters, full Advantage support; Blackboard's own tutorials
use it) or **ltijs** (Apache-2.0, Node) depending on what fits this repo's stack.
Hand-rolling OIDC launch validation is a well-known source of authentication bugs
and there is no upside to it here.

Repo convention note: this repo requires no em dashes and no en dashes in
documentation. Follow it in anything you write, and add a CHANGELOG.md entry for
any planning document you change, per the rule at the top of that file.*

---

## 1. Read first

- `docs/decisions/2026-08-08-analogue-scan.md` section 2 (the requirement, and the
  five pieces of the Tool side)
- `PLAN.1.md` and `INTEGRATION.md` (where this fits the phase plan and how Waypoint
  talks to the other modules)
- `keycloak/realm-export.json` and `docs/ONBOARDING.md`'s auth-gate recipe (the
  existing identity plumbing this must compose with, not bypass)
- The chosen library's own docs, in full, before writing code

## 2. Scope: the five pieces

Per the decision record. Implement the Tool side:

1. **Registration.** `client_id`, `deployment_id`, tool public JWKS URL, OIDC
   login-initiation URL, redirect URIs, `target_link_uri`. **Support Dynamic
   Registration** — Canvas and Moodle both do it, and it turns a multi-step manual
   admin exchange into a single URL paste, which is the difference between an
   instructor self-serving and an instructor filing a ticket.
2. **Launch.** Third-party-initiated OIDC login; validate the platform's
   RS256-signed `id_token` against the platform JWKS; check `nonce`, `iss`, `aud`,
   `deployment_id`. The library does this — your job is to not bypass it.
3. **Deep Linking.** Return a signed `LtiDeepLinkingResponse` JWT of content items,
   so an instructor picks which assignment from inside Canvas.
4. **NRPS.** GET the memberships endpoint for the roster.
5. **AGS.** Create line items, POST scores.

Services 4 and 5 authenticate via OAuth2 client_credentials with a
`private_key_jwt` assertion and scoped tokens.

## 3. How this composes with Keycloak

**LTI is not a replacement for Keycloak and must not become a second, parallel
identity system.** The launch establishes that a platform (Canvas) asserts this
user holds this role in this course context. Decide, and record the reasoning in
the decision document: does an LTI launch mint a Waypoint session directly, or
does it broker into Keycloak so that all sessions have one issuer?

The scan's framing argues for the latter (one issuer, one session model, LTI as an
inbound broker), consistent with how CILogon is planned to be configured as an
OIDC IdP inside Keycloak rather than as a second front door. But this is a real
architectural decision with a defensible alternative, so make it explicitly, write
it down, and do not let it be settled implicitly by whichever the library's
quickstart happens to demonstrate.

## 4. Key management

The tool's signing keypair is real key material. It must not enter the repository
— the existing hard rule (no secrets in the repo, `${ENV_VAR}` placeholders
resolved at import from the untracked `.env`, as `keycloak/README.md` documents)
applies here without exception. The public JWKS endpoint is served; the private key
is deployment configuration. Document key rotation, because a tool whose key
cannot be rotated without re-registering at every platform is a tool nobody can
operate for five years.

## 5. Testing without a Canvas instance

The realistic constraint: there is probably no Canvas to test against. Options, in
descending order of fidelity — pick one, say which, and be honest about what it
does and does not prove:

- Canvas has a free instructor account tier, and Moodle can be run locally in
  Docker. Either gives a real platform to launch from.
- 1EdTech provides a reference/certification test suite (certification itself
  requires paid membership, but check whether the test platform is usable
  without it).
- A stub platform in tests that issues correctly-signed `id_token`s against a test
  keypair. This proves your validation logic, and proves nothing about real
  platform quirks — which are the actual risk, since Canvas, Moodle, and
  Blackboard all differ in practice.

Follow `dev/verify-auth.sh`'s existing precedent: it drives the real OIDC
Authorization Code flow with curl and asserts a matrix of outcomes. An
`dev/verify-lti.sh` in the same spirit would fit this repo's established way of
proving auth works.

## 6. Report

- Which library, and why it fits this stack.
- The section 3 decision (LTI mints a session vs. brokers into Keycloak), with
  reasoning, written into the decision document as well as the report.
- What you tested against, and an honest statement of what that does not cover.
- A CHANGELOG.md entry, per the repo rule, in the established format.
- Whether Dynamic Registration works end to end, since that is the difference
  between instructor self-service and an admin ticket.
