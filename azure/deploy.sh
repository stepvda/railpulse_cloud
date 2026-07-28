#!/usr/bin/env bash
#
# RailPulse Cloud — package and publish the Function App (Flex Consumption).
#
# WHY THIS CALLS THE DEPLOYMENT API DIRECTLY INSTEAD OF USING THE CLI
# The obvious command is `az functionapp deploy --src-path pkg.zip --type zip`.
# Against a Flex Consumption app it fails with **HTTP 415 Unsupported Media
# Type** — with `--type zip`, without it, and with the file named `.zip`. The
# CLI sends a content type the OneDeploy endpoint refuses. (`az functionapp
# deployment source config-zip`, the older command, is a Kudu zipdeploy path
# that Flex does not implement at all.)
#
# The endpoint itself is fine. POSTing the same zip to
#   https://<app>.scm.azurewebsites.net/api/publish?RemoteBuild=true
# with `Content-Type: application/zip` and an Entra bearer token returns 202 and
# deploys correctly. So that is what this does: one curl, no extra toolchain, and
# no dependency on the CLI bug being fixed.
#
# A bearer token, not basic auth, is used on purpose — it means Flex apps need no
# SCM basic-auth publishing credentials enabled at all, which is one fewer
# security default to relax than the old Linux Consumption path required.
#
# `RemoteBuild=true` makes the platform run `pip install -r requirements.txt`, so
# pyodbc's Linux wheels are resolved there and nothing has to be
# cross-compiled locally.
#
# WHAT GOES IN THE PACKAGE
# The contents of function_app/, plus a copy of sql/. The SQL files have to
# travel with the code so that POST /api/migrate can read them, but the canonical
# copy stays at the repository root where it belongs — copying at package time
# keeps ONE source of truth instead of two directories that drift.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRET_FILE="${SECRET_FILE:-$REPO_ROOT/.azure-railpulse.env}"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
fail() { printf '\033[1;31m    FAILED: %s\033[0m\n' "$*"; exit 1; }

# --------------------------------------------------------------------------
# Where to deploy. Read from the file provision.sh wrote, unless overridden.
# --------------------------------------------------------------------------
if [[ -z "${FUNCTION_APP:-}" || -z "${RESOURCE_GROUP:-}" ]]; then
  [[ -f "$SECRET_FILE" ]] || fail "no $SECRET_FILE and no FUNCTION_APP/RESOURCE_GROUP set; run ./azure/provision.sh"
  FUNCTION_APP="$(grep -E '^FUNCTION_APP=' "$SECRET_FILE" | cut -d= -f2-)"
  RESOURCE_GROUP="$(grep -E '^RESOURCE_GROUP=' "$SECRET_FILE" | cut -d= -f2-)"
fi

say "Deploying to $FUNCTION_APP (resource group $RESOURCE_GROUP)"

# ARM is eventually consistent, and "eventually" is long enough to matter when
# deploy.sh runs straight after provision.sh: the app is created and Running, but
# the deployment call still answers ResourceNotFound for a minute or so. Waiting
# for a successful read turns that into a pause instead of a failure someone has
# to interpret.
for attempt in $(seq 1 20); do
  az functionapp show -n "$FUNCTION_APP" -g "$RESOURCE_GROUP" --output none 2>/dev/null && break
  [[ $attempt -eq 20 ]] && fail "$FUNCTION_APP is not visible in $RESOURCE_GROUP after 2 minutes"
  sleep 6
done

# `az functionapp show` returns an EMPTY defaultHostName for a Flex Consumption
# app (the same is true of state and sku) while `az webapp show` — same ARM
# resource, different command module — returns it. Hence the fallback.
APP_HOSTNAME="$(az functionapp show -n "$FUNCTION_APP" -g "$RESOURCE_GROUP" \
                --query defaultHostName -o tsv 2>/dev/null || true)"
[[ -n "$APP_HOSTNAME" ]] || APP_HOSTNAME="$(az webapp show -n "$FUNCTION_APP" \
                -g "$RESOURCE_GROUP" --query defaultHostName -o tsv 2>/dev/null || true)"
[[ -n "$APP_HOSTNAME" ]] || fail "could not resolve the hostname of $FUNCTION_APP"
SCM_HOSTNAME="${APP_HOSTNAME/./.scm.}"
info "app: https://$APP_HOSTNAME"
info "scm: https://$SCM_HOSTNAME"

# --------------------------------------------------------------------------
# Build the package in a temp directory.
# --------------------------------------------------------------------------
STAGING="$(mktemp -d)"
# shellcheck disable=SC2064
trap "rm -rf '$STAGING'" EXIT

