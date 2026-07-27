"""Parsed rows -> Azure SQL, idempotently.

THE PATTERN
Every table is written the same way: stage the payload into a session-scoped
temp table, then apply exactly ONE ``MERGE`` from it. Not row-by-row upserts,
and not "SELECT to check then INSERT".

Three reasons, in order of how much they matter here:

1. **Correctness under concurrency.** A check-then-insert has a race: the timer
   trigger and a manual HTTP call can both find no row and both insert, and the
   unique constraint then fails the whole load. A single MERGE with
   ``WITH (HOLDLOCK)`` takes a range lock on the key it is about to touch, which
   is what closes that window. (HOLDLOCK on a MERGE target is not optional
   pedantry — without it, MERGE is documented as vulnerable to exactly this.)

2. **Round trips.** A Function App talks to Azure SQL over ~10-50 ms of network.
   60 departures done one at a time is 120+ round trips per station and
   ~10 seconds of billed execution; staged as one insert plus one MERGE it is
   two. On a Consumption plan that difference is the bill.

3. **Idempotency, stated once.** The natural key lives in the MERGE's ON clause
   and nowhere else. There is no second code path that could disagree about what
   makes a departure "the same departure".

WRITE ORDER
stations -> vehicle_types -> vehicles -> platforms -> liveboard_records.
That is dictated by the foreign keys: a departure cannot reference a platform
that does not exist yet, and a vehicle cannot reference an unseeded type code.

WHAT AN UPDATE PRESERVES
On a second sighting of a departure, `first_seen_utc`, `first_seen_run_id` and
`delay_first_seen_s` are never touched, `last_seen_*` advance, and
`observation_count` increments. That is what turns a mutable current-state table
back into something that can still answer "did this delay grow?".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

import pyodbc

from .database import first_result_set, insert_rows
from .transform import (
    DepartureRow,
    LiveboardBatch,
    PlatformRow,
    StationRow,
    VehicleRow,
)

logger = logging.getLogger(__name__)


# ==========================================================================
# Staging tables. Each carries a PRIMARY KEY on the natural key it will be
# merged on — which both indexes the MERGE join and makes "the source must not
# contain duplicate keys" an enforced invariant rather than a hope. (MERGE
# raises error 8672 and abandons the entire statement if two source rows match
# one target row, so this failing early and clearly is a feature.)
# ==========================================================================
STAGE_STATIONS = "#stg_stations"
STAGE_VEHICLES = "#stg_vehicles"
STAGE_PLATFORMS = "#stg_platforms"
STAGE_DEPARTURES = "#stg_departures"

STAGING_DDL = f"""
IF OBJECT_ID('tempdb..{STAGE_STATIONS}') IS NOT NULL DROP TABLE {STAGE_STATIONS};
CREATE TABLE {STAGE_STATIONS} (
    station_id    VARCHAR(24)   NOT NULL PRIMARY KEY,
    uic_code      CHAR(9)       NOT NULL,
    country_code  CHAR(2)       NOT NULL,
    name          NVARCHAR(120) NOT NULL,
    standard_name NVARCHAR(120) NULL,
    latitude      DECIMAL(9,6)  NULL,
    longitude     DECIMAL(9,6)  NULL,
    irail_url     VARCHAR(200)  NULL,
    is_hub        BIT           NOT NULL,
    seen_utc      DATETIME2(0)  NOT NULL
);

IF OBJECT_ID('tempdb..{STAGE_VEHICLES}') IS NOT NULL DROP TABLE {STAGE_VEHICLES};
CREATE TABLE {STAGE_VEHICLES} (
    vehicle_id     VARCHAR(40)  NOT NULL PRIMARY KEY,
    short_name     NVARCHAR(40) NULL,
    vehicle_number VARCHAR(12)  NULL,
    type_raw       VARCHAR(12)  NULL,
    type_code      VARCHAR(8)   NOT NULL,
    service_line   VARCHAR(8)   NULL,
    irail_url      VARCHAR(200) NULL,
    seen_utc       DATETIME2(0) NOT NULL
);

