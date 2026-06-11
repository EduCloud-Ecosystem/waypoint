# ONBOARDING.md: How to publish an app on the EduCloud hosting layer

One page, the publishing contract in Coolify terms. Conventions: LF endings, no
em dashes or en dashes. A Coolify "application" IS this contract, enforced by
software. In this proof of concept the only tenants are the synthetic demo apps
in docs/TENANTS.md; the same contract is what a real app would follow at
promotion.

## What you provide to publish

To publish an app, hand the platform admin four things:

1. A self-contained source repo or directory. It must run from RELATIVE paths
   only: no absolute paths to a developer's home directory, no hardcoded
   machine names. Cross-links to other hosted apps use their subdomains
   (<other-app>.<HOSTNAME>), not localhost or an old server.

2. A declared environment, as a Dockerfile (canonical for production). Start
   from the repo templates:
   - R Shiny: templates/shiny.Dockerfile, with R_PKGS set to your package list.
   - Python (Dash, Flask, FastAPI, Streamlit, Shiny for Python):
     templates/py.Dockerfile, with START_CMD set for your framework.
   A plain dependency manifest (requirements.txt, renv.lock, or a package list)
   may be provided alongside, but the Dockerfile is the artifact that ships.
   Coolify's buildpack (nixpacks) is for preview or throwaway only, never for a
   production tenant (portability rule, INTEGRATION.md).

3. Secrets listed by NAME only. Never put secret values in the repo, in app
   code, or in any message. List the variable names (see templates/dot-env.example);
   the admin enters the values in Coolify's environment UI for your app. Any
   credential inherited from a previous environment is rotated at onboarding.

4. Publication details:
   - desired subdomain (becomes <app>.<HOSTNAME>),
   - sensitivity: public, internal, or confidential,
   - audience (who should reach it),
   - a named owner (a real person accountable for the app),
   - whether the app WRITES data (so a persistent volume is mounted and backed
     up), and the write path if so.

## What the platform does for you

- Builds your Dockerfile and runs it as a container on the Docker network.
- Puts it behind Traefik at https or http (dev scheme) on your subdomain, the
  sole public entrance; your container port is never exposed directly to the host.
- Provides env/secret injection, per-container memory limits, restart policy,
  and git-push or button redeploys.
- For internal and confidential apps, places a Keycloak-backed login challenge
  (oauth2-proxy forward-auth) in front of the app. Verified from a clean browser
  before the URL is shared.
- For data-writing apps, mounts a persistent volume at your declared write path
  and includes it in backups.

## Sensitivity, in plain terms

- public: no login. Anyone with the URL gets in. Verified to have NO challenge.
- internal: team-only. Keycloak login required; any realm user passes.
- confidential: restricted. Keycloak login plus a narrowed user/group; only
  authorized users pass.

Data files must not be directly downloadable through the app URL. This is tested
per app during verification (Phase 8).

## Admin section: wiring the auth gate (recipe)

This section is the exact, repeatable recipe for placing the oauth2-proxy /
Traefik forward-auth gate in front of an internal or confidential app. It is
filled in during Phase 3 once the dummy-app round trip is proven, so that
protecting each subsequent app is a recipe and not research.

(To be completed at Checkpoint 3: oauth2-proxy deployment, Keycloak OIDC client
settings, Traefik middleware labels, cookie-secure note for HTTP-only dev, and
the clean-browser verification steps.)
