/* ===========================================================================
   RailPulse Cloud — 03_views.sql
   The BI contract. Next week's dashboard connects to these views, never to the
   base tables.
   ===========================================================================

   WHY VIEWS AND NOT DIRECT TABLE ACCESS
   A view is the seam that lets the physical model change — a column renamed, a
   dimension split, the fact repartitioned — without breaking a report that
   someone else built. It is also where the project's *definitions* live: what
   counts as "on time", whether a cancelled train belongs in the denominator,
   which hour a 00:30 departure belongs to. Encoding those once here means two
   dashboards cannot quietly disagree.

   A NOTE ON BIT AGGREGATION
   T-SQL cannot SUM a BIT column, so every rate below is written as
       SUM(CONVERT(INT, flag)) / NULLIF(COUNT(flag), 0)
   This is not just a cast to satisfy the parser: COUNT(flag) counts only
   NON-NULL values, and the punctuality flags are deliberately NULL for
   cancelled trains. The denominator therefore excludes cancellations
   automatically — a cancelled train is not "late", it is absent, and counting
   it as a 0-second delay would flatter the operator.
   =========================================================================== */


/* ===========================================================================
   v_departures — one row per departure event, fully denormalised.
   ---------------------------------------------------------------------------
   The single view a BI tool should import. Everything a chart needs, no joins
   required downstream, no surprises about which "station" is meant.
   =========================================================================== */
CREATE OR ALTER VIEW dbo.v_departures AS
SELECT
    r.record_id,

    /* -- where -- */
    r.station_id,
    origin.name              AS station_name,
    origin.standard_name     AS station_standard_name,
    origin.country_code      AS station_country,
    origin.latitude          AS station_latitude,
    origin.longitude         AS station_longitude,
    origin.is_hub            AS station_is_hub,

    r.destination_station_id,
    dest.name                AS destination_name,
    dest.country_code        AS destination_country,
    /* An international departure is a different product with a different
       punctuality profile; flagging it here saves every report from
       re-deriving it. */
    CONVERT(BIT, CASE WHEN dest.country_code IS NULL THEN NULL
                      WHEN dest.country_code <> 'BE' THEN 1 ELSE 0 END)
                             AS is_international,

    r.platform_code,
    /* '?' in the feed became NULL on the row; the dashboard needs a label. */
    COALESCE(r.platform_code, 'unknown') AS platform_label,
    r.platform_is_normal,

    /* -- what -- */
    r.vehicle_id,
    v.short_name             AS vehicle_name,
    v.vehicle_number,
    v.type_code              AS vehicle_type_code,
    vt.label                 AS vehicle_type,
    v.service_line,

    /* -- when -- */
    r.scheduled_departure_utc,
    r.scheduled_departure_local,
    r.actual_departure_utc,
    r.departure_date_local,
    r.departure_hour_local,
    r.departure_dow_local,
    /* `departure_dow_local` is ISO — 1 = Monday ... 7 = Sunday — so the weekend
       is 6 and 7. It is computed from a day count rather than from
       DATEPART(WEEKDAY, ...) precisely so that this test does not depend on the
       session's SET DATEFIRST; see the column's comment in 01_schema.sql.
       DATENAME is avoided for the same class of reason: its output is
       locale-dependent, so a server language change would silently break it. */
    CASE WHEN r.departure_dow_local IN (6, 7) THEN 'weekend' ELSE 'weekday' END
                             AS day_type,
    /* Peak windows as the Belgian operator defines them, used by the capacity
       questions. */
    CASE WHEN r.departure_hour_local BETWEEN 6  AND 8  THEN 'morning peak'
         WHEN r.departure_hour_local BETWEEN 16 AND 18 THEN 'evening peak'
         WHEN r.departure_hour_local BETWEEN 9  AND 15 THEN 'off-peak day'
         ELSE 'off-peak night' END AS peak_window,

    /* -- outcome -- */
    r.delay_seconds,
    r.delay_minutes,
    r.delay_bucket,
    r.delay_bucket_order,
    r.is_on_time_2min,
    r.is_on_time_6min,
    r.is_canceled,
    r.has_left,
    r.is_extra,
    r.occupancy,

    /* -- provenance -- */
    r.first_seen_utc,
    r.last_seen_utc,
    r.observation_count,
    r.delay_first_seen_s,
    r.delay_growth_s,
    r.first_seen_run_id,
    r.last_seen_run_id
