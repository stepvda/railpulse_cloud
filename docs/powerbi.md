# Power BI on top of the warehouse — free, from the browser

Sprint 3's deliverable, brought forward. This is the free path to a Power BI
report over the live Azure SQL warehouse, using the **web client** at
[app.powerbi.com](https://app.powerbi.com) — because Power BI Desktop is
**Windows-only** and this project is developed on macOS.

Everything on the Azure side is already done. What remains is clicking through
the web client, and the two paragraphs on **Import vs DirectQuery** below are the
ones that decide whether this costs €0 or €78 a month.

---

## What "free" actually means here

Checked against the account rather than assumed:

```
$ az rest --url https://graph.microsoft.com/v1.0/me/licenseDetails
  SKU: POWER_BI_STANDARD
      -> BI_AZURE_P0: Success
```

`POWER_BI_STANDARD` is the **Power BI Free** licence, and it is already
provisioned on the `@becode.education` account. Nothing to buy.

What a Free licence gives you, and where the wall is:

| | Free | needs Pro (~$14/user/mo) |
|---|---|---|
| Build reports in the browser | ✅ **My Workspace** | shared workspaces |
| Connect to Azure SQL (no gateway — it is a cloud source) | ✅ | |
| Scheduled refresh | ✅ shared capacity, up to 8/day | more frequent |
| **Share a report with someone else** | ❌ | ✅ |
| Apps, deployment pipelines, dataflows | ❌ | ✅ |

The wall that matters for a graded submission: **a Free licence cannot share.**
Your report lives in My Workspace and only you can open it. For a demo you screen-
share it; to hand someone a link you need Pro (there is a self-service **60-day
Pro trial**, free, if the BeCode tenant allows self-service sign-up).

If sharing turns out to be blocked, the dashboard already deployed at
`https://web-railpulse-cdb4ce.azurewebsites.net` needs **no licence at all** and
is a public URL — see [`webapp.md`](webapp.md). The two are complementary: Power BI
for the graded BI exercise, Streamlit for the thing anyone can just open.

---

## Two routes, and there is no third

**There is no URL that makes the Power BI web client connect to a database.**
Power BI's only pre-filled-connection artifact is a `.pbids` file, and it opens
Power BI *Desktop* — Windows-only. In the Service, creating a data source is an
interactive flow with no documented deep link and no query-string parameters. No
script can route around that; what a script *can* do is create the dataset itself
through the REST API.

| | **A — scripted (model *and* report)** | **B — Azure SQL connection (interactive)** |
|---|---|---|
| how | `python scripts/build_powerbi_report.py` | click through the web client |
| you get | a 7-page report, built | a live, self-refreshing model |
| data | **snapshot** — re-run to refresh | Import + scheduled refresh, 1–2×/day |
| licence | Free ✅ | Free ✅ |
| gateway | none | none (Azure SQL is a cloud source) |
| best for | an instant link, a demo | the graded deliverable |

They are complementary. Route A exists because you asked for a URL and Route B
cannot produce one without a human in the wizard.

### Route A — scripted, gives you a URL immediately

```bash
python scripts/create_bi_reader.py          # once: the least-privilege login
python scripts/build_powerbi_report.py      # model + measures + data + report
python scripts/build_powerbi_report.py --url        # print the URL again later
python scripts/build_powerbi_report.py --data-only  # refresh the rows only
```

`publish_powerbi_dataset.py` is the same thing without the report, if all you
want is the dataset.

It reads the BI views **as `powerbi_reader`** (so it also proves that login is
sufficient for the whole BI surface) and pushes five tables with both
relationships already defined:

```
departures        3 578 rows     dim_date   730     dim_hour   24
pipeline_health      10 rows     data_quality  1
departures[date_key]    -> dim_date[date_key]
departures[hour_of_day] -> dim_hour[hour_of_day]
```

Verified by asking Power BI's own DAX engine, through `executeQueries`, rather
than by trusting the upload:

```
EVALUATE TOPN(5, SUMMARIZECOLUMNS(departures[station_name],
    "Departures", COUNTROWS(departures), "MeanDelay", AVERAGE(departures[delay_seconds])), ...)

  Brussels-Central              661   62.3 s
  Brussels-North                643   55.8 s
  Brussels-South/Midi           547   77.6 s
  Ghent-Sint-Pieters            327   73.9 s
  Antwerp-Central               315   30.5 s
```

**Route A's limits, plainly.** A push dataset holds a snapshot: it has no
connection to Azure SQL, so it never refreshes itself — re-run the script. It also
cannot be marked as a date table through the API, so `TOTALYTD` and friends still
need that one click in the browser (Table tools → Mark as date table →
`date_key`).

---

## The report is generated, not hand-built

`scripts/build_powerbi_report.py` creates the **same seven pages the Streamlit app
has**, as a real Power BI report, through the API. Power BI Desktop is Windows-only,
so the alternative was a written click-by-click guide — which puts the work back
on the reader and drifts from the app the moment either side changes.

```
$ python scripts/build_powerbi_report.py
  created model (ca329922-…) — 5 tables, 13 measures
    departures         8753 rows      dim_hour         24 rows
    dim_date            730 rows      pipeline_health  10 rows
  created report — 7 pages, 31 visuals
```

| page | mirrors the Streamlit page | headline visual |
|---|---|---|
| Overview | Overview | 6 KPI cards, hour histogram, delay buckets |
| Hub leaderboard | Hub leaderboard | punctuality by hub, volume-vs-delay scatter |
| Peak hours | Peak hours | `Departures per day` by hour × day type |
| Platform bottlenecks | Platform bottlenecks | load and mean delay per platform |
| Delay evolution | Delay evolution | first reading vs latest, per train |
| Services & destinations | Services & destinations | service class scatter, top destinations |
| Data quality & pipeline | Data quality + Pipeline | freshness per station, coverage |

**The measures live in the model, not in the visuals.** All 13 are defined once, on
the semantic model, so every visual — and any report anyone builds later over the
same dataset — shares one definition of "on time". That is the same argument
`create_bi_reader.py` makes with permissions and `03_views.sql` makes with SQL.

Verified rather than assumed, by asking Power BI's own DAX engine and comparing
with the warehouse:

```
metric            Azure SQL     Power BI   agree
Departures             8753         8753   yes
OnTime6              0.9645       0.9645   yes
OnTime2              0.9257       0.9257   yes
MeanDelay            51.146       51.146   yes
Cancelled                18           18   yes
PlatChg                 955          955   yes
DaysObs                   6            6   yes
Growth              21.2156      21.2156   yes
Deteriorated            173          173   yes
```

### The report format, and two dead ends

Written down because the API returns the *same* error for all three cases, so this
is not deducible from the message. A Fabric Report item is a set of base64 parts:

| attempt | parts | result |
|---|---|---|
| 1 | `.platform` + `definition.pbir` | `MissingDefinitionParts` |
| 2 | PBIR **enhanced** — `definition/pages/<id>/visuals/…` | `MissingDefinitionParts` |
| 3 | **legacy** — a single root `report.json` | ✅ |

The cause: `definition.pbir` declaring `"version": "1.0"` selects the **legacy**
format, which wants one root-level `report.json` holding the classic Layout JSON.
The service then rewrites that field to `"4.0"` itself, which is how you can tell
the report was genuinely accepted rather than merely stored.

One trap inside the legacy shape: nested `config` and `filters` fields are
**JSON-encoded strings**, not objects. Passing a real object is accepted without
complaint and renders a blank report.

Also worth knowing: deleting a dataset and recreating it under the same name
within a few seconds returns HTTP 500 from Power BI's own store (`Models_V0`, a
system-versioned temporal table). The identical payload succeeds a minute later,
so the script retries that status rather than treating it as fatal.

