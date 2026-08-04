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
    """Every (page, decoded config) pair for visuals that RUN A QUERY.

    Navigation buttons are excluded: an actionButton has no prototypeQuery and no
    projections, so the field-resolution checks below do not apply to it. It gets
    its own test instead.
    """
    for page in pages:
        for container in page["visualContainers"]:
            config = json.loads(container["config"])
            if "prototypeQuery" in config["singleVisual"]:
                yield page, config


def buttons_of(pages: list[dict]):
    """Every (page, decoded config) pair for navigation buttons."""
    for page in pages:
        for container in page["visualContainers"]:
            config = json.loads(container["config"])
            if config["singleVisual"].get("visualType") == "actionButton":
                yield page, config


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
    for name in ("On time 6min %", "On-Time Rate %"):
        assert "COUNTA" in by_name[name], f"{name} must use COUNTA as the denominator"
        assert "COUNTROWS" not in by_name[name], (
            f"{name} uses COUNTROWS, which counts cancellations into the denominator")
    for name in ("Mean delay s", "Mean delay min", "Delay minutes"):
        assert "is_canceled] = FALSE()" in by_name[name], (
            f"{name} must exclude cancellations")


def test_on_time_rate_uses_the_two_minute_threshold():
    """The brief defines on-time as under 2 minutes. The warehouse also carries a
    6-minute flag (the UIC threshold), and the two are easy to transpose — which
    would move the headline KPI by several points without any visible error."""
    by_name = {m["name"]: m["expression"] for m in MEASURES["departures"]}
    assert "is_on_time_2min" in by_name["On-Time Rate %"]
    assert "is_on_time_6min" not in by_name["On-Time Rate %"]
    assert "is_on_time_6min" in by_name["On time 6min %"]


def test_delay_minutes_is_a_sum_not_an_average():
    """The brief asks which train class ACCOUNTS FOR the most delayed minutes.

    That is a total. Ranking by the average gives a different — and also true —
    answer: InterCity dominates the total through volume while ICE has the worst
    mean. Silently substituting one for the other points the operator at the
    wrong class, so the report carries both and this pins which is which.
    """
    by_name = {m["name"]: m["expression"] for m in MEASURES["departures"]}
    assert "SUM(" in by_name["Delay minutes"]
    assert "AVERAGE" not in by_name["Delay minutes"]
    assert "AVERAGE" in by_name["Mean delay min"]


def test_hourly_measure_normalises_by_days_observed():
    """Ranking hours on a raw count reports the CAPTURE SCHEDULE as the peak.

    The timer samples weekday peak windows harder than the rest of the day, so
    `Departures` per hour is circular. Only `Departures per day` is meaningful,
    and the Peak hours page must use it.
    """
    by_name = {m["name"]: m["expression"] for m in MEASURES["departures"]}
    assert "DIVIDE([Departures], [Days observed])" in by_name["Departures per day"]

    peaks = next(p for p in build_powerbi_report.build_pages()
                 if p["displayName"] == "Rush hour matrix")
    charted = set()
    for container in peaks["visualContainers"]:
        single = json.loads(container["config"])["singleVisual"]
        if single.get("visualType") in ("tableEx", "slicer", "actionButton"):
            continue  # the table may show both, for comparison
        for entry in single["prototypeQuery"]["Select"]:
            if "Measure" in entry:
                charted.add(entry["Measure"]["Property"])
    assert "Departures" not in charted, (
        "a Rush hour chart plots the raw count, which reports the capture "
        "schedule as the peak")


# ==========================================================================
# structure
# ==========================================================================
def test_pages_are_the_agreed_set(pages):
    """A renamed or dropped page should be a deliberate edit here, not a silent
    divergence — the navigation buttons target these ids by name."""
    assert [page["displayName"] for page in pages] == [
        "Executive scorecard",
        "Rush hour matrix",
        "Train class breakdown",
        "Platform congestion",
        "Hub comparison",
        "Delay evolution",
        "Services & destinations",
        "Data quality & pipeline",
    ]


# ==========================================================================
# the sprint-3 brief's four required visuals
# ==========================================================================
def test_scorecard_leads_with_the_on_time_rate(pages):
    """Must-have 1: a high-level KPI tracking the network's On-Time Rate %.

    Checks it is not merely present but the LARGEST object on the page — the
    brief asks for visual hierarchy, and a headline KPI tied for smallest with
    six other cards is not one.
    """
    scorecard = next(p for p in pages if p["displayName"] == "Executive scorecard")
    cards = []
    for container in scorecard["visualContainers"]:
        config = json.loads(container["config"])
        single = config["singleVisual"]
        if single.get("visualType") != "card":
            continue
        measures = [e["Measure"]["Property"]
                    for e in single["prototypeQuery"]["Select"] if "Measure" in e]
        cards.append((container["width"] * container["height"], measures))
    assert cards, "no KPI cards on the scorecard"
    biggest_area, biggest_measures = max(cards)
    assert "On-Time Rate %" in biggest_measures, (
        "the largest card must be the On-Time Rate, not " + str(biggest_measures))
    others = [area for area, _ in cards if area != biggest_area]
    assert biggest_area > max(others), "the headline KPI is not visually dominant"


