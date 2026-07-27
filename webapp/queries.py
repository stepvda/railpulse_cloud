"""Every SQL statement the dashboard runs, in one file.

THE ANTI-DRIFT SEAM, AND HOW IT DIFFERS FROM SPRINT 1
Sprint 1's dashboard loaded its statements *verbatim* out of the graded
`sql/analysis/qN_*.sql` files, so the report and the deliverable could not
disagree — the same text produced both.

That seam cannot be reused as-is here: these pages are interactive (pick a
station, pick a date range), and a parameterised query is not the same artefact
as a standalone file you can paste into a client. So the seam moved down a layer:
**every statement below reads a VIEW from sql/03_views.sql.** The views are where
this project's definitions live — what counts as on time, whether a cancellation
belongs in the denominator, which local hour a departure falls in, how a delay's
growth is measured. The dashboard therefore cannot disagree with the warehouse
about any of them, because it never computes them.

Two rules this file keeps:

* **No arithmetic in Python.** Every number is produced by SQL Server. pandas
  only carries rows from the driver to Altair.
* **Every statement is shown to the reader.** Each page renders its SQL in a
  "Show the SQL" expander, so any figure can be checked by pasting the statement
  into the portal's Query editor.

Parameter markers are ``%s`` because the app talks to Azure SQL through pymssql
(see data.py). Values are always bound, never formatted into the string.
"""

from __future__ import annotations

# ==========================================================================
# Overview
# ==========================================================================
KPI_HEADER = """
SELECT
    COUNT(*)                                    AS departures,
    COUNT(DISTINCT station_id)                  AS stations_covered,
    COUNT(DISTINCT vehicle_id)                  AS distinct_vehicles,
    COUNT(DISTINCT departure_date_local)        AS days_covered,
    CONVERT(DECIMAL(6,1), AVG(CASE WHEN is_canceled = 0
        THEN CONVERT(DECIMAL(10,2), delay_seconds) END))    AS avg_delay_seconds,
    CONVERT(DECIMAL(5,2), 100.0 * SUM(CONVERT(INT, is_on_time_6min))
        / NULLIF(COUNT(is_on_time_6min), 0))               AS pct_on_time_6min,
    CONVERT(DECIMAL(5,2), 100.0 * SUM(CONVERT(INT, is_on_time_2min))
        / NULLIF(COUNT(is_on_time_2min), 0))               AS pct_on_time_2min,
    SUM(CONVERT(INT, is_canceled))                          AS cancellations,
    SUM(CASE WHEN platform_is_normal = 0 THEN 1 ELSE 0 END) AS platform_changes,
    MIN(scheduled_departure_local)                          AS earliest_local,
    MAX(scheduled_departure_local)                          AS latest_local
FROM dbo.v_departures;
"""

#: Departures per local hour across the whole capture, for the overview sparkline.
#: `departures_per_day` rather than a raw count, because the timer samples the
#: weekday peaks harder than the rest of the day — see docs/cost_control.md.
OVERVIEW_BY_HOUR = """
SELECT
    departure_hour_local                        AS hour_local,
    SUM(departures)                             AS departures,
    CONVERT(DECIMAL(10,2), AVG(departures_per_day)) AS departures_per_day,
    CONVERT(DECIMAL(8,1), AVG(avg_delay_seconds))   AS avg_delay_seconds
FROM dbo.v_hourly_pressure
GROUP BY departure_hour_local
ORDER BY hour_local;
"""

DELAY_BUCKETS = """
SELECT
    delay_bucket,
    delay_bucket_order,
    SUM(departures)                             AS departures
FROM dbo.v_delay_distribution
GROUP BY delay_bucket, delay_bucket_order
ORDER BY delay_bucket_order;
"""

# ==========================================================================
# Live departures
# ==========================================================================
#: The station picker's options. Only hubs are polled, so only hubs can appear
#: as an origin — but the list is read from the data rather than hard-coded, so a
#: widened RAILPULSE_HUBS setting shows up here without a redeploy.
ORIGIN_STATIONS = """
SELECT DISTINCT
    d.station_id,
    d.station_name
FROM dbo.v_departures AS d
ORDER BY d.station_name;
"""

#: One row per departure, newest schedule first. TOP is bound, not formatted.
LIVE_DEPARTURES = """
SELECT TOP (%s)
    d.scheduled_departure_local,
    d.station_name,
    d.destination_name,
    d.platform_label,
    d.vehicle_name,
    d.vehicle_type,
    d.delay_minutes,
    d.delay_bucket,
    d.is_canceled,
    d.platform_is_normal,
    d.occupancy,
    d.observation_count,
    d.delay_growth_s,
    d.last_seen_utc
FROM dbo.v_departures AS d
WHERE (%s = '' OR d.station_id = %s)
  AND (%s = 0 OR d.is_canceled = 1)
  AND (%s = 0 OR d.delay_seconds >= 360)
ORDER BY d.scheduled_departure_local DESC;
"""

