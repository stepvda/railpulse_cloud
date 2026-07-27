# The web app — what it is, and what changed from sprint 1

Sprint 1 shipped a Streamlit dashboard over a 980 MB SQLite file: nine pages, an
overview, one per graded question, a hub leaderboard, a data-quality page, and an
optional text-to-SQL page. This sprint deploys a dashboard to Azure over the
**live** warehouse instead.

It is not the same app, and this page is about why.

---

## What runs where

| | sprint 1 | sprint 2 (this) |
|---|---|---|
| host | local `streamlit run` | Azure App Service, **F1 Free**, France Central |
| data | `data/railpulse.db`, 980 MB SQLite | Azure SQL, live liveboard data |
| client | `sqlite3` | `pymssql` |
| grain | 2.17 M *scheduled* departures | departure *events*, with observed delays |
| anti-drift seam | graded `.sql` files loaded verbatim | every query reads a **view** |
| pages | 9 (overview, Q1–Q5, leaderboard, quality, chat) | 9 (overview, live, leaderboard, peaks, platforms, delay evolution, services, quality, pipeline) |

---

## Why last week's pages are not deployed

`dashboard/app.py` opens `config.DB_PATH` — the 980 MB SQLite build — and its
Q1–Q5 pages recompute from 2.17 M rows. **App Service F1 Free has 1 GB of total
storage**, so that file cannot ship. Three options were weighed:

1. **Live-only app on Free** ← chosen. $0, ~1 MB package, deploys in a minute.
   The sprint-1 pages stay runnable locally, where the database already is.
2. **Everything on B1 Basic** (~$13/month, 10 GB). Would work, and costs a real
   share of a $100 credit that is already carrying ~$54/month of Azure SQL — for
   pages whose answers have not changed since Friday.
3. **Everything on Free, with sprint-1's answers pre-loaded into Azure SQL** from
   the 192 KB of CSVs in `output/`. Free and complete, but it would break the
   property that made last week's dashboard defensible: those pages would read
   pre-computed answers while claiming, in a "Show the SQL" expander, to be
   computing them.

Option 1 keeps both apps honest about what they are. The sprint-1 dashboard is
the report on the static timetable; this one is the operational view of the live
pipeline.

---

## The anti-drift seam moved down a layer

This is the design decision worth defending.

Last week, every figure came from a statement loaded **verbatim** out of
`sql/analysis/qN_*.sql`, annotated with `-- @label:` markers. The dashboard and
the graded deliverable were the same text, so they could not disagree.

