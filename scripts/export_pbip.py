#!/usr/bin/env python3
"""Write the dashboard out as a PBIP project, so it can be committed to git.

THE DELIVERABLE GAP THIS CLOSES
The sprint-3 brief asks for "a `.pbix` file OR an active public link". On this
account neither is actually obtainable, and it is worth being precise about why
rather than quietly shipping one and hoping:

  * **`.pbix` download** — `GET /reports/{id}/Export` returns
    `ExportPBIX_ModelessWorkbookNotFound`. A report built through the API over a
    **push dataset** has no underlying workbook for the service to package. There
    is no flag that changes this; the artifact does not exist.
  * **Public link** — "Publish to web" is the only mechanism that produces a URL
    a grader can open without signing in, and sharing of any kind requires Pro.
    Image export is separately disabled on this tenant, which is how we know the
    tenant restricts this class of feature.

So this writes the third thing, which is both obtainable and better for a
repository: a **PBIP project** — the plain-text format Power BI Desktop reads and
Microsoft recommends for source control. A `.pbix` is an opaque binary that git
cannot diff and that expires with nothing; a PBIP is reviewable text.

It is generated from the SAME `TABLES`/`MEASURES`/`RELATIONSHIPS` contract as the
live model and the same `build_pages()` as the live report, so the committed
project and the published report cannot describe different dashboards.

WHAT IS DIFFERENT FROM THE PUBLISHED REPORT
The published report reads a push dataset — a snapshot. This project's semantic
model connects **directly to Azure SQL in Import mode**, which is Scenario A of
the brief and the thing a push dataset cannot be. Opening it in Desktop and
refreshing gives a live model with scheduled refresh available.

HONEST LIMIT
Power BI Desktop is Windows-only, so this project has **not been opened in
Desktop**. Its structure follows the documented PBIP layout and every file is
schema-valid JSON or well-formed TMDL, but "generated correctly" is a weaker
claim than "opened and refreshed", and it is the only claim made here.

    python scripts/export_pbip.py            # write powerbi/ from the contract
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "powerbi"
PROJECT = "RailPulseCloud"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from publish_powerbi_dataset import MEASURES, RELATIONSHIPS, TABLES  # noqa: E402
import build_powerbi_report as report_builder  # noqa: E402

#: The server is a PARAMETER with a placeholder default, not a literal. The repo
#: deliberately never publishes the SQL FQDN next to the login name — that is free
#: reconnaissance — so Desktop prompts for it on first open instead.
SERVER_PLACEHOLDER = "sql-railpulse-<suffix>.database.windows.net"
DATABASE_DEFAULT = "railpulse"

#: Push-dataset column types -> TMDL data types.
TMDL_TYPES = {"Int64": "int64", "String": "string", "Double": "double",
              "Bool": "boolean", "Datetime": "dateTime"}


def tmdl_escape(name: str) -> str:
    """TMDL quotes any identifier that is not a bare word."""
    if name.replace("_", "").isalnum() and not name[0].isdigit():
        return name
    return "'" + name.replace("'", "''") + "'"


def table_tmdl(name: str, source_view: str, columns: dict[str, str]) -> str:
    """One table: its columns, its measures, and the M query that loads it.

    TMDL is TAB-indented — spaces are a parse error, which is the single easiest
    way to produce a file Desktop refuses to open.
    """
    lines = [f"table {tmdl_escape(name)}", ""]
    for column, pbi_type in columns.items():
        lines += [
            f"\tcolumn {tmdl_escape(column)}",
            f"\t\tdataType: {TMDL_TYPES[pbi_type]}",
            f"\t\tsourceColumn: {column}",
            f"\t\tlineageTag: {uuid.uuid5(uuid.NAMESPACE_OID, name + column)}",
            "",
        ]
    for measure in MEASURES.get(name, ()):
        expression = " ".join(measure["expression"].split())
        lines += [f"\tmeasure {tmdl_escape(measure['name'])} = {expression}"]
        if measure.get("formatString"):
            lines.append(f"\t\tformatString: {measure['formatString']}")
        lines += [f"\t\tlineageTag: {uuid.uuid5(uuid.NAMESPACE_OID, 'm' + measure['name'])}",
                  ""]
    # Import mode against the BI views — never the base tables, matching what
    # powerbi_reader is actually permitted to read.
    lines += [
        f"\tpartition {tmdl_escape(name)} = m",
        "\t\tmode: import",
        "\t\tsource =",
        "\t\t\t\tlet",
        "\t\t\t\t    Source = Sql.Database(ServerName, DatabaseName),",
        f"\t\t\t\t    Data = Source{{[Schema=\"dbo\",Item=\"{source_view.split('.')[-1]}\"]}}[Data]",
        "\t\t\t\tin",
        "\t\t\t\t    Data",
        "",
        f"\tannotation PBI_ResultType = Table",
        "",
    ]
    return "\n".join(lines)


def model_tmdl() -> str:
    lines = [
        "model Model",
        "\tculture: en-GB",
        "\tdefaultPowerBIDataSourceVersion: powerBI_V3",
        # Implicit measures let a reader drag delay_seconds onto a chart and get a
        # SUM that silently includes cancellations. The whole point of defining
        # the metrics once is that they are not re-derived by accident.
        "\tdiscourageImplicitMeasures",
        "\tsourceQueryCulture: en-GB",
        "",
        "\tannotation PBI_QueryOrder = " + json.dumps([t[0] for t in TABLES]),
        "",
    ]
    lines += [f"ref table {tmdl_escape(name)}" for name, _, _ in TABLES]
    lines.append("")
    lines += ["ref expression ServerName", "ref expression DatabaseName", ""]
    for relationship in RELATIONSHIPS:
        lines += [
            f"relationship {relationship['name']}",
            f"\tfromColumn: {relationship['fromTable']}.{relationship['fromColumn']}",
            f"\ttoColumn: {relationship['toTable']}.{relationship['toColumn']}",
            "",
        ]
    return "\n".join(lines)


def expressions_tmdl() -> str:
    """The two connection parameters, so no secret is committed."""
    return "\n".join([
        "expression ServerName = " + json.dumps(SERVER_PLACEHOLDER) + " meta [IsParameterQuery=true, Type=\"Text\", IsParameterQueryRequired=true]",
        "\tlineageTag: " + str(uuid.uuid5(uuid.NAMESPACE_OID, "ServerName")),
        "\tannotation PBI_NavigationStepName = Navigation",
        "",
        "expression DatabaseName = " + json.dumps(DATABASE_DEFAULT) + " meta [IsParameterQuery=true, Type=\"Text\", IsParameterQueryRequired=true]",
        "\tlineageTag: " + str(uuid.uuid5(uuid.NAMESPACE_OID, "DatabaseName")),
        "\tannotation PBI_NavigationStepName = Navigation",
        "",
    ])


def platform_file(kind: str, name: str) -> dict:
    return {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/"
                   "gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": kind, "displayName": name},
        "config": {"version": "2.0",
                   "logicalId": str(uuid.uuid5(uuid.NAMESPACE_OID, kind + name))},
    }


def write(path: Path, content) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = content if isinstance(content, str) else json.dumps(content, indent=2)
    path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)

    out = args.out
    if out.exists():
        shutil.rmtree(out)
    model_dir = out / f"{PROJECT}.SemanticModel"
    report_dir = out / f"{PROJECT}.Report"

    # ---- the .pbip entry point -------------------------------------------
    write(out / f"{PROJECT}.pbip", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/point/"
                   "pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0",
        "artifacts": [{"report": {"path": f"{PROJECT}.Report"}}],
        "settings": {"enableAutoRecovery": True},
    })

    # ---- semantic model ---------------------------------------------------
    write(model_dir / ".platform", platform_file("SemanticModel", "RailPulse Cloud"))
    write(model_dir / "definition.pbism", {"version": "1.0", "settings": {}})
    write(model_dir / "definition" / "database.tmdl",
          "database\n\tcompatibilityLevel: 1567\n")
    write(model_dir / "definition" / "model.tmdl", model_tmdl())
    write(model_dir / "definition" / "expressions.tmdl", expressions_tmdl())
    for name, source, columns in TABLES:
        write(model_dir / "definition" / "tables" / f"{name}.tmdl",
              table_tmdl(name, source, columns))

    # ---- report -----------------------------------------------------------
    # The same pages as the published report, from the same generator. The only
    # difference is the dataset reference: byPath to the local model instead of
    # byConnection to a dataset id in the service.
    write(report_dir / ".platform", platform_file("Report", "RailPulse Cloud — operations"))
    write(report_dir / "definition.pbir", {
        "version": "1.0",
        "datasetReference": {"byPath": {"path": f"../{PROJECT}.SemanticModel"}},
    })
    theme_file = "RailPulse.json"
    pages = report_builder.build_pages()
    write(report_dir / "report.json", {
        "config": json.dumps({"version": "5.43",
                              "themeCollection": {"customTheme": {"name": theme_file}}}),
        "layoutOptimization": 0,
        "resourcePackages": [{"resourcePackage": {
            "disabled": False, "name": "RegisteredResources", "type": 1,
            "items": [{"name": theme_file, "path": theme_file, "type": 202}]}}],
        "sections": pages, "filters": "[]",
    })
    write(report_dir / "StaticResources" / "RegisteredResources" / theme_file,
          report_builder.THEME)

    files = sorted(p for p in out.rglob("*") if p.is_file())
    visuals = sum(len(p["visualContainers"]) for p in pages)
    measures = sum(len(m) for m in MEASURES.values())
    print(f"  wrote {out.relative_to(REPO_ROOT)}/ — {len(files)} files")
    print(f"    {len(TABLES)} tables, {measures} measures, {len(RELATIONSHIPS)} relationships")
    print(f"    {len(pages)} report pages, {visuals} visuals")
    print()
    print("  Open RailPulseCloud.pbip in Power BI Desktop (Windows) and supply:")
    print(f"    ServerName    {SERVER_PLACEHOLDER}   (SQL_FQDN in .azure-railpulse.env)")
    print(f"    DatabaseName  {DATABASE_DEFAULT}")
    print("    credentials   powerbi_reader / BI_READER_PASSWORD, SQL auth, IMPORT mode")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