### Route A's remaining manual step

A push dataset cannot be marked as a date table through the API, so open
**Table tools → Mark as date table → `date_key`** if you want `TOTALYTD`.

### Route B — the interactive connection

The rest of this page. Choose this for the graded deliverable: it is a real live
model with a refresh schedule.

---

## ⚠ Import, not DirectQuery — this is the whole cost story

When the web client asks for a connectivity mode, choose **Import**.

**DirectQuery** issues a SQL query for every visual, every filter click, every
page load. The warehouse is Azure SQL **serverless with a one-hour auto-pause**,
and the entire cost model of this project rests on that database being asleep most
of the time — measured spend so far is **€5.19, of which 99.8% is the database**
([cost_control.md](cost_control.md)). A DirectQuery report left open on a desk, or
a page auto-refreshing, keeps the database permanently awake:

| mode | database awake | est. cost |
|---|---|---|
| **Import**, refresh 2×/day | ~2 × 1 minute | **~€0** |
| DirectQuery, report used through the day | 8–10 h/day | ~€50–78/month |
| DirectQuery, auto-refresh left on | 24 h/day | ~€190/month — credit gone in 2 weeks |

Import is also simply *better* here: the whole model is a few thousand rows, so it
loads in seconds and every interaction afterwards is instant.

Set the refresh schedule to **1–2 times a day, inside a capture window** (the
timer only collects during weekday 06–09 and 16–19 Brussels, so refreshing at
03:00 would copy a stale snapshot and wake the database for nothing).