IF OBJECT_ID('tempdb..{STAGE_PLATFORMS}') IS NOT NULL DROP TABLE {STAGE_PLATFORMS};
CREATE TABLE {STAGE_PLATFORMS} (
    station_id    VARCHAR(24)  NOT NULL,
    platform_code VARCHAR(8)   NOT NULL,
    seen_utc      DATETIME2(0) NOT NULL,
    PRIMARY KEY (station_id, platform_code)
);

IF OBJECT_ID('tempdb..{STAGE_DEPARTURES}') IS NOT NULL DROP TABLE {STAGE_DEPARTURES};
CREATE TABLE {STAGE_DEPARTURES} (
    station_id                VARCHAR(24)  NOT NULL,
    vehicle_id                VARCHAR(40)  NOT NULL,
    scheduled_departure_utc   DATETIME2(0) NOT NULL,
    scheduled_departure_local DATETIME2(0) NOT NULL,
    destination_station_id    VARCHAR(24)  NULL,
    platform_code             VARCHAR(8)   NULL,
    platform_is_normal        BIT          NULL,
    delay_seconds             INT          NOT NULL,
    is_canceled               BIT          NOT NULL,
    has_left                  BIT          NOT NULL,
    is_extra                  BIT          NOT NULL,
    occupancy                 VARCHAR(10)  NULL,
    departure_connection      VARCHAR(200) NULL,
    seen_utc                  DATETIME2(0) NOT NULL,
    run_id                    BIGINT       NOT NULL,
    PRIMARY KEY (station_id, vehicle_id, scheduled_departure_utc)
);
"""

STATION_COLUMNS = ("station_id", "uic_code", "country_code", "name",
                   "standard_name", "latitude", "longitude", "irail_url",
                   "is_hub", "seen_utc")
VEHICLE_COLUMNS = ("vehicle_id", "short_name", "vehicle_number", "type_raw",
                   "type_code", "service_line", "irail_url", "seen_utc")
PLATFORM_COLUMNS = ("station_id", "platform_code", "seen_utc")
DEPARTURE_COLUMNS = ("station_id", "vehicle_id", "scheduled_departure_utc",
                     "scheduled_departure_local", "destination_station_id",
                     "platform_code", "platform_is_normal", "delay_seconds",
                     "is_canceled", "has_left", "is_extra", "occupancy",
                     "departure_connection", "seen_utc", "run_id")


# ==========================================================================
# MERGE statements
# ==========================================================================
MERGE_STATIONS = f"""
MERGE dbo.stations WITH (HOLDLOCK) AS t
USING {STAGE_STATIONS} AS s
   ON t.station_id = s.station_id
WHEN MATCHED THEN UPDATE SET
    t.uic_code      = s.uic_code,
    t.country_code  = s.country_code,
    t.name          = s.name,
    -- COALESCE, not assignment: a liveboard's inline stationinfo is sometimes
    -- thinner than the catalogue's, and a later sighting must not erase
    -- coordinates or the official name we already have.
    t.standard_name = COALESCE(s.standard_name, t.standard_name),
    t.latitude      = COALESCE(s.latitude, t.latitude),
    t.longitude     = COALESCE(s.longitude, t.longitude),
    t.irail_url     = COALESCE(s.irail_url, t.irail_url),
    -- Sticky: once polled as a hub, always a hub. Seeing Leuven as somebody
    -- else's destination must not demote it.
    t.is_hub        = CASE WHEN s.is_hub = 1 THEN 1 ELSE t.is_hub END,
    t.last_seen_utc = s.seen_utc
WHEN NOT MATCHED BY TARGET THEN
    INSERT (station_id, uic_code, country_code, name, standard_name,
            latitude, longitude, irail_url, is_hub,
            first_seen_utc, last_seen_utc)
    VALUES (s.station_id, s.uic_code, s.country_code, s.name, s.standard_name,
            s.latitude, s.longitude, s.irail_url, s.is_hub,
            s.seen_utc, s.seen_utc);
"""

#: Auto-extend the type reference table. A service class the project has never
#: seen must not be able to fail the foreign key and cost us a whole poll; it
#: lands with is_seeded = 0 and shows up as undocumented in
#: v_vehicle_type_performance, where it can be labelled properly later.
MERGE_VEHICLE_TYPES = f"""
MERGE dbo.vehicle_types WITH (HOLDLOCK) AS t
USING (SELECT DISTINCT type_code FROM {STAGE_VEHICLES}) AS s
   ON t.type_code = s.type_code
