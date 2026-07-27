/* ===========================================================================
   RailPulse Cloud — 01_schema.sql
   Azure SQL Database (T-SQL).  Core relational model for live liveboard data.
   ===========================================================================

   GRAIN OF THE MODEL
   ------------------
   The fact table `liveboard_records` holds ONE ROW PER SCHEDULED DEPARTURE
   EVENT — that is, one row per (station observed, vehicle, scheduled departure
   time).  It is *not* one row per API observation.

   That distinction is the single most important design decision in this
   project, so it is worth stating why.  A liveboard call to Brussels-Central
   returns the next ~55 departures.  Poll every 15 minutes and the 17:42 to
   Antwerp comes back in roughly a dozen consecutive responses, each time with
   a possibly-updated delay.  Two models are available:

     (a) append every observation      -> ~12x the rows, needs "latest
                                         observation" logic in every query,
                                         but keeps the full prediction history;
     (b) one row per departure event   -> stable row count, trivially correct
                                         BI queries, idempotent by construction.

   This schema takes (b), and buys back most of what (a) offers by keeping
   observation metadata ON the row: `first_seen_utc` / `last_seen_utc`,
   `observation_count`, and `delay_first_seen_s` beside the latest
   `delay_seconds`.  A query can therefore still ask "how much did this delay
   grow between the first and last time we saw it" without storing twelve
   copies of the row.  What is genuinely lost is the intermediate trajectory
   (the delay at each poll in between); that is documented in docs/schema.md as
   an accepted trade-off, taken because the database is capped at 2 GB and the
   downstream consumer is a BI dashboard, not a forecasting model.

   IDEMPOTENCY
   -----------
   Every load is a MERGE against the natural key
   `UQ_liveboard_records (station_id, vehicle_id, scheduled_departure_utc)`.
   A timer run that overlaps a manual run, a retry after a transient failure,
   or a poll five minutes after the last one all converge to the same table
   state.  Re-running the pipeline can never duplicate a departure.

   NAMING
   ------
   Plural table names, snake_case columns, `dbo` schema.  All timestamps are
   stored twice: `*_utc` (the source of truth) and, for the departure itself,
   `scheduled_departure_local` in Europe/Brussels — because "which hour is
   busiest" is a question about local clock time, and computing it from UTC in
   the BI layer would silently shift the answer by one or two hours depending
   on daylight saving.  See docs/schema.md.

   IDEMPOTENT DDL
   --------------
   Every object is created only if absent, so this file is safe to run against
   a live database as often as you like.  It is applied either by
   `POST /api/admin/migrate` on the Function App or by scripts/apply_schema.py.
   `GO` is a client-side batch separator: the migration runner splits on it.
   =========================================================================== */


/* ===========================================================================
   ingestion_runs — one row per API call + load. The pipeline's audit log.
   ---------------------------------------------------------------------------
   Created first because the fact table carries lineage FKs back to it: every
   departure row knows which run first saw it and which run last touched it.
   That is what makes the dataset defensible rather than merely present — any
   number on the dashboard can be traced to an HTTP response at a point in time.
   =========================================================================== */
