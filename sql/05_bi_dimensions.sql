/* ===========================================================================
   RailPulse Cloud — 05_bi_dimensions.sql
   Date and hour dimensions, for the BI layer specifically.
   ===========================================================================

   WHY A DATE TABLE EXISTS AT ALL, WHEN EVERY FACT ROW ALREADY HAS A DATE
   `liveboard_records.departure_date_local` is a real DATE column and every SQL
   question in sql/analysis/ groups on it happily. So this table is not here for
   SQL — it is here because **DAX time intelligence does not work without it**.

   `TOTALYTD`, `SAMEPERIODLASTYEAR`, `DATEADD` and every other time-intelligence
   function in Power BI require a table that is:

     * marked as the model's date table,
     * one row per day with **no gaps**, and
     * covering whole years for the period in play.

   Grouping directly on the fact's own date column silently gives wrong answers
   for anything comparative: a day on which no train was observed simply does not
   exist in the fact table, so "average departures per day" over a month divides
   by the days that *have* data rather than the days in the month, and a
   month-over-month comparison skips the gaps instead of showing them as zero.

   The pipeline deliberately samples only weekday peak windows
   (see docs/cost_control.md), so gaps are the normal case here, not an edge
   case. That makes this table load-bearing rather than a formality.

   WHY IT IS A TABLE AND NOT A VIEW
   A view generating rows from a recursive CTE would be recomputed on every
   refresh and, more importantly, Power BI's "Mark as date table" wants a stable
   physical column to key on. 731 rows is nothing.

   RANGE
   2026-01-01 .. 2027-12-31. Whole calendar years either side of the data, which
   is what time intelligence needs; the credit expires 2027-07-27, so the upper
   bound outlives the project.
   =========================================================================== */


/* ===========================================================================
   dim_date — one row per calendar day, contiguous.
   =========================================================================== */
