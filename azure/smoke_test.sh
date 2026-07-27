#!/usr/bin/env bash
#
# RailPulse Cloud — end-to-end verification of a live deployment.
#
# Runs the whole pipeline against the real thing, in the order a first
# deployment needs, and fails loudly at the first step that does not work:
#
#   1. /api/ping                  is the app up?           (no database)
#   2. /api/migrate         create the schema         (idempotent)
#   3. /api/seed-stations   load ~714 stations        (one API call)
#   4. /api/ingest?hubs=all       poll every hub            (the real work)
#   5. /api/ingest?hubs=all       poll again immediately    (THE IDEMPOTENCY TEST)
#   6. /api/stats                 counts and data quality
#   7. /api/health                per-station freshness
#
# Step 5 is the one worth watching. A second run seconds after the first must
# report rows_updated > 0 and far fewer inserts than run one: the same
# departures, recognised and revised rather than duplicated. If it reports
# inserts on the same scale instead, the MERGE key is wrong and the dataset is
# quietly accumulating duplicates.
#
# The first call that touches the database may take 30-60 seconds while the
# serverless database resumes from its auto-pause. That is not a fault, and the
# retry logic in railpulse/database.py is what absorbs it.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRET_FILE="${SECRET_FILE:-$REPO_ROOT/.azure-railpulse.env}"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
fail() { printf '\033[1;31m    FAILED: %s\033[0m\n' "$*"; exit 1; }

if [[ -z "${FUNCTION_APP:-}" || -z "${RESOURCE_GROUP:-}" ]]; then
  [[ -f "$SECRET_FILE" ]] || fail "no $SECRET_FILE; run ./azure/provision.sh"
  FUNCTION_APP="$(grep -E '^FUNCTION_APP=' "$SECRET_FILE" | cut -d= -f2-)"
  RESOURCE_GROUP="$(grep -E '^RESOURCE_GROUP=' "$SECRET_FILE" | cut -d= -f2-)"
fi

# Resolving the hostname takes two tries, and the reason is a live Azure CLI bug:
# on a FLEX CONSUMPTION app, `az functionapp show` returns an EMPTY string for
# defaultHostName (and for state and sku), while `az webapp show` — the same ARM
# resource, a different command module — returns it correctly. Trusting the first
# alone silently yields BASE="https://" and then `curl: (6) Could not resolve
# host: api`, which looks like a DNS problem and is not one.
# Never name this variable HOSTNAME: bash pre-populates that with the machine's
# own name, so a failed lookup would leave a plausible-looking wrong value.
APP_HOSTNAME="$(az functionapp show -n "$FUNCTION_APP" -g "$RESOURCE_GROUP" \
                --query defaultHostName -o tsv 2>/dev/null || true)"
if [[ -z "$APP_HOSTNAME" ]]; then
  APP_HOSTNAME="$(az webapp show -n "$FUNCTION_APP" -g "$RESOURCE_GROUP" \
                  --query defaultHostName -o tsv 2>/dev/null || true)"
fi
[[ -n "$APP_HOSTNAME" ]] || fail "could not resolve the hostname of $FUNCTION_APP"
BASE="https://$APP_HOSTNAME"
info "base URL: $BASE"

# --------------------------------------------------------------------------
# JSON handling without jq. python3 is already a hard requirement here (the
# Azure CLI is written in it), while jq is not installed by default on macOS —
# and a script that silently behaves differently depending on whether jq is
# present is worse than one with a dependency.
# --------------------------------------------------------------------------
pretty() { python3 -m json.tool 2>/dev/null || cat; }

# jval <dotted.path> — reads JSON on stdin, prints the value, or 0 if absent.
jval() {
  python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except ValueError:
    print(0); raise SystemExit
for part in sys.argv[1].split("."):
    data = data.get(part) if isinstance(data, dict) else None
    if data is None:
        break
print(0 if data is None else data)
' "$1"
}

# jrows <list-key> <field> [<field> ...] — one line per element of a JSON array.
jrows() {
  python3 -c '
import json, sys
data = json.load(sys.stdin)
rows = data.get(sys.argv[1]) or []
fields = sys.argv[2:]
for row in rows:
    print("    " + "  ".join(f"{f}={row.get(f)}" for f in fields))
' "$@"
}

# --------------------------------------------------------------------------
# The host key authorises every route except /api/ping. Fetched here rather than
# stored, so this script holds no secret of its own.
# --------------------------------------------------------------------------
say "Fetching the function key"
KEY="$(az functionapp keys list -n "$FUNCTION_APP" -g "$RESOURCE_GROUP" \
       --query 'functionKeys.default' -o tsv)"
[[ -n "$KEY" && "$KEY" != "null" ]] || fail "could not read the default function key"
info "got a key (${#KEY} characters)"

call() {  # call METHOD PATH [max-seconds]
  local method="$1" path="$2" max="${3:-300}"
  curl -fsS -X "$method" --max-time "$max" \
       -H "x-functions-key: $KEY" "$BASE$path"
}

# --------------------------------------------------------------------------
say "1/7  Liveness  GET /api/ping"
curl -fsS --max-time 30 "$BASE/api/ping" | pretty \
  || fail "the app is not answering — check: az functionapp log tail -n $FUNCTION_APP -g $RESOURCE_GROUP"

# --------------------------------------------------------------------------
say "2/7  Schema  POST /api/migrate"
info "(first database call — may wait up to a minute for a serverless resume)"
MIGRATE="$(call POST /api/migrate 300)" || fail "migration failed"
echo "$MIGRATE" | pretty
[[ "$(echo "$MIGRATE" | jval status)" == "ok" ]] || fail "migration did not report ok"

