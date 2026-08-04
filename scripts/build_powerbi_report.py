#!/usr/bin/env python3
"""Build the Streamlit dashboard's pages as a real Power BI report, via the API.

WHY THIS EXISTS
Power BI Desktop is Windows-only, so on macOS the usual "drag fields onto a
canvas" route is unavailable. A written build guide is a poor substitute: it puts
the work back on the reader, and it drifts from the Streamlit app the moment
either side changes. So this generates the report instead — model, measures,
relationships, pages, visuals — and creates it in the service.

HOW, AND THE TWO DEAD ENDS ON THE WAY
The Fabric items API takes a Report whose definition is a set of base64 parts.
Both failures below return the *same* unhelpful `MissingDefinitionParts`, so they
are recorded here rather than rediscovered:

  1. `.platform` + `definition.pbir` alone — no layout at all. Fails.
  2. The PBIR **enhanced** layout (`definition/pages/<id>/visuals/...`) — also
     fails, because `definition.pbir` declaring `"version": "1.0"` selects the
     **legacy** format, which wants a single root-level `report.json`.
  3. Root `report.json` in the classic Layout shape — succeeds.

The service then rewrites `definition.pbir` to version 4.0 by itself and keeps
the legacy layout, which is how you can tell the format was genuinely accepted
rather than merely stored.

The legacy shape has one trap: nested `config` and `filters` fields are
**JSON-encoded strings**, not objects. Passing a real object is accepted without
complaint and yields a blank report.

WHY MY WORKSPACE
A Free licence has full rights in My Workspace but restricted rights elsewhere,
so a report built in a named workspace can be created and then refuse to render.
Targeting My Workspace keeps this working on the free tier. The Fabric API needs
a workspace GUID even for My Workspace; it is discovered, not hardcoded.

    python scripts/build_powerbi_report.py             # model + data + report
    python scripts/build_powerbi_report.py --data-only # refresh the rows only
    python scripts/build_powerbi_report.py --url       # print the report URL
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SECRET_FILE = Path(os.environ.get("SECRET_FILE", REPO_ROOT / ".azure-railpulse.env"))

REPORT_NAME = "RailPulse Cloud — operations"

PBI_API = "https://api.powerbi.com/v1.0/myorg"
FABRIC_API = "https://api.fabric.microsoft.com/v1"
ROWS_PER_REQUEST = 1000

# The table/column/measure contract is shared with the publisher so the two
# cannot drift into describing different models under the same name.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from publish_powerbi_dataset import (  # noqa: E402
    DATASET_NAME, MEASURES, RELATIONSHIPS, TABLES, jsonable,
)


# ==========================================================================
# plumbing
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


def ssl_context() -> ssl.SSLContext:
    """certifi's bundle. The python.org macOS build has no usable trust store,
    and disabling verification on a call that carries a bearer token would hand
    that token to anyone able to intercept the connection."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL = ssl_context()


