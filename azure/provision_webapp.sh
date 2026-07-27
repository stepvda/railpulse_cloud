#!/usr/bin/env bash
#
# RailPulse Cloud — provision the App Service that hosts the Streamlit dashboard.
#
# Runs AFTER azure/provision.sh, and reuses everything it created: the same
# resource group, the same region, the same Azure SQL database, and the Function
# App whose ingest endpoint the dashboard's one button calls. Idempotent.
#
# ⚠ COST: the plan is **F1 (Free)** — €0, and the reason this page exists at all
# rather than being a local-only Streamlit app. What Free costs you instead:
#   * 60 CPU-minutes/day quota. Ample for a dashboard nobody is hammering;
#     exceed it and the app returns 403 until the quota rolls over.
#   * No Always On, so the app sleeps when idle and the first request after that
#     pays a ~30 s cold start (Streamlit boot + the first Azure SQL connection,
#     which may itself be resuming a paused database).
#   * 1 GB storage — which is precisely why last week's 980 MB SQLite dashboard
#     is not deployed here. See docs/webapp.md.
# B1 Basic (~$13/month) removes all three. Set SKU=B1 to use it.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRET_FILE="${SECRET_FILE:-$REPO_ROOT/.azure-railpulse.env}"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33m    ! %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m    FAILED: %s\033[0m\n' "$*"; exit 1; }

command -v az >/dev/null || fail "Azure CLI not found: brew install azure-cli"
az account show >/dev/null 2>&1 || fail "not logged in; run: az login"
[[ -f "$SECRET_FILE" ]] || fail "no $SECRET_FILE — run ./azure/provision.sh first"

# The connection string is single-quoted in the env file (it contains spaces,
# braces and semicolons), so it is read with sed rather than cut.
RESOURCE_GROUP="$(grep -E '^RESOURCE_GROUP=' "$SECRET_FILE" | cut -d= -f2-)"
LOCATION="$(grep -E '^LOCATION=' "$SECRET_FILE" | cut -d= -f2-)"
FUNCTION_APP="$(grep -E '^FUNCTION_APP=' "$SECRET_FILE" | cut -d= -f2-)"
SQL_CONNECTION_STRING="$(sed -n "s/^SQL_CONNECTION_STRING='\(.*\)'$/\1/p" "$SECRET_FILE")"
[[ -n "$SQL_CONNECTION_STRING" ]] || fail "could not read SQL_CONNECTION_STRING from $SECRET_FILE"

SUFFIX="${FUNCTION_APP##*-}"
WEBAPP="${WEBAPP:-web-railpulse-$SUFFIX}"
PLAN="${PLAN:-plan-railpulse-$SUFFIX}"
SKU="${SKU:-F1}"
PYTHON_VERSION="${PYTHON_VERSION:-3.11}"

say "Target"
info "resource group: $RESOURCE_GROUP ($LOCATION)"
info "web app:        $WEBAPP"
info "plan:           $PLAN ($SKU)"

# --------------------------------------------------------------------------
say "App Service plan: $PLAN ($SKU, Linux)"
if az appservice plan show -n "$PLAN" -g "$RESOURCE_GROUP" --output none 2>/dev/null; then
  info "already exists"
else
  az appservice plan create \
    --name "$PLAN" --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" --sku "$SKU" --is-linux \
    --output none
  info "created"
fi

# --------------------------------------------------------------------------
say "Web app: $WEBAPP (Python $PYTHON_VERSION)"
if az webapp show -n "$WEBAPP" -g "$RESOURCE_GROUP" --output none 2>/dev/null; then
  info "already exists"
else
  az webapp create \
    --name "$WEBAPP" --resource-group "$RESOURCE_GROUP" --plan "$PLAN" \
    --runtime "PYTHON:$PYTHON_VERSION" \
    --output none
  info "created"
fi

# --------------------------------------------------------------------------
say "Startup command (placeholder until code is deployed)"
# DO NOT point the startup command at /home/site/wwwroot/startup.sh here. On a
# freshly created app that file does not exist yet, so the container exits 127
# ("command not found") in about 16 seconds, App Service reverts by STOPPING the
# whole site — which takes Kudu down with it, so you cannot deploy into it — and
# every retry burns CPU against the F1 Free plan's 60-minutes-per-day quota.
# Exhaust that and the site returns 403 with state=QuotaExceeded until UTC
# midnight, `az webapp start` cannot clear it, and the web app is unfixable for
# the rest of the day. That is exactly how this project lost an afternoon; see
# docs/deployment_notes.md §10.
#
# So: start with something that cannot fail and exposes nothing (one line of
# text from an empty temp directory, not the source tree), and let
# azure/deploy_webapp.sh switch to the real command once app.py and startup.sh
# are actually on disk.
az webapp config set -n "$WEBAPP" -g "$RESOURCE_GROUP" \
  --startup-file 'sh -c "mkdir -p /tmp/ph && cd /tmp/ph && echo not-deployed-yet > index.html && exec python3 -m http.server 8000"' \
  --output none
