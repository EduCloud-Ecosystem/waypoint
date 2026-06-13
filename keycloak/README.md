# keycloak/: realm definition for the EduCloud dev identity

`realm-export.json` is the `educloud` realm, imported at container start by
`docker compose -f docker-compose.dev.yml up` (Keycloak runs `start
--import-realm`, reading `/opt/keycloak/data/import/`). It is the reusable
artifact that defines the OIDC client the visitor-auth gate uses; the same realm
is imported on the server in Phase 3.

## Why no secrets are in the file

The committed JSON contains NO secret values. The client secret and the demo
user passwords are written as `${ENV_VAR}` placeholders and resolved at import
time from the Keycloak container's environment (Keycloak supports `${ENV_VAR}`
substitution in imported realm files). The actual values live only in the
untracked `.env`. This keeps the hard rule (no secrets in the repo) intact while
the realm stays fully reproducible.

Placeholders used: `${OAUTH2_PROXY_CLIENT_SECRET}`, `${DEMO_ALICE_PASSWORD}`,
`${DEMO_MALLORY_PASSWORD}`.

## What it defines

- Realm `educloud`, `sslRequired: none` (HTTP-only dev; flip to `external` at
  promotion, see docs/PROMOTION.md).
- Confidential OIDC client `oauth2-proxy` (standard flow), with a group
  membership mapper (claim `groups`, full path) and an audience mapper (adds
  `oauth2-proxy` to the access token `aud`, which oauth2-proxy validates).
- Group `/educloud-users`.
- Users `alice` (in `/educloud-users`, passes the gate) and `mallory` (no group,
  authenticates but is denied by `--allowed-group=/educloud-users`).

## Note on realm representation fields

Keycloak rejects unknown top-level fields on import (no `comment` key, and JSON
has no comments), which is why this explanation lives here and not inside the
JSON.
