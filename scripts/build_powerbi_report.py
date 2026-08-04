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
           sort: tuple[dict, str] | None = None, color: str | None = None) -> dict:
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
            "text": {"expr": {"Literal": {"Value": f"'{title}'"}}},
            "fontColor": {"solid": {"color": {
                "expr": {"Literal": {"Value": f"'{INK}'"}}}}}}}]}
    # Per-visual colour as well as the report theme: the service rewrites the
    # theme resource's item name on upload, so the theme alone is not a safe bet.
    if color:
        fill = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{color}'"}}}}}
        single["objects"] = {"dataPoint": [{"properties": {"fill": fill}}]}
        if vtype == "card":
            single["objects"] = {"labels": [{"properties": {"color": fill}}]}

    # `config` is a JSON-encoded STRING here, not an object. An object is
    # accepted silently and renders an empty page.
    config = {"name": uuid.uuid4().hex,
              "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": 0,
                                                 "width": w, "height": h}}],
              "singleVisual": single}
    return {"x": x, "y": y, "z": 0, "width": w, "height": h,
            "config": json.dumps(config), "filters": "[]"}


def slicer(x: int, y: int, w: int, h: int, table: str, column: str,
           *, title: str | None = None) -> dict:
    """A slicer. Confirmed to survive a round-trip through the service.

    Slicers are how this report delivers the brief's cross-hub comparison and its
    "filter to Brussels-Central" platform question — one filtered page instead of
    ten near-identical ones.
    """
    return visual("slicer", x, y, w, h, {"Values": [{"t": table, "c": column}]},
                  title=title)


def button(x: int, y: int, w: int, h: int, label: str, target: str) -> dict:
    """A page-navigation button.

    Unlike every other visual this has NO prototypeQuery and no projections — it
    queries nothing. The generator and the tests both have to tolerate that.
    """
    single = {
        "visualType": "actionButton",
        "drillFilterOtherVisuals": True,
        "objects": {
            "text": [{"properties": {
                "text": {"expr": {"Literal": {"Value": f"'{label}'"}}},
                "fontColor": {"solid": {"color": {
                    "expr": {"Literal": {"Value": f"'{INK}'"}}}}},
                "fontSize": {"expr": {"Literal": {"Value": "10D"}}}}}],
            "icon": [{"properties": {
                "shapeType": {"expr": {"Literal": {"Value": "'blank'"}}}}}],
            "outline": [{"properties": {
                "lineColor": {"solid": {"color": {
                    "expr": {"Literal": {"Value": f"'{RULE}'"}}}}}}}],
        },
        "vcObjects": {"visualLink": [{"properties": {
            "type": {"expr": {"Literal": {"Value": "'PageNavigation'"}}},
            "navigationSection": {"expr": {"Literal": {"Value": f"'{target}'"}}}}}]},
    }
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
# The palette. Registered as a report theme AND applied per visual, because the
# service rewrites the theme resource's item name on upload and it is not worth
# betting the report's legibility on that resolving.
BRAND = "#0B6E4F"      # rail green — the neutral/positive series
ALERT = "#C1272D"      # delay and cancellation
WARN = "#F2A900"       # attention, second series
INK = "#1B3A5C"        # text
RULE = "#7A8B99"       # borders, gridlines

THEME = {
    "name": "RailPulse",
    "dataColors": [BRAND, ALERT, WARN, INK, RULE, "#4A9D7C", "#8C1A1F", "#B98600"],
    "background": "#FFFFFF",
    "foreground": INK,
    "tableAccent": BRAND,
    "good": BRAND,
    "bad": ALERT,
    "neutral": RULE,
}


# --------------------------------------------------------------------------
D, H, Q, P = "departures", "dim_hour", "data_quality", "pipeline_health"
CARD_H, ROW1 = 105, 20