That seam does not transfer. These pages are interactive — pick a station, pick a
row limit, toggle morning-only — and a parameterised query is not the same
artefact as a file you can paste into a client. Faking it (string-concatenating a
WHERE clause onto a file's text) would be worse than not claiming it.

So the seam moved: **every statement in `webapp/queries.py` reads a view from
[`sql/03_views.sql`](../sql/03_views.sql).** The views are where this project's
definitions live:

* what counts as on time (`is_on_time_2min` / `is_on_time_6min`, both exposed);
* whether a cancelled train is in the punctuality denominator (it is not — the
  flags are `NULL`, so `COUNT(flag)` excludes it automatically);
* which local hour a departure belongs to (`departure_hour_local`, computed from
  a stored Europe/Brussels timestamp, not from UTC at read time);
* how a delay's growth is measured (`delay_growth_s`).

The dashboard therefore *cannot* disagree with the warehouse, because it never
computes any of them. For a BI layer that is a stronger guarantee than sharing
text with a script: Power BI in sprint 3 will connect to the same views and get
the same numbers by construction.

What carried over unchanged:

* **Every figure has a "Show the SQL" expander** with the exact statement and its
  bound parameters. Paste it into the portal's Query editor and you get the same
  numbers.
* **pandas is a carrier, nothing more.** No groupby, no merge, no pivot, no
  boolean-mask filtering anywhere in `webapp/`. The two `.astype(float)` calls are
  type coercion for a map widget that rejects `Decimal`, and are commented as
  such.
* **Read-only by construction** — and rather than a `mode=ro` URI, `data.query`
  refuses any statement that does not begin with `SELECT` or `WITH`. The
  connection it holds *does* have write permission, so the guard is a hard stop
  rather than a convention.

---

## What is new, and only possible now

Four things could not be asked of a static timetable:

**Punctuality at all.** The GTFS static feed contains no delays. The hub
leaderboard, the delay distribution, the on-time percentages — none of sprint 1's
data could produce them.

**Delay evolution.** Because the pipeline polls the same departure repeatedly and
keeps both `delay_first_seen_s` and the latest `delay_seconds`, the *Delay
evolution* page can separate "the 17:42 was late" from "the 17:42 was on time
until 20 minutes before it left". It also shows minutes of notice, and repeat
offenders — the same train number late on several days, which is a timetabling
problem rather than weather.

**Disruptions.** Cancellations and trains moved off their booked platform
(`platform_is_normal = 0`) exist only in the live feed.

**Whether the pipeline is healthy.** A dashboard reading a file on disk never has
to ask. The *Pipeline* page shows per-station freshness, the run log with its
`trigger_source`, and the insert-versus-revise totals that prove the
deduplication is working — plus one button that triggers a load.

---

## The one control that changes state

The *Pipeline* page's **Run ingest now** button POSTs to the Function App's
`/api/ingest?hubs=all`. Three things about it:

* the function key lives in an App Service setting, server side, and **never
  reaches the browser**;
* it goes through the Function App's own key-protected endpoint, not through the
  dashboard's database connection — which cannot write;
* the button says out loud that it **wakes the serverless database**, the only
  thing in this project that costs real money.

If `FUNCTION_APP_URL` or `FUNCTION_KEY` is unset the button is replaced by a note
saying so, and the app is entirely read-only. That is the correct default for a
dashboard on a public URL.

---

## Cost and the Free tier's three catches

The plan is **F1 (Free)** — €0. What you pay instead:

| catch | consequence |
|---|---|
| 60 CPU-minutes/day | ample for *browsing*; a deploy-debug loop eats it. Exceed it and the app returns `state=QuotaExceeded` and answers 403 to everything — including the deployment API, so it cannot be fixed until the UTC-midnight reset. This bit us; see [deployment_notes.md §9](deployment_notes.md#9-the-f1-free-plan-bricks-itself-for-a-day--and-my-script-caused-it) |
| no Always On | the app sleeps when idle, so the first visit costs ~30 s (Streamlit boot, plus a possible Azure SQL resume) |
| 1 GB storage | the reason last week's 980 MB SQLite is not here |

`SKU=B1 ./azure/provision_webapp.sh` removes all three for ~$13/month.

Two deliberate choices keep the dashboard from spending money on the *database*:

* **Results are cached for 60 seconds.** The timer writes at most every 15
  minutes, so a minute of staleness is invisible — and it stops a reloading
  browser tab from waking a paused database over and over.
* **No auto-refresh.** There is a manual **Refresh data** button instead. A
  dashboard that polls on a timer would defeat the database's auto-pause exactly
  as an uptime probe would, and become the largest line on the bill. Same
  reasoning as `/api/ping` being database-free — see
  [`cost_control.md`](cost_control.md).

---

## Running it locally

```bash
set -a && source .azure-railpulse.env && set +a   # SQL_CONNECTION_STRING
export FUNCTION_APP_URL="https://<your-function-app>.azurewebsites.net"
export FUNCTION_KEY="$(az functionapp keys list -n <app> -g rg-railpulse-cloud \
                       --query functionKeys.default -o tsv)"

pip install -r webapp/requirements.txt
streamlit run webapp/app.py
```

Your machine's IP needs a SQL firewall rule — `provision.sh` adds one for the IP
you had when you ran it. If the connection times out after changing network, add
the new one:

```bash
az sql server firewall-rule create -g rg-railpulse-cloud -s <sql-server> \
  -n AllowLocalMachine2 --start-ip-address "$(curl -s https://api.ipify.org)" \
  --end-ip-address "$(curl -s https://api.ipify.org)"
```

---

## Two things I would fix next

**Streamlit's CORS and XSRF protection are disabled** in `startup.sh`. Behind App
Service's front end the WebSocket's `Origin` does not match what Streamlit
believes its own host to be, and with those enabled the socket is rejected and
the page hangs on "Please wait…". The exposure is bounded — the app is read-only
and the ingest key is server-side — but the right fix is App Service
authentication (Entra sign-in), which also removes the anonymous session there is
currently nothing to hijack *of*. That is a five-minute change and the first
thing to do if this URL is shared beyond the team.

**There is no `dashboard/` fallback for the sprint-1 pages.** Someone who wants
Q1–Q5 has to clone last week's repo and rebuild the SQLite. A cleaner end state
would be one app with a data-source switch — SQLite when the file is present,
Azure SQL otherwise — but that is a refactor of 1,879 lines of last week's code
for pages whose answers have not changed, and it was not worth it this week.