def token(resource: str) -> str:
    result = subprocess.run(
        ["az", "account", "get-access-token", "--resource", resource,
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        raise SystemExit(f"could not get a token for {resource} — run `az login`.\n"
                         f"{result.stderr.strip()[:200]}")
    return result.stdout.strip()


#: Power BI's own store is transiently faulty in a way this script provokes.
#: Deleting a dataset and recreating it under the same name within a few seconds
#: returns HTTP 500 `SqlException`: "Data modification failed on system-versioned
#: table ... Models_V0 because transaction time was earlier than period start
#: time". That is a temporal-table clash inside the service, not a bad request —
#: the identical payload succeeds a minute later. 429 and 503 are the ordinary
#: throttle/unavailable pair. Retrying is correct for all three; anything else is
#: a real error and must not be retried, least of all a row POST.
RETRYABLE = (429, 500, 502, 503, 504)


def call(tok: str, method: str, url: str, body=None, raw: bool = False,
         attempts: int = 5):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    last = ""
    for attempt in range(attempts):
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=300, context=_SSL) as response:
                text = response.read().decode()
                payload = json.loads(text) if text.strip() else {}
                return (response.headers, payload) if raw else payload
        except urllib.error.HTTPError as error:
            last = error.read().decode()[:400]
            if error.code not in RETRYABLE or attempt == attempts - 1:
                raise SystemExit(f"{method} {url} -> HTTP {error.code}\n{last}")
            delay = 15 * (attempt + 1)
            print(f"    HTTP {error.code} from the service, retrying in {delay}s "
                  f"({attempt + 1}/{attempts - 1})")
            time.sleep(delay)
    raise SystemExit(f"{method} {url} kept failing\n{last}")


def wait_for_operation(fabric_tok: str, operation_id: str, label: str) -> None:
    """A Fabric 202 means the request was accepted, and says nothing about
    success — the two rejected formats above both returned 202 and then failed."""
    for _ in range(40):
        time.sleep(6)
        state = call(fabric_tok, "GET", f"{FABRIC_API}/operations/{operation_id}")
        status = state.get("status")
        if status == "Succeeded":
            return
        if status == "Failed":
            error = state.get("error") or {}
            raise SystemExit(f"{label} failed: {error.get('errorCode')} — "
                             f"{str(error.get('message'))[:300]}")
    raise SystemExit(f"{label}: operation did not finish in time")


def my_workspace_id(fabric_tok: str) -> str:
    """My Workspace's GUID. The Power BI REST API addresses it by omitting the
    group segment, but Fabric has no such shorthand and needs the id."""
    workspaces = call(fabric_tok, "GET", f"{FABRIC_API}/workspaces").get("value", [])
    for workspace in workspaces:
        if workspace.get("type") == "Personal":
            return workspace["id"]
    for workspace in workspaces:
        if (workspace.get("displayName") or "").lower() == "my workspace":
            return workspace["id"]
    raise SystemExit("could not find My Workspace in the Fabric workspace list")


# ==========================================================================
# report definition (legacy PBIR)
# ==========================================================================
def field(spec: dict, source: str) -> dict:
    """One Select entry of a prototypeQuery, from a compact spec.

    {"t": table, "c": col}            a bare column
    {"t": table, "c": col, "agg": n}  an aggregate — 0 Sum, 1 Avg, 5 CountNonNull
    {"t": table, "m": measure}        a model measure
    """
    ref = {"SourceRef": {"Source": source}}
    if "m" in spec:
        return {"Measure": {"Expression": ref, "Property": spec["m"]},
                "Name": f"{spec['t']}.{spec['m']}"}
    column = {"Column": {"Expression": ref, "Property": spec["c"]}}
    if "agg" in spec:
        return {"Aggregation": {"Expression": column, "Function": spec["agg"]},
                "Name": f"Agg{spec['agg']}({spec['t']}.{spec['c']})"}
    return {**column, "Name": f"{spec['t']}.{spec['c']}"}


def visual(vtype: str, x: int, y: int, w: int, h: int,
           buckets: dict[str, list[dict]], *, title: str | None = None,
           sort: tuple[dict, str] | None = None) -> dict:
    """One visualContainer in the legacy Layout shape.

    `buckets` maps a visual role (Category, Y, Series, Values...) to field specs.
    Each distinct table gets its own query source alias; cross-table visuals are
    resolved through the model relationships, which is why those are declared.
    """
    specs = [s for group in buckets.values() for s in group]
    tables = list(dict.fromkeys(s["t"] for s in specs))
    sources = {table: f"s{i}" for i, table in enumerate(tables)}

    select: list[dict] = []
    projections: dict[str, list[dict]] = {}
    for role, group in buckets.items():
        projections[role] = []
        for spec in group:
            entry = field(spec, sources[spec["t"]])
            select.append(entry)
            projections[role].append({"queryRef": entry["Name"]})

    prototype = {
        "Version": 2,
        "From": [{"Name": alias, "Entity": table, "Type": 0}
                 for table, alias in sources.items()],
        "Select": select,
    }
    if sort is not None:
        spec, direction = sort
        expression = field(spec, sources[spec["t"]])
        prototype["OrderBy"] = [{
            "Direction": 2 if direction == "desc" else 1,
            "Expression": {k: v for k, v in expression.items() if k != "Name"},
        }]

    single = {"visualType": vtype, "projections": projections,
              "prototypeQuery": prototype, "drillFilterOtherVisuals": True}
    if title:
        single["vcObjects"] = {"title": [{"properties": {
            "show": {"expr": {"Literal": {"Value": "true"}}},
            "text": {"expr": {"Literal": {"Value": f"'{title}'"}}}}}]}

    # `config` is a JSON-encoded STRING here, not an object. An object is
    # accepted silently and renders an empty page.
    config = {"name": uuid.uuid4().hex,
              "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": 0,
                                                 "width": w, "height": h}}],
              "singleVisual": single}
    return {"x": x, "y": y, "z": 0, "width": w, "height": h,
            "config": json.dumps(config), "filters": "[]"}