#: Page ids, referenced by the navigation buttons. Kept as constants because a
#: button pointing at a section id that does not exist is accepted silently and
#: simply does nothing when clicked.
EXEC = "ReportSectionExec"
RUSH = "ReportSectionRush"
CLASS = "ReportSectionClass"
PLATFORM = "ReportSectionPlatforms"
HUBS = "ReportSectionHubs"
EVOLUTION = "ReportSectionEvolution"
SERVICES = "ReportSectionServices"
QUALITY = "ReportSectionQuality"

#: The nav bar, repeated on every page. Order is the reading order of the story:
#: how are we doing -> when does it break -> what breaks -> where -> who.
NAV = [(EXEC, "Scorecard"), (RUSH, "Rush hour"), (CLASS, "Train class"),
       (PLATFORM, "Platforms"), (HUBS, "Hubs"), (EVOLUTION, "Evolution"),
       (SERVICES, "Services"), (QUALITY, "Data quality")]


def nav_bar(current: str) -> list[dict]:
    """Page-navigation buttons along the foot of every page.

    A Power BI report's own page tabs are easy to miss in a browser, and the
    brief asks for a navigation system a stakeholder can actually use. The button
    for the current page is omitted rather than disabled, so the bar always shows
    where you can go rather than where you are.
    """
    targets = [(section, label) for section, label in NAV if section != current]
    width = 148
    return [button(20 + i * (width + 8), 668, width, 32, label, section)
            for i, (section, label) in enumerate(targets)]