# ==========================================================================
# Hub leaderboard
# ==========================================================================
#: Aggregated across dates from the per-date view, so selecting one day or a
#: whole week cannot double-count. The reliability score charges a cancellation
#: the same as a train 6+ minutes late — a judgement, kept visible here rather
#: than hidden in a dashboard measure.
HUB_LEADERBOARD = """
WITH totals AS (
    SELECT
        p.station_id,
        p.station_name,
        SUM(p.departures_observed)              AS departures,
        SUM(p.trains_measured)                  AS measured,
        SUM(p.cancellations)                    AS cancellations,
        SUM(p.platform_changes)                 AS platform_changes,
        COUNT(DISTINCT p.departure_date_local)  AS days_covered,
        SUM(p.total_delay_seconds)              AS total_delay_seconds,
        MAX(p.max_delay_seconds)                AS worst_delay_seconds,
        SUM(p.trains_measured * p.pct_on_time_6min / 100.0) AS on_time_6,
        SUM(p.trains_measured * p.pct_on_time_2min / 100.0) AS on_time_2
    FROM dbo.v_station_punctuality AS p
    WHERE p.station_is_hub = 1
    GROUP BY p.station_id, p.station_name
)
SELECT
    station_name,
    departures,
    days_covered,
    CONVERT(DECIMAL(8,1), 1.0 * total_delay_seconds / NULLIF(measured, 0))
                                                AS avg_delay_seconds,
    worst_delay_seconds,
    CONVERT(DECIMAL(5,2), 100.0 * on_time_6 / NULLIF(measured, 0))
                                                AS pct_on_time_6min,
    CONVERT(DECIMAL(5,2), 100.0 * on_time_2 / NULLIF(measured, 0))
                                                AS pct_on_time_2min,
    cancellations,
    platform_changes,
    CONVERT(DECIMAL(5,2), 100.0 * (on_time_6 - cancellations) / NULLIF(measured, 0))
                                                AS reliability_score
FROM totals
ORDER BY reliability_score DESC;
"""

#: Punctuality per station per day — the leaderboard's trend over time.
HUB_TREND = """
SELECT
    station_name,
    departure_date_local,
    departures_observed,
    pct_on_time_6min,
    avg_delay_seconds
FROM dbo.v_station_punctuality
WHERE station_is_hub = 1
ORDER BY departure_date_local, station_name;
"""

# ==========================================================================
# Peak hours
# ==========================================================================
#: The live continuation of sprint 1's Q1. `departures_per_day` is the column to
#: rank on: a raw COUNT(*) per hour would report the capture schedule as the peak
#: and be perfectly circular. `days_observed` is selected alongside so the
#: coverage behind every figure is visible.
HOURLY_PRESSURE = """
SELECT
    h.departure_hour_local                      AS hour_local,
    h.day_type,
    SUM(h.departures)                           AS departures,
    MAX(h.days_covered)                         AS days_observed,
    CONVERT(DECIMAL(10,2), SUM(h.departures) * 1.0 / NULLIF(MAX(h.days_covered), 0))
                                                AS departures_per_day,
    CONVERT(DECIMAL(8,1), AVG(h.avg_delay_seconds)) AS avg_delay_seconds,
    CONVERT(DECIMAL(5,2), AVG(h.pct_on_time_6min))  AS pct_on_time_6min,
    SUM(h.cancellations)                        AS cancellations
FROM dbo.v_hourly_pressure AS h
WHERE (%s = '' OR h.station_id = %s)
GROUP BY h.departure_hour_local, h.day_type
ORDER BY h.day_type, hour_local;
"""

# ==========================================================================
# Platform bottlenecks
# ==========================================================================
#: Sprint 1's Q2 on live data. Departures with an unallocated platform arrive as
#: 'unknown' rather than being dropped — at some hubs they are a material share,
#: and discarding them would understate the station total while leaving every
#: percentage looking fine.
PLATFORM_PRESSURE = """
SELECT
    platform_label,
    departures,
    days_covered,
    CONVERT(DECIMAL(10,2), 1.0 * departures / NULLIF(days_covered, 0))
                                                AS departures_per_day,
    peak_hour_departures,
    distinct_vehicles,
    avg_delay_seconds,
    pct_on_time_6min,
    cancellations,
    platform_changes,
    CONVERT(DECIMAL(5,2), 100.0 * departures / SUM(departures) OVER ())
                                                AS pct_of_station
FROM dbo.v_platform_pressure
WHERE station_id = %s
ORDER BY departures DESC;
"""

