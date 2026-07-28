#!/usr/bin/env bash
#
# RailPulse Cloud — delete everything, or just pause the expensive part.
#
# This exists because the brief asks for it: "Place ALL resources for this
# challenge into a single, dedicated Resource Group so you can delete/pause
# everything easily on Friday." One group means one delete.
#
#   ./azure/teardown.sh pause     stop the compute, keep the data (default)
#   ./azure/teardown.sh delete    remove the resource group entirely
#
# PAUSE is almost always what you want. It takes the database to zero compute
# cost (storage only, roughly $0.25/month for 2 GB) and stops the timer trigger,
# while keeping every row collected so far — which matters when next week's
# dashboard is supposed to read this data. DELETE is irreversible: the database,
# its backups and the whole collected history go with it.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRET_FILE="${SECRET_FILE:-$REPO_ROOT/.azure-railpulse.env}"
MODE="${1:-pause}"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33m    ! %s\033[0m\n' "$*"; }

if [[ -z "${RESOURCE_GROUP:-}" ]]; then
  [[ -f "$SECRET_FILE" ]] || { echo "no $SECRET_FILE; set RESOURCE_GROUP"; exit 1; }
  RESOURCE_GROUP="$(grep -E '^RESOURCE_GROUP=' "$SECRET_FILE" | cut -d= -f2-)"
  FUNCTION_APP="$(grep -E '^FUNCTION_APP=' "$SECRET_FILE" | cut -d= -f2-)"
  SQL_SERVER="$(grep -E '^SQL_SERVER=' "$SECRET_FILE" | cut -d= -f2-)"
  SQL_DATABASE="$(grep -E '^SQL_DATABASE=' "$SECRET_FILE" | cut -d= -f2-)"
  # Absent until provision_webapp.sh has run; every use below tolerates that.
  WEBAPP="$(grep -E '^WEBAPP=' "$SECRET_FILE" | cut -d= -f2-)"
fi

case "$MODE" in
  pause)
    say "Pausing compute in $RESOURCE_GROUP (data is kept)"

    # Stopping the Function App stops the timer trigger. Without this the timer
    # keeps firing, and every firing resumes the database — so pausing the
    # database alone would achieve nothing.
    info "stopping the Function App (this is what stops the timer)"
    az functionapp stop --name "$FUNCTION_APP" --resource-group "$RESOURCE_GROUP" \
      --output none
    info "stopped: $FUNCTION_APP"

    # The dashboard costs nothing on the F1 Free plan, but a browser tab left
    # open on it would keep querying — and every query resumes the database,
    # which is the part that does cost. Stopping it closes that path.
    if [[ -n "${WEBAPP:-}" ]]; then
      if az webapp stop --name "$WEBAPP" --resource-group "$RESOURCE_GROUP" \
           --output none 2>/dev/null; then
        info "stopped: $WEBAPP (the dashboard)"
      else
        warn "could not stop $WEBAPP — check it by hand if a tab is left open on it"
      fi
    fi

    # DO NOT issue `az sql db update` here. It looks like a harmless no-op and
    # is not: a control-plane update requires the database to be online, so it
    # RESUMES a serverless database that had already auto-paused — this command
    # used to cause the exact cost it exists to avoid. Observed directly: status
    # went Paused -> Online purely because `teardown.sh pause` was run.
    #
    # There is also nothing to issue. Azure SQL serverless has no manual pause;
    # it pauses itself after the configured idle delay (60 minutes here). The
    # only thing that matters is that nothing queries it, which is what stopping
    # the Function App and the dashboard above achieves.
    STATE="$(az sql db show -n "$SQL_DATABASE" -s "$SQL_SERVER" -g "$RESOURCE_GROUP" \
             --query status -o tsv 2>/dev/null || echo unknown)"
    info "database status: $STATE"
    if [[ "$STATE" == "Online" ]]; then
      info "it will auto-pause ~60 min after the last query; nothing queries it now"
    fi

    cat <<EOF

    Paused. Ongoing cost: storage only (~2 GB, well under a euro a month).
    The web dashboard's F1 plan is free either way.
    Resume everything with:
        make resume
    The database resumes by itself on the first query.
EOF
    ;;

  delete)
    say "DELETING the resource group $RESOURCE_GROUP"
    cat <<EOF
    This removes, irreversibly:
      - the Azure SQL database AND every liveboard record collected so far
      - its automatic backups
      - the Function App, its keys and its logs
      - the web dashboard and its App Service plan
      - the storage account

    If next week's dashboard is meant to read this data, use 'pause' instead.
EOF
    read -r -p "    Type the resource group name to confirm: " CONFIRM
    [[ "$CONFIRM" == "$RESOURCE_GROUP" ]] || { echo "    Aborted."; exit 1; }

    az group delete --name "$RESOURCE_GROUP" --yes --no-wait
    info "deletion started (runs in the background; --no-wait)"
    info "check with: az group show -n $RESOURCE_GROUP"
    ;;

  *)
    echo "usage: $0 [pause|delete]"
    exit 1
    ;;
esac