WHEN NOT MATCHED BY TARGET THEN
    INSERT (type_code, label, description, is_seeded)
    VALUES (s.type_code, s.type_code,
            N'Discovered by the loader; not yet documented in 04_seed_reference.sql.',
            0);
"""

MERGE_VEHICLES = f"""
MERGE dbo.vehicles WITH (HOLDLOCK) AS t
USING {STAGE_VEHICLES} AS s
   ON t.vehicle_id = s.vehicle_id
WHEN MATCHED THEN UPDATE SET
    t.short_name     = COALESCE(s.short_name, t.short_name),
    t.vehicle_number = COALESCE(s.vehicle_number, t.vehicle_number),
    t.type_raw       = COALESCE(s.type_raw, t.type_raw),
    t.type_code      = s.type_code,
    t.service_line   = COALESCE(s.service_line, t.service_line),
    t.irail_url      = COALESCE(s.irail_url, t.irail_url),
    t.last_seen_utc  = s.seen_utc
WHEN NOT MATCHED BY TARGET THEN
    INSERT (vehicle_id, short_name, vehicle_number, type_raw, type_code,
            service_line, irail_url, first_seen_utc, last_seen_utc)
    VALUES (s.vehicle_id, s.short_name, s.vehicle_number, s.type_raw,
            s.type_code, s.service_line, s.irail_url, s.seen_utc, s.seen_utc);
"""

MERGE_PLATFORMS = f"""
MERGE dbo.platforms WITH (HOLDLOCK) AS t
USING {STAGE_PLATFORMS} AS s
   ON t.station_id = s.station_id AND t.platform_code = s.platform_code
WHEN MATCHED THEN UPDATE SET
    t.last_seen_utc = s.seen_utc
WHEN NOT MATCHED BY TARGET THEN
    INSERT (station_id, platform_code, first_seen_utc, last_seen_utc)
    VALUES (s.station_id, s.platform_code, s.seen_utc, s.seen_utc);
"""

#: The fact MERGE. `OUTPUT $action` into a table variable is how the run log
#: learns how many departures were new versus revised — the two numbers that
#: prove the deduplication is actually working.
MERGE_DEPARTURES = f"""
DECLARE @actions TABLE (act NVARCHAR(10));

MERGE dbo.liveboard_records WITH (HOLDLOCK) AS t
USING {STAGE_DEPARTURES} AS s
   ON  t.station_id              = s.station_id
   AND t.vehicle_id              = s.vehicle_id
   AND t.scheduled_departure_utc = s.scheduled_departure_utc
-- The run_id guard makes a replay of the SAME run a true no-op, so a retry
-- after a partial failure cannot inflate observation_count.
WHEN MATCHED AND t.last_seen_run_id <> s.run_id THEN UPDATE SET
    t.destination_station_id = COALESCE(s.destination_station_id,
                                        t.destination_station_id),
    -- NULL here means "platform not allocated yet", not "platform withdrawn",
    -- so a known platform is kept rather than overwritten with unknown.
    t.platform_code          = COALESCE(s.platform_code, t.platform_code),
    t.platform_is_normal     = COALESCE(s.platform_is_normal,
                                        t.platform_is_normal),
    -- The latest reading wins: this column is the current delay.
    t.delay_seconds          = s.delay_seconds,
    t.is_canceled            = s.is_canceled,
    -- Sticky. A train that has left cannot un-leave, and it drops off the
    -- liveboard shortly after, so the last positive reading is the truth.
    t.has_left               = CASE WHEN s.has_left = 1 THEN 1
                                    ELSE t.has_left END,
    t.is_extra               = s.is_extra,
    -- 'unknown' is iRail's absence-of-data value, not an observation, so it
    -- must not overwrite a real crowding report from an earlier poll.
    t.occupancy              = CASE
                                 WHEN s.occupancy IS NOT NULL
                                  AND s.occupancy <> 'unknown' THEN s.occupancy
                                 ELSE t.occupancy END,
    t.departure_connection   = COALESCE(s.departure_connection,
                                        t.departure_connection),
    t.last_seen_utc          = s.seen_utc,
    t.last_seen_run_id       = s.run_id,
    t.observation_count      = t.observation_count + 1