def page(name: str, display: str, visuals: list[dict]) -> dict:
    return {"name": name, "displayName": display, "width": 1280, "height": 720,
            "displayOption": 1, "config": "{}", "filters": "[]",
            "visualContainers": visuals}


# --------------------------------------------------------------------------
D, H, Q, P = "departures", "dim_hour", "data_quality", "pipeline_health"
CARD_H, ROW1 = 105, 20


def build_pages() -> list[dict]:
    """One page per analytical page of the Streamlit app, over the same views
    and the same metric definitions."""
    pages: list[dict] = []

    # ---- Overview — mirrors the Streamlit landing page -------------------
    kpis = ["Departures", "On time 6min %", "On time 2min %",
            "Mean delay s", "Cancellations", "Platform changes"]
    overview = [visual("card", 20 + i * 208, ROW1, 198, CARD_H,
                       {"Values": [{"t": D, "m": m}]}, title=m)
                for i, m in enumerate(kpis)]
    overview += [
        visual("clusteredColumnChart", 20, 145, 620, 275,
               {"Category": [{"t": D, "c": "hour_of_day"}],
                "Y": [{"t": D, "m": "Departures"}]},
               title="Departures by local hour"),
        visual("clusteredBarChart", 660, 145, 600, 275,
               {"Category": [{"t": D, "c": "delay_bucket"}],
                "Y": [{"t": D, "m": "Departures"}]},
               title="Delay distribution"),
        visual("tableEx", 20, 435, 1240, 265,
               {"Values": [{"t": D, "c": "station_name"}, {"t": D, "m": "Departures"},
                           {"t": D, "m": "On time 6min %"}, {"t": D, "m": "Mean delay s"},
                           {"t": D, "m": "Cancellations"}]},
               title="By station"),
    ]
    pages.append(page("ReportSectionOverview", "Overview", overview))

    # ---- Hub leaderboard -------------------------------------------------
    pages.append(page("ReportSectionLeaderboard", "Hub leaderboard", [
        visual("clusteredBarChart", 20, ROW1, 620, 330,
               {"Category": [{"t": D, "c": "station_name"}],
                "Y": [{"t": D, "m": "On time 6min %"}]},
               title="Punctuality by hub (under 6 min)",
               sort=({"t": D, "m": "On time 6min %"}, "desc")),
        visual("scatterChart", 660, ROW1, 600, 330,
               {"Category": [{"t": D, "c": "station_name"}],
                "X": [{"t": D, "m": "Departures"}],
                "Y": [{"t": D, "m": "Mean delay s"}]},
               title="Volume against mean delay"),
        visual("tableEx", 20, 370, 1240, 330,
               {"Values": [{"t": D, "c": "station_name"}, {"t": D, "m": "Departures"},
                           {"t": D, "m": "On time 6min %"}, {"t": D, "m": "On time 2min %"},
                           {"t": D, "m": "Mean delay s"}, {"t": D, "m": "Cancellations"},
                           {"t": D, "m": "Platform changes"},
                           {"t": D, "m": "Reliability score"}]},
               title="Leaderboard — cancellations are excluded from the punctuality denominator"),
    ]))

    # ---- Peak hours ------------------------------------------------------
    pages.append(page("ReportSectionPeaks", "Peak hours", [
        visual("clusteredColumnChart", 20, ROW1, 1240, 300,
               {"Category": [{"t": H, "c": "hour_label"}],
                "Y": [{"t": D, "m": "Departures per day"}],
                "Series": [{"t": D, "c": "day_type"}]},
               title="Departures per DAY OBSERVED, not raw count — the timer samples peaks harder"),
        visual("lineChart", 20, 335, 620, 280,
               {"Category": [{"t": H, "c": "hour_label"}],
                "Y": [{"t": D, "m": "Mean delay s"}]},
               title="Mean delay by hour"),
        visual("tableEx", 660, 335, 600, 280,
               {"Values": [{"t": H, "c": "hour_label"}, {"t": H, "c": "peak_window"},
                           {"t": D, "m": "Departures"}, {"t": D, "m": "Departures per day"},
                           {"t": D, "m": "Mean delay s"}]},
               title="By hour"),
    ]))

    # ---- Platform bottlenecks --------------------------------------------
    pages.append(page("ReportSectionPlatforms", "Platform bottlenecks", [
        visual("clusteredColumnChart", 20, ROW1, 620, 330,
               {"Category": [{"t": D, "c": "platform_label"}],
                "Y": [{"t": D, "m": "Departures"}]},
               title="Load by platform", sort=({"t": D, "m": "Departures"}, "desc")),
        visual("clusteredColumnChart", 660, ROW1, 600, 330,
               {"Category": [{"t": D, "c": "platform_label"}],
                "Y": [{"t": D, "m": "Mean delay s"}]},
               title="Mean delay by platform"),
        visual("tableEx", 20, 370, 1240, 330,
               {"Values": [{"t": D, "c": "station_name"}, {"t": D, "c": "platform_label"},
                           {"t": D, "m": "Departures"}, {"t": D, "m": "Mean delay s"},
                           {"t": D, "m": "On time 6min %"}, {"t": D, "m": "Platform changes"}]},
               title="Platform pressure — 'unknown' means unallocated at poll time, not missing data"),
    ]))

    # ---- Delay evolution — only possible because the pipeline re-polls ----
    pages.append(page("ReportSectionEvolution", "Delay evolution", [
        visual("card", 20, ROW1, 300, CARD_H,
               {"Values": [{"t": D, "m": "Delay growth s"}]}, title="Mean delay growth (s)"),
        visual("card", 330, ROW1, 300, CARD_H,
               {"Values": [{"t": D, "m": "Deteriorated 5min+"}]}, title="Deteriorated by 5+ min"),
        visual("donutChart", 660, ROW1, 600, 300,
               {"Category": [{"t": D, "c": "delay_bucket"}],
                "Y": [{"t": D, "m": "Departures"}]},
               title="Where delays end up"),
        visual("tableEx", 20, 340, 1240, 360,
               {"Values": [{"t": D, "c": "station_name"}, {"t": D, "c": "vehicle_name"},
                           {"t": D, "c": "destination_name"},
                           {"t": D, "c": "scheduled_departure_local"},
                           {"t": D, "c": "delay_first_seen_s"}, {"t": D, "c": "delay_seconds"},
                           {"t": D, "c": "delay_growth_s"}, {"t": D, "c": "observation_count"}]},
               title="First reading against latest — visible only because each train is re-polled"),
    ]))

    # ---- Services & destinations -----------------------------------------
    pages.append(page("ReportSectionServices", "Services & destinations", [
        visual("scatterChart", 20, ROW1, 620, 330,
               {"Category": [{"t": D, "c": "vehicle_type"}],
                "X": [{"t": D, "m": "Departures"}],
                "Y": [{"t": D, "m": "Mean delay s"}]},
               title="Service class: volume against delay"),
        visual("clusteredBarChart", 660, ROW1, 600, 330,
               {"Category": [{"t": D, "c": "destination_name"}],
                "Y": [{"t": D, "m": "Departures"}]},
               title="Busiest destinations", sort=({"t": D, "m": "Departures"}, "desc")),
        visual("tableEx", 20, 370, 1240, 330,
               {"Values": [{"t": D, "c": "vehicle_type"}, {"t": D, "m": "Departures"},
                           {"t": D, "m": "Mean delay s"}, {"t": D, "m": "On time 6min %"},
                           {"t": D, "m": "Cancelled %"}]},
               title="By service class"),
    ]))

    # ---- Data quality & pipeline ------------------------------------------
    pages.append(page("ReportSectionQuality", "Data quality & pipeline", [
        visual("card", 20, ROW1, 300, CARD_H,
               {"Values": [{"t": Q, "c": "row_count", "agg": 0}]}, title="Rows"),
        visual("card", 330, ROW1, 300, CARD_H,
               {"Values": [{"t": Q, "c": "pct_platform_unknown", "agg": 1}]},
               title="Platform unknown %"),
        visual("card", 640, ROW1, 300, CARD_H,
               {"Values": [{"t": Q, "c": "pct_occupancy_unknown", "agg": 1}]},
               title="Occupancy unreported %"),
        visual("card", 950, ROW1, 310, CARD_H,
               {"Values": [{"t": Q, "c": "observed_once", "agg": 0}]}, title="Seen only once"),
        visual("tableEx", 20, 145, 1240, 285,
               {"Values": [{"t": P, "c": "station_name"}, {"t": P, "c": "last_run_status"},
                           {"t": P, "c": "last_run_departures"},
                           {"t": P, "c": "minutes_since_last_run"},
                           {"t": P, "c": "runs_last_24h"}, {"t": P, "c": "failures_last_24h"}]},
               title="Pipeline freshness by station"),
        visual("tableEx", 20, 445, 1240, 255,
               {"Values": [{"t": Q, "c": "distinct_dates", "agg": 0},
                           {"t": Q, "c": "distinct_stations", "agg": 0},
                           {"t": Q, "c": "avg_observations", "agg": 1},
                           {"t": Q, "c": "confirmed_departed", "agg": 0}]},
               title="Coverage"),
    ]))

    return pages


