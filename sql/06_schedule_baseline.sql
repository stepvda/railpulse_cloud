/* ===========================================================================
   RailPulse Cloud — 06_schedule_baseline.sql
   The static timetable, joined to the live observations.
   ===========================================================================

   THE BLIND SPOT THIS FIXES
   Everything else in this warehouse is observation: a row exists because the
   pipeline saw a departure on a liveboard. That makes one question unanswerable,
   and it is the question that governs how every other figure should be read —

       when an hour has no departures, is that "no trains" or "we did not look"?

   The pipeline samples weekday peak windows only (docs/cost_control.md), so the
   answer is usually "we did not look" — but nothing in the data says so, and a
   reader cannot tell the two apart. `departures_per_day` in v_hourly_pressure
   normalises by days *observed* precisely because the denominator was missing.

   The static GTFS timetable from sprint 1 IS that denominator. It says what was
   *scheduled*, independently of whether anyone was watching. Joining the two
   turns coverage from a caveat into a measured number, and makes a second thing
   visible for the first time: a train that was scheduled, never appeared on any
   liveboard, and was never flagged cancelled — a SILENT cancellation, which the
   live feed alone cannot distinguish from a train it simply did not show us.

   HOW THE TWO ID SYSTEMS MEET
   Sprint 1 is GTFS: `gs:nmbssncb:S8813003`. Sprint 2 is iRail:
   `BE.NMBS.008813003`. Both embed the UIC code, which is why `stations.uic_code`
   exists as its own CHAR(9) column rather than being left inside the id — the
   comment on it in 01_schema.sql calls it "the join key to any other European
   rail dataset", and this is that debt being collected.

   WHAT IS DELIBERATELY *NOT* LOADED
   Not the 2.17 M-row timetable. The database is capped at 2 GB and the whole
   cost model depends on it staying small. Only the slice that can actually be
   compared is materialised: the polled hubs, on the dates the pipeline has
   observations for. That is a few thousand rows per day instead of millions,
   and it is the only part that answers the question above.
   =========================================================================== */


/* ===========================================================================
   scheduled_departures — what the timetable says SHOULD depart.
   ---------------------------------------------------------------------------
   Loaded by scripts/load_schedule_baseline.py from sprint 1's SQLite build.
   One row per (station, train, scheduled minute) on a given service date.
   =========================================================================== */
IF OBJECT_ID('dbo.scheduled_departures', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.scheduled_departures (
        schedule_id             BIGINT IDENTITY(1,1)
                                CONSTRAINT pk_scheduled_departures PRIMARY KEY,

        /* iRail form, resolved from the GTFS id via the UIC code, so this joins
           straight to the rest of the warehouse with no translation at read
           time. */
        station_id              VARCHAR(24)  NOT NULL,
        /* Kept for traceability back into the sprint-1 database. */
        gtfs_station_id         VARCHAR(60)  NOT NULL,

        service_date            DATE         NOT NULL,
        /* Local Belgian wall-clock, matching liveboard_records.scheduled_
           departure_local exactly — same basis, so the join is a plain equality
           rather than a tolerance window. GTFS times past 24:00:00 have already
           been folded into the following day by the loader. */
        scheduled_departure_local DATETIME2(0) NOT NULL,

        /* The train number, e.g. '1958'. iRail publishes the same number in
           vehicles.vehicle_number, which is what makes a confident match
           possible rather than a time-only guess. */
        train_number            VARCHAR(12)  NULL,
        route_short_name        NVARCHAR(60) NULL,
        /* GTFS trip_headsign — the planned destination, comparable with the
           observed destination_station_id's name. */
        trip_headsign           NVARCHAR(120) NULL,
        /* The PLANNED platform. The live feed reports the actual one, so the two
           together give a platform change measured against the timetable rather
           than against the feed's own `normal` flag. */
        planned_platform        VARCHAR(8)   NULL,
        gtfs_trip_id            VARCHAR(80)  NULL,

        /* Which GTFS feed version this came from. The timetable is reissued
           regularly; without this, comparing observations from July against a
           schedule loaded in December would look like mass cancellation. */
        feed_version            VARCHAR(40)  NULL,
        loaded_utc              DATETIME2(0) NOT NULL
                                CONSTRAINT df_sched_loaded DEFAULT SYSUTCDATETIME(),

        CONSTRAINT fk_sched_station FOREIGN KEY (station_id)
            REFERENCES dbo.stations (station_id),
        /* One scheduled call per train per station per minute. The loader
           de-duplicates on this, and the constraint means a double load cannot
           silently double the denominator — which would halve every coverage
           figure and look like a data-collection failure. */
        CONSTRAINT uq_scheduled_departures
            UNIQUE (station_id, scheduled_departure_local, train_number)
    );
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'ix_sched_station_date'
                 AND object_id = OBJECT_ID('dbo.scheduled_departures'))
BEGIN
    CREATE NONCLUSTERED INDEX ix_sched_station_date
        ON dbo.scheduled_departures (station_id, service_date)
        INCLUDE (scheduled_departure_local, train_number, planned_platform);
END
GO


/* ===========================================================================
   v_schedule_vs_observed — the join, one row per SCHEDULED departure.
   ---------------------------------------------------------------------------
   A LEFT JOIN from the timetable, not an inner join: the whole point is the rows
   that have no match. `match_quality` says how the pairing was made, because a
   time-only match is weaker evidence than a match confirmed by train number and
   should be visible rather than averaged in.
   =========================================================================== */
