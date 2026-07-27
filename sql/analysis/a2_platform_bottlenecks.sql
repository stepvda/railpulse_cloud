/* ===========================================================================
   A2 — Platform bottlenecks at Brussels-Central, on live data.
   ===========================================================================
   The SQL sprint's finding: Brussels-Central handles more annual departures
   than Brussels-Midi across SIX platforms instead of twenty-one — 8,113
   timetabled calls per platform against Midi's 2,085, a 3.9x pressure
   differential with no trough in the day to absorb a disruption.

   Live data lets that be checked against what actually happens, and adds two
   things the timetable could not say: whether the busiest platforms are also
   the least punctual, and how often trains are moved off their booked platform
   (`platform_is_normal = 0`) — a disruption signal that exists only in the
   real-time feed.

   Departures with an unallocated platform are reported as 'unknown' rather than
   dropped. At some hubs they are a material share, and silently discarding them
   would understate the station total while leaving the percentages looking fine.
   =========================================================================== */

DECLARE @station VARCHAR(24) = 'BE.NMBS.008813003';   -- Brussels-Central

-- --------------------------------------------------------------------------
-- Top platforms by observed load.
-- --------------------------------------------------------------------------
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
    CONVERT(DECIMAL(5,2), 100.0 * departures
            / SUM(departures) OVER ())  AS pct_of_station,
    RANK() OVER (ORDER BY departures DESC) AS load_rank
FROM dbo.v_platform_pressure
WHERE station_id = @station
ORDER BY departures DESC;


-- --------------------------------------------------------------------------
-- Central vs Midi: the pressure differential, measured rather than assumed.
-- --------------------------------------------------------------------------
SELECT
    station_name,
    COUNT(*)                                          AS platforms_in_use,
    SUM(departures)                                   AS departures,
    CONVERT(DECIMAL(10,2), 1.0 * SUM(departures) / COUNT(*))
                                                      AS departures_per_platform,
    MAX(peak_hour_departures)                         AS busiest_platform_hour,
    CONVERT(DECIMAL(6,1), AVG(avg_delay_seconds))     AS avg_delay_seconds,
    SUM(platform_changes)                             AS platform_changes
FROM dbo.v_platform_pressure
WHERE station_id IN ('BE.NMBS.008813003',    -- Central
                     'BE.NMBS.008814001',    -- Midi/South
                     'BE.NMBS.008812005')    -- North
  AND platform_label <> 'unknown'
GROUP BY station_name
ORDER BY departures_per_platform DESC;


-- --------------------------------------------------------------------------
-- Is load actually what makes a platform unpunctual?
-- Reported side by side rather than asserted: with a few days of data the
-- honest answer is usually "the ordering is similar but not identical", and
-- claiming causation from twenty observations per platform would be overreach.
-- --------------------------------------------------------------------------
SELECT
    platform_label,
    departures,
    RANK() OVER (ORDER BY departures DESC)          AS load_rank,
    avg_delay_seconds,
    RANK() OVER (ORDER BY avg_delay_seconds DESC)   AS delay_rank,
    RANK() OVER (ORDER BY departures DESC)
      - RANK() OVER (ORDER BY avg_delay_seconds DESC) AS rank_divergence
FROM dbo.v_platform_pressure
WHERE station_id = @station
  AND platform_label <> 'unknown'
  AND departures >= 5          -- below this, one late train dominates the mean
ORDER BY load_rank;


-- --------------------------------------------------------------------------
-- Hour-by-platform grid: where and when the station has no slack.
-- A platform with departures in every hour has nowhere to absorb a delay.
-- --------------------------------------------------------------------------
SELECT
    COALESCE(platform_code, 'unknown')  AS platform_label,
    departure_hour_local                AS hour_local,
    COUNT(*)                            AS departures,
    AVG(CASE WHEN is_canceled = 0
             THEN CONVERT(DECIMAL(10,2), delay_seconds) END) AS avg_delay_s
FROM dbo.liveboard_records
WHERE station_id = @station
GROUP BY COALESCE(platform_code, 'unknown'), departure_hour_local
ORDER BY platform_label, hour_local;
