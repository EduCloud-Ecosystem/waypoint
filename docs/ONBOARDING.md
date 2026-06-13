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

   Networking rule: the app must listen on 0.0.0.0 (all interfaces) inside the
   container, on the port the Dockerfile EXPOSEs. Never bind 127.0.0.1 /
   localhost only. Traefik reaches your container over the Docker network, so a
   loopback-only bind is unreachable from the proxy and the app appears down.
   The templates already do this (uvicorn --host 0.0.0.0, Shiny host 0.0.0.0,
   gunicorn -b 0.0.0.0:PORT); keep it that way if you customize the start command.

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

   These publication details become the app's row in docs/TENANTS.md, the
   registry of record for who owns what, its sensitivity, and its onboarding
   status. An app is not considered onboarded until its row is present and
   marked live there.

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

This is the exact, repeatable recipe for placing the oauth2-proxy / Traefik
forward-auth gate in front of an internal or confidential app. It was proven end
to end locally in Phase 1.5 (docker-compose.dev.yml) and is reused unchanged on
the server in Phase 3; only the hostnames and the dev scheme differ. Protecting
the next app is then a recipe, not research.

The shape is: Traefik is the only public entrance. For a gated app, Traefik runs
two middlewares before the app: a forwardAuth that asks oauth2-proxy "is this
request authenticated and authorized?" and an errors middleware that turns a
deny into a redirect to Keycloak. Keycloak issues the identity; oauth2-proxy
holds the session cookie; the app itself carries no auth code.

### Values: local vs server

Everything is the same shape; only the domain and scheme change. Replace
`<VM-IP-dashed>` with the droplet IPv4 (dots to dashes) and use `https` once
Let's Encrypt issues (see server/NOTES.md for the scheme actually in use).

| Thing                | Local (Phase 1.5, HTTP)                                  | Server (Phase 3, sslip.io)                                       |
|----------------------|---------------------------------------------------------|------------------------------------------------------------------|
| Platform domain      | apps.127-0-0-1.nip.io                                   | apps.<VM-IP-dashed>.sslip.io                                     |
| Keycloak (issuer)    | http://auth.apps.127-0-0-1.nip.io/realms/educloud      | http(s)://auth.apps.<VM-IP-dashed>.sslip.io/realms/educloud      |
| oauth2-proxy host    | http://oauth2.apps.127-0-0-1.nip.io                    | http(s)://oauth2.apps.<VM-IP-dashed>.sslip.io                    |
| OIDC redirect URI    | http://oauth2.apps.127-0-0-1.nip.io/oauth2/callback    | http(s)://oauth2.apps.<VM-IP-dashed>.sslip.io/oauth2/callback    |
| Cookie domain        | .apps.127-0-0-1.nip.io                                  | .apps.<VM-IP-dashed>.sslip.io                                    |
| cookie-secure        | false (HTTP dev only)                                   | true once HTTPS is issued; false only while HTTP-only            |
| realm sslRequired    | none (HTTP dev only)                                    | external once HTTPS is issued                                    |

### Step 1: the Keycloak realm and OIDC client

The realm is defined once in keycloak/realm-export.json (imported by Keycloak at
start with `start --import-realm`). It is the canonical source; the settings
below describe what it contains so the same client can also be created by hand in
the Keycloak admin console on the server if preferred.

- Realm: `educloud`. For HTTP-only dev set `sslRequired: none`; flip to
  `external` at promotion.
- Client: `oauth2-proxy`, type OpenID Connect, **confidential** (Client
  authentication ON), Standard flow ON, Direct access grants OFF, Service
  accounts OFF.
- Valid redirect URIs: the oauth2-proxy callback for each environment (see the
  table). One confidential client serves all gated apps because oauth2-proxy is
  centralized and every app shares the `.apps...` cookie domain; you do NOT make
  a client per app.
- Client secret: generated by Keycloak (or supplied). It never lives in the
  repo. Locally it is injected into the realm import and into oauth2-proxy from
  the untracked `.env` via `${OAUTH2_PROXY_CLIENT_SECRET}`; on the server it is
  entered in Coolify's env UI for the oauth2-proxy service and copied into the
  Keycloak client.
- Two protocol mappers on the client:
  - Group membership mapper: claim name `groups`, full group path ON, added to
    the access token. This is what lets the gate authorize by group.
  - Audience mapper: adds `oauth2-proxy` to the access token `aud` (oauth2-proxy
    validates that its client id is in the audience).
- Authorization model:
  - internal apps: any realm user may pass. Membership in group
    `/educloud-users` is the team gate (oauth2-proxy `--allowed-group`).
  - confidential apps: create a narrower group (or realm/client role) and point
    that app's gate at it instead, so only those members pass.
