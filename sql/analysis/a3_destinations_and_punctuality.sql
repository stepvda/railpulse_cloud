/* ===========================================================================
   A3 — Busiest destinations, and the hub punctuality leaderboard.
   ===========================================================================
   Continues two sprint-1 questions on live data:
     * Q3, the busiest morning destinations (static answer: Antwerp-Central,
       Leuven, Charleroi-Central), and
     * the "Network Leaderboard" nice-to-have — which is only now answerable,
       because a static timetable contains no delays at all.

   The leaderboard is the part worth being careful with. Three separate
   definitions decide the ranking, and all three are stated here rather than
   left implicit:
     1. cancellations are EXCLUDED from the delay average and from the on-time
        denominator (they are absences, not late trains) and reported in their
        own column;
     2. "on time" is < 6 minutes, SNCB's own published threshold, with the SQL
        sprint's 2-minute definition available beside it;
     3. a hub needs a minimum number of observations before it is ranked at all —
        with a handful of departures, one broken train decides the league table.
   =========================================================================== */

-- --------------------------------------------------------------------------
-- Morning destinations (departures before 12:00 local) across the network.
-- --------------------------------------------------------------------------
SELECT TOP 15
    d.destination_name,
    d.destination_country,
    COUNT(*)                                        AS morning_departures,
    COUNT(DISTINCT d.station_id)                    AS served_from_hubs,
    COUNT(DISTINCT d.vehicle_id)                    AS distinct_services,
    AVG(CASE WHEN d.is_canceled = 0
             THEN CONVERT(DECIMAL(10,2), d.delay_seconds) END) AS avg_delay_s,
    CONVERT(DECIMAL(5,2), 100.0 * SUM(CONVERT(INT, d.is_on_time_6min))
            / NULLIF(COUNT(d.is_on_time_6min), 0))  AS pct_on_time_6min,
    SUM(CONVERT(INT, d.is_canceled))                AS cancellations
FROM dbo.v_departures AS d
WHERE d.departure_hour_local < 12
  AND d.destination_station_id IS NOT NULL
GROUP BY d.destination_name, d.destination_country
ORDER BY morning_departures DESC;


-- --------------------------------------------------------------------------
-- Morning vs afternoon: does the network's shape change, or just its volume?
-- --------------------------------------------------------------------------
SELECT
    d.destination_name,
    SUM(CASE WHEN d.departure_hour_local < 12 THEN 1 ELSE 0 END) AS morning,
    SUM(CASE WHEN d.departure_hour_local >= 12 THEN 1 ELSE 0 END) AS afternoon,
    CONVERT(DECIMAL(6,2),
            1.0 * SUM(CASE WHEN d.departure_hour_local < 12 THEN 1 ELSE 0 END)
            / NULLIF(SUM(CASE WHEN d.departure_hour_local >= 12 THEN 1 ELSE 0 END), 0))
                                                                 AS morning_ratio
FROM dbo.v_departures AS d
WHERE d.destination_station_id IS NOT NULL
GROUP BY d.destination_name
HAVING COUNT(*) >= 20
ORDER BY morning_ratio DESC;


-- --------------------------------------------------------------------------
-- THE HUB LEADERBOARD. Which city runs the most punctual station?
-- --------------------------------------------------------------------------
WITH hub_departures AS (
    SELECT r.*, s.name AS station_name
    FROM dbo.liveboard_records AS r
    JOIN dbo.stations AS s ON s.station_id = r.station_id
    WHERE s.is_hub = 1
),
/* The median is the honest centre for delays — the mean is dragged upwards by a
   single 90-minute failure. It needs its own CTE because in T-SQL
   PERCENTILE_CONT exists ONLY as a window function: there is no
   `PERCENTILE_CONT(...) GROUP BY` form, and nesting it inside an aggregate is a
   syntax error ("windowed functions cannot be used in the context of another
   windowed function or aggregate"). SELECT DISTINCT over the partition is the
   documented way to collapse it to one row per group. */
percentiles AS (
    SELECT DISTINCT
        station_id,
        PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY delay_seconds)
            OVER (PARTITION BY station_id) AS median_delay_s,
        PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY delay_seconds)
            OVER (PARTITION BY station_id) AS p90_delay_s
    FROM hub_departures
    WHERE is_canceled = 0
),
hub_totals AS (
    SELECT
        station_id,
        station_name,
        COUNT(*)                                 AS departures,
        COUNT(DISTINCT departure_date_local)      AS days_covered,
        COUNT(is_on_time_6min)                    AS measured,
        SUM(CONVERT(INT, is_on_time_6min))        AS on_time_6,
        SUM(CONVERT(INT, is_on_time_2min))        AS on_time_2,
        SUM(CONVERT(INT, is_canceled))            AS cancellations,
        SUM(CASE WHEN platform_is_normal = 0 THEN 1 ELSE 0 END)
                                                 AS platform_changes,
        AVG(CASE WHEN is_canceled = 0
                 THEN CONVERT(DECIMAL(10,2), delay_seconds) END) AS avg_delay_s,
        MAX(CASE WHEN is_canceled = 0 THEN delay_seconds END)    AS worst_delay_s
    FROM hub_departures
    GROUP BY station_id, station_name
)
SELECT
    h.station_name,
    h.departures,
    h.days_covered,
    CONVERT(DECIMAL(5,2), 100.0 * h.on_time_6 / NULLIF(h.measured, 0))
                                                 AS pct_on_time_6min,
    CONVERT(DECIMAL(5,2), 100.0 * h.on_time_2 / NULLIF(h.measured, 0))
                                                 AS pct_on_time_2min,
    h.avg_delay_s,
    CONVERT(DECIMAL(8,1), p.median_delay_s)      AS median_delay_s,
    CONVERT(DECIMAL(8,1), p.p90_delay_s)         AS p90_delay_s,
    h.worst_delay_s,
    h.cancellations,
    CONVERT(DECIMAL(5,2), 100.0 * h.cancellations / NULLIF(h.departures, 0))
                                                 AS pct_cancelled,
    h.platform_changes,
    /* A composite that does not pretend cancellations are free: each one costs
       the station as much as a train 6+ minutes late, because to a passenger it
       is worse. The weighting is a judgement, so it is visible here rather than
       buried in a dashboard measure. */
    CONVERT(DECIMAL(5,2),
            100.0 * (h.on_time_6 - h.cancellations) / NULLIF(h.measured, 0))
                                                 AS reliability_score,
    RANK() OVER (ORDER BY 1.0 * (h.on_time_6 - h.cancellations)
                          / NULLIF(h.measured, 0) DESC) AS reliability_rank
FROM hub_totals AS h
LEFT JOIN percentiles AS p ON p.station_id = h.station_id
-- Below ~30 measured departures a single failure moves the ranking by whole
-- percentage points, so an unranked hub is better than a misleading one.
WHERE h.measured >= 30
ORDER BY reliability_rank;


-- --------------------------------------------------------------------------
-- Service class performance: does an InterCity keep time better than an S-train?
-- --------------------------------------------------------------------------
SELECT
    vehicle_type,
    type_code,
    departures,
    distinct_vehicles,
    avg_delay_seconds,
    pct_on_time_6min,
    pct_cancelled,
    /* 0 flags a class the loader discovered but 04_seed_reference.sql does not
       document — a data-quality signal, not a category. */
    type_is_documented
FROM dbo.v_vehicle_type_performance
WHERE departures >= 20
ORDER BY departures DESC;
