#!/usr/bin/env bash
#
# RailPulse Cloud — package and publish the Streamlit dashboard to App Service.
#
# Publishes through OneDeploy with an Entra bearer token, the same mechanism
# azure/deploy.sh uses for the Function App and for the same reason: it needs no
# basic-auth publishing credentials, so App Service's modern default (scm and ftp
# basic auth both disabled) can stay as it is. There is no publishing password
# anywhere in this project.
#
# `SCM_DO_BUILD_DURING_DEPLOYMENT=true` (set by provision_webapp.sh) makes the
# platform run `pip install -r requirements.txt` on the server, so pymssql's
# Linux wheel is resolved there and nothing has to be cross-compiled locally.
#
# WHAT GOES IN THE PACKAGE
# The contents of webapp/ and nothing else — app.py, data.py, queries.py,
# requirements.txt, startup.sh. No SQL files: unlike the Function App, this app
# ships no DDL. It reads the views, which already exist in the database.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRET_FILE="${SECRET_FILE:-$REPO_ROOT/.azure-railpulse.env}"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
fail() { printf '\033[1;31m    FAILED: %s\033[0m\n' "$*"; exit 1; }

if [[ -z "${WEBAPP:-}" || -z "${RESOURCE_GROUP:-}" ]]; then
  [[ -f "$SECRET_FILE" ]] || fail "no $SECRET_FILE; run ./azure/provision_webapp.sh"
  WEBAPP="$(grep -E '^WEBAPP=' "$SECRET_FILE" | cut -d= -f2-)"
  RESOURCE_GROUP="$(grep -E '^RESOURCE_GROUP=' "$SECRET_FILE" | cut -d= -f2-)"
fi
[[ -n "$WEBAPP" ]] || fail "WEBAPP is not set; run ./azure/provision_webapp.sh"

say "Deploying to $WEBAPP (resource group $RESOURCE_GROUP)"

for attempt in $(seq 1 20); do
  az webapp show -n "$WEBAPP" -g "$RESOURCE_GROUP" --output none 2>/dev/null && break
  [[ $attempt -eq 20 ]] && fail "$WEBAPP is not visible in $RESOURCE_GROUP"
  sleep 6
done