def report_definition(dataset_id: str) -> dict:
    def b64(obj) -> str:
        text = obj if isinstance(obj, str) else json.dumps(obj)
        return base64.b64encode(text.encode("utf-8")).decode()

    platform = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/"
                   "gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "Report", "displayName": REPORT_NAME},
        "config": {"version": "2.0", "logicalId": str(uuid.uuid4())},
    }
    # version 1.0 selects the LEGACY layout: one root report.json. The service
    # rewrites this to 4.0 itself once it has accepted the report.
    pbir = {"version": "1.0", "datasetReference": {"byConnection": {
        "connectionString": None, "pbiServiceModelId": None,
        "pbiModelVirtualServerName": "sobe_wowvirtualserver",
        "pbiModelDatabaseName": dataset_id, "name": "EntityDataSource",
        "connectionType": "pbiServiceXmlaStyleLive"}}}
    report = {"config": json.dumps({"version": "5.43", "themeCollection": {}}),
              "layoutOptimization": 0, "resourcePackages": [],
              "sections": build_pages(), "filters": "[]"}

    parts = {".platform": platform, "definition.pbir": pbir, "report.json": report}
    return {"displayName": REPORT_NAME, "type": "Report", "definition": {"parts": [
        {"path": path, "payload": b64(obj), "payloadType": "InlineBase64"}
        for path, obj in parts.items()]}}