def build_pages() -> list[dict]:
    """The report, one page per operational question.

    The first five pages are the sprint-3 brief's must-haves and cross-hub
    nice-to-have; the last three carry over the Streamlit dashboard's remaining
    analysis so the two stay in step.
    """
    pages: list[dict] = []

    # ======================================================================
    # 1. Executive scorecard  — brief must-have #1, the punctuality scorecard
    # ======================================================================
    # On-Time Rate % is deliberately the largest object on the page. It is the
    # one number the board asked for, and visual hierarchy should say so.
    scorecard = [
        visual("card", 20, ROW1, 400, 170,
               {"Values": [{"t": D, "m": "On-Time Rate %"}]},
               title="ON-TIME RATE — departures under 2 min late", color=BRAND),
        visual("card", 432, ROW1, 268, 80,
               {"Values": [{"t": D, "m": "Departures"}]}, title="Departures", color=INK),
        visual("card", 712, ROW1, 268, 80,
               {"Values": [{"t": D, "m": "Mean delay min"}]},
               title="Mean delay (min)", color=WARN),
        visual("card", 992, ROW1, 268, 80,
               {"Values": [{"t": D, "m": "Delay minutes"}]},
               title="Total delay (min)", color=ALERT),
        visual("card", 432, 110, 268, 80,
               {"Values": [{"t": D, "m": "On time 6min %"}]},
               title="On time at 6 min (UIC)", color=BRAND),
        visual("card", 712, 110, 268, 80,
               {"Values": [{"t": D, "m": "Cancellations"}]},
               title="Cancellations", color=ALERT),
        visual("card", 992, 110, 268, 80,
               {"Values": [{"t": D, "m": "Platform changes"}]},
               title="Platform changes", color=WARN),
        # The headline chart repeats the Rush hour page in miniature, because the
        # single most actionable fact — the evening peak is worse than the
        # morning — should not need a click to reach.
        visual("lineClusteredColumnComboChart", 20, 205, 780, 290,
               {"Category": [{"t": H, "c": "hour_label"}],
                "Y": [{"t": D, "m": "Departures per day"}],
                "Y2": [{"t": D, "m": "Mean delay min"}]},
               title="Volume and delay by hour — the evening peak runs worse than the morning"),
        visual("clusteredBarChart", 812, 205, 448, 290,
               {"Category": [{"t": D, "c": "station_name"}],
                "Y": [{"t": D, "m": "On-Time Rate %"}]},
               title="On-time rate by hub", color=BRAND,
               sort=({"t": D, "m": "On-Time Rate %"}, "desc")),
        visual("tableEx", 20, 505, 1240, 150,
               {"Values": [{"t": D, "c": "station_name"}, {"t": D, "m": "Departures"},
                           {"t": D, "m": "On-Time Rate %"}, {"t": D, "m": "Mean delay min"},
                           {"t": D, "m": "Delay minutes"}, {"t": D, "m": "Cancellations"}]},
               title="Network at a glance"),
    ]
    pages.append(page(EXEC, "Executive scorecard", scorecard + nav_bar(EXEC)))

    # ======================================================================
    # 2. Rush hour matrix  — brief must-have #2
    # ======================================================================
    # Volume against mean delay on one pair of axes, which is what isolates a
    # bottleneck interval: an hour is only a problem if BOTH are high. A busy
    # punctual hour and a quiet late hour need different responses.
    rush = [
        slicer(20, ROW1, 220, 300, D, "day_type", title="Day type"),
        slicer(20, 330, 220, 300, D, "station_name", title="Hub"),
        visual("lineClusteredColumnComboChart", 252, ROW1, 1008, 320,
               {"Category": [{"t": H, "c": "hour_label"}],
                "Y": [{"t": D, "m": "Departures per day"}],
                "Y2": [{"t": D, "m": "Mean delay min"}]},
               title="THE RUSH HOUR MATRIX — bars: departures per day observed · line: mean delay (min)"),
        visual("lineChart", 252, 350, 500, 280,
               {"Category": [{"t": H, "c": "hour_label"}],
                "Y": [{"t": D, "m": "On-Time Rate %"}]},
               title="On-time rate through the day", color=BRAND),
        visual("tableEx", 764, 350, 496, 280,
               {"Values": [{"t": H, "c": "hour_label"}, {"t": H, "c": "peak_window"},
                           {"t": D, "m": "Departures"}, {"t": D, "m": "Mean delay min"},
                           {"t": D, "m": "On-Time Rate %"}]},
               title="By hour — read Departures before trusting a rate"),
    ]
    pages.append(page(RUSH, "Rush hour matrix", rush + nav_bar(RUSH)))

    # ======================================================================
    # 3. Train class breakdown  — brief must-have #3
    # ======================================================================
    # The brief asks which class "accounts for the most delayed minutes", which
    # is a SUM. Ranking classes by their MEAN gives a different and equally true
    # answer, so both are on the page: InterCity dominates the total through
    # sheer volume, while the international classes are individually far worse.
    # Showing only one would point the operator at the wrong problem.
    classes = [
        slicer(20, ROW1, 220, 300, D, "vehicle_type", title="Train class"),
        slicer(20, 330, 220, 300, D, "day_type", title="Day type"),
        visual("clusteredBarChart", 252, ROW1, 500, 300,
               {"Category": [{"t": D, "c": "vehicle_type"}],
                "Y": [{"t": D, "m": "Delay minutes"}]},
               title="TOTAL delayed minutes by class — the brief's question",
               color=ALERT, sort=({"t": D, "m": "Delay minutes"}, "desc")),
        visual("clusteredBarChart", 764, ROW1, 496, 300,
               {"Category": [{"t": D, "c": "vehicle_type"}],
                "Y": [{"t": D, "m": "Mean delay min"}]},
               title="MEAN delay per train — a different ranking, equally true",
               color=WARN, sort=({"t": D, "m": "Mean delay min"}, "desc")),
        visual("lineClusteredColumnComboChart", 252, 330, 500, 300,
               {"Category": [{"t": D, "c": "vehicle_type"}],
                "Y": [{"t": D, "m": "Departures"}],
                "Y2": [{"t": D, "m": "On-Time Rate %"}]},
               title="Volume against on-time rate"),
        visual("tableEx", 764, 330, 496, 300,
               {"Values": [{"t": D, "c": "vehicle_type"}, {"t": D, "m": "Departures"},
                           {"t": D, "m": "Delay minutes"}, {"t": D, "m": "Share of delay %"},
                           {"t": D, "m": "Mean delay min"}, {"t": D, "m": "On-Time Rate %"}]},
               title="By class, with each one's share of all delay"),
    ]
    pages.append(page(CLASS, "Train class breakdown", classes + nav_bar(CLASS)))

    # ======================================================================
    # 4. Platform congestion  — brief must-have #4
    # ======================================================================
    # The station slicer is the point of this page: platform numbers are only
    # meaningful within one station, and "platform 5" pooled across ten hubs is
    # a meaningless average of unrelated tracks.
    platforms = [
        slicer(20, ROW1, 220, 300, D, "station_name", title="Station — pick one"),
        slicer(20, 330, 220, 300, D, "vehicle_type", title="Train class"),
        visual("clusteredBarChart", 252, ROW1, 500, 300,
               {"Category": [{"t": D, "c": "platform_label"}],
                "Y": [{"t": D, "m": "Mean delay min"}]},
               title="Mean delay by platform — slice to one station first",
               color=ALERT, sort=({"t": D, "m": "Mean delay min"}, "desc")),
        visual("clusteredColumnChart", 764, ROW1, 496, 300,
               {"Category": [{"t": D, "c": "platform_label"}],
                "Y": [{"t": D, "m": "Departures"}]},
               title="Traffic by platform", color=INK,
               sort=({"t": D, "m": "Departures"}, "desc")),
        visual("lineClusteredColumnComboChart", 252, 330, 500, 300,
               {"Category": [{"t": D, "c": "platform_label"}],
                "Y": [{"t": D, "m": "Departures"}],
                "Y2": [{"t": D, "m": "Mean delay min"}]},
               title="Load against delay — congestion is both at once"),
        visual("tableEx", 764, 330, 496, 300,
               {"Values": [{"t": D, "c": "station_name"}, {"t": D, "c": "platform_label"},
                           {"t": D, "m": "Departures"}, {"t": D, "m": "Mean delay min"},
                           {"t": D, "m": "Delay minutes"}, {"t": D, "m": "On-Time Rate %"}]},
               title="'unknown' means unallocated at poll time, not missing data"),
    ]
    pages.append(page(PLATFORM, "Platform congestion", platforms + nav_bar(PLATFORM)))

    # ======================================================================
    # 5. Hub comparison  — nice-to-have: cross-hub, slicer-driven
    # ======================================================================
    # Mean delay and on-time rate disagree here, and that disagreement is the
    # insight: Liege has a high mean delay but a good on-time rate (few, severe),
    # while Brussels-Central has a lower mean and the worst rate (many, small).
    # Those two failure modes need different operational answers.
    hubs = [
        slicer(20, ROW1, 220, 240, D, "station_name", title="Hubs to compare"),
        slicer(20, 270, 220, 180, D, "day_type", title="Day type"),
        slicer(20, 460, 220, 170, D, "peak_window", title="Peak window"),
        visual("clusteredBarChart", 252, ROW1, 500, 300,
               {"Category": [{"t": D, "c": "station_name"}],
                "Y": [{"t": D, "m": "On-Time Rate %"}]},
               title="On-time rate — many small delays show up here", color=BRAND,
               sort=({"t": D, "m": "On-Time Rate %"}, "desc")),
        visual("clusteredBarChart", 764, ROW1, 496, 300,
               {"Category": [{"t": D, "c": "station_name"}],
                "Y": [{"t": D, "m": "Mean delay min"}]},
               title="Mean delay — a few severe delays show up here", color=ALERT,
               sort=({"t": D, "m": "Mean delay min"}, "desc")),
        visual("scatterChart", 252, 330, 500, 300,
               {"Category": [{"t": D, "c": "station_name"}],
                "X": [{"t": D, "m": "Departures"}],
                "Y": [{"t": D, "m": "Mean delay min"}]},
               title="Volume against delay"),
        visual("tableEx", 764, 330, 496, 300,
               {"Values": [{"t": D, "c": "station_name"}, {"t": D, "m": "Departures"},
                           {"t": D, "m": "On-Time Rate %"}, {"t": D, "m": "Mean delay min"},
                           {"t": D, "m": "Delay minutes"}, {"t": D, "m": "Share of delay %"},
                           {"t": D, "m": "Reliability score"}]},
               title="Hub leaderboard"),
    ]
    pages.append(page(HUBS, "Hub comparison", hubs + nav_bar(HUBS)))

    # ======================================================================
    # 6. Delay evolution — only possible because the pipeline re-polls
    # ======================================================================
    evolution = [
        visual("card", 20, ROW1, 300, CARD_H,
               {"Values": [{"t": D, "m": "Delay growth s"}]},
               title="Mean delay growth (s)", color=ALERT),
        visual("card", 330, ROW1, 300, CARD_H,
               {"Values": [{"t": D, "m": "Deteriorated 5min+"}]},
               title="Deteriorated by 5+ min", color=ALERT),
        visual("donutChart", 660, ROW1, 600, 300,
               {"Category": [{"t": D, "c": "delay_bucket"}],
                "Y": [{"t": D, "m": "Departures"}]},
               title="Where delays end up"),
        visual("tableEx", 20, 340, 1240, 310,
               {"Values": [{"t": D, "c": "station_name"}, {"t": D, "c": "vehicle_name"},
                           {"t": D, "c": "destination_name"},
                           {"t": D, "c": "scheduled_departure_local"},
                           {"t": D, "c": "delay_first_seen_s"}, {"t": D, "c": "delay_seconds"},
                           {"t": D, "c": "delay_growth_s"}, {"t": D, "c": "observation_count"}]},
               title="First reading against latest — visible only because each train is re-polled"),
    ]
    pages.append(page(EVOLUTION, "Delay evolution", evolution + nav_bar(EVOLUTION)))

    # ======================================================================
    # 7. Services & destinations
    # ======================================================================
    services = [
        slicer(20, ROW1, 220, 300, D, "destination_name", title="Destination"),
        visual("clusteredBarChart", 252, ROW1, 1008, 300,
               {"Category": [{"t": D, "c": "destination_name"}],
                "Y": [{"t": D, "m": "Departures"}]},
               title="Busiest destinations", color=INK,
               sort=({"t": D, "m": "Departures"}, "desc")),
        visual("scatterChart", 20, 330, 620, 300,
               {"Category": [{"t": D, "c": "destination_name"}],
                "X": [{"t": D, "m": "Departures"}],
                "Y": [{"t": D, "m": "Mean delay min"}]},
               title="Destination: volume against delay"),
        visual("tableEx", 660, 330, 600, 300,
               {"Values": [{"t": D, "c": "destination_name"}, {"t": D, "m": "Departures"},
                           {"t": D, "m": "Mean delay min"}, {"t": D, "m": "On-Time Rate %"},
                           {"t": D, "m": "Cancelled %"}]},
               title="By destination"),
    ]
    pages.append(page(SERVICES, "Services & destinations", services + nav_bar(SERVICES)))

    # ======================================================================
    # 8. Data quality & pipeline
    # ======================================================================
    # On the report rather than in an appendix: the capture window is partial by
    # design, and a reader who does not know that will over-read every other page.
    quality = [
        visual("card", 20, ROW1, 300, CARD_H,
               {"Values": [{"t": Q, "c": "row_count", "agg": 0}]}, title="Rows", color=INK),
        visual("card", 330, ROW1, 300, CARD_H,
               {"Values": [{"t": Q, "c": "pct_platform_unknown", "agg": 1}]},
               title="Platform unknown %", color=WARN),
        visual("card", 640, ROW1, 300, CARD_H,
               {"Values": [{"t": Q, "c": "pct_occupancy_unknown", "agg": 1}]},
               title="Occupancy unreported %", color=WARN),
        visual("card", 950, ROW1, 310, CARD_H,
               {"Values": [{"t": Q, "c": "observed_once", "agg": 0}]},
               title="Seen only once", color=RULE),
        visual("tableEx", 20, 145, 1240, 250,
               {"Values": [{"t": P, "c": "station_name"}, {"t": P, "c": "last_run_status"},
                           {"t": P, "c": "last_run_departures"},
                           {"t": P, "c": "minutes_since_last_run"},
                           {"t": P, "c": "runs_last_24h"}, {"t": P, "c": "failures_last_24h"}]},
               title="Pipeline freshness by station"),
        visual("tableEx", 20, 405, 1240, 245,
               {"Values": [{"t": Q, "c": "distinct_dates", "agg": 0},
                           {"t": Q, "c": "distinct_stations", "agg": 0},
                           {"t": Q, "c": "avg_observations", "agg": 1},
                           {"t": Q, "c": "confirmed_departed", "agg": 0}]},
               title="Coverage — the pipeline samples weekday peaks only, by design"),
    ]
    pages.append(page(QUALITY, "Data quality & pipeline", quality + nav_bar(QUALITY)))

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
    # The custom theme is a REGISTERED RESOURCE: a separate part, referenced from
    # report.json by resource-item name. The service rewrites that item name to
    # the file name on upload, so both are "RailPulse.json" here — naming the item
    # "RailPulse" leaves the reference pointing at something that no longer
    # exists, and the theme silently does not apply. Every visual also carries its
    # own colour, so the palette holds either way.
    theme_file = "RailPulse.json"
    report = {
        "config": json.dumps({"version": "5.43",
                              "themeCollection": {"customTheme": {"name": theme_file}}}),
        "layoutOptimization": 0,
        "resourcePackages": [{"resourcePackage": {
            "disabled": False, "name": "RegisteredResources", "type": 1,
            "items": [{"name": theme_file, "path": theme_file, "type": 202}]}}],
        "sections": build_pages(), "filters": "[]",
    }

    parts = {".platform": platform, "definition.pbir": pbir, "report.json": report,
             f"StaticResources/RegisteredResources/{theme_file}": THEME}
    return {"displayName": REPORT_NAME, "type": "Report", "definition": {"parts": [
        {"path": path, "payload": b64(obj), "payloadType": "InlineBase64"}
        for path, obj in parts.items()]}}


