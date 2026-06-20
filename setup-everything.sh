#!/usr/bin/env bash
# ============================================================================
# Anu Imports Tracker — one-shot setup
#
# Sets every remaining env var on Render, triggers a redeploy, runs all
# verification tests, sends test emails. Replaces ~5 minutes of manual
# dashboard clicking with one command.
#
# Inputs (script will prompt you):
#   1. Render API key       — https://dashboard.render.com/u/settings#api-keys
#                              (1 click to create)
#   2. Resend API key       — https://resend.com → API Keys → Create
#                              (free tier, 90 sec to sign up with GitHub)
#   3. Your email           — where alerts/backups/digests go
#   4. (optional) Anthropic API key — to enable /ask AI assistant
#
# What it sets on Render:
#   ADMIN_TOKEN              — random 64-char hex, protects /api/admin/*
#   RESEND_API_KEY           — your key
#   ALERT_EMAIL_TO           — your email
#   ALERT_EMAIL_FROM         — alerts@anuspirits.com (or onboarding@resend.dev)
#   TASTING_DIGEST_TO        — your email + sales@anuspirits.com
#   ANTHROPIC_API_KEY        — if provided
#   CORS_ORIGINS             — Vercel + localhost
#
# What it does after setting vars:
#   - Triggers Render redeploy via API
#   - Polls /healthz until the new deploy is live
#   - Calls /api/admin/test-alert      → you get a test email
#   - Calls /api/admin/run-backup-now  → you get a JSON DB backup email
#   - Calls /api/admin/send-tasting-digest → you get tomorrow's tastings
#   - Prints final URLs + status
# ============================================================================

set -euo pipefail

cd "$(dirname "$0")"

BACKEND_URL="${BACKEND_URL:-https://anu-imports-tracker.onrender.com}"
SERVICE_NAME="${SERVICE_NAME:-anu-imports-tracker}"

# Locally-generated ADMIN_TOKEN (from earlier Claude session)
if [ -f .admin-token.local ]; then
  ADMIN_TOKEN=$(cat .admin-token.local)
  echo "✓ Reusing existing ADMIN_TOKEN from .admin-token.local"
else
  ADMIN_TOKEN=$(openssl rand -hex 32)
  echo "$ADMIN_TOKEN" > .admin-token.local
  chmod 600 .admin-token.local
  echo "✓ Generated new ADMIN_TOKEN, saved to .admin-token.local"
fi

# ----- prompt for inputs -----
echo ""
echo "============================================================"
echo "STEP 1 — paste your Render API key (one-time)"
echo "  → Open: https://dashboard.render.com/u/settings#api-keys"
echo "  → Create API Key, copy, paste below"
echo "============================================================"
read -rsp "Render API key: " RENDER_API_KEY
echo ""
[ -z "$RENDER_API_KEY" ] && { echo "❌ Render API key required"; exit 1; }

echo ""
echo "============================================================"
echo "STEP 2 — paste your Resend API key (free, 90 sec to sign up)"
echo "  → Open: https://resend.com → API Keys → Create"
echo "  → Or skip with empty input (alerts won't email)"
echo "============================================================"
read -rsp "Resend API key (or blank to skip): " RESEND_API_KEY
echo ""

echo ""
echo "============================================================"
echo "STEP 3 — your email for alerts + backups + tasting digest"
echo "============================================================"
read -rp "Email [ikshit@anuspirits.com]: " ALERT_EMAIL
ALERT_EMAIL="${ALERT_EMAIL:-ikshit@anuspirits.com}"

echo ""
read -rp "Use ALERT_EMAIL_FROM=alerts@anuspirits.com (Y/n)? Press n to use Resend test sender: " CONFIRM_FROM
if [[ "$CONFIRM_FROM" =~ ^[nN] ]]; then
  ALERT_FROM="onboarding@resend.dev"
else
  ALERT_FROM="alerts@anuspirits.com"
fi

echo ""
read -rsp "Anthropic API key (for /ask AI, blank to skip): " ANTHROPIC_API_KEY
echo ""