- Users: real team members (or a federated upstream IdP later). The dev realm
  ships `alice` (in `/educloud-users`, passes) and `mallory` (no group,
  authenticates but is denied), which is how the deny path is tested.

### Step 2: deploy oauth2-proxy (the gate)

One oauth2-proxy instance fronts all gated apps. Provider `keycloak-oidc`. On the
server this is a Coolify service; secrets go in Coolify's env UI. Settings (flag
form; the local values are in docker-compose.dev.yml):

```
--provider=keycloak-oidc
--oidc-issuer-url=<Keycloak issuer from the table>
--client-id=oauth2-proxy
--redirect-url=<oauth2-proxy callback from the table>
--cookie-domain=.apps.<domain>
--whitelist-domain=.apps.<domain>
--email-domain=*                       # any realm user; group check narrows it
--allowed-group=/educloud-users        # per-app: swap for a narrower group on confidential apps
--scope=openid email profile
--upstream=static://200                # forward-auth mode: oauth2-proxy proxies nothing itself
--reverse-proxy=true
--set-xauthrequest=true                # exposes X-Auth-Request-User/Email/Groups to the app
--code-challenge-method=S256           # PKCE
--cookie-secure=<true on HTTPS, false on HTTP dev>
# secrets via env, never flags-in-repo:
OAUTH2_PROXY_CLIENT_SECRET=...         # matches the Keycloak client secret
OAUTH2_PROXY_COOKIE_SECRET=...         # exactly 16, 24, or 32 bytes (e.g. `openssl rand -hex 16` = 32 chars)
```

Note: the cookie secret must decode to 16/24/32 bytes. A 32-character value
(`openssl rand -hex 16`) is the safe choice; a `base64 32` value is 44 characters
and is rejected.

### Step 3: Traefik middlewares and routers

Two middlewares plus one extra router per environment. Local uses Traefik's file
provider (traefik/dynamic.yml); on the server Coolify generates equivalent
Traefik labels on the containers. The logic is identical.

- forwardAuth middleware: `address: http://oauth2-proxy:4180/oauth2/auth`,
  `trustForwardHeader: true`, and `authResponseHeaders` forwarding
  `X-Auth-Request-User`, `X-Auth-Request-Email`, `X-Auth-Request-Groups` so the
  app can read who was authenticated.
- errors middleware: on status `401-403`, fetch
  `/oauth2/sign_in?rd={url}` from the oauth2-proxy service and rewrite `401` to
  `302` so the browser actually redirects to Keycloak (carrying the original app
  URL in `rd`).
- A high-priority router for `PathPrefix(/oauth2/)` on every host, routed to
  oauth2-proxy and NOT gated, so the sign_in / start / callback / auth endpoints
  are reachable and the login bounce completes with the CSRF cookie on the shared
  cookie domain.
- The gated app's own router lists both middlewares: `[oauth-errors, oauth-auth]`.
  Public apps list neither.

Coolify label equivalents (per gated app), for reference on the server:

```
traefik.http.routers.<app>.middlewares=oauth-errors,oauth-auth
traefik.http.middlewares.oauth-auth.forwardauth.address=http://oauth2-proxy:4180/oauth2/auth
traefik.http.middlewares.oauth-auth.forwardauth.trustForwardHeader=true
traefik.http.middlewares.oauth-auth.forwardauth.authResponseHeaders=X-Auth-Request-User,X-Auth-Request-Email,X-Auth-Request-Groups
traefik.http.middlewares.oauth-errors.errors.status=401-403
traefik.http.middlewares.oauth-errors.errors.service=oauth2-proxy
traefik.http.middlewares.oauth-errors.errors.query=/oauth2/sign_in?rd={url}
traefik.http.middlewares.oauth-errors.errors.statusRewrites.401=302
```

### Step 4: cookie-secure and the HTTP-only dev caveat

While dev runs on HTTP, set oauth2-proxy `--cookie-secure=false` and realm
`sslRequired: none`. Both are dev-only. At promotion (HTTPS issued) flip
`--cookie-secure=true` and `sslRequired: external`, and add the https redirect
URI to the client. This is the only auth change promotion requires; it is listed
in docs/PROMOTION.md.

### Step 5: verify from a clean session before sharing the URL

Never assume the gate; prove it from a cookie-free client:

1. Request the app with no cookies. Expect `302` to the Keycloak authorize
   endpoint (the challenge). A `200` here means the app is UNGATED, stop.
2. Log in as an authorized user (in the app's allowed group). Expect to land on
   the app (`200`) and, if the app reads it, see the authenticated identity from
   `X-Auth-Request-User`.
3. Log in as a user NOT in the allowed group. Expect `403` (authenticated but
   denied).
4. Confirm no data file is directly downloadable through the app URL (Phase 8
   data-exposure check).

`dev/verify-auth.sh` runs exactly this matrix against the local stack with curl
and is the template for the server check.