WHEN NOT MATCHED BY TARGET THEN
    INSERT (station_id, vehicle_id, scheduled_departure_utc,
            scheduled_departure_local, destination_station_id, platform_code,
            platform_is_normal, delay_seconds, is_canceled, has_left, is_extra,
            occupancy, departure_connection, first_seen_utc, last_seen_utc,
            observation_count, delay_first_seen_s,
            first_seen_run_id, last_seen_run_id)
    VALUES (s.station_id, s.vehicle_id, s.scheduled_departure_utc,
            s.scheduled_departure_local, s.destination_station_id,
            s.platform_code, s.platform_is_normal, s.delay_seconds,
            s.is_canceled, s.has_left, s.is_extra, s.occupancy,
            s.departure_connection, s.seen_utc, s.seen_utc,
            1, s.delay_seconds, s.run_id, s.run_id)
OUTPUT $action INTO @actions;

SELECT act, COUNT(*) AS affected FROM @actions GROUP BY act;
"""


# ==========================================================================
# Results
# ==========================================================================
@dataclass
class LoadCounts:
    """What one station's load changed. Written straight into ingestion_runs."""

    rows_inserted: int = 0
    rows_updated: int = 0
    stations_upserted: int = 0
    vehicles_upserted: int = 0
    platforms_upserted: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


# ==========================================================================
# Row flattening. Kept next to the column tuples so the two cannot drift.
# ==========================================================================
def _station_values(row: StationRow) -> tuple[Any, ...]:
    return (row.station_id, row.uic_code, row.country_code, row.name,
            row.standard_name, row.latitude, row.longitude, row.irail_url,
            row.is_hub, row.seen_utc)


def _vehicle_values(row: VehicleRow) -> tuple[Any, ...]:
    return (row.vehicle_id, row.short_name, row.vehicle_number, row.type_raw,
            row.type_code, row.service_line, row.irail_url, row.seen_utc)


def _platform_values(row: PlatformRow) -> tuple[Any, ...]:
    return (row.station_id, row.platform_code, row.seen_utc)


def _departure_values(row: DepartureRow, run_id: int) -> tuple[Any, ...]:
    return (row.station_id, row.vehicle_id, row.scheduled_departure_utc,
            row.scheduled_departure_local, row.destination_station_id,
            row.platform_code, row.platform_is_normal, row.delay_seconds,
            row.is_canceled, row.has_left, row.is_extra, row.occupancy,
            row.departure_connection, row.seen_utc, run_id)


# ==========================================================================
# Run bookkeeping
# ==========================================================================
def open_run(
    cursor: pyodbc.Cursor,
    *,
    trigger_source: str,
    requested_station: str,
    started_utc: datetime,
    invocation_id: str | None = None,
    station_id: str | None = None,
) -> int:
    """Insert a 'running' audit row and return its id.

    Committed by the caller before the API is even called, so that a run which
    dies mid-flight leaves evidence. SCOPE_IDENTITY() rather than @@IDENTITY:
    the latter would return an id from a trigger on another table if one were
    ever added.
    """
    cursor.execute(
        """
        INSERT INTO dbo.ingestion_runs
            (trigger_source, invocation_id, requested_station, station_id,
             started_utc, status)
        VALUES (?, ?, ?, ?, ?, 'running');
        SELECT CAST(SCOPE_IDENTITY() AS BIGINT) AS run_id;
        """,
        [trigger_source, invocation_id, requested_station, station_id,
         started_utc],
    )
    rows = first_result_set(cursor)
    if not rows or rows[0][0] is None:
        raise RuntimeError("could not obtain a run_id for the ingestion run")
    return int(rows[0][0])