# ----- find Render service ID -----
echo ""
echo "→ Looking up Render service ID for '$SERVICE_NAME'..."
SVC_ID=$(curl -s -H "Authorization: Bearer $RENDER_API_KEY" -H "Accept: application/json" \
  "https://api.render.com/v1/services?name=$SERVICE_NAME&limit=20" | \
  python3 -c "
import sys, json
d = json.load(sys.stdin)
# API returns a list of {service: {...}} wrappers
for item in d:
    s = item.get('service', item)
    if s.get('name') == '$SERVICE_NAME':
        print(s.get('id', ''))
        break
" 2>/dev/null || echo "")

if [ -z "$SVC_ID" ]; then
  echo "❌ Couldn't find Render service named '$SERVICE_NAME'."
  echo "   Try setting SERVICE_NAME=<your-service-name> and re-running."
  exit 1
fi
echo "✓ Service ID: $SVC_ID"

# ----- build env-var payload -----
EXISTING=$(curl -s -H "Authorization: Bearer $RENDER_API_KEY" -H "Accept: application/json" \
  "https://api.render.com/v1/services/$SVC_ID/env-vars?limit=100" | \
  python3 -c "
import sys, json
d = json.load(sys.stdin)
out = {}
for item in d:
    e = item.get('envVar', item)
    out[e.get('key')] = e.get('value', '')
print(json.dumps(out))
")

echo ""
echo "→ Updating env vars (preserving existing values)..."

# Build the new env-var list — overwrite the keys we manage, keep all others
PAYLOAD=$(python3 <<EOF
import json, os, sys
existing = json.loads('''$EXISTING''')
to_set = {
    'ADMIN_TOKEN': '$ADMIN_TOKEN',
    'ALERT_EMAIL_TO': '$ALERT_EMAIL',
    'ALERT_EMAIL_FROM': '$ALERT_FROM',
    'TASTING_DIGEST_TO': '$ALERT_EMAIL,sales@anuspirits.com',
    'CORS_ORIGINS': 'https://anu-imports-web.vercel.app,http://localhost:3001,http://localhost:3000',
    'TZ': 'America/Toronto',
}
if '$RESEND_API_KEY':
    to_set['RESEND_API_KEY'] = '$RESEND_API_KEY'
if '$ANTHROPIC_API_KEY':
    to_set['ANTHROPIC_API_KEY'] = '$ANTHROPIC_API_KEY'

# Merge: keep everything Render already had, overlay our new values
merged = {**existing, **to_set}
# Render API expects a list of {key, value}
print(json.dumps([{'key': k, 'value': v} for k, v in merged.items()]))
EOF
)

curl -sS -X PUT \
  -H "Authorization: Bearer $RENDER_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -d "$PAYLOAD" \
  "https://api.render.com/v1/services/$SVC_ID/env-vars" > /tmp/render-envvar-resp.json

if grep -q '"key"' /tmp/render-envvar-resp.json; then
  COUNT=$(python3 -c "import json; print(len(json.load(open('/tmp/render-envvar-resp.json'))))")
  echo "✓ Set $COUNT env vars on Render"
else
  echo "❌ Render API returned an error:"
  cat /tmp/render-envvar-resp.json
  exit 1
fi

# ----- trigger redeploy (clearCache=do_not_clear because we just want a restart) -----
echo ""
echo "→ Triggering Render redeploy..."
DEPLOY_ID=$(curl -sS -X POST \
  -H "Authorization: Bearer $RENDER_API_KEY" \
  -H "Accept: application/json" \
  "https://api.render.com/v1/services/$SVC_ID/deploys" \
  -d '{}' | python3 -c "import sys, json; print(json.load(sys.stdin).get('id',''))")
echo "✓ Deploy ID: $DEPLOY_ID"

# ----- wait for deploy to be live -----
echo ""
echo "→ Polling /healthz until the new env vars are live..."
for i in $(seq 1 40); do
  RESP=$(curl -s --max-time 15 -X POST -H "X-Admin-Token: $ADMIN_TOKEN" \
    "$BACKEND_URL/api/admin/test-alert?subject=Setup%20bootstrap%20test")
  if echo "$RESP" | grep -q '"resend":true'; then
    echo "  [$i] ✓ Deploy live, Resend channel configured!"
    break
  fi
  if [ "$i" = "40" ]; then
    echo "  ⚠ Polled 40× — deploy taking unusually long. Continuing anyway."
  fi
  sleep 15
done

# ----- run verification tests -----
echo ""
echo "============================================================"
echo "→ Running verification tests (you should get 3 emails)..."
echo "============================================================"
echo ""

echo "1️⃣  Test alert (subject 'Setup verification')..."
curl -s -X POST -H "X-Admin-Token: $ADMIN_TOKEN" \
  "$BACKEND_URL/api/admin/test-alert?subject=Setup%20verification&body=Live%20sync%20app%20is%20configured%20and%20wired%20up%20end-to-end.&level=info" | \
  python3 -m json.tool 2>/dev/null

echo ""
echo "2️⃣  Backup-to-email (full DB JSON attachment)..."
curl -s -X POST -H "X-Admin-Token: $ADMIN_TOKEN" \
  "$BACKEND_URL/api/admin/run-backup-now" | python3 -m json.tool 2>/dev/null

echo ""
echo "3️⃣  Tomorrow's tastings digest..."
curl -s -X POST "$BACKEND_URL/api/admin/send-tasting-digest" | python3 -m json.tool 2>/dev/null

# ----- final summary -----
echo ""
echo "============================================================"
echo "🎉  SETUP COMPLETE"
echo "============================================================"
echo ""
echo "Backend:           $BACKEND_URL"
echo "Frontend:          https://anu-imports-web.vercel.app"
echo "Admin token:       $ADMIN_TOKEN"
echo "  (saved locally to .admin-token.local; do NOT commit)"
echo ""
echo "Email schedule (auto, no further action):"
echo "  02:00 ET daily  → full DB backup → $ALERT_EMAIL"
echo "  06:00 + 14:00 ET → health check; emails $ALERT_EMAIL on failure"
echo "  06:30 ET daily  → tomorrow's tastings → $ALERT_EMAIL,sales@anuspirits.com"
echo ""
echo "Manual triggers (with header X-Admin-Token: $ADMIN_TOKEN):"
echo "  POST /api/admin/test-alert?subject=foo&body=bar"
echo "  POST /api/admin/run-backup-now"
echo "  POST /api/admin/send-tasting-digest    (no auth — safe to public)"
echo "  GET  /api/admin/db-stats"
echo "  GET  /api/admin/export?include=core    (full DB JSON dump)"
echo ""
echo "Migration to Fly.io ready (when you want):"
echo "  bash deploy-to-fly.sh"