#: Central vs Midi vs North: the pressure differential sprint 1 found in the
#: static timetable, measured on live data. Platforms actually in use, not a
#: published inventory — the feed publishes none.
BRUSSELS_COMPARISON = """
SELECT
    station_name,
    COUNT(*)                                    AS platforms_in_use,
    SUM(departures)                             AS departures,
    CONVERT(DECIMAL(10,2), 1.0 * SUM(departures) / COUNT(*))
                                                AS departures_per_platform,
    MAX(peak_hour_departures)                   AS busiest_platform_hour,
    CONVERT(DECIMAL(8,1), AVG(avg_delay_seconds)) AS avg_delay_seconds,
    SUM(platform_changes)                       AS platform_changes
FROM dbo.v_platform_pressure
WHERE station_id IN ('BE.NMBS.008813003', 'BE.NMBS.008814001', 'BE.NMBS.008812005')
  AND platform_label <> 'unknown'
GROUP BY station_name
ORDER BY departures_per_platform DESC;
"""

# ==========================================================================
# Delay evolution — only possible because the pipeline polls repeatedly
# ==========================================================================
#: `delay_first_seen_s` is the delay the first time a departure appeared on a
#: liveboard; `delay_seconds` is the latest reading. The difference measures how
#: a delay DEVELOPED as departure approached — the difference between "the 17:42
#: was late" and "the 17:42 was on time until 20 minutes before it left".
DELAY_TRAJECTORY = """
SELECT
    CASE
        WHEN observation_count = 1  THEN 'seen once (no revision possible)'
        WHEN delay_growth_s >  300  THEN 'deteriorated by 5+ min'
        WHEN delay_growth_s >   60  THEN 'deteriorated by 1-5 min'
        WHEN delay_growth_s <  -60  THEN 'recovered'
        ELSE                             'held steady'
    END                                         AS trajectory,
    COUNT(*)                                    AS departures,
    CONVERT(DECIMAL(5,2), 100.0 * COUNT(*) / SUM(COUNT(*)) OVER ())
                                                AS pct_of_all,
    CONVERT(DECIMAL(8,1), AVG(1.0 * delay_first_seen_s)) AS avg_first_delay_s,
    CONVERT(DECIMAL(8,1), AVG(1.0 * delay_seconds))      AS avg_final_delay_s,
    CONVERT(DECIMAL(6,2), AVG(1.0 * observation_count))  AS avg_observations
FROM dbo.v_departures
GROUP BY
    CASE
        WHEN observation_count = 1  THEN 'seen once (no revision possible)'
        WHEN delay_growth_s >  300  THEN 'deteriorated by 5+ min'
        WHEN delay_growth_s >   60  THEN 'deteriorated by 1-5 min'
        WHEN delay_growth_s <  -60  THEN 'recovered'
        ELSE                             'held steady'
    END
ORDER BY departures DESC;
"""

#: How much warning a passenger got. A train announced on time that left 15
#: minutes late is a different operational failure from one flagged an hour ahead.
WORST_DETERIORATIONS = """
SELECT TOP (%s)
    d.station_name,
    d.vehicle_name,
    d.destination_name,
    d.scheduled_departure_local,
    CONVERT(DECIMAL(6,1), d.delay_first_seen_s / 60.0) AS first_reported_min,
    d.delay_minutes                             AS final_delay_min,
    CONVERT(DECIMAL(6,1), d.delay_growth_s / 60.0)     AS growth_min,
    d.observation_count,
    DATEDIFF(MINUTE, d.first_seen_utc, d.scheduled_departure_utc) AS minutes_of_notice,
    d.is_canceled
FROM dbo.v_departures AS d
WHERE d.observation_count > 1
  AND d.delay_growth_s > 60
ORDER BY d.delay_growth_s DESC;
"""

#: Same train number late on several days is a timetabling problem; one bad day
#: is weather. This is the query that tells them apart.
REPEAT_OFFENDERS = """
SELECT TOP (%s)
    d.vehicle_name,
    d.vehicle_type,
    d.station_name,
    d.destination_name,
    COUNT(*)                                    AS observations,
    COUNT(DISTINCT d.departure_date_local)      AS days_seen,
    CONVERT(DECIMAL(8,1), AVG(1.0 * d.delay_seconds)) AS avg_delay_s,
    MAX(d.delay_seconds)                        AS worst_delay_s,
    SUM(CONVERT(INT, d.is_canceled))            AS times_cancelled,
    CONVERT(DECIMAL(5,2), 100.0 * SUM(CONVERT(INT, d.is_on_time_6min))
        / NULLIF(COUNT(d.is_on_time_6min), 0))  AS pct_on_time_6min
FROM dbo.v_departures AS d
GROUP BY d.vehicle_name, d.vehicle_type, d.station_name, d.destination_name
HAVING COUNT(*) >= 2
ORDER BY avg_delay_s DESC;
"""