def close_run(
    cursor: pyodbc.Cursor,
    run_id: int,
    *,
    status: str,
    finished_utc: datetime,
    duration_ms: int,
    station_id: str | None = None,
    api_status_code: int | None = None,
    api_url: str | None = None,
    feed_timestamp_utc: datetime | None = None,
    departures_returned: int | None = None,
    rows_skipped: int | None = None,
    counts: LoadCounts | None = None,
    error_message: str | None = None,
) -> None:
    """Finalise the audit row. COALESCE keeps whatever was already recorded."""
    counts = counts or LoadCounts()
    cursor.execute(
        """
        UPDATE dbo.ingestion_runs
           SET status              = ?,
               finished_utc        = ?,
               duration_ms         = ?,
               station_id          = COALESCE(?, station_id),
               api_status_code     = COALESCE(?, api_status_code),
               api_url             = COALESCE(?, api_url),
               feed_timestamp_utc  = COALESCE(?, feed_timestamp_utc),
               departures_returned = COALESCE(?, departures_returned),
               rows_skipped        = COALESCE(?, rows_skipped),
               rows_inserted       = ?,
               rows_updated        = ?,
               stations_upserted   = ?,
               vehicles_upserted   = ?,
               error_message       = ?
         WHERE run_id = ?;
        """,
        [status, finished_utc, duration_ms, station_id, api_status_code,
         api_url, feed_timestamp_utc, departures_returned, rows_skipped,
         counts.rows_inserted, counts.rows_updated, counts.stations_upserted,
         counts.vehicles_upserted,
         (error_message[:1000] if error_message else None), run_id],
    )


# ==========================================================================
# The load itself
# ==========================================================================
def create_staging_tables(cursor: pyodbc.Cursor) -> None:
    """(Re)create the session-scoped temp tables.

    Dropped first because a pooled connection may still carry them from a
    previous load on the same handle.
    """
    cursor.execute(STAGING_DDL)
    while cursor.nextset():
        pass


def load_batch(
    cursor: pyodbc.Cursor, batch: LiveboardBatch, run_id: int
) -> LoadCounts:
    """Write one parsed liveboard. Caller owns the transaction."""
    counts = LoadCounts()
    create_staging_tables(cursor)

    counts.stations_upserted = _stage_and_merge(
        cursor, STAGE_STATIONS, STATION_COLUMNS,
        [_station_values(row) for row in batch.stations], MERGE_STATIONS)

    vehicle_rows = [_vehicle_values(row) for row in batch.vehicles]
    if vehicle_rows:
        insert_rows(cursor, STAGE_VEHICLES, VEHICLE_COLUMNS, vehicle_rows)
        # Types before vehicles: the FK on vehicles.type_code demands it.
        cursor.execute(MERGE_VEHICLE_TYPES)
        cursor.execute(MERGE_VEHICLES)
        counts.vehicles_upserted = max(cursor.rowcount, 0)

    counts.platforms_upserted = _stage_and_merge(
        cursor, STAGE_PLATFORMS, PLATFORM_COLUMNS,
        [_platform_values(row) for row in batch.platforms], MERGE_PLATFORMS)

    departure_rows = [_departure_values(row, run_id) for row in batch.departures]
    if departure_rows:
        insert_rows(cursor, STAGE_DEPARTURES, DEPARTURE_COLUMNS, departure_rows)
        cursor.execute(MERGE_DEPARTURES)
        for action, affected in first_result_set(cursor):
            if str(action).upper() == "INSERT":
                counts.rows_inserted = int(affected)
            elif str(action).upper() == "UPDATE":
                counts.rows_updated = int(affected)

    return counts


def _stage_and_merge(
    cursor: pyodbc.Cursor,
    table: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    merge_statement: str,
) -> int:
    """Stage rows and merge them; returns the number of affected target rows."""
    if not rows:
        return 0
    insert_rows(cursor, table, columns, rows)
    cursor.execute(merge_statement)
    # pyodbc reports -1 when the driver cannot determine a count; clamp so the
    # audit table never stores a negative "upserted" figure.
    return max(cursor.rowcount, 0)


def seed_stations(cursor: pyodbc.Cursor, rows: Iterable[StationRow]) -> int:
    """Bulk-load the station catalogue through the same MERGE as the liveboards.

    Deliberately reuses MERGE_STATIONS rather than having its own INSERT: the
    seed and the incremental path must agree about precedence (hub flag sticky,
    coordinates never erased), and the only way to guarantee that is for there
    to be one statement.
    """
    values = [_station_values(row) for row in rows]
    if not values:
        return 0
    create_staging_tables(cursor)
    insert_rows(cursor, STAGE_STATIONS, STATION_COLUMNS, values)
    cursor.execute(MERGE_STATIONS)
    return len(values)
