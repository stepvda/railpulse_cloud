/* ===========================================================================
   RailPulse Cloud — 02_indexes.sql
   ===========================================================================
   Indexes are chosen for the two access patterns this database actually has,
   and nothing else. Every index is a write cost paid on every 15-minute load,
   so a speculative one is not free.

     PATTERN 1 — the loader.  A MERGE that seeks the natural key
                 (station_id, vehicle_id, scheduled_departure_utc) once per
                 departure. Served by `uq_liveboard_records` (created with the
                 UNIQUE constraint in 01_schema.sql), which is why no extra
                 index is defined for it here.

     PATTERN 2 — BI.  Range scans over time, sliced by station, platform,
                 vehicle type or destination, aggregating delay. Column-store
                 would be the textbook answer for that shape, but at this row
                 count (~5 000 departures/day) it would cost more in
                 maintenance than it returns, so these are covering B-trees.

   `WITH (ONLINE = OFF)` is not specified anywhere: on a serverless database
   that spends most of its life paused, an offline build during a maintenance
   window is both cheaper and simpler.
   =========================================================================== */


/* ---------------------------------------------------------------------------
   Time-series scans: "everything that departed between X and Y".
   Leading on the local timestamp rather than UTC because that is the column
   every dashboard filter and every hour-of-day rollup uses; a UTC-leading
   index would be scanned rather than sought for a "yesterday, local time"
   filter. INCLUDE carries the measures so the query never touches the base
   table.
   --------------------------------------------------------------------------- */
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'ix_lbr_local_time'
                 AND object_id = OBJECT_ID('dbo.liveboard_records'))
BEGIN
    CREATE NONCLUSTERED INDEX ix_lbr_local_time
        ON dbo.liveboard_records (scheduled_departure_local)
        INCLUDE (station_id, delay_seconds, is_canceled, is_on_time_6min);
END
GO

/* ---------------------------------------------------------------------------
   Per-station punctuality over a date range — the leaderboard query, and the
   most common dashboard interaction (pick a station, look at a period).
   (station_id, departure_date_local) is the composite that lets both the
   equality and the range predicate be seeks.
   --------------------------------------------------------------------------- */
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'ix_lbr_station_date'
                 AND object_id = OBJECT_ID('dbo.liveboard_records'))
BEGIN
    CREATE NONCLUSTERED INDEX ix_lbr_station_date
        ON dbo.liveboard_records (station_id, departure_date_local)
        INCLUDE (delay_seconds, is_canceled, is_on_time_2min, is_on_time_6min,
                 platform_code, delay_bucket_order);
END
GO

/* ---------------------------------------------------------------------------
   Platform pressure at a hub (the SQL sprint's Q2, now on live data).
   --------------------------------------------------------------------------- */
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'ix_lbr_platform'
                 AND object_id = OBJECT_ID('dbo.liveboard_records'))
BEGIN
    CREATE NONCLUSTERED INDEX ix_lbr_platform
        ON dbo.liveboard_records (station_id, platform_code, departure_hour_local)
        INCLUDE (delay_seconds, is_canceled);
END
GO

/* ---------------------------------------------------------------------------
   Foreign-key support. SQL Server does NOT index a foreign key automatically:
   without these, a DELETE on a parent row (or a join from the dimension side)
   scans the whole fact table.
   --------------------------------------------------------------------------- */
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'ix_lbr_vehicle'
                 AND object_id = OBJECT_ID('dbo.liveboard_records'))
BEGIN
    CREATE NONCLUSTERED INDEX ix_lbr_vehicle
        ON dbo.liveboard_records (vehicle_id, scheduled_departure_local)
        INCLUDE (station_id, delay_seconds, is_canceled);
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'ix_lbr_destination'
                 AND object_id = OBJECT_ID('dbo.liveboard_records'))
BEGIN
    CREATE NONCLUSTERED INDEX ix_lbr_destination
        ON dbo.liveboard_records (destination_station_id, departure_hour_local)
        INCLUDE (station_id, delay_seconds);
END
GO

/* ---------------------------------------------------------------------------
   Disruptions are rare and always queried in isolation, which is the textbook
   case for a FILTERED index: each of these stores only the ~2% of rows that
   matter, so "show me today's cancellations" reads a few pages instead of the
   whole table.

   TWO INDEXES, NOT ONE, AND NOT BY CHOICE
   The natural way to write this is a single index filtered on
       WHERE is_canceled = 1 OR platform_is_normal = 0
   and SQL Server rejects it. A filtered-index predicate is not a general
   boolean expression: the documented grammar is
       <filter_predicate> ::= <conjunct> [ AND <conjunct> ]
   so conjuncts may be ANDed, a single column may use IN (...), and **OR between
   two different columns is not expressible at all**. Splitting into two
   filtered indexes is the supported equivalent — and is arguably better here,
   since a cancellation and a platform change are asked about separately anyway.
   --------------------------------------------------------------------------- */
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'ix_lbr_cancellations'
                 AND object_id = OBJECT_ID('dbo.liveboard_records'))
BEGIN
    CREATE NONCLUSTERED INDEX ix_lbr_cancellations
        ON dbo.liveboard_records (station_id, scheduled_departure_local)
        INCLUDE (vehicle_id, destination_station_id, platform_code, delay_seconds)
        WHERE is_canceled = 1;
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'ix_lbr_platform_changes'
                 AND object_id = OBJECT_ID('dbo.liveboard_records'))
BEGIN
    CREATE NONCLUSTERED INDEX ix_lbr_platform_changes
        ON dbo.liveboard_records (station_id, scheduled_departure_local)
        INCLUDE (vehicle_id, destination_station_id, platform_code, delay_seconds)
        WHERE platform_is_normal = 0;
END
GO

/* ---------------------------------------------------------------------------
   Freshness monitoring: the health endpoint asks "when did each station last
   load successfully". Descending, because it only ever wants the newest row.
   --------------------------------------------------------------------------- */
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'ix_runs_station_started'
                 AND object_id = OBJECT_ID('dbo.ingestion_runs'))
BEGIN
    CREATE NONCLUSTERED INDEX ix_runs_station_started
        ON dbo.ingestion_runs (station_id, started_utc DESC)
        INCLUDE (status, departures_returned, rows_inserted, rows_updated);
END
GO

/* Hubs are ~10 rows out of 714; a filtered index keeps the hub list a
   single-page read for the timer trigger's station lookup. */
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'ix_stations_hub'
                 AND object_id = OBJECT_ID('dbo.stations'))
BEGIN
    CREATE NONCLUSTERED INDEX ix_stations_hub
        ON dbo.stations (station_id)
        INCLUDE (name, standard_name)
        WHERE is_hub = 1;
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'ix_vehicles_type'
                 AND object_id = OBJECT_ID('dbo.vehicles'))
BEGIN
    CREATE NONCLUSTERED INDEX ix_vehicles_type
        ON dbo.vehicles (type_code)
        INCLUDE (short_name, service_line);
END
GO