say "Staging package"
# --exclude rather than a .funcignore-aware tool, because plain rsync is doing
# the copying. Keep this list in step with function_app/.funcignore.
rsync -a \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.DS_Store' \
  --exclude '.pytest_cache' \
  --exclude 'local.settings.json' \
  --exclude '.venv' \
  "$REPO_ROOT/function_app/" "$STAGING/package/"

# The SQL files, so the migrate endpoint finds them at /home/site/wwwroot/sql —
# see railpulse/migrations.sql_directory().
rsync -a --exclude 'analysis' "$REPO_ROOT/sql/" "$STAGING/package/sql/"

info "$(find "$STAGING/package" -type f | wc -l | tr -d ' ') files"
info "sql files:   $(ls "$STAGING/package/sql" | tr '\n' ' ')"
[[ -f "$STAGING/package/function_app.py" ]] || fail "function_app.py missing from the package"

ZIP="$STAGING/railpulse.zip"
( cd "$STAGING/package" && zip -qr "$ZIP" . )
info "package: $(du -h "$ZIP" | cut -f1)"

# --------------------------------------------------------------------------
# Publish via OneDeploy.
# --------------------------------------------------------------------------
say "Publishing (remote build — this takes 2-4 minutes)"
# The Kudu/SCM site accepts an ARM access token as a bearer credential, so no
# publishing password is needed or stored.
TOKEN="$(az account get-access-token --resource https://management.core.windows.net/ \
         --query accessToken -o tsv)"
[[ -n "$TOKEN" ]] || fail "could not obtain an access token; run az login"

HTTP_CODE="$(curl -sS -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/zip" \
  --data-binary "@$ZIP" \
  -o "$STAGING/publish.out" -w '%{http_code}' --max-time 900 \
  "https://$SCM_HOSTNAME/api/publish?RemoteBuild=true&Deployer=railpulse-deploy-sh")"

if [[ "$HTTP_CODE" != "202" && "$HTTP_CODE" != "200" ]]; then
  info "response: $(head -c 400 "$STAGING/publish.out")"
  fail "publish returned HTTP $HTTP_CODE"
fi
info "accepted (HTTP $HTTP_CODE)"

# --------------------------------------------------------------------------
# Wait for the remote build. Kudu DeployStatus: 3 = Failed, 4 = Success.
# --------------------------------------------------------------------------
say "Waiting for the remote build"
for attempt in $(seq 1 40); do
  curl -sS -H "Authorization: Bearer $TOKEN" --max-time 60 \
    "https://$SCM_HOSTNAME/api/deployments/latest" -o "$STAGING/dep.json" 2>/dev/null || true
  STATE="$(python3 -c "
import json, sys
try:
    d = json.load(open('$STAGING/dep.json'))
except Exception:
    print('unknown|False|'); raise SystemExit
print('%s|%s|%s' % (d.get('status'), d.get('complete'), str(d.get('log_url') or '')))
" 2>/dev/null || echo 'unknown|False|')"
  STATUS="${STATE%%|*}"; REST="${STATE#*|}"; COMPLETE="${REST%%|*}"; LOG_URL="${REST#*|}"
  info "status=$STATUS complete=$COMPLETE"
  if [[ "$COMPLETE" == "True" ]]; then
    [[ "$STATUS" == "3" ]] && { info "build log: $LOG_URL"; fail "the remote build failed"; }
    info "build finished"
    break
  fi
  [[ $attempt -eq 40 ]] && fail "the build did not finish within 10 minutes"
  sleep 15
done

# --------------------------------------------------------------------------
say "Waiting for the runtime to index the functions"
# The build completing is not the same as the worker having re-read the
# decorators, so polling /api/ping is the only honest readiness signal. It is
# anonymous, which is what makes it usable here.
for attempt in $(seq 1 30); do
  if curl -fsS --max-time 30 "https://$APP_HOSTNAME/api/ping" >/dev/null 2>&1; then
    info "app is answering after ${attempt} attempt(s)"
    break
  fi
  [[ $attempt -eq 30 ]] && fail "/api/ping never answered — check the portal's Log stream"
  sleep 10
done

say "Deployed functions"
az functionapp function list -n "$FUNCTION_APP" -g "$RESOURCE_GROUP" \
  --query "[].{name:name, trigger:config.bindings[0].type, route:config.bindings[0].route}" \
  -o table 2>/dev/null \
  || info "(function list not available yet; it populates a minute after indexing)"

say "Done"
cat <<EOF
    Base URL   https://$APP_HOSTNAME
    Liveness   curl https://$APP_HOSTNAME/api/ping

Next:
    ./azure/smoke_test.sh      # migrate the schema, seed stations, ingest, verify
EOF