# ==========================================================================
def push_rows(env: dict[str, str], pbi: str, dataset_id: str) -> int:
    try:
        import pymssql
    except ImportError:
        raise SystemExit("pymssql is required: pip install pymssql")

    login, password = env.get("BI_READER_LOGIN"), env.get("BI_READER_PASSWORD")
    if not (login and password):
        raise SystemExit("no BI_READER_* in the env file — run "
                         "python scripts/create_bi_reader.py first")

    connection = pymssql.connect(server=env["SQL_FQDN"], user=login, password=password,
                                 database=env["SQL_DATABASE"], timeout=600,
                                 login_timeout=180)
    total = 0
    with connection:
        cursor = connection.cursor(as_dict=True)
        for table_name, source, columns in TABLES:
            cursor.execute(f"SELECT {', '.join(columns)} FROM {source}")
            rows = cursor.fetchall()
            # Replace, never append: this is a snapshot, and appending would
            # double every row on the second run.
            call(pbi, "DELETE", f"{PBI_API}/datasets/{dataset_id}/tables/{table_name}/rows")
            for start in range(0, len(rows), ROWS_PER_REQUEST):
                chunk = rows[start:start + ROWS_PER_REQUEST]
                call(pbi, "POST",
                     f"{PBI_API}/datasets/{dataset_id}/tables/{table_name}/rows",
                     {"rows": [{k: jsonable(v) for k, v in row.items()} for row in chunk]})
            total += len(rows)
            print(f"    {table_name:<16} {len(rows):>6} rows")
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-only", action="store_true",
                        help="refresh the rows, leave the model and report alone")
    parser.add_argument("--keep-model", action="store_true",
                        help="reuse the existing model instead of rebuilding it")
    parser.add_argument("--url", action="store_true", help="print the report URL and exit")
    args = parser.parse_args(argv)

    pbi = token("https://analysis.windows.net/powerbi/api")

    if args.url:
        reports = call(pbi, "GET", f"{PBI_API}/reports").get("value", [])
        found = next((r for r in reports if r["name"] == REPORT_NAME), None)
        if not found:
            raise SystemExit("report not built yet — run without --url")
        print(found.get("webUrl")
              or f"https://app.powerbi.com/groups/me/reports/{found['id']}")
        return 0

    env = load_env()
    fabric = token("https://api.fabric.microsoft.com")
    workspace_id = my_workspace_id(fabric)
    print(f"  My Workspace: {workspace_id}")

    # ---- model ------------------------------------------------------------
    datasets = {d["name"]: d["id"]
                for d in call(pbi, "GET", f"{PBI_API}/datasets").get("value", [])}
    dataset_id = datasets.get(DATASET_NAME)

    # A push dataset's measures can only be set AT CREATION — there is no API to
    # add them afterwards — so a model that predates them has to be rebuilt. The
    # report is a separate item, so its URL survives this.
    if dataset_id and not (args.keep_model or args.data_only):
        call(pbi, "DELETE", f"{PBI_API}/datasets/{dataset_id}")
        print(f"  dropped the previous model ({dataset_id}) — measures are creation-time only")
        dataset_id = None

    if not dataset_id:
        payload = {"name": DATASET_NAME, "defaultMode": "Push",
                   "tables": [{"name": name,
                               "columns": [{"name": c, "dataType": t}
                                           for c, t in columns.items()],
                               "measures": list(MEASURES.get(name, ()))}
                              for name, _, columns in TABLES],
                   "relationships": list(RELATIONSHIPS)}
        dataset_id = call(pbi, "POST", f"{PBI_API}/datasets", payload)["id"]
        measures = sum(len(m) for m in MEASURES.values())
        print(f"  created model ({dataset_id}) — {len(TABLES)} tables, {measures} measures")
    else:
        print(f"  reusing model ({dataset_id})")

    # ---- data -------------------------------------------------------------
    print(f"  reading as {env.get('BI_READER_LOGIN')} (views only)")
    print(f"  pushed {push_rows(env, pbi, dataset_id):,} rows")

    if args.data_only:
        return 0

    # ---- report -----------------------------------------------------------
    definition = report_definition(dataset_id)
    pages = build_pages()
    items = call(fabric, "GET",
                 f"{FABRIC_API}/workspaces/{workspace_id}/items").get("value", [])
    existing = next((i for i in items if i.get("type") == "Report"
                     and i.get("displayName") == REPORT_NAME), None)

    if existing:
        headers, _ = call(fabric, "POST",
                          f"{FABRIC_API}/workspaces/{workspace_id}/items/"
                          f"{existing['id']}/updateDefinition",
                          {"definition": definition["definition"]}, raw=True)
        verb, report_id = "updated", existing["id"]
    else:
        headers, body = call(fabric, "POST",
                             f"{FABRIC_API}/workspaces/{workspace_id}/items",
                             definition, raw=True)
        verb, report_id = "created", (body or {}).get("id")

    operation = headers.get("x-ms-operation-id")
    if operation:
        wait_for_operation(fabric, operation, f"report {verb}")

    if not report_id:
        items = call(fabric, "GET",
                     f"{FABRIC_API}/workspaces/{workspace_id}/items").get("value", [])
        found = next((i for i in items if i.get("type") == "Report"
                      and i.get("displayName") == REPORT_NAME), None)
        report_id = (found or {}).get("id")

    visuals = sum(len(p["visualContainers"]) for p in pages)
    print(f"  {verb} report — {len(pages)} pages, {visuals} visuals")
    print()
    print(f"  https://app.powerbi.com/groups/me/reports/{report_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