---

## The credentials Power BI should use

**Not the admin login.** `scripts/create_bi_reader.py` has created a
least-privilege login for exactly this:

```
Server    sql-railpulse-<suffix>.database.windows.net
Database  railpulse
Auth      SQL Server authentication (Basic, in the web client)
          The real server name is SQL_FQDN in .azure-railpulse.env, and
          `./azure/provision.sh` prints it. Kept out of this file on purpose:
          publishing a server's FQDN next to its login name is free
          reconnaissance, and every other doc here uses the placeholder too.
User      powerbi_reader
Password  BI_READER_PASSWORD in .azure-railpulse.env   (gitignored, mode 600)
```

What it can do, verified by connecting as it and trying:

```
CAN read    v_bi_departures, v_departures, v_station_punctuality,
            v_hourly_pressure, v_platform_pressure, v_delay_distribution,
            v_vehicle_type_performance, v_ingestion_health, v_data_quality,
            dim_date, dim_hour
BLOCKED     liveboard_records, stations, platforms, vehicles, vehicle_types,
            ingestion_runs          (explicit DENY, not merely a withheld grant)
BLOCKED     every INSERT / UPDATE / DELETE / DROP
```

That is the project's design turned into a permission. `webapp.md` argues the
views are the BI contract — the place where "on time", "is a cancellation in the
denominator" and "which local hour" are defined. Granting SELECT on views only
means **Power BI cannot bypass those definitions even if someone writes a query
that tries.** The `DENY` is deliberate belt-and-braces: it outranks a role grant,
so the obvious "just add db_datareader to make it work" fix cannot silently hand
over the base tables.

Rotate it with `python scripts/create_bi_reader.py --rotate`; audit it with
`--show`.

**No data gateway is needed.** Azure SQL is a cloud source, so the Power BI
Service connects to it directly, and the server's existing *"Allow Azure services
and resources to access this server"* firewall rule already permits it. Nothing to
install, nothing to open.

---

## Step by step, in the browser

