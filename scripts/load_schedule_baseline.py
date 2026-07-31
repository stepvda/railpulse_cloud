#!/usr/bin/env python3
"""Load sprint 1's static timetable into the cloud warehouse as a baseline.

WHY
The cloud warehouse is pure observation: a row exists because the pipeline saw a
departure. So it cannot answer the question that governs how every other figure
should be read — when an hour is empty, was there no train, or was nobody
looking? The static GTFS timetable is that missing denominator.

WHAT IT LOADS, AND WHAT IT REFUSES TO
Not the 2.17 M-row timetable. Only the slice that can actually be compared:

    the polled hubs  x  the dates the pipeline already has observations for

which is a few thousand rows per day rather than millions. The Azure SQL database
is capped at 2 GB and its cost model depends on staying small; loading a year of
national timetable to compare against four days of observation would be both
expensive and pointless.

THE JOIN BETWEEN TWO ID SYSTEMS
    sprint 1 (GTFS)   gs:nmbssncb:S8813003
    sprint 2 (iRail)  BE.NMBS.008813003
Both embed the UIC code, which is why `stations.uic_code` is a column of its own.
This script resolves GTFS -> UIC -> iRail using the `stations` table already in
the cloud database, so nothing is hard-coded and a hub added later resolves
automatically.

    python scripts/load_schedule_baseline.py                 # dates we observe
    python scripts/load_schedule_baseline.py --dates 2026-07-30,2026-07-31
    python scripts/load_schedule_baseline.py --dry-run       # extract, don't write
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SECRET_FILE = Path(os.environ.get("SECRET_FILE", REPO_ROOT / ".azure-railpulse.env"))

#: Sprint 1's build. Overridable because it lives in a sibling repository.
SPRINT1_DB = Path(os.environ.get(
    "RAILPULSE_SQLITE",
    REPO_ROOT.parent / "railpulse_sql_analysis" / "data" / "railpulse.db",
))

#: Rows per INSERT. SQL Server caps a statement at 2 100 parameters and this
#: table binds 10 columns, so 200 rows is the largest safe multiple.
ROWS_PER_INSERT = 200


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


def uic_from_gtfs(station_id: str) -> str | None:
    """'gs:nmbssncb:S8813003' -> '008813003'.

    Returns None for the stops whose id carries no UIC code at all — the feed
    uses name-based ids for some foreign and bus stops ('nmbssncb:s-baisieuxfr').
    Those simply cannot be matched, and are skipped rather than guessed at.
    """
    digits = re.sub(r"\D", "", station_id or "")
    if len(digits) < 6:
        return None
    return digits.rjust(9, "0")[:9]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dates", help="comma-separated YYYY-MM-DD; default: the "
                                        "dates the warehouse already observes")
    parser.add_argument("--dry-run", action="store_true",
                        help="extract and report, write nothing")
    parser.add_argument("--sqlite", type=Path, default=SPRINT1_DB)
    args = parser.parse_args(argv)

    if not args.sqlite.is_file():
        raise SystemExit(
            f"sprint 1's database not found at {args.sqlite}\n"
            "Set RAILPULSE_SQLITE, or rebuild it in the railpulse_sql_analysis repo."
        )

    try:
        import pymssql
    except ImportError:
        raise SystemExit("pymssql is required: pip install pymssql")

    env = load_env()
    cloud = pymssql.connect(
        server=env["SQL_FQDN"], user=env["SQL_ADMIN_USER"],
        password=env["SQL_ADMIN_PASSWORD"], database=env["SQL_DATABASE"],
        timeout=600, login_timeout=180,
    )
    cursor = cloud.cursor()

    # ---- which hubs, and what are their UIC codes -------------------------
    cursor.execute("SELECT station_id, uic_code, name FROM dbo.stations WHERE is_hub = 1")
    hubs = {row[1]: (row[0], row[2]) for row in cursor.fetchall()}
    if not hubs:
        raise SystemExit("no hub stations in the warehouse — run the ingest first")
    print(f"  {len(hubs)} hub(s) in the warehouse")

    # ---- which dates are worth loading ------------------------------------
    if args.dates:
        dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    else:
        cursor.execute("SELECT DISTINCT departure_date_local FROM dbo.liveboard_records "
                       "ORDER BY departure_date_local")
        dates = [row[0].isoformat() for row in cursor.fetchall()]
    if not dates:
        raise SystemExit("no observed dates yet — run the ingest first")
    print(f"  loading the timetable for {len(dates)} date(s): {', '.join(dates)}")

    # ---- pull the matching slice out of sprint 1's SQLite ------------------
    sqlite_conn = sqlite3.connect(f"file:{args.sqlite}?mode=ro", uri=True)
    sqlite_conn.row_factory = sqlite3.Row

    feed_version = ""
    try:
        row = sqlite_conn.execute(
            "SELECT feed_version FROM feed_info LIMIT 1").fetchone()
        feed_version = (row["feed_version"] if row else "") or ""
    except sqlite3.Error:
        pass
    print(f"  static feed version: {feed_version or 'unknown'}")

    # v_departure is sprint 1's own definition of a boardable scheduled call —
    # reused verbatim rather than re-deriving the pickup_type/departure_secs
    # filters here, so the two projects cannot disagree about what a departure is.
    query = """
        SELECT d.station_id      AS gtfs_station_id,
               d.departure_secs,
               d.platform_code,
               d.trip_short_name,
               d.route_short_name,
               d.trip_headsign,
               d.trip_id,
               sd.service_date
        FROM v_departure d
        JOIN service_date sd ON sd.service_id = d.service_id
        WHERE sd.service_date = ?
          AND sd.exception_type = 1
    """

    staged: dict[tuple, tuple] = {}
    skipped_no_uic = skipped_not_a_hub = 0
    for service_date in dates:
        count_before = len(staged)
        for row in sqlite_conn.execute(query, (service_date,)):
            uic = uic_from_gtfs(row["gtfs_station_id"])
            if uic is None:
                skipped_no_uic += 1
                continue
            hub = hubs.get(uic)
            if hub is None:
                skipped_not_a_hub += 1
                continue
            station_id, _name = hub

            # GTFS counts seconds from the start of the service day, so a value
            # past 86 400 is a train after midnight and belongs to the NEXT
            # calendar day. Folding it here means the cloud column and this one
            # are the same wall-clock basis and the join is plain equality.
            base = datetime.fromisoformat(service_date)
            local = base + timedelta(seconds=int(row["departure_secs"]))

            train_number = (row["trip_short_name"] or "").strip() or None
            key = (station_id, local, train_number)
            staged[key] = (
                station_id, row["gtfs_station_id"], service_date, local,
                train_number, row["route_short_name"], row["trip_headsign"],
                (row["platform_code"] or None), row["trip_id"], feed_version,
            )
        print(f"    {service_date}: {len(staged) - count_before:>5} scheduled departures")

    sqlite_conn.close()
    print(f"  {len(staged):,} rows staged "
          f"({skipped_not_a_hub:,} calls at non-hub stations skipped, "
          f"{skipped_no_uic:,} stops with no UIC code skipped)")

    if args.dry_run:
        print("  --dry-run: nothing written")
        return 0

    # ---- write, idempotently ---------------------------------------------
    rows = list(staged.values())
    cursor.execute(
        "DELETE FROM dbo.scheduled_departures WHERE service_date IN (%s)"
        % ",".join(["%s"] * len(dates)), tuple(dates))
    print(f"  cleared {cursor.rowcount if cursor.rowcount > 0 else 0} existing row(s) "
          "for these dates")

    columns = ("station_id, gtfs_station_id, service_date, scheduled_departure_local, "
               "train_number, route_short_name, trip_headsign, planned_platform, "
               "gtfs_trip_id, feed_version")
    placeholder = "(" + ", ".join(["%s"] * 10) + ")"
    for start in range(0, len(rows), ROWS_PER_INSERT):
        chunk = rows[start:start + ROWS_PER_INSERT]
        cursor.execute(
            f"INSERT INTO dbo.scheduled_departures ({columns}) VALUES "
            + ", ".join([placeholder] * len(chunk)),
            tuple(value for row in chunk for value in row),
        )
    cloud.commit()
    print(f"  inserted {len(rows):,} scheduled departures")

    # ---- report what the join actually shows ------------------------------
    cursor.execute("""
        SELECT COUNT(*) AS scheduled,
               SUM(CONVERT(INT, was_observed)) AS observed,
               SUM(CASE WHEN unobserved = 1 AND hour_was_sampled = 1 THEN 1 ELSE 0 END)
                   AS silent_candidates
        FROM dbo.v_schedule_vs_observed
    """)
    scheduled, observed, silent = cursor.fetchone()
    print()
    print("  === the combined picture ===")
    print(f"    scheduled (timetable):            {scheduled:,}")
    print(f"    of those, observed live:          {observed:,} "
          f"({100.0 * (observed or 0) / max(scheduled or 1, 1):.1f}%)")
    print(f"    scheduled, unseen, hour WAS sampled: {silent:,}  <- silent-cancellation candidates")

    cursor.execute("""
        SELECT TOP 5 station_name, SUM(scheduled) AS scheduled, SUM(observed) AS observed,
               CONVERT(DECIMAL(5,2), 100.0*SUM(observed)/NULLIF(SUM(scheduled),0)) AS coverage_pct
        FROM dbo.v_schedule_coverage WHERE hour_was_sampled = 1
        GROUP BY station_name ORDER BY scheduled DESC
    """)
    print("    coverage during sampled hours, by hub:")
    for name, sched, obs, pct in cursor.fetchall():
        print(f"      {str(name)[:28]:<28} {obs or 0:>5}/{sched:<5} = {pct}%")
    cloud.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
