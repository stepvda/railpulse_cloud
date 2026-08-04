#!/usr/bin/env python3
"""Publish the warehouse into Power BI as a push dataset, and print its URL.

WHAT THIS SOLVES, AND WHAT IT DOES NOT
There is **no URL that makes the Power BI web client connect to a database**.
Power BI has exactly one pre-filled-connection artifact — a `.pbids` file — and it
opens Power BI *Desktop*, which is Windows-only. In the Service, setting up a data
source is an interactive flow with no documented deep link and no query-string
parameters. That is a Power BI limitation, not something a script can route
around.

What a script *can* do is create the dataset itself through the Power BI REST API
and hand back a real URL. That is what this does: it reads the BI views out of
Azure SQL and pushes them into a **push dataset** in My Workspace, complete with
relationships, so the report is one click from existing rather than one wizard.

THE TRADE-OFF, STATED PLAINLY
A push dataset holds a **snapshot**. It does not hold a live connection to Azure
SQL, so it does not refresh itself — re-run this script to update it. In exchange
it works on a **Free** licence, needs no gateway, and costs nothing.

If you want a genuinely live, self-refreshing model, do the interactive Azure SQL
connection once (docs/powerbi.md) and choose Import with a scheduled refresh. The
two are complementary: this script for an instant URL, the interactive model for
the graded deliverable.

WHY IT READS AS powerbi_reader
Deliberately the least-privilege login from scripts/create_bi_reader.py, not the
admin. It also proves that login is sufficient for the whole BI surface — if this
script works, so will Power BI's own connection.

    python scripts/publish_powerbi_dataset.py            # create or refresh
    python scripts/publish_powerbi_dataset.py --recreate # drop and rebuild
    python scripts/publish_powerbi_dataset.py --url      # just print the URL
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SECRET_FILE = Path(os.environ.get("SECRET_FILE", REPO_ROOT / ".azure-railpulse.env"))

DATASET_NAME = "RailPulse Cloud"
API = "https://api.powerbi.com/v1.0/myorg"

#: Push-dataset limits that shape the code below: 75 columns per table, 10 000
#: rows per POST, ~16 MB per request. Chunking at 1 000 keeps each request small
#: enough that a slow link does not time out mid-push.
ROWS_PER_REQUEST = 1000

#: (power bi table, source view, {column: power bi type}) — declarative so that
#: adding a column is one line and the SELECT is generated from it, which stops
#: the two drifting.
TABLES: tuple[tuple[str, str, dict[str, str]], ...] = (
    ("departures", "dbo.v_bi_departures", {
        "record_id": "Int64",
        "station_name": "String",
        "station_country": "String",
        "station_latitude": "Double",
        "station_longitude": "Double",
        "destination_name": "String",
        "destination_country": "String",
        "is_international": "Bool",
        "platform_label": "String",
        "platform_is_normal": "Bool",
        "vehicle_name": "String",
        "vehicle_type": "String",
        "service_line": "String",
        "scheduled_departure_local": "Datetime",
        "date_key": "Datetime",
        "hour_of_day": "Int64",
        "day_type": "String",
        "peak_window": "String",
        "delay_seconds": "Int64",
        "delay_minutes": "Double",
        "delay_bucket": "String",
        "delay_bucket_order": "Int64",
        "is_on_time_2min": "Bool",
        "is_on_time_6min": "Bool",
        "is_canceled": "Bool",
        "has_left": "Bool",
        "occupancy": "String",
        "observation_count": "Int64",
        "delay_first_seen_s": "Int64",
        "delay_growth_s": "Int64",
    }),
    ("dim_date", "dbo.dim_date", {
        "date_key": "Datetime",
        "year_number": "Int64",
        "quarter_number": "Int64",
        "month_number": "Int64",
        "day_of_month": "Int64",
        "day_of_week_iso": "Int64",
        "iso_week": "Int64",
        "month_name": "String",
        "month_short": "String",
        "day_name": "String",
        "day_short": "String",
        "year_month": "String",
        "year_month_sort": "Int64",
        "is_weekend": "Bool",
        "is_capture_day": "Bool",
    }),
    ("dim_hour", "dbo.dim_hour", {
        "hour_of_day": "Int64",
        "hour_label": "String",
        "peak_window": "String",
        "window_sort": "Int64",
        "is_sampled": "Bool",
    }),
    ("pipeline_health", "dbo.v_ingestion_health", {
        "station_name": "String",
        "last_run_status": "String",
        "last_run_departures": "Int64",
        "last_run_inserted": "Int64",
        "last_run_updated": "Int64",
        "minutes_since_last_run": "Int64",
        "is_stale": "Bool",
        "failures_last_24h": "Int64",
        "runs_last_24h": "Int64",
    }),
    ("data_quality", "dbo.v_data_quality", {
        "row_count": "Int64",
        "distinct_dates": "Int64",
        "distinct_stations": "Int64",
        "platform_unknown": "Int64",
        "pct_platform_unknown": "Double",
        "occupancy_unknown": "Int64",
        "pct_occupancy_unknown": "Double",
        "observed_once": "Int64",
        "avg_observations": "Double",
        "confirmed_departed": "Int64",
    }),
)

#: DAX measures, defined in the MODEL rather than in each visual, so that every
#: report over this dataset shares one definition of "on time" — the same reason
#: sql/03_views.sql exists. They restate the view definitions in DAX, including
#: the rule that matters most: cancellations are excluded from punctuality
#: denominators. That falls out of COUNTA, because the views store NULL in the
#: flags for a cancelled train rather than 0.
#:
#: scripts/build_powerbi_report.py imports these; keeping them here means the
#: model has them whichever script creates it.
MEASURES: dict[str, tuple[dict[str, str], ...]] = {
    "departures": (
        {"name": "Departures", "expression": "COUNTROWS('departures')"},
        {"name": "Cancellations",
         "expression": "CALCULATE(COUNTROWS('departures'), 'departures'[is_canceled] = TRUE())"},
        {"name": "Trains measured", "expression": "COUNTA('departures'[is_on_time_6min])"},
        {"name": "On time 6min %",
         "expression": ("DIVIDE(CALCULATE(COUNTA('departures'[is_on_time_6min]), "
                        "'departures'[is_on_time_6min] = TRUE()), "
                        "COUNTA('departures'[is_on_time_6min]))"),
         "formatString": "0.0%"},
        {"name": "On time 2min %",
         "expression": ("DIVIDE(CALCULATE(COUNTA('departures'[is_on_time_2min]), "
                        "'departures'[is_on_time_2min] = TRUE()), "
                        "COUNTA('departures'[is_on_time_2min]))"),
         "formatString": "0.0%"},
        {"name": "Mean delay s",
         "expression": ("CALCULATE(AVERAGE('departures'[delay_seconds]), "
                        "'departures'[is_canceled] = FALSE())"),
         "formatString": "0.0"},
        {"name": "Cancelled %", "expression": "DIVIDE([Cancellations], [Departures])",
         "formatString": "0.0%"},
        {"name": "Platform changes",
         "expression": ("CALCULATE(COUNTROWS('departures'), "
                        "'departures'[platform_is_normal] = FALSE())")},
        {"name": "Reliability score",
         "expression": ("DIVIDE(CALCULATE(COUNTA('departures'[is_on_time_6min]), "
                        "'departures'[is_on_time_6min] = TRUE()) - [Cancellations], "
                        "COUNTA('departures'[is_on_time_6min]))"),
         "formatString": "0.0%"},
        {"name": "Days observed", "expression": "DISTINCTCOUNT('departures'[date_key])"},
        # Never rank hours on a raw count. The timer samples peak windows harder
        # than the rest of the day, so a raw count would report the CAPTURE
        # SCHEDULE as the peak — circular. Normalising by days observed is the
        # same correction v_hourly_pressure.departures_per_day makes in SQL.
        {"name": "Departures per day", "expression": "DIVIDE([Departures], [Days observed])",
         "formatString": "0.0"},
        {"name": "Delay growth s", "expression": "AVERAGE('departures'[delay_growth_s])",
         "formatString": "0.0"},
        {"name": "Deteriorated 5min+",
         "expression": "CALCULATE(COUNTROWS('departures'), 'departures'[delay_growth_s] > 300)"},
    ),
}

#: Relationships, so the model works the moment it lands rather than after the
#: reader has guessed which columns join. Push datasets accept these at creation.
RELATIONSHIPS = (
    {
        "name": "departures_to_date",
        "fromTable": "departures", "fromColumn": "date_key",
        "toTable": "dim_date", "toColumn": "date_key",
        "crossFilteringBehavior": "OneDirection",
    },
    {
        "name": "departures_to_hour",
        "fromTable": "departures", "fromColumn": "hour_of_day",
        "toTable": "dim_hour", "toColumn": "hour_of_day",
        "crossFilteringBehavior": "OneDirection",
    },
)


# ==========================================================================
def load_env() -> dict[str, str]:
    if not SECRET_FILE.is_file():
        raise SystemExit(f"no {SECRET_FILE} — run ./azure/provision.sh first")
    values: dict[str, str] = {}
    for line in SECRET_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def powerbi_token() -> str:
    """Borrow the Azure CLI's token for the Power BI API.

    Avoids registering an app or storing another secret: the signed-in user
    already has a Power BI licence, and `az` can mint a token for that audience.
    """
    result = subprocess.run(
        ["az", "account", "get-access-token",
         "--resource", "https://analysis.windows.net/powerbi/api",
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise SystemExit(
            "could not get a Power BI token. Run `az login`, and check the account "
            f"has a Power BI licence.\n{result.stderr.strip()[:300]}"
        )
    return result.stdout.strip()


def ssl_context() -> ssl.SSLContext:
    """A context with a working CA bundle.

    The python.org macOS build ships without linking to the system trust store,
    so `urllib` fails every HTTPS request with CERTIFICATE_VERIFY_FAILED until
    "Install Certificates.command" has been run. Using certifi's bundle when it
    is available fixes that without the tempting and wrong alternative of
    disabling verification — which on a call that carries a bearer token would
    be handing the token to anyone who can intercept the connection.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL = ssl_context()