info "placeholder set — deploy_webapp.sh installs the real Streamlit command"

# --------------------------------------------------------------------------
say "Application settings"
# The function key is fetched now rather than stored anywhere: it is what lets
# the dashboard's one button call the ingest endpoint, and it lives only in the
# app settings, server side. It never reaches the browser.
FUNCTION_KEY="$(az functionapp keys list -n "$FUNCTION_APP" -g "$RESOURCE_GROUP" \
                --query 'functionKeys.default' -o tsv 2>/dev/null || true)"
if [[ -z "$FUNCTION_KEY" || "$FUNCTION_KEY" == "null" ]]; then
  warn "could not read the Function App key — the 'run ingest' button will be"
  warn "disabled and the page will say so. Everything else works."
  FUNCTION_KEY=""
fi

FUNCTION_APP_HOSTNAME="$(az webapp show -n "$FUNCTION_APP" -g "$RESOURCE_GROUP" \
                         --query defaultHostName -o tsv 2>/dev/null || true)"
[[ -n "$FUNCTION_APP_HOSTNAME" ]] || FUNCTION_APP_HOSTNAME="$FUNCTION_APP.azurewebsites.net"

az webapp config appsettings set -n "$WEBAPP" -g "$RESOURCE_GROUP" --settings \
  "SQL_CONNECTION_STRING=$SQL_CONNECTION_STRING" \
  "FUNCTION_APP_URL=https://$FUNCTION_APP_HOSTNAME" \
  "FUNCTION_KEY=$FUNCTION_KEY" \
  "SQL_LOGIN_TIMEOUT=90" \
  "SQL_MAX_ATTEMPTS=4" \
  "SCM_DO_BUILD_DURING_DEPLOYMENT=true" \
  "ENABLE_ORYX_BUILD=true" \
  "WEBSITE_TIME_ZONE=Europe/Brussels" \
  --output none
info "SQL_CONNECTION_STRING set (value not printed)"
info "FUNCTION_APP_URL=https://$FUNCTION_APP_HOSTNAME"
info "FUNCTION_KEY $([[ -n "$FUNCTION_KEY" ]] && echo 'set (value not printed)' || echo 'NOT set')"

# --------------------------------------------------------------------------
say "Transport security"
az webapp update -n "$WEBAPP" -g "$RESOURCE_GROUP" --set httpsOnly=true --output none
info "httpsOnly enabled"
az webapp config set -n "$WEBAPP" -g "$RESOURCE_GROUP" \
  --min-tls-version 1.2 --output none 2>/dev/null || true
info "minimum TLS 1.2"

# --------------------------------------------------------------------------
# No SQL firewall rule is needed. The database already allows Azure services
# (the 0.0.0.0 sentinel rule from provision.sh), and an App Service is one — so
# the dashboard reaches SQL without opening anything further to the internet.
say "Database access"
info "covered by the existing AllowAzureServices firewall rule — nothing to add"

# --------------------------------------------------------------------------
say "Recording the web app in $SECRET_FILE"
if grep -qE '^WEBAPP=' "$SECRET_FILE"; then
  # Portable in-place edit: BSD sed (macOS) needs the empty -i argument.
  sed -i '' "s|^WEBAPP=.*|WEBAPP=$WEBAPP|" "$SECRET_FILE" 2>/dev/null \
    || sed -i "s|^WEBAPP=.*|WEBAPP=$WEBAPP|" "$SECRET_FILE"
else
  printf 'WEBAPP=%s\nWEBAPP_PLAN=%s\n' "$WEBAPP" "$PLAN" >> "$SECRET_FILE"
fi
info "WEBAPP=$WEBAPP"

WEBAPP_HOSTNAME="$(az webapp show -n "$WEBAPP" -g "$RESOURCE_GROUP" \
                   --query defaultHostName -o tsv)"

say "Provisioned"
cat <<EOF
    Web app    https://$WEBAPP_HOSTNAME
    Plan       $PLAN ($SKU, Linux)   $([[ "$SKU" == "F1" ]] && echo '— free, sleeps when idle' || echo '')
    Reads      $(sed -n 's/.*Database=\([^;]*\).*/\1/p' <<<"$SQL_CONNECTION_STRING") via pymssql, read-only

Next:
    ./azure/deploy_webapp.sh
EOF