# --------------------------------------------------------------------------
# The site has to be RUNNING before anything can be published: Kudu lives on the
# same site, so a stopped or quota-exceeded app answers 403 to the deployment
# API too. Both states are reported clearly rather than as a mystery 403 later.
# --------------------------------------------------------------------------
STATE="$(az webapp show -n "$WEBAPP" -g "$RESOURCE_GROUP" --query state -o tsv)"
if [[ "$STATE" == "QuotaExceeded" ]]; then
  cat <<EOF

    The F1 Free plan has exhausted its 60-CPU-minutes-per-day quota, so the
    site answers 403 and nothing can be published to it. \`az webapp start\`
    cannot clear this — the quota resets at UTC midnight.

    Options:
      * wait for the reset, then re-run this script;
      * or remove the quota:  az appservice plan update -g $RESOURCE_GROUP \\
                                -n <plan> --sku B1     (~\$0.018/hour)
EOF
  fail "state=QuotaExceeded"
fi
if [[ "$STATE" != "Running" ]]; then
  info "site is $STATE — starting it so the deployment API is reachable"
  az webapp start -n "$WEBAPP" -g "$RESOURCE_GROUP" --output none
  for attempt in $(seq 1 20); do
    STATE="$(az webapp show -n "$WEBAPP" -g "$RESOURCE_GROUP" --query state -o tsv)"
    [[ "$STATE" == "Running" ]] && { info "running"; break; }
    [[ $attempt -eq 20 ]] && fail "site did not reach Running (state=$STATE)"
    sleep 6
  done
fi

APP_HOSTNAME="$(az webapp show -n "$WEBAPP" -g "$RESOURCE_GROUP" \
                --query defaultHostName -o tsv)"
SCM_HOSTNAME="${APP_HOSTNAME/./.scm.}"
info "app: https://$APP_HOSTNAME"

# --------------------------------------------------------------------------
STAGING="$(mktemp -d)"
# shellcheck disable=SC2064
trap "rm -rf '$STAGING'" EXIT

say "Staging package"
rsync -a \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.streamlit/secrets.toml' \
  "$REPO_ROOT/webapp/" "$STAGING/package/"

for required in app.py data.py queries.py requirements.txt startup.sh; do
  [[ -f "$STAGING/package/$required" ]] || fail "$required missing from the package"
done
info "$(find "$STAGING/package" -type f | wc -l | tr -d ' ') files: $(ls "$STAGING/package" | tr '\n' ' ')"

ZIP="$STAGING/webapp.zip"
( cd "$STAGING/package" && zip -qr "$ZIP" . )
info "package: $(du -h "$ZIP" | cut -f1)"

# --------------------------------------------------------------------------
say "Publishing (remote pip install — this takes 2-5 minutes)"
TOKEN="$(az account get-access-token --resource https://management.core.windows.net/ \
         --query accessToken -o tsv)"
[[ -n "$TOKEN" ]] || fail "could not obtain an access token; run az login"

HTTP_CODE="$(curl -sS -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/zip" \
  --data-binary "@$ZIP" \
  -o "$STAGING/publish.out" -w '%{http_code}' --max-time 900 \
  "https://$SCM_HOSTNAME/api/publish?type=zip&Deployer=railpulse-deploy-webapp")"

if [[ "$HTTP_CODE" != "200" && "$HTTP_CODE" != "202" ]]; then
  info "response: $(head -c 400 "$STAGING/publish.out")"
  fail "publish returned HTTP $HTTP_CODE"
fi
info "accepted (HTTP $HTTP_CODE)"

# --------------------------------------------------------------------------
say "Waiting for the build"
for attempt in $(seq 1 40); do
  curl -sS -H "Authorization: Bearer $TOKEN" --max-time 60 \
    "https://$SCM_HOSTNAME/api/deployments/latest" -o "$STAGING/dep.json" 2>/dev/null || true
  STATE="$(python3 -c "
import json
try:
    d = json.load(open('$STAGING/dep.json'))
except Exception:
    print('unknown|False'); raise SystemExit
print('%s|%s' % (d.get('status'), d.get('complete')))
" 2>/dev/null || echo 'unknown|False')"
  STATUS="${STATE%%|*}"; COMPLETE="${STATE##*|}"
  info "status=$STATUS complete=$COMPLETE"
  if [[ "$COMPLETE" == "True" ]]; then
    [[ "$STATUS" == "3" ]] && fail "the remote build failed — see https://$SCM_HOSTNAME/api/deployments/latest"
    info "build finished"
    break
  fi
  [[ $attempt -eq 40 ]] && fail "the build did not finish within 10 minutes"
  sleep 15
done

# --------------------------------------------------------------------------
# Only NOW is it safe to point the startup command at startup.sh: the file is on
# disk, so the container cannot exit 127 in a loop and burn the Free plan's daily
# CPU quota. provision_webapp.sh deliberately leaves a harmless placeholder for
# exactly this window. Setting it also restarts the app, which is what picks up
# the newly published code.
# --------------------------------------------------------------------------
say "Installing the Streamlit startup command"
CURRENT_STARTUP="$(az webapp config show -n "$WEBAPP" -g "$RESOURCE_GROUP" \
                   --query appCommandLine -o tsv 2>/dev/null || true)"
if [[ "$CURRENT_STARTUP" == "bash /home/site/wwwroot/startup.sh" ]]; then
  info "already set — restarting to pick up the new code"
  az webapp restart -n "$WEBAPP" -g "$RESOURCE_GROUP" --output none
else
  az webapp config set -n "$WEBAPP" -g "$RESOURCE_GROUP" \
    --startup-file "bash /home/site/wwwroot/startup.sh" --output none
  info "bash /home/site/wwwroot/startup.sh"
fi

# --------------------------------------------------------------------------
say "Waiting for Streamlit to serve"
# /_stcore/health is Streamlit's own readiness endpoint. On the F1 Free plan the
# first request also pays a cold start, so the budget here is generous.
for attempt in $(seq 1 40); do
  CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 45 \
          "https://$APP_HOSTNAME/_stcore/health" 2>/dev/null || echo 000)"
  if [[ "$CODE" == "200" ]]; then
    info "healthy after ~$((attempt * 10))s"
    break
  fi
  info "attempt $attempt -> HTTP $CODE"
  [[ $attempt -eq 40 ]] && {
    echo
    STATE="$(az webapp show -n "$WEBAPP" -g "$RESOURCE_GROUP" --query state -o tsv 2>/dev/null || echo unknown)"
    echo "    Streamlit never answered. Site state: $STATE"
    if [[ "$STATE" == "QuotaExceeded" ]]; then
      echo "    The F1 Free plan's daily CPU quota is exhausted — most likely a"
      echo "    container restart loop. Read the container log for the exit code,"
      echo "    fix it, and retry after the UTC-midnight reset (or use --sku B1)."
    fi
    echo "    Container log:  az webapp log tail -n $WEBAPP -g $RESOURCE_GROUP"
    echo "    Build log:      https://$SCM_HOSTNAME/api/deployments/latest"
    exit 1
  }
  sleep 10
done

say "Deployed"
cat <<EOF
    Dashboard  https://$APP_HOSTNAME

    On the F1 Free plan the app sleeps when idle, so the first visit after a
    quiet spell takes ~30 s (Streamlit boot, plus a possible serverless
    database resume). Subsequent pages are fast.
EOF
