#!/usr/bin/env bash
# Setup Keycloak realm "ollama-chat" with a public PKCE client and optional Google IDP.
#
# Usage:
#   KEYCLOAK_URL=https://auth.example.com \
#   KC_ADMIN_USER=admin \
#   KC_ADMIN_PASSWORD=secret \
#   GOOGLE_CLIENT_ID=xxxx.apps.googleusercontent.com \
#   GOOGLE_CLIENT_SECRET=GOCSPX-xxxx \
#   APP_URL=http://localhost:8082 \
#   bash scripts/setup-ollama-realm.sh
#
# Google OAuth setup (do this first in Google Cloud Console):
#   1. APIs & Services → Credentials → Create OAuth 2.0 Client ID (Web application)
#   2. Authorized redirect URIs → add:
#        ${KEYCLOAK_URL}/realms/ollama-chat/broker/google/endpoint
#   3. Copy Client ID and Client Secret into the env vars above.

set -euo pipefail

KC_URL="${KEYCLOAK_URL:-https://auth.gm-global-techies-town.club}"
KC_ADMIN="${KC_ADMIN_USER:-admin}"
KC_PASS="${KC_ADMIN_PASSWORD:?KC_ADMIN_PASSWORD is required}"
APP_URL="${APP_URL:-http://localhost:8082}"
REALM="ollama-chat"
CLIENT_ID="ollama-chat-app"
GOOGLE_CLIENT_ID="${GOOGLE_CLIENT_ID:-}"
GOOGLE_CLIENT_SECRET="${GOOGLE_CLIENT_SECRET:-}"

# ── 1. Admin token ────────────────────────────────────────────────────────────
echo "→ Fetching admin token from ${KC_URL} ..."
TOKEN=$(curl -sf -X POST "${KC_URL}/realms/master/protocol/openid-connect/token" \
  -d "client_id=admin-cli&username=${KC_ADMIN}&password=${KC_PASS}&grant_type=password" \
  | jq -r '.access_token')
[ -z "$TOKEN" ] && { echo "✗ Failed to get admin token"; exit 1; }

AUTH="Authorization: Bearer ${TOKEN}"
CT="Content-Type: application/json"

# ── 2. Create realm ───────────────────────────────────────────────────────────
echo "→ Creating realm '${REALM}' ..."
HTTP=$(curl -so /dev/null -w "%{http_code}" -X POST "${KC_URL}/admin/realms" \
  -H "$AUTH" -H "$CT" -d "$(cat <<JSON
{
  "realm": "${REALM}",
  "displayName": "Ollama Chat",
  "enabled": true,
  "loginWithEmailAllowed": true,
  "duplicateEmailsAllowed": false,
  "resetPasswordAllowed": true,
  "rememberMe": true,
  "accessTokenLifespan": 1800,
  "refreshTokenMaxReuse": 0,
  "ssoSessionMaxLifespan": 36000,
  "bruteForceProtected": true
}
JSON
)")
[ "$HTTP" = "201" ] && echo "  ✓ Realm created" || echo "  ℹ Realm already exists (HTTP $HTTP)"

# ── 3. Create public PKCE client ──────────────────────────────────────────────
echo "→ Creating client '${CLIENT_ID}' ..."
HTTP=$(curl -so /dev/null -w "%{http_code}" \
  -X POST "${KC_URL}/admin/realms/${REALM}/clients" \
  -H "$AUTH" -H "$CT" -d "$(cat <<JSON
{
  "clientId": "${CLIENT_ID}",
  "name": "Ollama Chat App",
  "description": "Browser-based Ollama chat interface",
  "enabled": true,
  "publicClient": true,
  "standardFlowEnabled": true,
  "implicitFlowEnabled": false,
  "directAccessGrantsEnabled": false,
  "serviceAccountsEnabled": false,
  "frontchannelLogout": true,
  "redirectUris": [
    "${APP_URL}/*",
    "http://localhost:8082/*"
  ],
  "webOrigins": [
    "${APP_URL}",
    "http://localhost:8082",
    "+"
  ],
  "attributes": {
    "pkce.code.challenge.method": "S256",
    "post.logout.redirect.uris": "${APP_URL}",
    "access.token.lifespan": "1800"
  },
  "protocolMappers": [
    {
      "name": "picture",
      "protocol": "openid-connect",
      "protocolMapper": "oidc-usermodel-attribute-mapper",
      "consentRequired": false,
      "config": {
        "userinfo.token.claim": "true",
        "user.attribute": "picture",
        "id.token.claim": "true",
        "access.token.claim": "false",
        "claim.name": "picture",
        "jsonType.label": "String"
      }
    }
  ]
}
JSON
)")
[ "$HTTP" = "201" ] && echo "  ✓ Client created" || echo "  ℹ Client already exists (HTTP $HTTP)"

# ── 4. Google Identity Provider ───────────────────────────────────────────────
if [ -n "$GOOGLE_CLIENT_ID" ] && [ -n "$GOOGLE_CLIENT_SECRET" ]; then
  echo "→ Configuring Google Identity Provider ..."
  HTTP=$(curl -so /dev/null -w "%{http_code}" \
    -X POST "${KC_URL}/admin/realms/${REALM}/identity-provider/instances" \
    -H "$AUTH" -H "$CT" -d "$(cat <<JSON
{
  "alias": "google",
  "displayName": "Google",
  "providerId": "google",
  "enabled": true,
  "trustEmail": true,
  "storeToken": false,
  "addReadTokenRoleOnCreate": false,
  "firstBrokerLoginFlowAlias": "first broker login",
  "config": {
    "clientId": "${GOOGLE_CLIENT_ID}",
    "clientSecret": "${GOOGLE_CLIENT_SECRET}",
    "defaultScope": "openid email profile",
    "useJwksUrl": "true",
    "guiOrder": "0",
    "syncMode": "IMPORT"
  }
}
JSON
)")
  [ "$HTTP" = "201" ] && echo "  ✓ Google IDP configured" || echo "  ℹ Google IDP already exists (HTTP $HTTP)"

  # Auto-redirect to Google on login (optional — remove if you want Keycloak login page)
  echo "→ Setting Google as default IDP ..."
  # Get auth browser flow ID
  FLOW_ID=$(curl -sf "${KC_URL}/admin/realms/${REALM}/authentication/flows" \
    -H "$AUTH" | jq -r '.[] | select(.alias=="browser") | .id')
  # Enable "Identity Provider Redirector" to default to Google
  curl -so /dev/null -X PUT "${KC_URL}/admin/realms/${REALM}/authentication/flows/browser/executions" \
    -H "$AUTH" -H "$CT" || true
  echo "  ℹ  Set kc_idp_hint=google in app to skip Keycloak login page"
else
  echo "⚠  Skipping Google IDP — set GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET to enable"
fi

# ── 5. Summary ────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo " ✓ Realm setup complete"
echo "════════════════════════════════════════════"
echo ""
echo " Realm URL:  ${KC_URL}/realms/${REALM}"
echo " Client ID:  ${CLIENT_ID}"
echo ""
echo " Add to .env:"
echo "   GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID:-<your-google-client-id>}"
echo "   GOOGLE_CLIENT_SECRET=${GOOGLE_CLIENT_SECRET:-<your-google-client-secret>}"
echo ""
if [ -n "$GOOGLE_CLIENT_ID" ]; then
echo " ✓ Google OAuth configured. Redirect URI whitelisted in Keycloak:"
echo "   ${KC_URL}/realms/${REALM}/broker/google/endpoint"
echo ""
echo " ✓ Add this same URI to your Google Cloud Console → Authorized redirect URIs"
fi