FROM dbo.liveboard_records AS r
JOIN dbo.stations     AS origin ON origin.station_id = r.station_id
JOIN dbo.vehicles     AS v      ON v.vehicle_id      = r.vehicle_id
JOIN dbo.vehicle_types AS vt    ON vt.type_code      = v.type_code
/* LEFT: a destination is optional in the payload, and losing a departure
   because its terminus was not reported would be the wrong trade. */
LEFT JOIN dbo.stations AS dest   ON dest.station_id   = r.destination_station_id;
GO


/* ===========================================================================
   v_station_punctuality — the hub leaderboard.
   ---------------------------------------------------------------------------
   Continues the SQL sprint's "Network Leaderboard" on live data. One row per
   station per local date, so a dashboard can either show a single day or roll
   several up without ever double-counting.
   =========================================================================== */
CREATE OR ALTER VIEW dbo.v_station_punctuality AS
SELECT
    r.station_id,
    s.name                AS station_name,
    s.is_hub              AS station_is_hub,
    r.departure_date_local,

    COUNT(*)                                        AS departures_observed,
    SUM(CONVERT(INT, r.is_canceled))                AS cancellations,
    SUM(CONVERT(INT, CASE WHEN r.platform_is_normal = 0 THEN 1 ELSE 0 END))
                                                    AS platform_changes,

    /* Delay statistics over RUNNING trains only (the flags are NULL for
       cancellations, and CASE keeps the same population for the averages). */
    COUNT(r.is_on_time_6min)                        AS trains_measured,
    AVG(CASE WHEN r.is_canceled = 0 THEN CONVERT(DECIMAL(10,2), r.delay_seconds) END)
                                                    AS avg_delay_seconds,
    MAX(CASE WHEN r.is_canceled = 0 THEN r.delay_seconds END)
                                                    AS max_delay_seconds,
    SUM(CASE WHEN r.is_canceled = 0 THEN r.delay_seconds ELSE 0 END)
                                                    AS total_delay_seconds,

    CONVERT(DECIMAL(5,2), 100.0 * SUM(CONVERT(INT, r.is_on_time_2min))
                          / NULLIF(COUNT(r.is_on_time_2min), 0))
                                                    AS pct_on_time_2min,
    CONVERT(DECIMAL(5,2), 100.0 * SUM(CONVERT(INT, r.is_on_time_6min))
                          / NULLIF(COUNT(r.is_on_time_6min), 0))
                                                    AS pct_on_time_6min,
    CONVERT(DECIMAL(5,2), 100.0 * SUM(CONVERT(INT, r.is_canceled))
                          / NULLIF(COUNT(*), 0))     AS pct_cancelled
FROM dbo.liveboard_records AS r
JOIN dbo.stations AS s ON s.station_id = r.station_id
GROUP BY r.station_id, s.name, s.is_hub, r.departure_date_local;
GO


/* ===========================================================================
   v_hourly_pressure — departures and delay by local hour.
   ---------------------------------------------------------------------------
   The live answer to the sprint's Q1 ("which hour is busiest"). Note it counts
   *observed* departures: unlike the static timetable, this needs no annualised
   weighting, because every row here is a train that was really scheduled to
   leave on a real date.
   =========================================================================== */
CREATE OR ALTER VIEW dbo.v_hourly_pressure AS
SELECT
    r.station_id,
    s.name                  AS station_name,
    r.departure_hour_local,
    CASE WHEN r.departure_dow_local IN (6, 7) THEN 'weekend' ELSE 'weekday' END
                            AS day_type,

    COUNT(*)                                    AS departures,
    COUNT(DISTINCT r.departure_date_local)      AS days_covered,
    /* Departures per day observed: the honest comparison across hours when the
       capture window is not uniform (see docs/cost_control.md — the timer
       deliberately samples peak windows harder than the small hours, and an
       unadjusted count would report that as a peak). */
    CONVERT(DECIMAL(10,2), 1.0 * COUNT(*)
            / NULLIF(COUNT(DISTINCT r.departure_date_local), 0))
                                                AS departures_per_day,
    AVG(CASE WHEN r.is_canceled = 0 THEN CONVERT(DECIMAL(10,2), r.delay_seconds) END)
                                                AS avg_delay_seconds,
    CONVERT(DECIMAL(5,2), 100.0 * SUM(CONVERT(INT, r.is_on_time_6min))
                          / NULLIF(COUNT(r.is_on_time_6min), 0))
                                                AS pct_on_time_6min,
    SUM(CONVERT(INT, r.is_canceled))            AS cancellations
