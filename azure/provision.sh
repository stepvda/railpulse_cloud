#!/usr/bin/env bash
#
# RailPulse Cloud — provision every Azure resource, with the cost settings the
# brief demands baked in.
#
# THIS IS THE PORTAL WALKTHROUGH, EXECUTED
# The challenge asks for a manual portal deployment, and docs/portal_walkthrough.md
# is that click-by-click guide. This script is the same resources with the same
# settings, expressed as commands — because a screenshot cannot be re-run, and on
# Friday, when everything gets deleted, being able to rebuild the whole stack in
# four minutes is worth more than the memory of having clicked it.
#
# Every step is IDEMPOTENT: run it again and it converges rather than failing or
# duplicating. That property is what makes it safe to re-run after fixing one
# setting.
#
# COST SETTINGS, AND WHY EACH ONE IS HERE
#   * Azure SQL: GP_S_Gen5_1 = General Purpose SERVERLESS, 1 vCore maximum,
#     0.5 vCore minimum. Serverless bills per vCore-second and can pause.
#   * --auto-pause-delay 60: pause after one hour idle (the minimum Azure
#     allows). While paused you pay for storage only — about $0.12/GB/month.
#   * --max-size 2GB: the smallest useful ceiling; storage is billed on what is
#     allocated, not used.
#   * --backup-storage-redundancy Local: LRS. Geo-redundant backup costs
#     roughly triple for a dataset that can be rebuilt from a public API.
#   * Function App on the Y1 Consumption plan: billed per execution, with a free
#     grant of 1M executions and 400,000 GB-s per month. This workload — twenty
#     runs a day of a few seconds — does not approach it.
#   * Storage account Standard_LRS, required by the Function App for its own
#     bookkeeping (timer schedule state, keys, the deployment package).
#
# Usage:
#   az login                        # once, interactively
#   ./azure/provision.sh            # uses the defaults below
#   RESOURCE_GROUP=rg-other ./azure/provision.sh
#
set -euo pipefail

# --------------------------------------------------------------------------
# Configuration. Override any of these from the environment.
# --------------------------------------------------------------------------
# francecentral (Paris) is the closest allowed region to Belgium on an Azure for
# Students subscription. West Europe (Amsterdam) would be the obvious choice and
# is DISALLOWED — see the region-policy step below, which discovers the allowed
# set rather than trusting this default.
LOCATION="${LOCATION:-francecentral}"
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-railpulse-cloud}"

#: Preference order used when the configured LOCATION is blocked by policy.
#: Ordered by distance from Belgium, because every millisecond of latency here
#: is paid on every one of the ~600 round trips a day between the Function App
#: and the database.
PREFERRED_LOCATIONS=(francecentral germanywestcentral westeurope northeurope
                     italynorth spaincentral polandcentral)

# A random suffix keeps the globally-unique names (SQL server, storage account,
# function app) available. Derived from the subscription id so that re-running
# the script reaches the SAME resources instead of creating a second set.
SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
SUFFIX="${SUFFIX:-$(printf '%s' "$SUBSCRIPTION_ID" | shasum | cut -c1-6)}"

SQL_SERVER="${SQL_SERVER:-sql-railpulse-$SUFFIX}"
SQL_DATABASE="${SQL_DATABASE:-railpulse}"
SQL_ADMIN_USER="${SQL_ADMIN_USER:-railpulse_admin}"
STORAGE_ACCOUNT="${STORAGE_ACCOUNT:-strailpulse$SUFFIX}"
FUNCTION_APP="${FUNCTION_APP:-func-railpulse-$SUFFIX}"
PYTHON_VERSION="${PYTHON_VERSION:-3.11}"

# The timer cadence. See docs/cost_control.md for why this is not "every 15
# minutes, all day": that would prevent the database from ever auto-pausing.
INGEST_SCHEDULE="${INGEST_SCHEDULE:-0 */15 6-9,16-19 * * 1-5}"

