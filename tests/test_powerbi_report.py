"""Offline tests for the generated Power BI report.

The report in `scripts/build_powerbi_report.py` is assembled as JSON and handed to
the Fabric API, and almost everything that can go wrong with it fails **silently**
— the service accepts the payload, returns 200, and renders a blank or broken
page. Nothing catches that except opening the report in a browser, which is
exactly the manual step the generator exists to remove. So the invariants are
pinned here instead.

The four that actually bit during development:

* **`config` and `filters` must be JSON-encoded STRINGS**, not objects. The legacy
  PBIR layout double-encodes them. An object is accepted without complaint and
  produces an empty report.
* **`definition.pbir` must declare version 1.0 with a root-level `report.json`.**
  Version 1.0 selects the *legacy* layout; pairing it with the enhanced
  `definition/pages/...` part set fails with `MissingDefinitionParts`, and so does
  omitting the layout entirely. All three cases return the same message.
* **Every field a visual projects must exist in the model.** A typo'd column name
  is not validated at publish time; the visual just renders empty.
* **Every `queryRef` must resolve to a Select entry** of its own visual's
  prototypeQuery, or the projection points at nothing.

None of this needs Azure, Power BI or a network.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

build_powerbi_report = pytest.importorskip("build_powerbi_report")
from publish_powerbi_dataset import MEASURES, RELATIONSHIPS, TABLES  # noqa: E402


@pytest.fixture(scope="module")
def pages() -> list[dict]:
    return build_powerbi_report.build_pages()


@pytest.fixture(scope="module")
def definition() -> dict:
    return build_powerbi_report.report_definition(
        "00000000-0000-0000-0000-000000000000")


@pytest.fixture(scope="module")
def parts(definition) -> dict[str, str]:
    """The definition's parts, decoded back from base64 to raw text."""
    return {part["path"]: base64.b64decode(part["payload"]).decode("utf-8")
            for part in definition["definition"]["parts"]}


def visuals_of(pages: list[dict]):
    """Every (page, decoded visual config) pair in the report."""
    for page in pages:
        for container in page["visualContainers"]:
            yield page, json.loads(container["config"])


# ==========================================================================
# the part set — attempts 1 and 2 both failed here
# ==========================================================================
def test_uses_the_legacy_layout_with_a_root_report_json(parts):
    """`report.json` at the ROOT, not under `definition/`.

    The enhanced PBIR layout (`definition/pages/<id>/visuals/<id>/visual.json`)
    is rejected with `MissingDefinitionParts` when version is 1.0.
    """
    assert "report.json" in parts
    assert not any(path.startswith("definition/") for path in parts), (
        "enhanced-format parts are present; version 1.0 selects the legacy layout"
    )


def test_pbir_declares_version_one_and_binds_a_dataset(parts):
    pbir = json.loads(parts["definition.pbir"])
    assert pbir["version"] == "1.0", (
        "the legacy layout is selected by version 1.0; the service rewrites this "
        "to 4.0 itself after accepting the report"
    )
    connection = pbir["datasetReference"]["byConnection"]
    assert connection["pbiModelDatabaseName"]
    assert connection["connectionType"] == "pbiServiceXmlaStyleLive"


def test_platform_part_is_present_and_declares_a_report(parts):
    platform = json.loads(parts[".platform"])
    assert platform["metadata"]["type"] == "Report"
    assert platform["metadata"]["displayName"]


# ==========================================================================
# the double-encoding trap
# ==========================================================================
@pytest.mark.parametrize("key", ["config", "filters"])
def test_page_level_config_and_filters_are_json_strings(pages, key):
    for page in pages:
        assert isinstance(page[key], str), (
            f"page {page['displayName']!r}: {key} must be a JSON-encoded string; "
            "an object is accepted silently and renders a blank page"
        )
        json.loads(page[key])


@pytest.mark.parametrize("key", ["config", "filters"])
def test_visual_level_config_and_filters_are_json_strings(pages, key):
    for page in pages:
        for container in page["visualContainers"]:
            assert isinstance(container[key], str), (
                f"page {page['displayName']!r}: visual {key} must be a "
                "JSON-encoded string, not an object"
            )
            json.loads(container[key])