FROM dbo.liveboard_records AS r
JOIN dbo.stations AS s ON s.station_id = r.station_id
GROUP BY r.station_id, s.name, r.departure_hour_local,
         CASE WHEN r.departure_dow_local IN (6, 7) THEN 'weekend' ELSE 'weekday' END;
GO


/* ===========================================================================
   v_platform_pressure — platform-level load and reliability.
   ---------------------------------------------------------------------------
   The live continuation of the sprint's Q2 (busiest platforms at
   Brussels-Central). Departures with an unallocated platform are reported
   under 'unknown' rather than dropped, because at some hubs they are a
   material share and silently discarding them understates the total.
   =========================================================================== */
CREATE OR ALTER VIEW dbo.v_platform_pressure AS
SELECT
    r.station_id,
    s.name                               AS station_name,
    COALESCE(r.platform_code, 'unknown') AS platform_label,

    COUNT(*)                                    AS departures,
    COUNT(DISTINCT r.departure_date_local)      AS days_covered,
    COUNT(DISTINCT r.vehicle_id)                AS distinct_vehicles,
    AVG(CASE WHEN r.is_canceled = 0 THEN CONVERT(DECIMAL(10,2), r.delay_seconds) END)
                                                AS avg_delay_seconds,
    CONVERT(DECIMAL(5,2), 100.0 * SUM(CONVERT(INT, r.is_on_time_6min))
                          / NULLIF(COUNT(r.is_on_time_6min), 0))
                                                AS pct_on_time_6min,
    SUM(CONVERT(INT, r.is_canceled))            AS cancellations,
    SUM(CONVERT(INT, CASE WHEN r.platform_is_normal = 0 THEN 1 ELSE 0 END))
                                                AS platform_changes,
    /* Busiest single hour on this platform — the number that decides whether a
       platform is a bottleneck, as opposed to merely busy across the day. */
    MAX(r.departures_in_hour)                   AS peak_hour_departures
FROM (
    SELECT r.*,
           COUNT(*) OVER (PARTITION BY r.station_id, r.platform_code,
                                       r.departure_date_local,
                                       r.departure_hour_local) AS departures_in_hour
    FROM dbo.liveboard_records AS r
) AS r
JOIN dbo.stations AS s ON s.station_id = r.station_id
GROUP BY r.station_id, s.name, COALESCE(r.platform_code, 'unknown');
GO


/* ===========================================================================
   v_delay_distribution — histogram source.
   =========================================================================== */
CREATE OR ALTER VIEW dbo.v_delay_distribution AS
SELECT
    r.station_id,
    s.name                AS station_name,
    r.delay_bucket,
    r.delay_bucket_order,
    COUNT(*)              AS departures,
    CONVERT(DECIMAL(5,2), 100.0 * COUNT(*)
            / SUM(COUNT(*)) OVER (PARTITION BY r.station_id))
                          AS pct_of_station
FROM dbo.liveboard_records AS r
JOIN dbo.stations AS s ON s.station_id = r.station_id
GROUP BY r.station_id, s.name, r.delay_bucket, r.delay_bucket_order;
GO


/* ===========================================================================
   v_vehicle_type_performance — which service classes run late.
   =========================================================================== */
CREATE OR ALTER VIEW dbo.v_vehicle_type_performance AS
SELECT
    v.type_code,
    vt.label                AS vehicle_type,
    vt.is_seeded            AS type_is_documented,
    COUNT(*)                                    AS departures,
    COUNT(DISTINCT r.vehicle_id)                AS distinct_vehicles,
    AVG(CASE WHEN r.is_canceled = 0 THEN CONVERT(DECIMAL(10,2), r.delay_seconds) END)
                                                AS avg_delay_seconds,
    CONVERT(DECIMAL(5,2), 100.0 * SUM(CONVERT(INT, r.is_on_time_6min))
                          / NULLIF(COUNT(r.is_on_time_6min), 0))
                                                AS pct_on_time_6min,
    CONVERT(DECIMAL(5,2), 100.0 * SUM(CONVERT(INT, r.is_canceled))
                          / NULLIF(COUNT(*), 0)) AS pct_cancelled
FROM dbo.liveboard_records AS r
JOIN dbo.vehicles      AS v  ON v.vehicle_id = r.vehicle_id
JOIN dbo.vehicle_types AS vt ON vt.type_code = v.type_code
GROUP BY v.type_code, vt.label, vt.is_seeded;
GO