IF OBJECT_ID('dbo.ingestion_runs', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.ingestion_runs (
        run_id              BIGINT IDENTITY(1,1)
                            CONSTRAINT pk_ingestion_runs PRIMARY KEY,

        /* Where the run came from. 'timer' is the scheduled NCRONTAB trigger,
           'http' a manual call to /api/ingest, 'local' the local CLI. */
        trigger_source      VARCHAR(10)   NOT NULL
                            CONSTRAINT ck_ingestion_runs_source
                            CHECK (trigger_source IN ('timer', 'http', 'local')),

        /* Azure Functions invocation id — the join key to Application Insights
           when you need the log lines behind a run. */
        invocation_id       VARCHAR(64)   NULL,

        /* What was asked for, verbatim (a station id or a name), and what the
           feed said it actually answered with. They differ when iRail resolves
           a name loosely, which is exactly the sort of drift worth recording. */
        requested_station   NVARCHAR(120) NOT NULL,
        station_id          VARCHAR(24)   NULL,

        api_status_code     INT           NULL,
        api_url             VARCHAR(400)  NULL,
        /* header timestamp of the feed itself, not of our request */
        feed_timestamp_utc  DATETIME2(0)  NULL,

        departures_returned INT           NULL,
        rows_inserted       INT           NULL,
        rows_updated        INT           NULL,
        rows_skipped        INT           NULL,   -- duplicate keys within one payload
        stations_upserted   INT           NULL,
        vehicles_upserted   INT           NULL,

        started_utc         DATETIME2(0)  NOT NULL,
        finished_utc        DATETIME2(0)  NULL,
        duration_ms         INT           NULL,

        status              VARCHAR(10)   NOT NULL
                            CONSTRAINT ck_ingestion_runs_status
                            CHECK (status IN ('running', 'success', 'failed')),
        error_message       NVARCHAR(1000) NULL
    );
END
GO


/* ===========================================================================
   stations — the station dimension.
   ---------------------------------------------------------------------------
   Populated from two directions, which is why it is a dimension of its own and
   not a column on the fact:
     * seeded in bulk from iRail's /stations endpoint (714 rows, one call), and
     * upserted opportunistically from every liveboard response, because each
       departure names its destination station.
   `is_hub` is sticky: once a station has been polled as a hub it stays flagged
   even if a later run only ever sees it as somebody else's destination.
   =========================================================================== */
IF OBJECT_ID('dbo.stations', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.stations (
        /* iRail's stable identifier, e.g. 'BE.NMBS.008813003'. Used as the PK
           rather than the bare UIC code because it is what every other payload
           field references — no translation layer, no chance of mismatch. */
        station_id      VARCHAR(24)   NOT NULL
                        CONSTRAINT pk_stations PRIMARY KEY,

        /* The 9-digit UIC code, split out because it is the join key to any
           other European rail dataset (GTFS, SNCB open data, Eurostat). */
        uic_code        CHAR(9)       NOT NULL,

        /* ISO-3166 alpha-2, derived in the loader from UIC digits 3-4
           (88 -> BE, 84 -> NL, 80 -> DE, 87 -> FR, 82 -> LU ...). The feed
           carries no country field, but 137 of its 714 stations are foreign —
           without this column every network-wide average is quietly polluted
           by Amsterdam and Lille. 'XX' when the prefix is unrecognised. */
        country_code    CHAR(2)       NOT NULL
                        CONSTRAINT df_stations_country DEFAULT ('XX'),

        /* Localised name in the language the feed was polled with (default en). */
        name            NVARCHAR(120) NOT NULL,
        /* The operator's official form, which for Brussels is bilingual:
           'Brussel-Centraal/Bruxelles-Central'. Kept because it is the only
           spelling that matches SNCB's own published material. */
        standard_name   NVARCHAR(120) NULL,

        /* DECIMAL, not FLOAT: coordinates are exact decimal values in the feed
           and Power BI map visuals join on equality. 6 dp ~ 0.11 m. */
        latitude        DECIMAL(9,6)  NULL,
        longitude       DECIMAL(9,6)  NULL,

        irail_url       VARCHAR(200)  NULL,

        is_hub          BIT           NOT NULL
                        CONSTRAINT df_stations_is_hub DEFAULT (0),

        first_seen_utc  DATETIME2(0)  NOT NULL,
        last_seen_utc   DATETIME2(0)  NOT NULL
    );
END
GO


/* ===========================================================================
   vehicle_types — reference table for the service class of a train.
   ---------------------------------------------------------------------------
   The feed reports `vehicleinfo.type` as 'IC', 'L', 'EUR' ... but for suburban
   services it reports the *line*: 'S1', 'S10', 'S32'. Storing that raw string
   as the type would create a new "type" every time the operator opens an
   S-line, and would make "how do suburban trains perform" impossible to ask
   without a LIKE 'S%' scan. The loader therefore splits it into a family code
   (S) and a line number (1), and this table holds the families.

   AUTO-EXTENSION, ON PURPOSE
   The loader inserts any unseen family code with `is_seeded = 0` rather than
   letting the foreign key reject the row. A new service class must never be
   able to stop the pipeline; it should show up as an unlabelled code in a
   data-quality view and be labelled here afterwards. This is the same
   soft-reference reasoning used for real-time trip ids in the SQL sprint.
   =========================================================================== */
IF OBJECT_ID('dbo.vehicle_types', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.vehicle_types (
        type_code   VARCHAR(8)    NOT NULL
                    CONSTRAINT pk_vehicle_types PRIMARY KEY,
        label       NVARCHAR(60)  NOT NULL,
        description NVARCHAR(300) NULL,
        /* 1 = shipped in 04_seed_reference.sql, 0 = discovered by the loader. */
        is_seeded   BIT           NOT NULL
                    CONSTRAINT df_vehicle_types_seeded DEFAULT (0)
    );
END
GO


/* ===========================================================================
   vehicles — the train dimension.
   ---------------------------------------------------------------------------
   A "vehicle" in this feed is a train *run* identified by its number
   (BE.NMBS.IC1832), not a physical carriage set. The same number recurs daily,
   which is precisely what makes it a useful dimension: "is IC 1832 always
   late" is a one-line GROUP BY.
   =========================================================================== */
IF OBJECT_ID('dbo.vehicles', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.vehicles (
        vehicle_id     VARCHAR(40)   NOT NULL
                       CONSTRAINT pk_vehicles PRIMARY KEY,   -- 'BE.NMBS.S11958'

        short_name     NVARCHAR(40)  NULL,                   -- 'S1 1958'
        vehicle_number VARCHAR(12)   NULL,                   -- '1958'
        type_raw       VARCHAR(12)   NULL,                   -- 'S1' as published
        type_code      VARCHAR(8)    NOT NULL,               -- 'S'  (family)
        service_line   VARCHAR(8)    NULL,                   -- '1'  (S-line only)

        irail_url      VARCHAR(200)  NULL,
        first_seen_utc DATETIME2(0)  NOT NULL,
        last_seen_utc  DATETIME2(0)  NOT NULL,

        CONSTRAINT fk_vehicles_type FOREIGN KEY (type_code)
            REFERENCES dbo.vehicle_types (type_code)
    );
END
GO


/* ===========================================================================
   platforms — station-scoped platform dimension.
   ---------------------------------------------------------------------------
   The primary key is the composite (station_id, platform_code), because
   "platform 4" only means something inside a station. Modelling it this way
   makes the fact table's foreign key composite too, which is what enforces the
   real-world rule that a departure cannot use a platform belonging to another
   station — an integrity guarantee a single `platform_code` column on the fact
   simply cannot give you.

   Platforms are discovered, not seeded: the feed publishes no platform
   inventory, so a station's platform set grows as departures are observed
   using them.

   The feed reports an unknown platform as the literal string '?'. That is
   normalised to NULL on the fact row, which also neatly sidesteps the FK
   (a composite foreign key with a NULL member is not checked), so unallocated
   departures load without needing a fake '?' platform row.
   =========================================================================== */
IF OBJECT_ID('dbo.platforms', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.platforms (
        station_id     VARCHAR(24)  NOT NULL,
        platform_code  VARCHAR(8)   NOT NULL,
        first_seen_utc DATETIME2(0) NOT NULL,
        last_seen_utc  DATETIME2(0) NOT NULL,

        CONSTRAINT pk_platforms PRIMARY KEY (station_id, platform_code),
        CONSTRAINT fk_platforms_station FOREIGN KEY (station_id)
            REFERENCES dbo.stations (station_id)
    );
END
GO


/* ===========================================================================
   liveboard_records — THE FACT TABLE. One row per scheduled departure event.
   =========================================================================== */
IF OBJECT_ID('dbo.liveboard_records', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.liveboard_records (
        /* Narrow surrogate key. Clustered, so the table is a physically
           append-ordered heap-with-index: new departures always land at the end
           and never split a page. The natural key gets its own non-clustered
           unique index below — that is the one MERGE seeks on. */
        record_id                BIGINT IDENTITY(1,1)
                                 CONSTRAINT pk_liveboard_records PRIMARY KEY CLUSTERED,

        /* ---- the natural key: what makes a departure event unique ---------- */
        /* The station whose liveboard was polled — i.e. where this departure
           departs FROM. */
        station_id               VARCHAR(24)   NOT NULL,
        vehicle_id               VARCHAR(40)   NOT NULL,
        /* Timetabled departure, UTC. The feed's `time` field is the SCHEDULED
           time; `delay` is added on top of it, never folded into it. */
        scheduled_departure_utc  DATETIME2(0)  NOT NULL,

        /* Same instant in Europe/Brussels, computed in the loader with a real
           tz database rather than a fixed +1/+2 offset. Every "which hour is
           busiest" question uses this column; see the file header. */
        scheduled_departure_local DATETIME2(0) NOT NULL,

        /* ---- attributes --------------------------------------------------- */
        /* In a liveboard *departures* payload, each entry's `stationinfo` is the
           train's TERMINUS, not the station you queried. Naming it
           `destination_station_id` removes an easy and expensive misreading. */
        destination_station_id   VARCHAR(24)   NULL,

        platform_code            VARCHAR(8)    NULL,   -- NULL when feed says '?'
        /* platforminfo.normal = 0 means the train was moved off its usual
           platform: a first-class disruption signal, and the reason this is a
           column rather than being discarded. */
        platform_is_normal       BIT           NULL,

        delay_seconds            INT           NOT NULL
                                 CONSTRAINT df_lbr_delay DEFAULT (0),
        is_canceled              BIT           NOT NULL
                                 CONSTRAINT df_lbr_canceled DEFAULT (0),
        /* 1 once the feed reports the train as having left. Distinguishes an
           observed outcome from a still-open prediction. */
        has_left                 BIT           NOT NULL
                                 CONSTRAINT df_lbr_left DEFAULT (0),
        /* An unscheduled extra service, not in the published timetable. */
        is_extra                 BIT           NOT NULL
                                 CONSTRAINT df_lbr_extra DEFAULT (0),

        /* low | medium | high | unknown — crowd-sourced by iRail's app users. */
        occupancy                VARCHAR(10)   NULL,
        /* iRail's own connection URI: station/date/vehicle. Kept for
           traceability back to the source record. */
        departure_connection     VARCHAR(200)  NULL,

        /* ---- observation bookkeeping (see file header) --------------------- */
        first_seen_utc           DATETIME2(0)  NOT NULL,
        last_seen_utc            DATETIME2(0)  NOT NULL,
        observation_count        INT           NOT NULL
                                 CONSTRAINT df_lbr_obs DEFAULT (1),
        /* The delay the very first time we saw this departure. Compared with
           `delay_seconds` it shows how a delay developed as the hour
           approached — the cheap 90% of a full observation log. */
        delay_first_seen_s       INT           NOT NULL
                                 CONSTRAINT df_lbr_delay_first DEFAULT (0),

        /* ---- lineage ------------------------------------------------------ */
        first_seen_run_id        BIGINT        NOT NULL,
        last_seen_run_id         BIGINT        NOT NULL,

        /* ---- derived columns, PERSISTED ----------------------------------- */
        /* Computed in the database rather than in the loader so they can never
           drift from their inputs, and PERSISTED so BI queries filter and
           group on stored values (a non-persisted computed column would be
           re-evaluated per row and could not be indexed). All expressions are
           deterministic, which PERSISTED requires. */
        departure_date_local     AS CONVERT(DATE, scheduled_departure_local) PERSISTED,
        departure_hour_local     AS DATEPART(HOUR, scheduled_departure_local) PERSISTED,

        /* ISO day of week: 1 = Monday ... 7 = Sunday.
           Weekend is therefore 6 and 7 everywhere in this project — in the views
           and in sql/analysis/ alike.

           Getting this to persist took three attempts, and all three failures
           were the same error (4936, "cannot be persisted because the column is
           non-deterministic") for three different reasons. Tested against Azure
           SQL rather than reasoned about, because the rules are not guessable:

             DATEPART(WEEKDAY, d)                          REJECTED — the weekday
               datepart depends on the session's SET DATEFIRST.
             DATEDIFF(DAY, '19000101', d)                  REJECTED — the implicit
               string-to-date conversion of the anchor is itself
               non-deterministic (it depends on DATEFORMAT/language).
             DATEDIFF(DAY, CONVERT(DATE,'1900-01-01',23), d)  REJECTED — an
               explicit style does not rescue a conversion whose TARGET is DATE.
             DATEDIFF(DAY, 0, d)                           ACCEPTED — integer 0
               converts to 1900-01-01 with no locale involved at all.

           1900-01-01 was a Monday, so counting whole days from it and taking
           mod 7 lands Monday on 1. Verified: 2026-07-27 (Mon) -> 1,
           2026-08-01 (Sat) -> 6, 2026-08-02 (Sun) -> 7. */
        departure_dow_local      AS ((DATEDIFF(DAY, 0,
                                       scheduled_departure_local) % 7) + 1) PERSISTED,

        actual_departure_utc     AS DATEADD(SECOND, delay_seconds, scheduled_departure_utc) PERSISTED,
        delay_minutes            AS CONVERT(DECIMAL(8,2), delay_seconds / 60.0) PERSISTED,
        /* How much the delay grew after we first saw the departure. */
        delay_growth_s           AS (delay_seconds - delay_first_seen_s) PERSISTED,

        /* Punctuality is reported at two thresholds on purpose. 120 s is the
           threshold used in the SQL sprint; 360 s is SNCB's own published
           definition of a late train. Storing both means a dashboard never has
           to silently pick one, and NULL for a cancelled train keeps
           cancellations out of the punctuality denominator instead of
           flattering the operator with a delay of zero. */
        is_on_time_2min          AS CONVERT(BIT, CASE WHEN is_canceled = 1 THEN NULL
                                                      WHEN delay_seconds < 120 THEN 1
                                                      ELSE 0 END) PERSISTED,
        is_on_time_6min          AS CONVERT(BIT, CASE WHEN is_canceled = 1 THEN NULL
                                                      WHEN delay_seconds < 360 THEN 1
                                                      ELSE 0 END) PERSISTED,

        delay_bucket             AS CONVERT(VARCHAR(12),
                                     CASE WHEN is_canceled = 1        THEN 'cancelled'
                                          WHEN delay_seconds < 60     THEN 'on time'
                                          WHEN delay_seconds < 300    THEN '1-5 min'
                                          WHEN delay_seconds < 900    THEN '5-15 min'
                                          WHEN delay_seconds < 1800   THEN '15-30 min'
                                          ELSE '30+ min' END) PERSISTED,
        /* Buckets must sort by severity, not alphabetically ('1-5 min' before
           'on time' would be nonsense on an axis). BI tools sort a label by a
           companion numeric column; this is it. */
        delay_bucket_order       AS CONVERT(TINYINT,
                                     CASE WHEN is_canceled = 1        THEN 6
                                          WHEN delay_seconds < 60     THEN 1
                                          WHEN delay_seconds < 300    THEN 2
                                          WHEN delay_seconds < 900    THEN 3
                                          WHEN delay_seconds < 1800   THEN 4
                                          ELSE 5 END) PERSISTED,

        /* ---- constraints -------------------------------------------------- */
        CONSTRAINT uq_liveboard_records
            UNIQUE (station_id, vehicle_id, scheduled_departure_utc),

        CONSTRAINT fk_lbr_station FOREIGN KEY (station_id)
            REFERENCES dbo.stations (station_id),
        CONSTRAINT fk_lbr_destination FOREIGN KEY (destination_station_id)
            REFERENCES dbo.stations (station_id),
        CONSTRAINT fk_lbr_vehicle FOREIGN KEY (vehicle_id)
            REFERENCES dbo.vehicles (vehicle_id),
        /* Composite: guarantees the platform belongs to the departure station. */
        CONSTRAINT fk_lbr_platform FOREIGN KEY (station_id, platform_code)
            REFERENCES dbo.platforms (station_id, platform_code),
        CONSTRAINT fk_lbr_first_run FOREIGN KEY (first_seen_run_id)
            REFERENCES dbo.ingestion_runs (run_id),
        CONSTRAINT fk_lbr_last_run FOREIGN KEY (last_seen_run_id)
            REFERENCES dbo.ingestion_runs (run_id),

        /* Deliberately wide. A CHECK on a fact table is a tripwire, not a
           cleaning step: it must catch a feed that has started publishing
           milliseconds or garbage, without ever rejecting a genuinely
           extraordinary delay and taking the whole batch down with it.
           (-3600: the feed reports 0 for early trains, but a future version
           reporting negative delays should load, not fail.) */
        CONSTRAINT ck_lbr_delay_sane
            CHECK (delay_seconds BETWEEN -3600 AND 172800),
        CONSTRAINT ck_lbr_obs_positive
            CHECK (observation_count > 0),
        CONSTRAINT ck_lbr_seen_order
            CHECK (last_seen_utc >= first_seen_utc)
    );
END
GO