# ==========================================================================
# every reference must resolve against the model contract
# ==========================================================================
def test_every_column_reference_exists_in_the_model(pages):
    columns = {name: set(cols) for name, _, cols in TABLES}
    missing = []
    for page, config in visuals_of(pages):
        single = config["singleVisual"]
        prototype = single["prototypeQuery"]
        sources = {a["Name"]: a["Entity"] for a in prototype["From"]}
        for entry in prototype["Select"]:
            column = entry.get("Column") or (
                entry.get("Aggregation", {}).get("Expression", {}).get("Column"))
            if column is None:
                continue  # a measure, checked separately
            entity = sources[column["Expression"]["SourceRef"]["Source"]]
            if column["Property"] not in columns.get(entity, set()):
                missing.append(f"{page['displayName']}: {entity}.{column['Property']}")
    assert not missing, "columns referenced but not in the model: " + ", ".join(missing)


def test_every_measure_reference_exists_in_the_model(pages):
    defined = {table: {m["name"] for m in measures}
               for table, measures in MEASURES.items()}
    missing = []
    for page, config in visuals_of(pages):
        prototype = config["singleVisual"]["prototypeQuery"]
        sources = {a["Name"]: a["Entity"] for a in prototype["From"]}
        for entry in prototype["Select"]:
            measure = entry.get("Measure")
            if measure is None:
                continue
            entity = sources[measure["Expression"]["SourceRef"]["Source"]]
            if measure["Property"] not in defined.get(entity, set()):
                missing.append(f"{page['displayName']}: {entity}.{measure['Property']}")
    assert not missing, "measures referenced but not defined: " + ", ".join(missing)


def test_every_projection_resolves_to_a_select_entry(pages):
    """A `queryRef` that names nothing in the Select renders an empty bucket."""
    dangling = []
    for page, config in visuals_of(pages):
        single = config["singleVisual"]
        names = {entry["Name"] for entry in single["prototypeQuery"]["Select"]}
        for role, refs in single["projections"].items():
            for ref in refs:
                if ref["queryRef"] not in names:
                    dangling.append(f"{page['displayName']}/{role}: {ref['queryRef']}")
    assert not dangling, "projections with no matching Select: " + ", ".join(dangling)


def test_every_source_alias_used_is_declared_in_from(pages):
    unknown = []
    for page, config in visuals_of(pages):
        prototype = config["singleVisual"]["prototypeQuery"]
        declared = {alias["Name"] for alias in prototype["From"]}
        for entry in prototype["Select"]:
            node = (entry.get("Column") or entry.get("Measure")
                    or entry["Aggregation"]["Expression"]["Column"])
            source = node["Expression"]["SourceRef"]["Source"]
            if source not in declared:
                unknown.append(f"{page['displayName']}: {source}")
    assert not unknown, "source aliases not in From: " + ", ".join(unknown)


# ==========================================================================
# the measures themselves
# ==========================================================================
def test_measures_only_reference_columns_that_exist(the_model_columns=None):
    """A DAX measure naming a dropped column fails at query time, not at push."""
    import re
    columns = {name: set(cols) for name, _, cols in TABLES}
    measure_names = {m["name"] for ms in MEASURES.values() for m in ms}
    bad = []
    for table, measures in MEASURES.items():
        for measure in measures:
            for ref_table, ref_col in re.findall(r"'([^']+)'\[([^\]]+)\]",
                                                 measure["expression"]):
                if ref_col not in columns.get(ref_table, set()):
                    bad.append(f"{measure['name']}: '{ref_table}'[{ref_col}]")
            # [Other measure] references must resolve too
            for ref in re.findall(r"(?<!')\[([^\]]+)\]", measure["expression"]):
                if ref in columns.get(table, set()) or ref in measure_names:
                    continue
                bad.append(f"{measure['name']}: [{ref}]")
    assert not bad, "measures reference unknown fields: " + ", ".join(bad)