CREATE OR ALTER VIEW dbo.v_schedule_vs_observed AS
SELECT
    s.schedule_id,
    s.station_id,
    st.name                       AS station_name,
    s.service_date,
    s.scheduled_departure_local,
    DATEPART(HOUR, s.scheduled_departure_local) AS scheduled_hour_local,
    s.train_number,
    s.route_short_name,
    s.trip_headsign,
    s.planned_platform,

    /* -- the observation, if there was one -- */
    m.record_id,
    m.delay_seconds,
    m.is_canceled,
    m.platform_code               AS observed_platform,
    m.observation_count,

    CONVERT(BIT, CASE WHEN m.record_id IS NULL THEN 0 ELSE 1 END) AS was_observed,

    /* A scheduled train that never appeared on any liveboard AND was never
       flagged cancelled. Two very different causes share this bucket — a real
       silent cancellation, and an hour the pipeline simply did not sample — so
       it is only interpretable next to `hour_was_sampled` below. */
    CONVERT(BIT, CASE WHEN m.record_id IS NULL THEN 1 ELSE 0 END) AS unobserved,

    /* Did the pipeline look at this station in this hour at all? Derived from
       the observations themselves rather than from the configured schedule, so
       it stays true if the cadence changes. */
    CONVERT(BIT, CASE WHEN EXISTS (
        SELECT 1 FROM dbo.liveboard_records k
        WHERE k.station_id = s.station_id
          AND k.departure_date_local = s.service_date
          AND k.departure_hour_local = DATEPART(HOUR, s.scheduled_departure_local)
    ) THEN 1 ELSE 0 END) AS hour_was_sampled,

    CASE
        WHEN m.record_id IS NULL                       THEN 'not observed'
        WHEN s.train_number IS NOT NULL
         AND m.vehicle_number = s.train_number         THEN 'time + train number'
        ELSE                                                'time only'
    END AS match_quality,

    /* Planned platform against the platform actually used. The live feed's own
       `platform_is_normal` flag says the operator moved the train; this says the
       train did not depart from the platform the published timetable promised,
       which is the passenger's version of the same question. */
    CONVERT(BIT, CASE
        WHEN m.record_id IS NULL                       THEN NULL
        WHEN s.planned_platform IS NULL
          OR m.platform_code IS NULL                   THEN NULL
        WHEN s.planned_platform <> m.platform_code     THEN 1
        ELSE 0 END) AS departed_from_different_platform
FROM dbo.scheduled_departures AS s
JOIN dbo.stations AS st ON st.station_id = s.station_id

/* OUTER APPLY ... TOP 1, not a LEFT JOIN.
   ---------------------------------------------------------------------------
   A LEFT JOIN on (station, scheduled minute) FANS OUT, and the first version of
   this view did exactly that. Two different trains can be scheduled from one
   station in the same minute — Brussels-Central has a 00:25 to Liege and a 00:25
   to Ostende — so two scheduled rows matched two observed rows and produced
   four. The view returned 24 874 rows for 21 904 scheduled departures, which
   inflated every coverage denominator.

   OUTER APPLY with TOP 1 guarantees AT MOST ONE observation per scheduled
   departure, so the view's row count equals the timetable's by construction.
   The ORDER BY prefers a match confirmed by train number over a bare
   time-and-station coincidence, and `match_quality` reports which was used
   rather than hiding the difference. */
OUTER APPLY (
    SELECT TOP 1
        r.record_id, r.delay_seconds, r.is_canceled, r.platform_code,
        r.observation_count, v.vehicle_number
    FROM dbo.liveboard_records AS r
    LEFT JOIN dbo.vehicles AS v ON v.vehicle_id = r.vehicle_id
    WHERE r.station_id = s.station_id
      AND r.scheduled_departure_local = s.scheduled_departure_local
    ORDER BY
        CASE WHEN v.vehicle_number = s.train_number THEN 0 ELSE 1 END,
        r.record_id
) AS m;
GO


/* ===========================================================================
   v_schedule_coverage — the headline number, per station and hour.
   ---------------------------------------------------------------------------
   This is what the whole file exists to produce: for each station-hour, how many
   trains the timetable scheduled, how many the pipeline saw, and therefore what
   fraction of reality this warehouse actually contains.
   =========================================================================== */
CREATE OR ALTER VIEW dbo.v_schedule_coverage AS
SELECT
    v.station_id,
    v.station_name,
    v.service_date,
    v.scheduled_hour_local,
    MAX(CONVERT(INT, v.hour_was_sampled))        AS hour_was_sampled,

    COUNT(*)                                     AS scheduled,
    SUM(CONVERT(INT, v.was_observed))            AS observed,
    SUM(CONVERT(INT, v.unobserved))              AS not_observed,

    CONVERT(DECIMAL(5,2), 100.0 * SUM(CONVERT(INT, v.was_observed))
            / NULLIF(COUNT(*), 0))               AS coverage_pct,

    /* Cancellations the live feed DID flag — the visible kind. */
    SUM(CASE WHEN v.is_canceled = 1 THEN 1 ELSE 0 END) AS cancelled_observed,

    /* Scheduled, never seen, in an hour the pipeline WAS sampling. This is the
       honest candidate set for a silent cancellation: the pipeline was looking,
       the timetable promised a train, and it never appeared. Still not proof —
       a train can be retimed, or the feed version can predate a schedule change
       — which is why it is named "candidates" and not "cancellations". */
    SUM(CASE WHEN v.unobserved = 1 AND v.hour_was_sampled = 1 THEN 1 ELSE 0 END)
                                                 AS silent_cancellation_candidates
FROM dbo.v_schedule_vs_observed AS v
GROUP BY v.station_id, v.station_name, v.service_date, v.scheduled_hour_local;
GO