# ==========================================================================
# Service classes and destinations
# ==========================================================================
VEHICLE_TYPE_PERFORMANCE = """
SELECT
    vehicle_type,
    type_code,
    departures,
    distinct_vehicles,
    avg_delay_seconds,
    pct_on_time_6min,
    pct_cancelled,
    type_is_documented
FROM dbo.v_vehicle_type_performance
ORDER BY departures DESC;
"""

TOP_DESTINATIONS = """
SELECT TOP (%s)
    d.destination_name,
    d.destination_country,
    COUNT(*)                                    AS departures,
    COUNT(DISTINCT d.station_id)                AS served_from_hubs,
    COUNT(DISTINCT d.vehicle_id)                AS distinct_services,
    CONVERT(DECIMAL(8,1), AVG(CASE WHEN d.is_canceled = 0
        THEN CONVERT(DECIMAL(10,2), d.delay_seconds) END)) AS avg_delay_seconds,
    CONVERT(DECIMAL(5,2), 100.0 * SUM(CONVERT(INT, d.is_on_time_6min))
        / NULLIF(COUNT(d.is_on_time_6min), 0))  AS pct_on_time_6min,
    SUM(CONVERT(INT, d.is_canceled))            AS cancellations
FROM dbo.v_departures AS d
WHERE d.destination_station_id IS NOT NULL
  AND (%s = 0 OR d.departure_hour_local < 12)
GROUP BY d.destination_name, d.destination_country
ORDER BY departures DESC;
"""

# ==========================================================================
# Data quality and pipeline health
# ==========================================================================
DATA_QUALITY = "SELECT * FROM dbo.v_data_quality;"

TABLE_COUNTS = """
SELECT 'stations' AS table_name, COUNT(*) AS row_count FROM dbo.stations
UNION ALL SELECT 'platforms',         COUNT(*) FROM dbo.platforms
UNION ALL SELECT 'vehicles',          COUNT(*) FROM dbo.vehicles
UNION ALL SELECT 'vehicle_types',     COUNT(*) FROM dbo.vehicle_types
UNION ALL SELECT 'liveboard_records', COUNT(*) FROM dbo.liveboard_records
UNION ALL SELECT 'ingestion_runs',    COUNT(*) FROM dbo.ingestion_runs;
"""

INGESTION_HEALTH = """
SELECT
    station_name,
    is_hub,
    last_run_started_utc,
    last_run_status,
    last_run_departures,
    last_run_inserted,
    last_run_updated,
    last_run_duration_ms,
    minutes_since_last_run,
    is_stale,
    failures_last_24h,
    runs_last_24h
FROM dbo.v_ingestion_health
ORDER BY is_hub DESC, minutes_since_last_run ASC;
"""

RECENT_RUNS = """
SELECT TOP (%s)
    run_id,
    trigger_source,
    requested_station,
    station_id,
    status,
    api_status_code,
    departures_returned,
    rows_inserted,
    rows_updated,
    rows_skipped,
    duration_ms,
    started_utc,
    error_message
FROM dbo.ingestion_runs
ORDER BY run_id DESC;
"""

#: Proof that the deduplication works, straight from the audit log. A healthy
#: pipeline inserts on the first sighting of a departure and revises afterwards,
#: so `rows_updated` should dominate once the window has been polled twice.
RUN_TOTALS_BY_TRIGGER = """
SELECT
    trigger_source,
    COUNT(*)                                    AS runs,
    SUM(rows_inserted)                          AS rows_inserted,
    SUM(rows_updated)                           AS rows_updated,
    SUM(rows_skipped)                           AS rows_skipped,
    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_runs,
    CONVERT(DECIMAL(8,1), AVG(1.0 * duration_ms)) AS avg_duration_ms
FROM dbo.ingestion_runs
GROUP BY trigger_source
ORDER BY trigger_source;
"""

#: The station dimension is seeded from iRail's full catalogue, so it covers the
#: whole network and not only the polled hubs — which is what lets a map show
#: Belgium rather than ten dots.
STATION_MAP = """
SELECT
    name                                        AS station_name,
    country_code,
    latitude,
    longitude,
    is_hub
FROM dbo.stations
WHERE latitude IS NOT NULL
  AND longitude IS NOT NULL
  AND (%s = 0 OR is_hub = 1);
"""