def test_rush_hour_page_plots_volume_against_delay_on_one_chart(pages):
    """Must-have 2: volume vs average delay across hours, to isolate bottlenecks.

    Two separate charts do not answer it — an hour is only a bottleneck when both
    are high, and that comparison has to happen on one pair of axes.
    """
    rush = next(p for p in pages if p["displayName"] == "Rush hour matrix")
    combos = []
    for container in rush["visualContainers"]:
        single = json.loads(container["config"])["singleVisual"]
        if "Combo" in single.get("visualType", ""):
            combos.append(single)
    assert combos, "the Rush hour page has no combo chart"
    combo = combos[0]
    assert "Y" in combo["projections"] and "Y2" in combo["projections"], (
        "the combo chart needs both a column axis and a secondary line axis")
    measures = [e["Measure"]["Property"]
                for e in combo["prototypeQuery"]["Select"] if "Measure" in e]
    assert any("delay" in m.lower() for m in measures), "no delay measure on the matrix"
    assert any("Departures" in m for m in measures), "no volume measure on the matrix"


def test_train_class_page_ranks_by_total_delayed_minutes(pages):
    """Must-have 3: which train category accounts for the most delayed minutes."""
    classes = next(p for p in pages if p["displayName"] == "Train class breakdown")
    charted = set()
    grouped_by = set()
    for container in classes["visualContainers"]:
        single = json.loads(container["config"])["singleVisual"]
        if "prototypeQuery" not in single:
            continue
        for entry in single["prototypeQuery"]["Select"]:
            if "Measure" in entry:
                charted.add(entry["Measure"]["Property"])
            elif "Column" in entry:
                grouped_by.add(entry["Column"]["Property"])
    assert "Delay minutes" in charted, "the page never shows total delayed minutes"
    assert "vehicle_type" in grouped_by, "the page never groups by train class"


def test_platform_page_can_be_filtered_to_one_station(pages):
    """Must-have 4: delays by platform, at a chosen station.

    Platform numbers are only meaningful within one station — 'platform 5' pooled
    across ten hubs averages unrelated tracks — so the slicer is load-bearing,
    not decoration.
    """
    platforms = next(p for p in pages if p["displayName"] == "Platform congestion")
    sliced = set()
    for container in platforms["visualContainers"]:
        single = json.loads(container["config"])["singleVisual"]
        if single.get("visualType") != "slicer":
            continue
        for entry in single["prototypeQuery"]["Select"]:
            if "Column" in entry:
                sliced.add(entry["Column"]["Property"])
    assert "station_name" in sliced, "no station slicer on the platform page"


def test_hub_comparison_page_has_slicers(pages):
    """Nice-to-have: multi-station comparison driven by slicers."""
    hubs = next(p for p in pages if p["displayName"] == "Hub comparison")
    slicers = [json.loads(c["config"])["singleVisual"] for c in hubs["visualContainers"]
               if json.loads(c["config"])["singleVisual"].get("visualType") == "slicer"]
    assert len(slicers) >= 2, "the comparison page needs more than one slicer"


# ==========================================================================
# navigation and theme
# ==========================================================================
def test_navigation_buttons_target_pages_that_exist(pages):
    """A button pointing at a section id that does not exist is accepted without
    complaint and simply does nothing when clicked — invisible until a
    stakeholder tries it in the meeting."""
    sections = {page["name"] for page in pages}
    broken = []
    for page, config in buttons_of(pages):
        link = config["singleVisual"]["vcObjects"]["visualLink"][0]["properties"]
        target = link["navigationSection"]["expr"]["Literal"]["Value"].strip("'")
        if target not in sections:
            broken.append(f"{page['displayName']} -> {target}")
    assert not broken, "navigation buttons pointing nowhere: " + ", ".join(broken)


def test_every_page_is_reachable_and_no_button_points_at_itself(pages):
    reachable = set()
    for page, config in buttons_of(pages):
        link = config["singleVisual"]["vcObjects"]["visualLink"][0]["properties"]
        target = link["navigationSection"]["expr"]["Literal"]["Value"].strip("'")
        assert target != page["name"], f"{page['displayName']} links to itself"
        reachable.add(target)
    assert reachable == {page["name"] for page in pages}, "some page has no way in"


def test_theme_is_registered_and_the_reference_resolves(parts, definition):
    """The service rewrites a resource item's name to its file name on upload, so
    a report referencing the item as 'RailPulse' ends up pointing at nothing and
    the theme silently does not apply."""
    theme_parts = [p for p in parts if p.startswith("StaticResources/")]
    assert theme_parts, "no theme resource part"
    report = json.loads(parts["report.json"])
    referenced = json.loads(report["config"])["themeCollection"]["customTheme"]["name"]
    package = report["resourcePackages"][0]["resourcePackage"]
    item = package["items"][0]
    assert item["name"] == referenced, (
        f"report references theme {referenced!r} but the item is named {item['name']!r}")
    assert theme_parts[0].endswith(item["path"])
    theme = json.loads(parts[theme_parts[0]])
    assert theme["dataColors"], "the theme defines no data colours"


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