IF OBJECT_ID('dbo.dim_date', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.dim_date (
        date_key        DATE         NOT NULL
                        CONSTRAINT pk_dim_date PRIMARY KEY,

        year_number     SMALLINT     NOT NULL,
        quarter_number  TINYINT      NOT NULL,
        month_number    TINYINT      NOT NULL,
        day_of_month    TINYINT      NOT NULL,

        /* ISO day of week: 1 = Monday .. 7 = Sunday, computed the same
           deterministic way as the fact table's departure_dow_local (a day count
           from a known Monday) so the two can never disagree. DATEPART(WEEKDAY)
           would depend on SET DATEFIRST — see 01_schema.sql. */
        day_of_week_iso TINYINT      NOT NULL,
        iso_week        TINYINT      NOT NULL,

        /* Labels are stored rather than derived in DAX so that every visual —
           and any other consumer — spells a month the same way. English, to
           match `stations.name` being loaded with lang=en. */
        month_name      VARCHAR(12)  NOT NULL,
        month_short     CHAR(3)      NOT NULL,
        day_name        VARCHAR(9)   NOT NULL,
        day_short       CHAR(3)      NOT NULL,

        /* `year_month` sorts correctly as a string (2026-07 < 2026-10), which a
           month name never does. Power BI sorts a label column by a companion
           numeric/sortable column; this is it. */
        year_month      CHAR(7)      NOT NULL,
        year_month_sort INT          NOT NULL,

        is_weekend      BIT          NOT NULL,
        /* The pipeline only samples weekdays by default, so "is this a day the
           pipeline would have collected on" is a genuinely useful filter that
           separates "no trains" from "not observed". */
        is_capture_day  BIT          NOT NULL
    );
END
GO

/* Fill any missing days. Written as a MERGE from a generated range so the file
   is idempotent and so extending the range later is a one-line edit and a re-run
   rather than a truncate.

   The range is generated with a recursive CTE and MAXRECURSION 0 — the classic
   alternatives (a numbers table, or spt_values) either need another object or
   rely on an undocumented system table. */
WITH days AS (
    SELECT CONVERT(DATE, '2026-01-01') AS d
    UNION ALL
    SELECT DATEADD(DAY, 1, d) FROM days WHERE d < CONVERT(DATE, '2027-12-31')
),
enriched AS (
    SELECT
        d                                                   AS date_key,
        DATEPART(YEAR, d)                                   AS year_number,
        DATEPART(QUARTER, d)                                AS quarter_number,
        DATEPART(MONTH, d)                                  AS month_number,
        DATEPART(DAY, d)                                    AS day_of_month,
        ((DATEDIFF(DAY, 0, d) % 7) + 1)                     AS day_of_week_iso,
        DATEPART(ISO_WEEK, d)                               AS iso_week,
        DATENAME(MONTH, d)                                  AS month_name,
        LEFT(DATENAME(MONTH, d), 3)                         AS month_short,
        DATENAME(WEEKDAY, d)                                AS day_name,
        LEFT(DATENAME(WEEKDAY, d), 3)                       AS day_short,
        CONVERT(CHAR(7), FORMAT(d, 'yyyy-MM'))              AS year_month,
        (DATEPART(YEAR, d) * 100 + DATEPART(MONTH, d))      AS year_month_sort
    FROM days
)
MERGE dbo.dim_date WITH (HOLDLOCK) AS target
USING enriched AS source
    ON target.date_key = source.date_key
WHEN NOT MATCHED BY TARGET THEN
    INSERT (date_key, year_number, quarter_number, month_number, day_of_month,
            day_of_week_iso, iso_week, month_name, month_short, day_name,
            day_short, year_month, year_month_sort, is_weekend, is_capture_day)
    VALUES (source.date_key, source.year_number, source.quarter_number,
            source.month_number, source.day_of_month, source.day_of_week_iso,
            source.iso_week, source.month_name, source.month_short,
            source.day_name, source.day_short, source.year_month,
            source.year_month_sort,
            CASE WHEN source.day_of_week_iso IN (6, 7) THEN 1 ELSE 0 END,
            CASE WHEN source.day_of_week_iso IN (6, 7) THEN 0 ELSE 1 END)
OPTION (MAXRECURSION 0);
GO


/* ===========================================================================
   dim_hour — 24 rows, so an hour axis has labels and a peak-window grouping.
   ---------------------------------------------------------------------------
   `v_departures.peak_window` already carries the same classification for a
   departure. This table exists so that a chart can show ALL 24 hours including
   the ones with no departures — which, given the capture schedule, is most of
   them. Without it an hour-of-day visual silently omits the small hours and
   looks like the network stops at midnight.
   =========================================================================== */
IF OBJECT_ID('dbo.dim_hour', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.dim_hour (
        hour_of_day   TINYINT     NOT NULL
                      CONSTRAINT pk_dim_hour PRIMARY KEY,
        hour_label    CHAR(5)     NOT NULL,   -- '07:00'
        peak_window   VARCHAR(16) NOT NULL,
        /* Sort order for the label, because 'evening peak' has no natural place
           next to 'morning peak' alphabetically. */
        window_sort   TINYINT     NOT NULL,
        /* Whether the default timer schedule samples this hour at all. The
           honest companion to any hourly chart: an empty hour here means "not
           observed", not "no trains". */
        is_sampled    BIT         NOT NULL
    );
END
GO

WITH hours AS (
    SELECT 0 AS h
    UNION ALL SELECT h + 1 FROM hours WHERE h < 23
)
MERGE dbo.dim_hour WITH (HOLDLOCK) AS target
USING (
    SELECT
        h                                                   AS hour_of_day,
        RIGHT('0' + CONVERT(VARCHAR(2), h), 2) + ':00'      AS hour_label,
        CASE WHEN h BETWEEN 6  AND 8  THEN 'morning peak'
             WHEN h BETWEEN 16 AND 18 THEN 'evening peak'
             WHEN h BETWEEN 9  AND 15 THEN 'off-peak day'
             ELSE                          'off-peak night' END AS peak_window,
        CASE WHEN h BETWEEN 6  AND 8  THEN 1
             WHEN h BETWEEN 9  AND 15 THEN 2
             WHEN h BETWEEN 16 AND 18 THEN 3
             ELSE                          4 END               AS window_sort,
        -- Mirrors the INGEST_SCHEDULE default: every 15 minutes during local
        -- hours 6-9 and 16-19 on weekdays. Deliberately a LINE comment: the
        -- NCRONTAB spelling of that schedule contains the two characters that
        -- close a T-SQL block comment, so writing it inside one truncates the
        -- comment and the remainder becomes syntax errors. (Cost one failed
        -- batch to discover: "Incorrect syntax near '6'".)
        CASE WHEN h BETWEEN 6 AND 9 OR h BETWEEN 16 AND 19 THEN 1 ELSE 0 END
                                                               AS is_sampled
    FROM hours
) AS source
    ON target.hour_of_day = source.hour_of_day
WHEN MATCHED THEN UPDATE SET
    target.hour_label  = source.hour_label,
    target.peak_window = source.peak_window,
    target.window_sort = source.window_sort,
    target.is_sampled  = source.is_sampled
WHEN NOT MATCHED BY TARGET THEN
    INSERT (hour_of_day, hour_label, peak_window, window_sort, is_sampled)
    VALUES (source.hour_of_day, source.hour_label, source.peak_window,
            source.window_sort, source.is_sampled)
OPTION (MAXRECURSION 0);
GO


/* ===========================================================================
   v_bi_departures — the fact table for the Power BI model.
   ---------------------------------------------------------------------------
   v_departures is already flat and complete; this adds only the two foreign
   keys the model relates on, and nothing else. It exists so that the Power BI
   side does not have to know that `departure_date_local` is the join column and
   `departure_hour_local` the other — the relationship is named in the contract.
   =========================================================================== */
CREATE OR ALTER VIEW dbo.v_bi_departures AS
SELECT
    d.*,
    /* Explicit relationship keys. Same values, unambiguous names. */
    d.departure_date_local AS date_key,
    d.departure_hour_local AS hour_of_day
FROM dbo.v_departures AS d;
GO