/* ===========================================================================
   v_ingestion_health — is the pipeline actually running?
   ---------------------------------------------------------------------------
   Backs the /api/health endpoint and is the first thing to look at when a
   dashboard number looks wrong. Freshness is measured in minutes against
   SYSUTCDATETIME() so it needs no parameter.
   =========================================================================== */
CREATE OR ALTER VIEW dbo.v_ingestion_health AS
WITH ranked AS (
    SELECT
        run.*,
        ROW_NUMBER() OVER (PARTITION BY run.station_id
                           ORDER BY run.started_utc DESC) AS recency_rank
    FROM dbo.ingestion_runs AS run
    WHERE run.station_id IS NOT NULL
)
SELECT
    r.station_id,
    s.name                  AS station_name,
    s.is_hub,
    r.started_utc           AS last_run_started_utc,
    r.status                AS last_run_status,
    r.departures_returned   AS last_run_departures,
    r.rows_inserted         AS last_run_inserted,
    r.rows_updated          AS last_run_updated,
    r.duration_ms           AS last_run_duration_ms,
    DATEDIFF(MINUTE, r.started_utc, SYSUTCDATETIME()) AS minutes_since_last_run,
    /* A hub that has not loaded in over an hour is stale regardless of the
       configured cadence: the widest configured window is peak-hours-only
       15 minutes, so 60 is four missed slots. */
    CONVERT(BIT, CASE WHEN DATEDIFF(MINUTE, r.started_utc, SYSUTCDATETIME()) > 60
                      THEN 1 ELSE 0 END) AS is_stale,
    (SELECT COUNT(*) FROM dbo.ingestion_runs h
      WHERE h.station_id = r.station_id AND h.status = 'failed'
        AND h.started_utc > DATEADD(DAY, -1, SYSUTCDATETIME())) AS failures_last_24h,
    (SELECT COUNT(*) FROM dbo.ingestion_runs h
      WHERE h.station_id = r.station_id
        AND h.started_utc > DATEADD(DAY, -1, SYSUTCDATETIME())) AS runs_last_24h
FROM ranked AS r
LEFT JOIN dbo.stations AS s ON s.station_id = r.station_id
WHERE r.recency_rank = 1;
GO


/* ===========================================================================
   v_data_quality — what is missing, stated as a number.
   ---------------------------------------------------------------------------
   Every dataset has holes; the difference between a trustworthy one and a
   misleading one is whether the holes are measured. This view is meant to be
   put on the dashboard, not hidden.
   =========================================================================== */
CREATE OR ALTER VIEW dbo.v_data_quality AS
SELECT
    'liveboard_records'                     AS table_name,
    COUNT(*)                                AS row_count,
    MIN(r.scheduled_departure_local)        AS earliest_departure_local,
    MAX(r.scheduled_departure_local)        AS latest_departure_local,
    COUNT(DISTINCT r.departure_date_local)  AS distinct_dates,
    COUNT(DISTINCT r.station_id)            AS distinct_stations,

    SUM(CASE WHEN r.platform_code IS NULL THEN 1 ELSE 0 END)
                                            AS platform_unknown,
    CONVERT(DECIMAL(5,2), 100.0 * SUM(CASE WHEN r.platform_code IS NULL THEN 1 ELSE 0 END)
                          / NULLIF(COUNT(*), 0)) AS pct_platform_unknown,

    SUM(CASE WHEN r.destination_station_id IS NULL THEN 1 ELSE 0 END)
                                            AS destination_missing,
    SUM(CASE WHEN r.occupancy IS NULL OR r.occupancy = 'unknown' THEN 1 ELSE 0 END)
                                            AS occupancy_unknown,
    CONVERT(DECIMAL(5,2), 100.0 * SUM(CASE WHEN r.occupancy IS NULL OR r.occupancy = 'unknown'
                                           THEN 1 ELSE 0 END)
                          / NULLIF(COUNT(*), 0)) AS pct_occupancy_unknown,

    /* Departures seen exactly once had no chance to have their delay revised,
       so their delay figure is a first impression rather than an outcome.
       A high share here means the capture window is too narrow. */
    SUM(CASE WHEN r.observation_count = 1 THEN 1 ELSE 0 END)
                                            AS observed_once,
    CONVERT(DECIMAL(6,2), AVG(1.0 * r.observation_count)) AS avg_observations,
    SUM(CASE WHEN r.has_left = 1 THEN 1 ELSE 0 END) AS confirmed_departed
FROM dbo.liveboard_records AS r;
GO
