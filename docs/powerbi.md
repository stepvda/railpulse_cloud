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
| you get | an 8-page report, built | a live, self-refreshing model |
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
departures        8 907 rows     dim_date   730     dim_hour   24
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

`scripts/build_powerbi_report.py` creates a real Power BI report through the API —
model, measures, relationships, theme, pages and visuals. Power BI Desktop is
Windows-only, so the alternative was a written click-by-click guide, which puts
the work back on the reader and drifts from the app the moment either side changes.

```
$ python scripts/build_powerbi_report.py
  created model (c409483c-…) — 5 tables, 16 measures
    departures         8907 rows      dim_hour         24 rows
    dim_date            730 rows      pipeline_health  10 rows
  created report — 8 pages, 104 visuals
```

| page | answers | headline visual |
|---|---|---|
| Executive scorecard | is the network on time? | On-Time Rate % as the dominant card |
| Rush hour matrix | *when* does it break? | volume + delay on one pair of axes |
| Train class breakdown | *what* breaks? | total delayed minutes, and the mean beside it |
| Platform congestion | *where* does it break? | delay by platform, station slicer |
| Hub comparison | *who* runs best? | on-time rate vs mean delay, 3 slicers |
| Delay evolution | do delays grow while you wait? | first reading vs latest, per train |
| Services & destinations | which routes carry the pain? | destination scatter |
| Data quality & pipeline | should I trust this? | freshness per station, coverage |