# ==========================================================================
# the committed PBIP project
# ==========================================================================
# scripts/export_pbip.py writes powerbi/ as plain text so the dashboard can live
# in git. Two things about it are worth a test: that it stays in step with the
# published report (it is generated from the same contract, so drift would mean a
# bug rather than an edit), and that it never commits a credential.
PBIP_DIR = REPO_ROOT / "powerbi"
PBIP_PROJECT = "RailPulseCloud"

pbip_exists = pytest.mark.skipif(
    not PBIP_DIR.is_dir(), reason="run scripts/export_pbip.py first")


@pytest.fixture(scope="module")
def pbip_files() -> dict[str, str]:
    return {str(p.relative_to(PBIP_DIR)): p.read_text(encoding="utf-8")
            for p in sorted(PBIP_DIR.rglob("*")) if p.is_file()}


@pbip_exists
def test_pbip_has_the_layout_desktop_expects(pbip_files):
    for required in (f"{PBIP_PROJECT}.pbip",
                     f"{PBIP_PROJECT}.SemanticModel/definition.pbism",
                     f"{PBIP_PROJECT}.SemanticModel/definition/model.tmdl",
                     f"{PBIP_PROJECT}.SemanticModel/definition/database.tmdl",
                     f"{PBIP_PROJECT}.Report/definition.pbir",
                     f"{PBIP_PROJECT}.Report/report.json"):
        assert required in pbip_files, f"missing {required}"


@pbip_exists
def test_pbip_never_commits_a_credential(pbip_files):
    """The repo deliberately keeps the SQL FQDN and every password out of tracked
    files. This project is generated from an environment that HAS them, so the
    placeholder substitution is the only thing standing between the two."""
    secret_file = REPO_ROOT / ".azure-railpulse.env"
    secrets = []
    if secret_file.is_file():
        for line in secret_file.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.strip().startswith("#"):
                continue
            value = line.partition("=")[2].strip().strip("'")
            # short values (a database name, a plan tier) are not secrets and
            # would produce false positives
            if len(value) >= 12:
                secrets.append(value)
    leaked = [f"{path}: {value[:12]}..."
              for path, text in pbip_files.items()
              for value in secrets if value in text]
    assert not leaked, "secrets committed in powerbi/: " + ", ".join(leaked)


@pbip_exists
def test_pbip_model_declares_every_table_and_measure(pbip_files):
    model = pbip_files[f"{PBIP_PROJECT}.SemanticModel/definition/model.tmdl"]
    for name, _, _ in TABLES:
        assert f"ref table {name}" in model, f"{name} not referenced by the model"
        table_file = f"{PBIP_PROJECT}.SemanticModel/definition/tables/{name}.tmdl"
        assert table_file in pbip_files, f"missing {table_file}"
        body = pbip_files[table_file]
        for measure in MEASURES.get(name, ()):
            # TMDL quotes an identifier only when it is not a bare word, so
            # `Departures` is unquoted while `'On-Time Rate %'` is not.
            declared = (f"measure {measure['name']} =" in body
                        or f"measure '{measure['name']}' =" in body)
            assert declared, f"{measure['name']} missing from {name}.tmdl"


@pbip_exists
def test_pbip_tmdl_is_tab_indented(pbip_files):
    """TMDL rejects space indentation, and Desktop reports it as a corrupt file
    rather than as a formatting problem."""
    for path, text in pbip_files.items():
        if not path.endswith(".tmdl"):
            continue
        for number, line in enumerate(text.split("\n"), 1):
            assert not line.startswith(" "), f"{path}:{number} is space-indented"


@pbip_exists
def test_pbip_binds_to_the_local_model_not_a_service_dataset(pbip_files):
    """The published report binds byConnection to a push dataset; this one must
    bind byPath to the model in the folder next to it, or Desktop opens a report
    with no data."""
    pbir = json.loads(pbip_files[f"{PBIP_PROJECT}.Report/definition.pbir"])
    reference = pbir["datasetReference"]
    assert "byPath" in reference, "PBIP report must use byPath"
    assert "byConnection" not in reference
    assert reference["byPath"]["path"].endswith(".SemanticModel")


@pbip_exists
def test_pbip_report_matches_the_published_report(pbip_files, pages):
    """Both come from build_pages(); a difference means a bug, not an edit."""
    report = json.loads(pbip_files[f"{PBIP_PROJECT}.Report/report.json"])
    assert [s["displayName"] for s in report["sections"]] == \
           [p["displayName"] for p in pages]
    assert sum(len(s["visualContainers"]) for s in report["sections"]) == \
           sum(len(p["visualContainers"]) for p in pages)