SECRET_FILE="${SECRET_FILE:-.azure-railpulse.env}"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33m    ! %s\033[0m\n' "$*"; }

# --------------------------------------------------------------------------
# 0. Preconditions
# --------------------------------------------------------------------------
say "Checking prerequisites"
command -v az >/dev/null || { echo "Azure CLI not found: brew install azure-cli"; exit 1; }
az account show >/dev/null 2>&1 || { echo "Not logged in. Run: az login"; exit 1; }

ACCOUNT_NAME="$(az account show --query user.name -o tsv)"
SUBSCRIPTION_NAME="$(az account show --query name -o tsv)"
info "account:      $ACCOUNT_NAME"
info "subscription: $SUBSCRIPTION_NAME"
info "suffix:       $SUFFIX  (derived from the subscription id)"

# The Azure for Students offer is what makes this free. A Free Trial or
# Pay-As-You-Go subscription will still work, but it will bill a real card, so
# it is worth saying out loud rather than discovering later.
#
# The subscription NAME is checked as well as the quota id, because
# `az account show` does not always return subscriptionPolicies (it is absent on
# a freshly activated subscription) — and warning "this is not a student
# subscription" at someone who has just activated one is worse than not checking.
QUOTA_ID="$(az account show --query 'subscriptionPolicies.quotaId' -o tsv 2>/dev/null || true)"
case "$QUOTA_ID$SUBSCRIPTION_NAME" in
  *Student*|*Sponsored*)
     info "offer:        ${QUOTA_ID:-$SUBSCRIPTION_NAME} (student/sponsored credit)";;
  *) warn "this does not look like an Azure for Students subscription"
     warn "  name: '$SUBSCRIPTION_NAME'  quotaId: '${QUOTA_ID:-unavailable}'"
     warn "Activate Azure for Students at https://azure.microsoft.com/free/students"
     warn "or press Ctrl-C now if this subscription bills a real payment method."
     sleep 5;;
esac

# --------------------------------------------------------------------------
# 0b. Resource providers.
# --------------------------------------------------------------------------
# A brand-new subscription has NOTHING registered, and the failure is neither
# obvious nor early: `az group create` succeeds, then `az sql server create`
# fails with "MissingSubscriptionRegistration". Registering up front turns a
# confusing mid-script error into a 90-second wait. Idempotent — re-registering
# an already-registered provider is a no-op.
say "Resource providers"
REQUIRED_PROVIDERS=(Microsoft.Sql Microsoft.Web Microsoft.Storage
                    Microsoft.Insights Microsoft.OperationalInsights)
NEEDED=()
for ns in "${REQUIRED_PROVIDERS[@]}"; do
  state="$(az provider show -n "$ns" --query registrationState -o tsv 2>/dev/null || echo Unknown)"
  if [[ "$state" == "Registered" ]]; then
    info "$ns already registered"
  else
    info "$ns is $state — registering"
    az provider register --namespace "$ns" --output none
    NEEDED+=("$ns")
  fi
done