# --------------------------------------------------------------------------
say "3/7  Station catalogue  POST /api/seed-stations"
SEED="$(call POST /api/seed-stations 300)" || fail "station seed failed"
STATIONS="$(echo "$SEED" | jval stations_written)"
info "stations written: $STATIONS"
[[ "${STATIONS:-0}" -gt 100 ]] || fail "expected several hundred stations, got $STATIONS"

# --------------------------------------------------------------------------
say "4/7  First ingest  POST /api/ingest?hubs=all"
FIRST="$(call POST '/api/ingest?hubs=all' 420)" || fail "ingest failed"
FIRST_POLLED="$(echo "$FIRST" | jval stations_polled)"
FIRST_OK="$(echo "$FIRST" | jval stations_succeeded)"
FIRST_FAILED="$(echo "$FIRST" | jval stations_failed)"
FIRST_RETURNED="$(echo "$FIRST" | jval departures_returned)"
FIRST_INSERTED="$(echo "$FIRST" | jval rows_inserted)"
info "polled $FIRST_POLLED hub(s): $FIRST_OK ok, $FIRST_FAILED failed"
info "$FIRST_RETURNED departures returned, $FIRST_INSERTED inserted"
echo "$FIRST" | jrows results requested status departures_returned rows_inserted rows_updated
if [[ "${FIRST_FAILED:-0}" != "0" ]]; then
  info "errors reported:"
  # No escaped quotes inside these inline snippets: the shell wrapper is
  # single-quoted, so a \" would reach Python as a literal backslash — and a
  # backslash is not allowed inside an f-string replacement field.
  echo "$FIRST" | python3 -c '
import json, sys
for row in json.load(sys.stdin).get("results", []):
    if row.get("status") != "success":
        print("      " + str(row.get("requested")) + ": " + str(row.get("error")))
'
fi
[[ "${FIRST_INSERTED:-0}" -gt 0 ]] || fail "the first ingest inserted nothing"

# --------------------------------------------------------------------------
say "5/7  IDEMPOTENCY  POST /api/ingest?hubs=all (again, immediately)"
SECOND="$(call POST '/api/ingest?hubs=all' 420)" || fail "second ingest failed"
SECOND_INSERTED="$(echo "$SECOND" | jval rows_inserted)"
SECOND_UPDATED="$(echo "$SECOND" | jval rows_updated)"
info "run 1: $FIRST_INSERTED inserted"
info "run 2: $SECOND_INSERTED inserted, $SECOND_UPDATED revised"
[[ "${SECOND_UPDATED:-0}" -gt 0 ]] \
  || fail "the second run revised nothing — the MERGE is not matching existing rows"
# A handful of genuine inserts is expected and correct: a minute later the
# liveboard has rolled forward and shows a few departures further into the
# future. What must not happen is the second run re-inserting the FIRST run's
# departures, which would show up as inserts on the same scale as run one.
if [[ "${SECOND_INSERTED:-0}" -ge "${FIRST_INSERTED:-1}" ]]; then
  fail "run 2 inserted as many rows as run 1 ($SECOND_INSERTED vs $FIRST_INSERTED) — duplicates are being created"
fi
info "OK: repeated polls revise rather than duplicate"

# --------------------------------------------------------------------------
say "6/7  Warehouse contents  GET /api/stats"
STATS="$(call GET /api/stats 180)" || fail "stats failed"
echo "$STATS" | python3 -c '
import json, sys
data = json.load(sys.stdin)
print("    table counts:")
for table, count in (data.get("table_counts") or {}).items():
    print(f"      {table:<22} {count:>9,}")
quality = data.get("data_quality") or {}
if quality:
    print("    data quality:")
    for key in ("row_count", "distinct_dates", "distinct_stations",
                "earliest_departure_local", "latest_departure_local",
                "pct_platform_unknown", "pct_occupancy_unknown",
                "observed_once", "avg_observations", "confirmed_departed"):
        if key in quality:
            print(f"      {key:<26} {quality[key]}")
rows = data.get("punctuality_by_station") or []
if rows:
    print("    punctuality:")
    for row in rows:
        name = str(row.get("station_name"))[:28]
        print("      %-28s departures=%s avg_delay_s=%s on_time_6min=%s" % (
            name, row.get("departures"), row.get("avg_delay_seconds"),
            row.get("pct_on_time_6min")))
'

TOTAL="$(echo "$STATS" | jval table_counts.liveboard_records)"
[[ "${TOTAL:-0}" -gt 0 ]] || fail "liveboard_records is empty"
info "liveboard_records now holds $TOTAL row(s)"

# --------------------------------------------------------------------------
say "7/7  Pipeline health  GET /api/health"
# 207 when a station is stale; tolerated either way, because a stale station is
# a finding to report, not a reason to abort the smoke test.
HEALTH="$(call GET /api/health 180 || true)"
echo "$HEALTH" | jrows stations station_name last_run_status last_run_departures minutes_since_last_run

say "All checks passed"
cat <<EOF
    Base URL      $BASE
    Ingest        curl -X POST "$BASE/api/ingest?hubs=all&code=<key>"
    Stats         curl "$BASE/api/stats?code=<key>"
    Function key  az functionapp keys list -n $FUNCTION_APP -g $RESOURCE_GROUP \\
                    --query functionKeys.default -o tsv

The timer trigger now runs on its own, inside the configured windows.
Check tomorrow with:  make health
EOF