# ==========================================================================
def model_is_current(pbi: str, dataset_id: str) -> bool:
    """Does the existing model already define every measure this report needs?

    This matters more than it looks. A push dataset's measures can only be set at
    creation, so a changed measure set means dropping and recreating the dataset —
    and **deleting a push dataset cascade-deletes every report bound to it**. The
    report is then rebuilt with a NEW id, so the URL changes. For a dashboard whose
    whole deliverable is a link someone has bookmarked, silently churning that link
    on every run is the wrong default.

    So: ask Power BI's own DAX engine to evaluate all of them at once. If the query
    succeeds the model is current and can be reused, leaving the report — and its
    URL — untouched. If a measure is missing the query fails and the rebuild is
    genuinely necessary.
    """
    names = [m["name"] for measures in MEASURES.values() for m in measures]
    row = ", ".join(f'"m{i}", [{name}]' for i, name in enumerate(names))
    try:
        result = call(pbi, "POST",
                      f"{PBI_API}/datasets/{dataset_id}/executeQueries",
                      {"queries": [{"query": f"EVALUATE ROW({row})"}]}, attempts=1)
    except SystemExit:
        return False
    return bool(result.get("results"))


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
    parser.add_argument("--rebuild-model", action="store_true",
                        help="drop and recreate the model even if it is current "
                             "(this changes the report URL — see model_is_current)")
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
    # add them afterwards — so a changed measure set forces a rebuild. Dropping
    # the dataset also destroys every report bound to it, which changes the
    # report URL, so it is only done when the model is genuinely out of date.
    if dataset_id and not args.data_only:
        if args.rebuild_model or not model_is_current(pbi, dataset_id):
            reason = "forced" if args.rebuild_model else "measures are out of date"
            call(pbi, "DELETE", f"{PBI_API}/datasets/{dataset_id}")
            print(f"  dropped the previous model ({reason}) — the report URL will change")
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
