# TENANTS.md: Tenant intake manifest for the EduCloud hosting layer

Drafted in Phase 0. Conventions: LF endings, no em dashes or en dashes.

SCOPE: every tenant below is SYNTHETIC (see docs/REQUIREMENTS.md section 0). No
real or university-affiliated app is onboarded in this proof of concept; the
real portfolio is out of scope and is a promotion-time concern. Each demo app
is hello-world level but must genuinely exercise its flagged behavior so the
platform path is actually validated.

Only status=production rows onboard. Internal and confidential apps must sit
behind a working Keycloak-backed auth challenge, verified from a clean browser
session, before their URL is recorded as live. Onboarding status is updated in
the "Onboarded" column as each app goes live (Phases 4 through 6).

## Manifest

| App name            | Framework         | Status     | Sensitivity | Audience  | Refresh   | Owner | Writes data            | Secrets                         | Onboarded |
|---------------------|-------------------|------------|-------------|-----------|-----------|-------|------------------------|---------------------------------|-----------|
| demo-public-shiny   | R Shiny           | production | public      | anyone    | static    | Ben   | no                     | no                              | no        |
| demo-internal-shiny | R Shiny           | production | internal    | team only | static    | Ben   | no                     | no                              | no        |
| demo-dash-secret    | Dash              | production | internal    | team only | on-demand | Ben   | no                     | yes: one dummy API key          | no        |
| demo-survey         | Shiny for Python  | production | internal    | team only | on-demand | Ben   | yes: saves submissions | no                              | no        |
| demo-static-report  | static HTML       | production | public      | anyone    | static    | Ben   | no                     | no                              | no        |

## Per app, what each one proves

- demo-public-shiny (Phase 4): the R Shiny templated image and the public path.
  Loads with NO auth challenge; verified absence of a login (a public app must
  prove the gate is off). WebSockets interactive through the Traefik proxy.
- demo-internal-shiny (Phase 4): the internal auth gate on an R Shiny app.
  oauth2-proxy forward-auth challenges a clean browser; a realm user passes, a
  non-user does not.
- demo-dash-secret (Phase 5): env/secret handling. Reads ONE dummy API key
  whose value is entered only in Coolify's env UI, never in the repo or
  transcript. Proves a confidential value never touches git. Internal auth gate
  applies as well.
- demo-survey (Phase 5): persistent write path. Saves submitted form records to
  a Coolify persistent volume; the volume is confirmed included in backups
  (Phase 8) and a test submission survives a container restart. Internal auth
  gate applies.
- demo-static-report (Phase 6): static serving. A self-contained HTML report
  served by a single static container at its subdomain; no app process; public.

## Notes

- Writes-data apps (demo-survey): mount a Coolify persistent volume for the
  write path; include it in the Phase 8 backup and the test restore.
- Secret-bearing apps (demo-dash-secret): the dummy API key is created fresh in
  Coolify's env UI at onboarding; no inherited credential exists because every
  tenant is synthetic. Value never enters the repo, app code, or the transcript.
- Sensitivity gate: demo-internal-shiny, demo-dash-secret, and demo-survey are
  internal and do not have their URL recorded as live until their Keycloak
  challenge is verified from a clean browser session (Phases 4, 5, and 7).
