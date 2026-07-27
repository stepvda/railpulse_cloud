#!/usr/bin/env python3
"""Run the pipeline from a laptop instead of from the Function App.

WHY THIS EXISTS
Three jobs the deployed app cannot do as conveniently:

  * **Debug a change before deploying it.** A two-minute deploy cycle is a poor
    way to find a typo in a MERGE. This runs the identical code path against the
    identical database, with a Python traceback instead of a 500.
  * **Run the analysis queries** in sql/analysis/ and print or export the
    results — the numbers that go in the report.
  * **Apply the schema** without an HTTP round trip.

WHAT IT NEEDS THAT THE FUNCTION APP DOES NOT
An ODBC driver on this machine, and a firewall rule for this machine's IP
(provision.sh adds one). The Azure Functions runtime image ships the driver;
macOS does not:

    brew tap microsoft/mssql-release https://github.com/Microsoft/homebrew-mssql-release
    brew update && HOMEBREW_ACCEPT_EULA=Y brew install msodbcsql18

If you would rather not install it, there are three no-install alternatives:

  * the deployed endpoints — POST /api/migrate for the schema, POST /api/ingest
    for a poll;
  * the Azure portal's Query editor, or the VS Code **mssql** extension, which
    bundles its own driver, for the analysis SQL;
  * `pip install pymssql` — a pure-wheel client with TDS statically bundled, so
    it needs no system driver and no Homebrew tap. It was used during development
    to iterate on the schema against the live database in seconds rather than in
    4-minute redeploy cycles. Note its paramstyle is `%s`, not `?`, so it is a
    convenience for ad-hoc SQL and not a drop-in for this module.

    python scripts/local_cli.py migrate
    python scripts/local_cli.py seed-stations
    python scripts/local_cli.py ingest --station Brussels-Central
    python scripts/local_cli.py ingest --hubs
    python scripts/local_cli.py verify
    python scripts/local_cli.py query sql/analysis/a1_peak_hour.sql --csv output/
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# The deployment root, so imports match what runs in Azure (see tests/conftest.py).
sys.path.insert(0, str(REPO_ROOT / "function_app"))

from railpulse import config, database, hubs, migrations, pipeline, reporting  # noqa: E402


def _banner(text: str) -> None:
    print(f"\n\033[1;36m==> {text}\033[0m")


def _require_driver() -> None:
    """Fail with the fix, not just the symptom."""
    import pyodbc

    if not pyodbc.drivers():
        raise SystemExit(
            "No ODBC drivers are registered on this machine, so pyodbc cannot "
            "connect.\n"
            "  brew tap microsoft/mssql-release "
            "https://github.com/Microsoft/homebrew-mssql-release\n"
            "  brew update && HOMEBREW_ACCEPT_EULA=Y brew install msodbcsql18\n"
            "Or use the deployed endpoints instead — see the module docstring."
        )


# ==========================================================================
# Commands
# ==========================================================================
def cmd_migrate(_args: argparse.Namespace) -> int:
    _banner(f"Applying {len(migrations.MIGRATION_FILES)} migration file(s)")
    print(f"    sql directory: {migrations.sql_directory()}")
    print(f"    target:        {config.describe_sql_target()}")

    def _apply(connection):
        with database.transaction(connection) as cursor:
            return migrations.apply_all(cursor)

    for result in database.run_with_retry(_apply):
        state = "skipped" if result.skipped else f"{result.batches} batches"
        print(f"    {result.file_name:<24} {state}"
              + (f"  ({result.reason})" if result.reason else ""))
    return 0


def cmd_seed_stations(_args: argparse.Namespace) -> int:
    _banner("Seeding the station catalogue from iRail")
    summary = pipeline.seed_station_catalogue(trigger_source="local")
    print(json.dumps(summary, indent=2))
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    if args.hubs:
        stations = [hub.station_id for hub in hubs.configured_hubs()]
        _banner(f"Ingesting {len(stations)} configured hub(s)")
    else:
        stations = [args.station]
        _banner(f"Ingesting {args.station}")

    result = pipeline.ingest_stations(stations, trigger_source="local")

    print(f"\n    {'station':<30} {'ret':>5} {'new':>6} {'rev':>6} "
          f"{'ms':>7}  status")
    print(f"    {'-' * 30} {'-' * 5} {'-' * 6} {'-' * 6} {'-' * 7}  ------")
    for station in result.stations:
        label = (station.station_name or station.requested)[:30]
        print(f"    {label:<30} {station.departures_returned:>5} "
              f"{station.rows_inserted:>6} {station.rows_updated:>6} "
              f"{station.duration_ms:>7}  {station.status}")
        if station.error:
            print(f"        ! {station.error}")
    print(f"\n    totals: {result.departures_returned} departures returned, "
          f"{result.rows_inserted} new, {result.rows_updated} revised, "
          f"{result.failed} station(s) failed")
    return 0 if result.failed == 0 else 1


def cmd_verify(_args: argparse.Namespace) -> int:
    """Print what is in the warehouse and how fresh it is."""
    _banner("Warehouse contents")

    def _read(connection):
        cursor = connection.cursor()
        try:
            return {
                "counts": reporting.table_counts(cursor),
                "quality": reporting.data_quality(cursor),
                "health": reporting.ingestion_health(cursor),
                "punctuality": reporting.station_punctuality(cursor),
                "runs": reporting.recent_runs(cursor, limit=10),
            }
        finally:
            cursor.close()

    snapshot = database.run_with_retry(_read)

    for table, count in snapshot["counts"].items():
        print(f"    {table:<22} {count:>9,}")

    quality = snapshot["quality"]
    if quality:
        _banner("Data quality")
        for key in ("row_count", "distinct_dates", "distinct_stations",
                    "earliest_departure_local", "latest_departure_local",
                    "pct_platform_unknown", "pct_occupancy_unknown",
                    "observed_once", "avg_observations", "confirmed_departed"):
            if key in quality:
                print(f"    {key:<26} {quality[key]}")

    if snapshot["punctuality"]:
        _banner("Punctuality by station")
        print(f"    {'station':<30} {'departures':>10} {'avg delay s':>12} "
              f"{'on time %':>10} {'days':>5}")
        for row in snapshot["punctuality"]:
            print(f"    {str(row['station_name'])[:30]:<30} "
                  f"{row['departures']:>10} "
                  f"{str(row['avg_delay_seconds']):>12} "
                  f"{str(row['pct_on_time_6min']):>10} "
                  f"{row['days_covered']:>5}")

    if snapshot["health"]:
        _banner("Freshness")
        for row in snapshot["health"]:
            flag = "STALE" if row.get("is_stale") else "ok"
            print(f"    {str(row['station_name'])[:30]:<30} "
                  f"{row['last_run_status']:<8} "
                  f"{row['minutes_since_last_run']:>5} min ago  {flag}")
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    """Run a .sql file and print every result set it produces.

    Analysis files hold several statements on purpose (one question, several
    angles), so this walks the cursor with nextset() rather than assuming one
    result — the same reason database.first_result_set exists.
    """
    path = Path(args.file)
    if not path.is_file():
        path = REPO_ROOT / args.file
    if not path.is_file():
        raise SystemExit(f"no such file: {args.file}")

    script = path.read_text(encoding="utf-8")
    _banner(f"Running {path.name}")

    def _run(connection):
        cursor = connection.cursor()
        results = []
        try:
            # Batches, because an analysis file may use GO; and each batch may
            # itself return several result sets.
            for batch in database.split_batches(script):
                cursor.execute(batch)
                while True:
                    if cursor.description is not None:
                        columns = [column[0] for column in cursor.description]
                        results.append((columns, cursor.fetchall()))
                    if not cursor.nextset():
                        break
            return results
        finally:
            cursor.close()

    for index, (columns, rows) in enumerate(database.run_with_retry(_run), start=1):
        print(f"\n    -- result set {index}: {len(rows)} row(s) "
              f"-------------------------")
        widths = [max(len(str(column)), 12) for column in columns]
        for row in rows[:args.limit]:
            for position, value in enumerate(row):
                widths[position] = max(widths[position], len(str(value)))
        print("    " + " | ".join(str(c).ljust(w) for c, w in zip(columns, widths)))
        print("    " + "-+-".join("-" * w for w in widths))
        for row in rows[:args.limit]:
            print("    " + " | ".join(
                str(v).ljust(w) for v, w in zip(row, widths)))
        if len(rows) > args.limit:
            print(f"    ... {len(rows) - args.limit} more row(s)")

        if args.csv:
            out_dir = Path(args.csv)
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"{path.stem}_{index}.csv"
            with out_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(columns)
                writer.writerows([list(row) for row in rows])
            print(f"    -> {out_path}")
    return 0


# ==========================================================================
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="local_cli",
        description="Run the RailPulse Cloud pipeline locally against Azure SQL.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply sql/*.sql").set_defaults(func=cmd_migrate)
    sub.add_parser("seed-stations",
                   help="load iRail's ~714-station catalogue"
                   ).set_defaults(func=cmd_seed_stations)

    ingest = sub.add_parser("ingest", help="poll liveboards and load them")
    group = ingest.add_mutually_exclusive_group()
    group.add_argument("--station", default="BE.NMBS.008813003",
                       help="station id or name (default: Brussels-Central)")
    group.add_argument("--hubs", action="store_true",
                       help="poll every configured hub instead")
    ingest.set_defaults(func=cmd_ingest)

    sub.add_parser("verify", help="print counts, quality and freshness"
                   ).set_defaults(func=cmd_verify)

    query = sub.add_parser("query", help="run a .sql file and print the results")
    query.add_argument("file", help="path to a .sql file")
    query.add_argument("--limit", type=int, default=25,
                       help="rows to print per result set (default 25)")
    query.add_argument("--csv", metavar="DIR",
                       help="also write each result set to DIR as CSV")
    query.set_defaults(func=cmd_query)

    args = parser.parse_args(argv)
    _require_driver()
    if not config.SQL_CONNECTION_STRING:
        raise SystemExit(
            "SQL_CONNECTION_STRING is not set.\n"
            "  set -a && source .azure-railpulse.env && set +a\n"
            "(that file is written by azure/provision.sh)"
        )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
