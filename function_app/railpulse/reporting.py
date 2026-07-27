"""Read-only snapshots for the /api/health and /api/stats endpoints.

These exist so that the pipeline can be verified without a SQL client, from a
browser, from `curl`, or from an uptime monitor. Both are deliberately built on
the views in 03_views.sql rather than on ad-hoc SQL: if `v_ingestion_health`
disagrees with what /api/health reports, that is a bug, and sharing one
definition is how it is prevented.

Everything here is fixed SQL with no interpolation of caller input. The one
value that comes from outside — the row limit — is bound as a parameter, not
formatted into the string.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import pyodbc

from .database import fetch_dicts

#: Table names reported by /api/stats, with their row counts. Fixed list, so
#: nothing a caller sends can reach the query.
COUNTED_TABLES: tuple[str, ...] = (
    "stations", "platforms", "vehicles", "vehicle_types",
    "liveboard_records", "ingestion_runs",
)


def jsonable(value: Any) -> Any:
    """Make a pyodbc row value JSON-serialisable, losing nothing.

    Decimal -> float would be the obvious move and is wrong for money-shaped
    values; here the decimals are percentages and second counts, where float is
    both lossless enough and what a dashboard wants. Dates, datetimes and times
    go to ISO-8601 so a JavaScript client can parse them without a format guess.

    TOTAL BY CONTRACT, and that matters more than it looks. This function is also
    passed as ``json.dumps(default=...)``, which calls it only for values the
    encoder could not handle — and if it returned such a value unchanged, the
    encoder would call it again on the same object and recurse until the stack
    ran out. So known-safe types are returned as-is and *everything else* falls
    through to ``str()``. A future `uniqueidentifier` column then renders as a
    string rather than taking down the endpoint.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return str(value)


def _rows(cursor: pyodbc.Cursor, statement: str,
          params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [
        {key: jsonable(value) for key, value in row.items()}
        for row in fetch_dicts(cursor, statement, params)
    ]


def table_counts(cursor: pyodbc.Cursor) -> dict[str, int]:
    """Row count per table, in one round trip.

    UNION ALL of COUNT(*) rather than six queries — six round trips to a
    serverless database is six chances to be billed for a resume, and this is
    the endpoint most likely to be hit by something automated.
    """
    statement = " UNION ALL ".join(
        f"SELECT '{name}' AS table_name, COUNT(*) AS row_count FROM dbo.{name}"
        for name in COUNTED_TABLES
    )
    return {
        str(row["table_name"]): int(row["row_count"])
        for row in fetch_dicts(cursor, statement)
    }


def ingestion_health(cursor: pyodbc.Cursor) -> list[dict[str, Any]]:
    """Per-station freshness, straight from v_ingestion_health."""
    return _rows(cursor, """
        SELECT station_id, station_name, is_hub, last_run_started_utc,
               last_run_status, last_run_departures, last_run_inserted,
               last_run_updated, last_run_duration_ms, minutes_since_last_run,
               is_stale, failures_last_24h, runs_last_24h
          FROM dbo.v_ingestion_health
         ORDER BY is_hub DESC, minutes_since_last_run ASC;
    """)


def data_quality(cursor: pyodbc.Cursor) -> dict[str, Any]:
    """The single-row data-quality summary."""
    rows = _rows(cursor, "SELECT * FROM dbo.v_data_quality;")
    return rows[0] if rows else {}


def recent_runs(cursor: pyodbc.Cursor, limit: int = 20) -> list[dict[str, Any]]:
    """The last N runs, newest first."""
    return _rows(cursor, """
        SELECT TOP (?) run_id, trigger_source, requested_station, station_id,
               status, api_status_code, departures_returned, rows_inserted,
               rows_updated, rows_skipped, duration_ms, started_utc,
               finished_utc, error_message
          FROM dbo.ingestion_runs
         ORDER BY run_id DESC;
    """, (max(1, min(int(limit), 200)),))


def station_punctuality(cursor: pyodbc.Cursor, limit: int = 25) -> list[dict[str, Any]]:
    """Hub leaderboard over everything collected so far.

    Aggregated across dates here (the view is per date) because this is a
    "how is the pipeline doing" endpoint, not the dashboard.
    """
    return _rows(cursor, """
        SELECT TOP (?)
               s.name                         AS station_name,
               SUM(p.departures_observed)     AS departures,
               SUM(p.cancellations)           AS cancellations,
               CONVERT(DECIMAL(6,1), AVG(p.avg_delay_seconds))
                                              AS avg_delay_seconds,
               CONVERT(DECIMAL(5,2),
                       100.0 * SUM(CASE WHEN p.pct_on_time_6min IS NULL THEN 0
                                        ELSE p.trains_measured
                                             * p.pct_on_time_6min / 100.0 END)
                       / NULLIF(SUM(p.trains_measured), 0))
                                              AS pct_on_time_6min,
               COUNT(DISTINCT p.departure_date_local) AS days_covered
          FROM dbo.v_station_punctuality AS p
          JOIN dbo.stations AS s ON s.station_id = p.station_id
         GROUP BY s.name
         ORDER BY departures DESC;
    """, (max(1, min(int(limit), 200)),))


def health_snapshot(cursor: pyodbc.Cursor) -> dict[str, Any]:
    """Everything /api/health reports."""
    return {
        "database": "reachable",
        "server_time_utc": jsonable(
            fetch_dicts(cursor, "SELECT SYSUTCDATETIME() AS now_utc;")[0]["now_utc"]
        ),
        "table_counts": table_counts(cursor),
        "stations": ingestion_health(cursor),
    }


def stats_snapshot(cursor: pyodbc.Cursor) -> dict[str, Any]:
    """Everything /api/stats reports."""
    return {
        "table_counts": table_counts(cursor),
        "data_quality": data_quality(cursor),
        "punctuality_by_station": station_punctuality(cursor),
        "recent_runs": recent_runs(cursor, limit=10),
    }