1. Sign in to [app.powerbi.com](https://app.powerbi.com) with the
   `@becode.education` account.
2. **My Workspace** → **New** → **Semantic model** (older tenants:
   *Get data → Databases → Azure SQL Database*).
3. Enter the server and database above, choose **Import**, and authenticate with
   **Basic** / SQL Server authentication using `powerbi_reader`.
4. Select these objects and nothing else:
   - `v_bi_departures` — the fact table
   - `dim_date`, `dim_hour` — the dimensions
   - `v_ingestion_health`, `v_data_quality` — for a "is this data trustworthy" page
   - (`v_station_punctuality`, `v_hourly_pressure`, `v_platform_pressure` are
     pre-aggregated conveniences; the fact table plus dimensions can produce all
     of them, so take them only if you want the SQL to do the work.)
5. **Mark `dim_date` as the date table** (Table tools → Mark as date table →
   `date_key`). Without this, no time-intelligence measure works — this is the
   single most-skipped step in Power BI.
6. Create the two relationships (both many-to-one, single direction):
   - `v_bi_departures[date_key]` → `dim_date[date_key]`
   - `v_bi_departures[hour_of_day]` → `dim_hour[hour_of_day]`
7. Sort the label columns by their companions, or every axis sorts
   alphabetically: `dim_date[year_month]` by `year_month_sort`,
   `dim_hour[peak_window]` by `window_sort`, and
   `v_bi_departures[delay_bucket]` by `delay_bucket_order`.
8. Set the refresh schedule: **Settings → Semantic model → Scheduled refresh**,
   1–2×/day inside a capture window.

### Why `dim_hour` matters more than it looks

The pipeline samples only weekday peak windows. Plot `departure_hour_local`
straight off the fact table and the small hours simply **do not appear** — the
chart implies the network stops at midnight. Relating to `dim_hour` (24 rows, with
an `is_sampled` flag) makes the unsampled hours show as gaps you can label, which
is the honest presentation.

---

## Measures to paste

These mirror the view definitions exactly, so a Power BI number and a SQL number
cannot disagree. Note the `DIVIDE` and the `is_canceled = 0` filters — they are
where the project's definitions live.

```dax
Departures = COUNTROWS('v_bi_departures')

Cancellations = CALCULATE([Departures], 'v_bi_departures'[is_canceled] = TRUE())

-- Cancellations are excluded from every punctuality denominator: a cancelled
-- train is absent, not late. The view already stores NULL in the flags for them,
-- so COUNT of the flag is the correct denominator.
Trains measured = COUNTA('v_bi_departures'[is_on_time_6min])

On time (<6 min) % =
DIVIDE (
    CALCULATE ( [Trains measured], 'v_bi_departures'[is_on_time_6min] = TRUE() ),
    [Trains measured]
)

On time (<2 min) % =
DIVIDE (
    CALCULATE ( [Trains measured], 'v_bi_departures'[is_on_time_2min] = TRUE() ),
    [Trains measured]
)

Mean delay (s) =
CALCULATE (
    AVERAGE ( 'v_bi_departures'[delay_seconds] ),
    'v_bi_departures'[is_canceled] = FALSE()
)

Cancelled % = DIVIDE ( [Cancellations], [Departures] )

Platform changes =
CALCULATE ( [Departures], 'v_bi_departures'[platform_is_normal] = FALSE() )

-- Charges a cancellation the same as a train 6+ minutes late, because to a
-- passenger it is worse. The weighting is a judgement, stated rather than buried.
Reliability score =
DIVIDE (
    CALCULATE ( [Trains measured], 'v_bi_departures'[is_on_time_6min] = TRUE() )
        - [Cancellations],
    [Trains measured]
)

-- Departures per DAY OBSERVED, not per calendar day. The capture schedule
-- samples peaks harder than the rest of the day, so a raw count per hour would
-- report the schedule as the peak and be circular. See sql/analysis/a1.
Days observed = DISTINCTCOUNT ( 'v_bi_departures'[date_key] )
Departures per day = DIVIDE ( [Departures], [Days observed] )

-- Only possible because the pipeline polls the same departure repeatedly.
Delay growth (s) = AVERAGE ( 'v_bi_departures'[delay_growth_s] )
Deteriorated 5+ min =
CALCULATE ( [Departures], 'v_bi_departures'[delay_growth_s] > 300 )

-- Needs dim_date marked as the date table.
Departures YTD = TOTALYTD ( [Departures], 'dim_date'[date_key] )
```

Format `On time %` and `Cancelled %` as percentages and `Reliability score` to 2
decimals; DAX returns them as ratios.

---

## A four-page report that matches the analysis

Route A already builds seven pages for you (above). This is the **Route B**
equivalent — what to build by hand on the interactive model, mirroring
`sql/analysis/` so the Power BI report and the graded SQL answer the same
questions:

1. **Network overview** — KPI row (`Departures`, `On time (<6 min) %`,
   `Mean delay (s)`, `Cancellations`), a map from
   `station_latitude`/`station_longitude`, and `delay_bucket` as a bar chart.
2. **Hub leaderboard** — `station_name` against `On time (<6 min) %` and
   `Reliability score`, with `Departures` as a second axis. The sprint-1 finding
   to look for: Brussels carrying the most departures *and* the worst delays.
3. **Peak hours & platforms** — `dim_hour[hour_label]` against
   `Departures per day` (never the raw count), split by `day_type`; then
   `platform_label` for a chosen station.
4. **Data quality & pipeline** — `v_data_quality` and `v_ingestion_health` as
   tables. Put this **on the report, not in an appendix**: the capture window is
   partial by design, and a reader who does not know that will over-read every
   other page.

---

## Refreshing, and the one gotcha

The first refresh after a quiet spell may take up to a minute: the database is
resuming from auto-pause. That is expected, not a failure — the same cold-start
path the Function App's retry logic exists for. If a refresh fails with a login
timeout, run it again.

If a refresh fails with **"cannot open server … requested by the login"**, the
firewall is the cause. The Power BI Service comes from Azure and is covered by the
existing rule; a *Desktop* client on someone's home network is not, and needs its
own rule:

```bash
IP=$(curl -s https://api.ipify.org)
az sql server firewall-rule create -g rg-railpulse-cloud -s sql-railpulse-<suffix> \
  -n "AllowDesktop-$IP" --start-ip-address "$IP" --end-ip-address "$IP"
```

---

## Honest limits

* **A Free licence cannot share the report.** Screen-share it, or start the 60-day
  Pro trial, or point people at the Streamlit dashboard instead.
* **Two days of data.** Anything month-over-month will be empty until the pipeline
  has run longer. `Departures YTD` works but is not yet interesting.
* **The capture window is partial by design.** Every per-hour figure must be
  normalised by days observed; `Departures per day` above does it, a raw
  `COUNTROWS` does not.
* **`dim_date` covers 2026-01-01 → 2027-12-31.** Extend the range in
  `sql/05_bi_dimensions.sql` and re-run the migration if the project outlives it;
  the file is idempotent and only inserts missing days.
