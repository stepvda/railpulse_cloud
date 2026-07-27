/* ===========================================================================
   A4 — Delay evolution and disruptions. Questions the static feed cannot ask.
   ===========================================================================
   These are the analyses that only exist because the pipeline polls repeatedly.
   `delay_first_seen_s` is the delay the very first time a departure appeared on
   a liveboard; `delay_seconds` is the latest reading. The difference between
   them is a measurement of how a delay DEVELOPED as the departure approached —
   which is the difference between "the 17:42 was late" and "the 17:42 was on
   time until 20 minutes before it left".

   That column pair is the cheap 90% of a full observation log, and it is the
   reason the fact table stores one row per departure instead of one row per
   poll. What it cannot show is the intermediate trajectory; see the trade-off
   note in the header of 01_schema.sql.
   =========================================================================== */

-- --------------------------------------------------------------------------
-- Do delays grow, hold, or recover between first sighting and last?
-- --------------------------------------------------------------------------
SELECT
    CASE
        WHEN observation_count = 1        THEN 'seen once (no revision possible)'
        WHEN delay_growth_s >  300        THEN 'deteriorated by 5+ min'
        WHEN delay_growth_s >   60        THEN 'deteriorated by 1-5 min'
        WHEN delay_growth_s <  -60        THEN 'recovered'
        ELSE                                  'held steady'
    END                                                   AS trajectory,
    COUNT(*)                                              AS departures,
    CONVERT(DECIMAL(5,2), 100.0 * COUNT(*) / SUM(COUNT(*)) OVER ())
                                                          AS pct_of_all,
    CONVERT(DECIMAL(8,1), AVG(1.0 * delay_first_seen_s))   AS avg_first_delay_s,
    CONVERT(DECIMAL(8,1), AVG(1.0 * delay_seconds))        AS avg_final_delay_s,
    CONVERT(DECIMAL(8,1), AVG(1.0 * observation_count))    AS avg_observations
FROM dbo.liveboard_records
GROUP BY
    CASE
        WHEN observation_count = 1        THEN 'seen once (no revision possible)'
        WHEN delay_growth_s >  300        THEN 'deteriorated by 5+ min'
        WHEN delay_growth_s >   60        THEN 'deteriorated by 1-5 min'
        WHEN delay_growth_s <  -60        THEN 'recovered'
        ELSE                                  'held steady'
    END
ORDER BY departures DESC;


-- --------------------------------------------------------------------------
-- How much warning did passengers get? A train that was announced on time and
-- left 15 minutes late is an operational failure of a different kind from one
-- that was flagged late an hour ahead.
-- --------------------------------------------------------------------------
SELECT TOP 25
    d.station_name,
    d.vehicle_name,
    d.destination_name,
    d.scheduled_departure_local,
    d.delay_first_seen_s / 60.0     AS first_reported_delay_min,
    d.delay_minutes                 AS final_delay_min,
    d.delay_growth_s / 60.0         AS growth_min,
    d.observation_count,
    DATEDIFF(MINUTE, d.first_seen_utc, d.scheduled_departure_utc)
                                    AS minutes_of_notice,
    d.is_canceled
FROM dbo.v_departures AS d
WHERE d.observation_count > 1
  AND d.delay_growth_s > 300
ORDER BY d.delay_growth_s DESC;


-- --------------------------------------------------------------------------
-- Cancellations and platform changes: the disruption picture.
-- --------------------------------------------------------------------------
SELECT
    d.station_name,
    d.departure_date_local,
    COUNT(*)                                              AS departures,
    SUM(CONVERT(INT, d.is_canceled))                      AS cancellations,
    SUM(CASE WHEN d.platform_is_normal = 0 THEN 1 ELSE 0 END) AS platform_changes,
    SUM(CONVERT(INT, d.is_extra))                         AS extra_services,
    CONVERT(DECIMAL(5,2), 100.0 * SUM(CONVERT(INT, d.is_canceled))
            / NULLIF(COUNT(*), 0))                        AS pct_cancelled
FROM dbo.v_departures AS d
GROUP BY d.station_name, d.departure_date_local
HAVING SUM(CONVERT(INT, d.is_canceled))
       + SUM(CASE WHEN d.platform_is_normal = 0 THEN 1 ELSE 0 END) > 0
ORDER BY cancellations DESC, platform_changes DESC;


-- --------------------------------------------------------------------------
-- Repeat offenders: is it the same train number every day?
-- One bad day is weather. The same service late on four separate days is a
-- timetabling problem, and this is the query that tells them apart.
-- --------------------------------------------------------------------------
SELECT TOP 20
    d.vehicle_name,
    d.vehicle_type,
    d.station_name,
    d.destination_name,
    COUNT(*)                                      AS observations,
    COUNT(DISTINCT d.departure_date_local)        AS days_seen,
    CONVERT(DECIMAL(8,1), AVG(1.0 * d.delay_seconds)) AS avg_delay_s,
    MAX(d.delay_seconds)                          AS worst_delay_s,
    SUM(CONVERT(INT, d.is_canceled))              AS times_cancelled,
    CONVERT(DECIMAL(5,2), 100.0 * SUM(CONVERT(INT, d.is_on_time_6min))
            / NULLIF(COUNT(d.is_on_time_6min), 0)) AS pct_on_time_6min
FROM dbo.v_departures AS d
GROUP BY d.vehicle_name, d.vehicle_type, d.station_name, d.destination_name
HAVING COUNT(DISTINCT d.departure_date_local) >= 2
ORDER BY avg_delay_s DESC;


-- --------------------------------------------------------------------------
-- Occupancy against delay. iRail's occupancy is crowd-sourced from app users,
-- so it is sparse and self-selecting: `pct_occupancy_unknown` in
-- v_data_quality is the number that says whether this is worth reading at all.
-- --------------------------------------------------------------------------
SELECT
    COALESCE(occupancy, 'not reported')          AS occupancy,
    COUNT(*)                                     AS departures,
    CONVERT(DECIMAL(5,2), 100.0 * COUNT(*) / SUM(COUNT(*)) OVER ())
                                                 AS pct_of_all,
    CONVERT(DECIMAL(8,1), AVG(1.0 * delay_seconds)) AS avg_delay_s,
    CONVERT(DECIMAL(5,2), 100.0 * SUM(CONVERT(INT, is_on_time_6min))
            / NULLIF(COUNT(is_on_time_6min), 0)) AS pct_on_time_6min
FROM dbo.liveboard_records
GROUP BY COALESCE(occupancy, 'not reported')
ORDER BY departures DESC;
