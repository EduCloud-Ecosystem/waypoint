#!/usr/bin/env bash
# dev/verify-auth.sh: prove the Phase 1.5 local auth round trip end to end.
#
# Drives the real OIDC Authorization Code flow with curl (no browser) against the
# docker-compose.dev.yml stack and asserts the full matrix:
#   1. public apps load with NO challenge (200),
#   2. an unauthenticated request to an internal app is challenged (302 to Keycloak),
#   3. a realm user IN the allowed group (alice) passes and reaches the app (200),
#   4. a realm user NOT in the allowed group (mallory) is denied by the gate (403),
#   5. the env/secret app and the persistent-write app behave as declared.
#
# Passwords are read from the untracked .env; nothing secret is printed.
# Usage: bash dev/verify-auth.sh   (from the repo root, stack already up)

set -uo pipefail

BASE="apps.127-0-0-1.nip.io"
INTERNAL="http://demo-internal-shiny.${BASE}/"
PASS=0; FAIL=0
say()  { printf '%s\n' "$*"; }
ok()   { printf '  PASS: %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  FAIL: %s\n' "$*"; FAIL=$((FAIL+1)); }

# Load demo user passwords from .env (DEMO_ALICE_PASSWORD, DEMO_MALLORY_PASSWORD).
if [ -f ./.env ]; then set -a; . ./.env; set +a; else say "ERROR: .env not found"; exit 2; fi

code_of() { curl -s -o /dev/null -w '%{http_code}' "$1"; }

# Runs the full auth code flow for a user. Echoes the FINAL http code reached
# after login (200 if the gate admits them to the app, 403 if the gate denies).
login_final_code() {
  local user="$1" pass="$2" jar page action
  jar="$(mktemp)"
  # 1. Hit the protected app and follow the bounce to the Keycloak login page.
  page="$(curl -s -c "$jar" -b "$jar" -L "$INTERNAL")"
  # 2. Extract the login form action URL (login-actions/authenticate?...).
  action="$(printf '%s' "$page" \
    | grep -oE 'action="[^"]*login-actions/authenticate[^"]*"' \
    | head -1 | sed -e 's/^action="//' -e 's/"$//' -e 's/&amp;/\&/g')"
  if [ -z "$action" ]; then echo "NOFORM"; rm -f "$jar"; return; fi
  # 3. Submit credentials and follow the callback bounce back to the app.
  local final
  final="$(curl -s -c "$jar" -b "$jar" -L -o /dev/null -w '%{http_code}' \
    --data-urlencode "username=${user}" \
    --data-urlencode "password=${pass}" \
    --data-urlencode "credentialId=" \
    "$action")"
  echo "$final"
  rm -f "$jar"
}

say "== 1. Public apps load with no challenge =="
c="$(code_of "http://demo-public-shiny.${BASE}/")";  [ "$c" = 200 ] && ok "demo-public-shiny 200" || bad "demo-public-shiny got $c"
c="$(code_of "http://demo-static-report.${BASE}/")"; [ "$c" = 200 ] && ok "demo-static-report 200" || bad "demo-static-report got $c"

say "== 2. Unauthenticated internal app is challenged =="
loc="$(curl -s -D - -o /dev/null "$INTERNAL" | tr -d '\r' | awk -F': ' 'tolower($1)=="location"{print $2}')"
ccode="$(code_of "$INTERNAL")"
if [ "$ccode" = 302 ] && printf '%s' "$loc" | grep -q "auth.${BASE}/realms/educloud/protocol/openid-connect/auth"; then
  ok "302 to Keycloak authorize endpoint"
else
  bad "expected 302 to Keycloak, got code=$ccode location=$loc"
fi

say "== 3. alice (in /educloud-users) passes the gate =="
fa="$(login_final_code alice "${DEMO_ALICE_PASSWORD}")"
[ "$fa" = 200 ] && ok "alice reached the app (200)" || bad "alice expected 200, got $fa"

say "== 4. mallory (no group) is denied by the gate =="
fm="$(login_final_code mallory "${DEMO_MALLORY_PASSWORD}")"
[ "$fm" = 403 ] && ok "mallory denied (403)" || bad "mallory expected 403, got $fm"

say "== 5. Other tenants =="
c="$(code_of "http://demo-dash-secret.${BASE}/")"; [ "$c" = 302 ] && ok "demo-dash-secret gated (302)" || bad "demo-dash-secret expected 302, got $c"
c="$(code_of "http://demo-survey.${BASE}/")";       [ "$c" = 302 ] && ok "demo-survey gated (302)" || bad "demo-survey expected 302, got $c"

say ""
say "RESULT: ${PASS} passed, ${FAIL} failed"
[ "$FAIL" = 0 ]