def api(token: str, method: str, path: str, body: dict | None = None) -> dict:
    """One Power BI REST call. Raises with the server's own message on failure."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{API}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=180, context=_SSL) as response:
            text = response.read().decode()
            return json.loads(text) if text.strip() else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode()[:400]
        raise SystemExit(f"Power BI API {method} {path} -> HTTP {error.code}\n{detail}")


def jsonable(value):
    """Push-dataset JSON: ISO timestamps, plain floats, real nulls."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day).isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return value


# ==========================================================================
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--recreate", action="store_true",
                        help="delete and rebuild the dataset (needed after a schema change)")
    parser.add_argument("--url", action="store_true",
                        help="print the dataset URL and exit")
    args = parser.parse_args(argv)

    env = load_env()
    token = powerbi_token()

    existing = {d["name"]: d["id"] for d in api(token, "GET", "/datasets").get("value", [])}
    dataset_id = existing.get(DATASET_NAME)

    if args.url:
        if not dataset_id:
            raise SystemExit(f"'{DATASET_NAME}' does not exist yet — run without --url")
        print(f"https://app.powerbi.com/groups/me/datasets/{dataset_id}/details")
        return 0

    if dataset_id and args.recreate:
        api(token, "DELETE", f"/datasets/{dataset_id}")
        print(f"  deleted the existing '{DATASET_NAME}'")
        dataset_id = None

    if not dataset_id:
        payload = {
            "name": DATASET_NAME,
            "defaultMode": "Push",
            "tables": [
                {"name": name,
                 "columns": [{"name": c, "dataType": t} for c, t in columns.items()],
                 "measures": list(MEASURES.get(name, ()))}
                for name, _, columns in TABLES
            ],
            "relationships": list(RELATIONSHIPS),
        }
        created = api(token, "POST", "/datasets", payload)
        dataset_id = created["id"]
        measure_count = sum(len(m) for m in MEASURES.values())
        print(f"  created dataset '{DATASET_NAME}' ({dataset_id})")
        print(f"  {len(TABLES)} tables, {len(RELATIONSHIPS)} relationships, "
              f"{measure_count} measures")
    else:
        print(f"  reusing dataset '{DATASET_NAME}' ({dataset_id})")

    # ---- read from Azure SQL as the least-privilege BI login ---------------
    try:
        import pymssql
    except ImportError:
        raise SystemExit("pymssql is required: pip install pymssql")

    login = env.get("BI_READER_LOGIN")
    password = env.get("BI_READER_PASSWORD")
    if not (login and password):
        raise SystemExit("no BI_READER_* in the env file — run "
                         "python scripts/create_bi_reader.py first")

    connection = pymssql.connect(
        server=env["SQL_FQDN"], user=login, password=password,
        database=env["SQL_DATABASE"], timeout=300, login_timeout=180,
    )
    print(f"  reading as {login} (views only — cannot see the base tables)")

    total = 0
    with connection:
        cursor = connection.cursor(as_dict=True)
        for table_name, source, columns in TABLES:
            column_list = ", ".join(columns)
            cursor.execute(f"SELECT {column_list} FROM {source}")
            rows = cursor.fetchall()

            # Replacing rather than appending: this is a snapshot, and appending
            # would duplicate every row on the second run.
            api(token, "DELETE", f"/datasets/{dataset_id}/tables/{table_name}/rows")

            for start in range(0, len(rows), ROWS_PER_REQUEST):
                chunk = rows[start:start + ROWS_PER_REQUEST]
                api(token, "POST",
                    f"/datasets/{dataset_id}/tables/{table_name}/rows",
                    {"rows": [{k: jsonable(v) for k, v in row.items()} for row in chunk]})
            total += len(rows)
            print(f"    {table_name:<16} {len(rows):>6} rows")

    print(f"  pushed {total:,} rows in total")
    print()
    print("  Open it here:")
    print(f"    dataset  https://app.powerbi.com/groups/me/datasets/{dataset_id}/details")
    print(f"    new report  https://app.powerbi.com/groups/me/datasets/{dataset_id}/createreport")
    print()
    print("  This is a SNAPSHOT. Re-run this script to refresh it; use the")
    print("  interactive Azure SQL connection in docs/powerbi.md for a live model.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