if (( ${#NEEDED[@]} )); then
  info "waiting for ${#NEEDED[@]} provider(s) to finish registering"
  for attempt in $(seq 1 40); do
    pending=()
    for ns in "${NEEDED[@]}"; do
      state="$(az provider show -n "$ns" --query registrationState -o tsv 2>/dev/null || echo Unknown)"
      [[ "$state" == "Registered" ]] || pending+=("$ns")
    done
    (( ${#pending[@]} )) || { info "all registered"; break; }
    [[ $attempt -eq 40 ]] && { warn "still unregistered: ${pending[*]}"
                               warn "provisioning will likely fail; check the portal"; }
    sleep 10
  done
fi

# --------------------------------------------------------------------------
# 0c. Region policy.
# --------------------------------------------------------------------------
# Azure applies an "Allowed resource deployment regions" policy to Free/Student
# subscriptions, restricting them to a handful of regions it picks. West Europe
# — the obvious choice for Belgian data — is NOT in the set for this
# subscription, and the failure is late and opaque: the resource group is
# created happily, then `az sql server create` dies with
# RequestDisallowedByAzure and a message about "best available regions" that
# never names the regions.
#
# So the allowed list is read from the policy itself. Discovered rather than
# hard-coded because the set varies between subscriptions, and a teammate
# running this script should not have to debug someone else's default.
say "Region policy"
ALLOWED_LOCATIONS="$(az policy assignment list --disable-scope-strict-match -o json 2>/dev/null \
  | python3 -c '
import json, sys
try:
    assignments = json.load(sys.stdin)
except Exception:
    raise SystemExit
for assignment in assignments:
    for name, spec in (assignment.get("parameters") or {}).items():
        if name == "listOfAllowedLocations":
            value = spec.get("value") if isinstance(spec, dict) else spec
            if isinstance(value, list):
                print(" ".join(value))
                raise SystemExit
' || true)"

if [[ -z "$ALLOWED_LOCATIONS" ]]; then
  info "no region policy found (or not readable) — using $LOCATION as configured"
else
  info "policy allows: $ALLOWED_LOCATIONS"
  if [[ " $ALLOWED_LOCATIONS " == *" $LOCATION "* ]]; then
    info "$LOCATION is allowed"
  else
    warn "$LOCATION is BLOCKED by the subscription's region policy"
    CHOSEN=""
    for candidate in "${PREFERRED_LOCATIONS[@]}"; do
      if [[ " $ALLOWED_LOCATIONS " == *" $candidate "* ]]; then CHOSEN="$candidate"; break; fi
    done
    # Fall back to whatever the policy lists first rather than failing: any
    # allowed region beats no deployment.
    [[ -n "$CHOSEN" ]] || CHOSEN="${ALLOWED_LOCATIONS%% *}"
    LOCATION="$CHOSEN"
    warn "falling back to the nearest allowed region: $LOCATION"
  fi
fi

# --------------------------------------------------------------------------
# 1. Resource group — one group, so Friday's cleanup is a single delete.
# --------------------------------------------------------------------------
say "Resource group: $RESOURCE_GROUP ($LOCATION)"
EXISTING_RG_LOCATION="$(az group show -n "$RESOURCE_GROUP" --query location -o tsv 2>/dev/null || true)"
if [[ -n "$EXISTING_RG_LOCATION" && "$EXISTING_RG_LOCATION" != "$LOCATION" ]]; then
  # A resource group's location holds only its metadata, so resources inside it
  # may live elsewhere and this is not fatal. But an empty group left behind in a
  # blocked region is confusing, and `az group create` will not move it.
  if [[ -z "$(az resource list -g "$RESOURCE_GROUP" --query "[0].id" -o tsv 2>/dev/null)" ]]; then
    warn "existing group is in $EXISTING_RG_LOCATION and is empty — recreating in $LOCATION"
    az group delete --name "$RESOURCE_GROUP" --yes --output none
  else
    warn "existing group is in $EXISTING_RG_LOCATION, not $LOCATION, and is NOT empty"
    warn "leaving it as it is; new resources will still be created in $LOCATION"
  fi
fi
az group create \
  --name "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --tags project=railpulse-cloud owner="$ACCOUNT_NAME" cost-control=delete-on-friday \
  --output none
info "created or already present"

# --------------------------------------------------------------------------
# 2. SQL logical server
# --------------------------------------------------------------------------
say "SQL server: $SQL_SERVER"
if az sql server show --name "$SQL_SERVER" --resource-group "$RESOURCE_GROUP" \
     --output none 2>/dev/null; then
  info "already exists — password unchanged"
  SQL_ADMIN_PASSWORD="${SQL_ADMIN_PASSWORD:-}"
  if [[ -z "$SQL_ADMIN_PASSWORD" && -f "$SECRET_FILE" ]]; then
    # The value is written single-quoted (so the file can be `source`d), hence
    # the quote-stripping rather than a bare cut.
    SQL_ADMIN_PASSWORD="$(sed -n "s/^SQL_ADMIN_PASSWORD='\(.*\)'$/\1/p" "$SECRET_FILE")"
    info "password read back from $SECRET_FILE"
  fi
  if [[ -z "$SQL_ADMIN_PASSWORD" ]]; then
    warn "No password available. Reset it with:"
    warn "  az sql server update -g $RESOURCE_GROUP -n $SQL_SERVER --admin-password '<new>'"
    warn "then re-run with SQL_ADMIN_PASSWORD='<new>'"
    exit 1
  fi
else
  # Generated locally and never echoed to the terminal. openssl rather than a
  # memorable phrase: this credential is going into an app setting and a local
  # gitignored file, and is never typed by a human.
  SQL_ADMIN_PASSWORD="${SQL_ADMIN_PASSWORD:-$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-20)Aa9!}"
  az sql server create \
    --name "$SQL_SERVER" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --admin-user "$SQL_ADMIN_USER" \
    --admin-password "$SQL_ADMIN_PASSWORD" \
    --enable-public-network true \
    --minimal-tls-version 1.2 \
    --output none
  info "created with a generated 24-character password"
fi

# --------------------------------------------------------------------------
# 3. Firewall — the two rules the brief asks for, and nothing wider.
# --------------------------------------------------------------------------
say "SQL firewall"
# 0.0.0.0 is not a real address: it is Azure's documented sentinel for "allow
# other Azure services". It is what lets the Function App connect at all, and it
# is NOT the same as opening the server to the internet.
az sql server firewall-rule create \
  --resource-group "$RESOURCE_GROUP" --server "$SQL_SERVER" \
  --name AllowAzureServices \
  --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0 \
  --output none
info "AllowAzureServices (0.0.0.0) — required by the Function App"

MY_IP="$(curl -fsS --max-time 10 https://api.ipify.org || true)"
if [[ -n "$MY_IP" ]]; then
  az sql server firewall-rule create \
    --resource-group "$RESOURCE_GROUP" --server "$SQL_SERVER" \
    --name "AllowLocalMachine" \
    --start-ip-address "$MY_IP" --end-ip-address "$MY_IP" \
    --output none
  info "AllowLocalMachine ($MY_IP) — for Query editor / VS Code / psql clients"
else
  warn "could not determine this machine's public IP; add the rule by hand if"
  warn "you want to query the database from here"
fi

# --------------------------------------------------------------------------
# 4. The database — serverless, auto-pausing, 2 GB, LRS backups.
# --------------------------------------------------------------------------
say "SQL database: $SQL_DATABASE (serverless, auto-pause 1h, 2 GB, LRS)"
if az sql db show --name "$SQL_DATABASE" --server "$SQL_SERVER" \
     --resource-group "$RESOURCE_GROUP" --output none 2>/dev/null; then
  info "already exists — reapplying the cost settings"
  # --edition/--family/--compute-model repeated on the update: without them the
  # CLI can reject a --capacity change on an existing database, and re-running
  # this script has to converge rather than fail.
  az sql db update \
    --name "$SQL_DATABASE" --server "$SQL_SERVER" --resource-group "$RESOURCE_GROUP" \
    --edition GeneralPurpose --compute-model Serverless --family Gen5 \
    --capacity 1 --min-capacity 0.5 --auto-pause-delay 60 --max-size 2GB \
    --output none
else
  az sql db create \
    --name "$SQL_DATABASE" \
    --server "$SQL_SERVER" \
    --resource-group "$RESOURCE_GROUP" \
    --edition GeneralPurpose \
    --compute-model Serverless \
    --family Gen5 \
    --capacity 1 \
    --min-capacity 0.5 \
    --auto-pause-delay 60 \
    --max-size 2GB \
    --backup-storage-redundancy Local \
    --collation SQL_Latin1_General_CP1_CI_AS \
    --output none
fi
info "$(az sql db show -n "$SQL_DATABASE" -s "$SQL_SERVER" -g "$RESOURCE_GROUP" \
        --query '{sku:sku.name, minCapacity:minCapacity, autoPauseMinutes:autoPauseDelay, maxSizeBytes:maxSizeBytes}' -o tsv)"

# --------------------------------------------------------------------------
# 5. Storage account — LRS, required by the Function App runtime.
# --------------------------------------------------------------------------
say "Storage account: $STORAGE_ACCOUNT (Standard_LRS)"
az storage account create \
  --name "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --sku Standard_LRS \
  --kind StorageV2 \
  --min-tls-version TLS1_2 \
  --allow-blob-public-access false \
  --output none
info "created or already present"

# --------------------------------------------------------------------------
# 6. Function App — Python on FLEX CONSUMPTION.
# --------------------------------------------------------------------------
# WHY FLEX CONSUMPTION AND NOT THE CLASSIC Y1 CONSUMPTION PLAN
# The brief says "Consumption (Serverless)". Both plans are that — billed per
# execution against a monthly free grant, scaling to zero, no always-on
# instance. Flex Consumption is the one Microsoft now recommends (the CLI prints
# a migration notice on every Y1 call; Linux Consumption reaches end of life on
# 2028-09-30), and this project was built on Y1 first and moved for concrete
# reasons, not for the label:
#
#   * On the Y1 app, the host's KEY MANAGEMENT API never worked. `listKeys`
#     answered "Encountered an error (InternalServerError) from host runtime"
#     indefinitely and the host never created its `azure-webjobs-secrets`
#     container — so no function key could be issued and every protected
#     endpoint was unusable. Ruled out as causes: storage credentials (the same
#     connection string creates that container by hand), the storage firewall,
#     the content share, the runtime config, cold-start timeouts (it failed
#     identically with the app warm at 0.1 s), and AzureWebJobsSecretStorageType.
#     The same app on Flex issued a key immediately.
#   * Y1 cold starts were measured at 42-60 s. Flex answers in ~0.1 s warm and
#     seconds cold.
#   * Flex has a working deployment API. `az functionapp deploy` (OneDeploy) on
#     Y1 answers "This API isn't available in this environment yet!".
#
# Cost is unchanged in practice: this workload is ~700 executions/month and
# ~42,000 GB-s, comfortably inside Flex's free grant, and the bill here is
# ~97% Azure SQL either way. See docs/cost_control.md.
say "Function App: $FUNCTION_APP (Python $PYTHON_VERSION, Flex Consumption)"
if az functionapp show --name "$FUNCTION_APP" --resource-group "$RESOURCE_GROUP" \
     --output none 2>/dev/null; then
  info "already exists"
else
  # --flexconsumption-location (rather than --consumption-plan-location, which
  # selects Y1) is what makes this an FC1 plan. The CLI creates the deployment
  # storage container it needs inside the storage account automatically.
  # --instance-memory 2048 is the default; 512 would quarter the GB-s but this
  # workload is inside the free grant either way, so the better cold start wins.
  az functionapp create \
    --name "$FUNCTION_APP" \
    --resource-group "$RESOURCE_GROUP" \
    --storage-account "$STORAGE_ACCOUNT" \
    --flexconsumption-location "$LOCATION" \
    --runtime python \
    --runtime-version "$PYTHON_VERSION" \
    --instance-memory 2048 \
    --maximum-instance-count 40 \
    --output none
  info "created"
fi

# --------------------------------------------------------------------------
# 7. Application settings — where the connection string lives.
# --------------------------------------------------------------------------
say "Application settings"
SQL_FQDN="$(az sql server show -n "$SQL_SERVER" -g "$RESOURCE_GROUP" \
            --query fullyQualifiedDomainName -o tsv)"
SQL_CONNECTION_STRING="Driver={ODBC Driver 18 for SQL Server};Server=tcp:${SQL_FQDN},1433;Database=${SQL_DATABASE};Uid=${SQL_ADMIN_USER};Pwd=${SQL_ADMIN_PASSWORD};Encrypt=yes;TrustServerCertificate=no;Connection Timeout=60;"

# WEBSITE_TIME_ZONE makes the NCRONTAB schedule Belgian local time instead of
# UTC. Without it, a "morning peak" window would drift by an hour twice a year.
# On Linux plans this takes an IANA zone name; on Windows it would be
# "Romance Standard Time".
az functionapp config appsettings set \
  --name "$FUNCTION_APP" --resource-group "$RESOURCE_GROUP" \
  --settings \
    "SQL_CONNECTION_STRING=$SQL_CONNECTION_STRING" \
    "INGEST_SCHEDULE=$INGEST_SCHEDULE" \
    "WEBSITE_TIME_ZONE=Europe/Brussels" \
    "IRAIL_USER_AGENT=RailPulseCloud/1.0 (BeCode data-engineering exercise; ${ACCOUNT_NAME})" \
    "IRAIL_LANG=en" \
    "SQL_LOGIN_TIMEOUT=60" \
    "SQL_MAX_ATTEMPTS=5" \
  --output none
# Note the absence of SCM_DO_BUILD_DURING_DEPLOYMENT / ENABLE_ORYX_BUILD. Those
# are how the OLD Linux Consumption path asked for a remote pip install. On Flex,
# the build is requested per deployment by the `RemoteBuild=true` query parameter
# in azure/deploy.sh, so setting them here would be cargo cult.
info "SQL_CONNECTION_STRING set (value not printed)"
info "INGEST_SCHEDULE=$INGEST_SCHEDULE  WEBSITE_TIME_ZONE=Europe/Brussels"

# HTTPS only: the connection string travels in app settings, but the function
# keys travel in URLs, and those should never cross plain HTTP.
az functionapp update --name "$FUNCTION_APP" --resource-group "$RESOURCE_GROUP" \
  --set httpsOnly=true --output none
info "httpsOnly enabled"

# --------------------------------------------------------------------------
# 7b. Publishing credentials — deliberately left DISABLED.
# --------------------------------------------------------------------------
# Azure now creates apps with both basic-auth publishing paths (scm and ftp)
# switched off, and this project keeps them that way. azure/deploy.sh publishes
# through OneDeploy with an Entra bearer token, so there is no password to
# enable, store or rotate.
#
# Worth recording because the Linux Consumption path did NOT allow this: there,
# `az functionapp deployment source config-zip` publishes through Kudu with basic
# auth, so scm had to be re-enabled — and its failure mode when disabled is
# `ResourceNotFound: Microsoft.Web/sites/<app>`, which sends you hunting for a
# missing resource that is sitting right there, Running. Moving to Flex removed
# the need to relax the default at all.
say "Publishing credentials"
SITE_ID="$(az functionapp show -n "$FUNCTION_APP" -g "$RESOURCE_GROUP" --query id -o tsv)"
for policy in scm ftp; do
  az resource update --ids "$SITE_ID/basicPublishingCredentialsPolicies/$policy" \
    --set properties.allow=false --api-version 2023-12-01 --output none 2>/dev/null \
    && info "$policy basic-auth publishing: disabled" \
    || info "$policy basic-auth policy not present (already off)"
done

# --------------------------------------------------------------------------
# 8. Record the outputs locally (gitignored).
# --------------------------------------------------------------------------
say "Writing $SECRET_FILE"
# The password and the connection string are SINGLE-QUOTED, the rest are not.
# That is not cosmetic: the connection string contains spaces, braces and
# semicolons, so an unquoted value would make `set -a; . .azure-railpulse.env`
# (how the Makefile's local-* targets load it) try to execute part of it. The
# plain values are read back by grep|cut in the other scripts, so they stay bare.
cat > "$SECRET_FILE" <<EOF
# Generated by azure/provision.sh on $(date -u '+%Y-%m-%dT%H:%M:%SZ')
# CONTAINS A PASSWORD. Gitignored. Do not commit, do not paste.
# Load with:  set -a && source $SECRET_FILE && set +a
RESOURCE_GROUP=$RESOURCE_GROUP
LOCATION=$LOCATION
SQL_SERVER=$SQL_SERVER
SQL_FQDN=$SQL_FQDN
SQL_DATABASE=$SQL_DATABASE
SQL_ADMIN_USER=$SQL_ADMIN_USER
STORAGE_ACCOUNT=$STORAGE_ACCOUNT
FUNCTION_APP=$FUNCTION_APP
SQL_ADMIN_PASSWORD='$SQL_ADMIN_PASSWORD'
SQL_CONNECTION_STRING='$SQL_CONNECTION_STRING'
EOF
chmod 600 "$SECRET_FILE"
info "mode 600, listed in .gitignore"

say "Provisioned"
cat <<EOF
    Resource group   $RESOURCE_GROUP
    SQL server       $SQL_FQDN
    Database         $SQL_DATABASE   (serverless, 1 vCore max / 0.5 min, pauses after 60 min)
    Function App     https://$FUNCTION_APP.azurewebsites.net
    Secrets          $SECRET_FILE  (mode 600, gitignored)

Next:
    ./azure/deploy.sh          # package and publish the function code
    ./azure/smoke_test.sh      # migrate, seed, ingest, verify
EOF
