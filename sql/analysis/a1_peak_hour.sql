/* ===========================================================================
   A1 — The Peak Hour Problem, on live data.
   ===========================================================================
   The SQL sprint asked this of the static timetable and got a trap for its
   trouble: counting timetable ROWS said the network peaks at 10:00, while
   counting departures that actually OPERATE said 17:00 — because a midday
   timetable row runs on ~8.8 days a year and a 17:00 row on ~12.4.

   Live liveboard data does not have that trap. Every row here is a real
   departure on a real date, so no annualised weighting is needed.

   It has a different one, though, and this file is written around it: the timer
   samples the weekday peaks every 15 minutes and does not sample the small
   hours at all (see docs/cost_control.md). A raw COUNT(*) per hour would
   therefore report the *capture schedule* as the peak and be perfectly
   circular. So:
     * `departures_per_day` normalises by how many days each hour was observed,
       and is the column to rank on — never the raw count;
     * `days_observed` is selected alongside it, so the coverage behind every
       figure is visible rather than hidden;
     * hours with no coverage at all drop out instead of appearing as zero.

   `departure_dow_local` is ISO (1 = Monday ... 7 = Sunday), so the weekend is
   6 and 7. See the column's comment in 01_schema.sql for why it is not
   DATEPART(WEEKDAY, ...).
   =========================================================================== */

-- --------------------------------------------------------------------------
-- Headline: busiest local hour, normalised for uneven capture.
-- --------------------------------------------------------------------------
WITH coverage AS (
    SELECT
        departure_hour_local,
        CASE WHEN departure_dow_local IN (6, 7) THEN 'weekend' ELSE 'weekday' END
            AS day_type,
        COUNT(*)                                  AS departures,
        COUNT(DISTINCT departure_date_local)      AS days_observed,
        AVG(CASE WHEN is_canceled = 0
                 THEN CONVERT(DECIMAL(10,2), delay_seconds) END) AS avg_delay_s,
        SUM(CONVERT(INT, is_canceled))            AS cancellations,
        SUM(CONVERT(INT, is_on_time_6min))        AS on_time,
        COUNT(is_on_time_6min)                    AS measured
    FROM dbo.liveboard_records
    GROUP BY departure_hour_local,
             CASE WHEN departure_dow_local IN (6, 7) THEN 'weekend' ELSE 'weekday' END
)
SELECT
    departure_hour_local                                       AS hour_local,
    day_type,
    departures,
    days_observed,
    CONVERT(DECIMAL(10,2), 1.0 * departures / days_observed)    AS departures_per_day,
    avg_delay_s,
    cancellations,
    CONVERT(DECIMAL(5,2), 100.0 * on_time / NULLIF(measured, 0)) AS pct_on_time_6min,
    RANK() OVER (PARTITION BY day_type
                 ORDER BY 1.0 * departures / days_observed DESC) AS rank_in_day_type
FROM coverage
WHERE days_observed >= 1
ORDER BY day_type, departures_per_day DESC;


-- --------------------------------------------------------------------------
-- The same question per hub: a network peak and a station peak need not be the
-- same hour, and a station that peaks against the network is a capacity risk.
-- --------------------------------------------------------------------------
SELECT
    station_name,
    departure_hour_local        AS hour_local,
    departures,
    departures_per_day,
    avg_delay_seconds,
    pct_on_time_6min
FROM (
    SELECT
        h.*,
        RANK() OVER (PARTITION BY h.station_id
                     ORDER BY h.departures_per_day DESC) AS hour_rank
    FROM dbo.v_hourly_pressure AS h
    WHERE h.day_type = 'weekday'
) AS ranked
WHERE hour_rank = 1
ORDER BY departures_per_day DESC;


-- --------------------------------------------------------------------------
-- Does the network's busiest hour also run the latest?
-- If the worst delays fall OUTSIDE the busiest hour, congestion is not the
-- cause and adding capacity at the peak would not fix it. Computed from the
-- base table so both rows are genuinely network-wide (v_hourly_pressure is
-- aggregated per station, so a TOP 1 from it would be one station's peak).
-- --------------------------------------------------------------------------
WITH network_hours AS (
    SELECT
        departure_hour_local                     AS hour_local,
        COUNT(*)                                 AS departures,
        COUNT(DISTINCT departure_date_local)     AS days_observed,
        CONVERT(DECIMAL(10,2), 1.0 * COUNT(*)
                / NULLIF(COUNT(DISTINCT departure_date_local), 0))
                                                 AS departures_per_day,
        AVG(CASE WHEN is_canceled = 0
                 THEN CONVERT(DECIMAL(10,2), delay_seconds) END) AS avg_delay_s
    FROM dbo.liveboard_records
    WHERE departure_dow_local NOT IN (6, 7)      -- weekdays only
    GROUP BY departure_hour_local
)
SELECT 'busiest hour' AS metric, hour_local, departures, departures_per_day, avg_delay_s
FROM (SELECT TOP 1 * FROM network_hours ORDER BY departures_per_day DESC) AS busiest
UNION ALL
SELECT 'worst-delay hour', hour_local, departures, departures_per_day, avg_delay_s
FROM (SELECT TOP 1 * FROM network_hours
       WHERE departures >= 10   -- one late train must not crown an empty hour
       ORDER BY avg_delay_s DESC) AS worst;