The first five are the sprint-3 brief's must-haves and its cross-hub
nice-to-have; the last three carry the Streamlit dashboard's remaining analysis
so the two stay in step. Page-by-page design rationale is in the
[project README](../README.md#-the-dashboard-design-choices-and-why).

**The measures live in the model, not in the visuals.** All 16 are defined once, on
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

Route A already builds eight pages for you (above). This is the **Route B**
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

---

# Sprint 3 — the operations dashboard

The sprint-3 brief asks for a connected, modelled, interactive executive
dashboard. `make bi-report` builds it; this section maps the brief's requirements
to what exists, including the two it is **not** possible to satisfy on this
licence and what was done instead.

## Must-haves

| # | Requirement | Where it lives | Design note |
|---|---|---|---|
| 1 | **Punctuality scorecard** — On-Time Rate %, on time = under 2 min | Executive scorecard, the largest card on the page | `On-Time Rate %` uses `is_on_time_2min`. The 6-minute UIC rate sits beside it: a network reads as excellent at 6 min and merely adequate at 2, and showing one threshold alone lets the choice flatter the result. A test pins which measure uses which flag — transposing them is invisible and moves the headline by several points. |
| 2 | **Rush hour matrix** — volume vs average delay by hour | Rush hour matrix, `lineClusteredColumnComboChart` | One pair of axes, not two charts: an hour is a bottleneck only when volume *and* delay are high. Bars are `Departures per day`, never a raw count — the timer samples peaks harder than off-peak, so a raw count would report the capture schedule as the peak. |
| 3 | **Train class breakdown** — which class accounts for the most delayed minutes | Train class breakdown | The brief's question is a **sum**, so `Delay minutes` is a `SUM`. The mean is charted next to it because the two rankings disagree: InterCity leads the total (3 670 min) through volume; ICE leads the average at 13.6 min/train on 63 trains. Answering with only one sends the operator after the wrong class. |
| 4 | **Platform congestion** | Platform congestion, with a station slicer | The slicer is load-bearing. "Platform 5" pooled across ten hubs averages unrelated tracks; the number only means something inside one station. |

## Nice-to-haves

| Requirement | Status |
|---|---|
| **Cross-hub comparison with slicers** | ✅ Hub comparison page — 3 slicers (hub, day type, peak window). On-time rate and mean delay are charted separately *because they disagree*, and the disagreement is the finding. |
| **Navigation** | ✅ A navigation bar on every page (56 `actionButton` visuals). A test asserts every button targets a page that exists and that every page is reachable — a button pointing at a missing section id is accepted silently and simply does nothing when clicked. |
| **Custom colours** | ✅ A registered theme resource *and* an explicit colour on every visual. Green = on time, red = delay/cancellation, amber = attention, navy = neutral volume. Nothing is coloured decoratively. |
| **Scheduled refresh** | ⚠️ Not possible for this model — see below. |

## The two things this licence cannot do

Stated plainly rather than worked around, because both are checklist items.

### A `.pbix` file does not exist for this report

```
GET /v1.0/myorg/reports/{id}/Export
  -> 404 ExportPBIX_ModelessWorkbookNotFound
```

A report built through the API over a **push dataset** has no underlying workbook
for the service to package. This is not a permission or a flag — the artifact
isn't there. And "Publish to web", the only mechanism producing a link a grader
could open without signing in, needs Pro; image export is already disabled
tenant-wide (`Export report to image is disabled on tenant level`), which is the
same restriction showing up elsewhere.

**What was done instead:** `make bi-pbip` writes [`powerbi/`](../powerbi/) — a
**PBIP project**, the plain-text format Power BI Desktop reads and Microsoft
recommends for source control. It is generated from the same
`TABLES`/`MEASURES`/`RELATIONSHIPS` contract and the same `build_pages()` as the
published report, so the two cannot drift.

It is arguably the better artifact for a repository: a `.pbix` is an opaque binary
git cannot diff, while this is reviewable text — and it directly answers the
brief's own warning about not losing work when a trial licence lapses.

```
powerbi/
  RailPulseCloud.pbip
  RailPulseCloud.SemanticModel/definition/model.tmdl        16 measures, 2 relationships
                              definition/tables/*.tmdl      5 tables
                              definition/expressions.tmdl   ServerName / DatabaseName
  RailPulseCloud.Report/report.json                         8 pages, 104 visuals
```

Unlike the published report, this model connects **directly to Azure SQL in Import
mode** — Scenario A of the brief, and the thing a push dataset cannot be. The
server is an M **parameter** with a placeholder default, so no credential is
committed; a test fails the build if any value from `.azure-railpulse.env` ever
appears in `powerbi/`.

> **Honest limit:** Power BI Desktop is Windows-only, so this project has **not
> been opened in Desktop**. Every file is schema-valid JSON or tab-indented TMDL
> generated from the live contract, but "generated correctly" is a weaker claim
> than "opened and refreshed", and it is the only claim made.

### Scheduled refresh needs the interactive model, not this one

A push dataset **has no data source**, so there is nothing for Power BI to refresh
*from* — this is not a Free-licence restriction, the refresh schedule simply does
not apply to it. Two real options:

**1. Refresh the push dataset on a schedule from here.** `make bi-refresh`
re-reads the views and re-pushes the rows. On macOS, matched to the Function
timer's weekday peaks:

```bash
# ~/Library/LaunchAgents/com.railpulse.bi.plist runs this twice a day
cd ~/Dev/AI_Data_Science_training/railpulse_cloud && make bi-refresh
```

Full automation needs a service principal rather than the interactive `az login`
token this uses, which a student subscription may not permit — so treat this as
"scheduled on the workstation", not "scheduled in the cloud".

**2. Use the PBIP / interactive Import model.** That one *does* have a data
source, so Power BI Service gives it a native refresh schedule — **up to 8×/day on
a Free licence, with no gateway**, because Azure SQL is a cloud source. This is
the correct answer to the brief's nice-to-have, and the reason the PBIP model is
built as Import against Azure SQL rather than as a copy of the push dataset.

Set the cadence **inside a capture window**. The timer only collects during
weekday 06–09 and 16–19 Brussels, so a 03:00 refresh copies a stale snapshot and
wakes the serverless database for nothing.

### Screenshots

`POST /reports/{id}/ExportTo` returns `403 — Export report to image is disabled on
tenant level`, so the report cannot be rendered to PNG or PDF from here. Capturing
the pages requires opening the report in a browser and screenshotting them; there
is no scripted path on this tenant, and nothing in this repository claims to be a
rendered screenshot of a page that has not been opened.