def test_punctuality_measures_exclude_cancellations():
    """The project's central definition: a cancelled train is absent, not late.

    The views store NULL in the punctuality flags for cancellations, so COUNTA is
    the correct denominator and COUNTROWS is not. Swapping one for the other
    would quietly change every punctuality figure in the report.
    """
    by_name = {m["name"]: m["expression"] for m in MEASURES["departures"]}
    for name in ("On time 6min %", "On time 2min %"):
        assert "COUNTA" in by_name[name], f"{name} must use COUNTA as the denominator"
        assert "COUNTROWS" not in by_name[name], (
            f"{name} uses COUNTROWS, which counts cancellations into the denominator")
    assert "is_canceled] = FALSE()" in by_name["Mean delay s"], (
        "mean delay must exclude cancellations")


def test_hourly_measure_normalises_by_days_observed():
    """Ranking hours on a raw count reports the CAPTURE SCHEDULE as the peak.

    The timer samples weekday peak windows harder than the rest of the day, so
    `Departures` per hour is circular. Only `Departures per day` is meaningful,
    and the Peak hours page must use it.
    """
    by_name = {m["name"]: m["expression"] for m in MEASURES["departures"]}
    assert "DIVIDE([Departures], [Days observed])" in by_name["Departures per day"]

    peaks = next(p for p in build_powerbi_report.build_pages()
                 if p["displayName"] == "Peak hours")
    charted = set()
    for container in peaks["visualContainers"]:
        config = json.loads(container["config"])
        single = config["singleVisual"]
        if single["visualType"] == "tableEx":
            continue  # the table may show both, for comparison
        for entry in single["prototypeQuery"]["Select"]:
            if "Measure" in entry:
                charted.add(entry["Measure"]["Property"])
    assert "Departures" not in charted, (
        "a Peak hours chart plots the raw count, which reports the capture "
        "schedule as the peak")


# ==========================================================================
# structure
# ==========================================================================
def test_pages_mirror_the_streamlit_dashboard(pages):
    """The report exists to mirror the Streamlit app; a renamed or dropped page
    should be a deliberate edit here, not a silent divergence."""
    assert [page["displayName"] for page in pages] == [
        "Overview",
        "Hub leaderboard",
        "Peak hours",
        "Platform bottlenecks",
        "Delay evolution",
        "Services & destinations",
        "Data quality & pipeline",
    ]


def test_page_names_and_visual_ids_are_unique(pages):
    names = [page["name"] for page in pages]
    assert len(names) == len(set(names)), "duplicate section names"
    ids = [config["name"] for _, config in visuals_of(pages)]
    assert len(ids) == len(set(ids)), "duplicate visual ids"


def test_visuals_stay_inside_the_canvas(pages):
    """Power BI does not clamp an out-of-bounds visual; it scrolls or hides it."""
    outside = []
    for page in pages:
        for container in page["visualContainers"]:
            if (container["x"] + container["width"] > page["width"]
                    or container["y"] + container["height"] > page["height"]):
                outside.append(f"{page['displayName']} at ({container['x']},"
                               f"{container['y']})")
    assert not outside, "visuals overflow the page: " + ", ".join(outside)


def test_relationship_endpoints_exist(pages):
    """A relationship naming a missing column is accepted at creation and then
    breaks every cross-table visual — which is most of the Peak hours page."""
    columns = {name: set(cols) for name, _, cols in TABLES}
    for relationship in RELATIONSHIPS:
        assert relationship["fromColumn"] in columns[relationship["fromTable"]]
        assert relationship["toColumn"] in columns[relationship["toTable"]]


def test_cross_table_visuals_have_a_relationship_to_join_on(pages):
    """A visual mixing two tables is resolved by the model relationships. Without
    one the visual renders the cartesian product, which looks like real data."""
    related = {frozenset((r["fromTable"], r["toTable"])) for r in RELATIONSHIPS}
    unjoined = []
    for page, config in visuals_of(pages):
        entities = {a["Entity"] for a in config["singleVisual"]["prototypeQuery"]["From"]}
        if len(entities) > 1 and frozenset(entities) not in related:
            unjoined.append(f"{page['displayName']}: {sorted(entities)}")
    assert not unjoined, "cross-table visuals with no relationship: " + ", ".join(unjoined)
