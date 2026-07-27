# Cost control — and the one requirement that contradicts itself

> **The short version.** The brief asks for a timer every 15 or 30 minutes *and*
> an auto-pause delay of exactly one hour. Those two requirements cannot both be
> satisfied round the clock: a timer firing every 15 minutes means the database is
> never idle for an hour, so it never pauses, so it bills ~0.5 vCore continuously
> — roughly **$190/month**, which exhausts the $100 student credit in about two
> weeks. This project resolves the tension by sampling the **weekday peaks** at
> 15-minute resolution and letting the database sleep the rest of the time:
> about **$54/month** while collecting, and **under $0.30/month** once paused on
> Friday.

---

## What actually costs money

All figures are estimates for European regions at the rates published in mid-2026 (this
deployment runs in France Central), and
the point of them is the *ratio*, not the third decimal place. Check the current
numbers at [Azure SQL Database pricing](https://azure.microsoft.com/pricing/details/azure-sql-database/single/)
and your own **Cost Management + Billing** blade.

| resource | model | rate (approx) | this project |
|---|---|---|---|
| **Azure SQL, serverless compute** | per vCore-**second**, floored at min capacity, **only while not paused** | ~$0.000145/vCore-s ⇒ **$0.52/vCore-hour** ⇒ **$0.26/hour** at 0.5 vCore | **~97% of the bill** |
| Azure SQL, storage | per allocated GB-month | ~$0.115/GB | 2 GB ⇒ **~$0.23/month** |
| Function App (**Flex Consumption**, FC1) | per execution + GB-s, after a free monthly grant (250,000 executions / 100,000 GB-s) | — | ~700 executions and ~42,000 GB-s/month ⇒ **$0** |
| Storage account (LRS) | per GB + transactions | ~$0.02/GB | a few MB of runtime bookkeeping ⇒ **~$0.05/month** |
| Application Insights | per GB ingested, 5 GB/month free | — | **$0** at this log volume |

So: **the only number that matters is how many hours the database is awake.**
Everything else in this architecture is rounding error.

---

## The arithmetic that drives the schedule

Serverless auto-pause needs a **continuously idle** period equal to the
auto-pause delay. With the delay at 60 minutes (the minimum Azure allows), the
database pauses 60 minutes after the *last* query, and any query inside that hour
resets the clock.

That has a counter-intuitive consequence which is the key to this whole page:

> **Within a capture window, the polling cadence does not affect cost.**
> Every 5 minutes and every 15 minutes keep the database awake for exactly the
> same number of hours. What costs money is the **width of the window**, plus the
> one-hour pause tail after it.

| schedule | awake per weekday | awake/month | est. compute | credit lasts |
|---|---|---|---|---|
| every 15 min, 24/7 | 24 h | ~730 h | **~$190** | ~2 weeks |
| every 60 min, 24/7 | 24 h *(never idles a full hour)* | ~730 h | **~$190** | ~2 weeks |
| **15 min, 06–09 + 16–19, Mon–Fri** ← default | ~9.5 h (2 × [4 h window + 1 h tail]) | ~209 h | **~$54** | ~2 months |
| 15 min, 06–09 only, Mon–Fri | ~4.75 h | ~105 h | **~$27** | ~4 months |
| 5 min, 07–09 + 17–19, Mon–Fri | ~7 h | ~154 h | **~$40** | ~2.5 months |
| paused (`make pause`) | 0 h | 0 h | **~$0.28** (storage only) | indefinitely |

Note rows 1 and 2: **hourly polling costs the same as 15-minute polling** and
collects a quarter of the data. Anyone reaching for "poll less often to save
money" without checking the pause threshold makes the dataset worse for nothing.

---

## The default, and why these hours

```
INGEST_SCHEDULE = 0 */15 6-9,16-19 * * 1-5
WEBSITE_TIME_ZONE = Europe/Brussels
```

Every 15 minutes, hours 06–09 and 16–19, Monday to Friday, **Belgian local time**.

* **It is where the signal is.** Delay analysis is about the peaks. A network at
  02:00 is punctual and uninteresting; the SQL sprint already showed the evening
  peak (17:00) is the busiest hour on the network.
* **Two windows, not one**, because the morning and evening peaks fail
  differently — morning delays are mostly departure-side (crew, stock), evening
  delays accumulate through the day. Capturing only one would answer half the
  question.
* **Weekdays**, because the weekend timetable is a different product. Including
  it at the same resolution would triple neither the cost nor the insight, but it
  would let a weekend average dilute a weekday one. `day_type` exists in the
  views so weekend data *can* be collected and separated — just not by default.
* **15 minutes**, not 5, because of iRail rather than Azure: the API is a free
  volunteer-run service and 10 hubs × 4/hour × 8 hours = 320 requests/day is
  polite. Cost-wise 5 minutes would be nearly free (see above); courtesy is the
  binding constraint, not billing.

`WEBSITE_TIME_ZONE` matters more than it looks. NCRONTAB is UTC by default, so
without it the "morning peak" window would drift by an hour twice a year and
quietly capture 05:00–08:00 for half the dataset. On a **Linux** plan the value
is an IANA name (`Europe/Brussels`); on Windows it would be
`Romance Standard Time`.

### Changing it

One app setting, no redeploy — the app reads it at start-up and an app-setting
change restarts the app:

```bash
# Round the clock (accepting ~$190/month):
az functionapp config appsettings set -g rg-railpulse-cloud -n func-railpulse-XXXX \
  --settings "INGEST_SCHEDULE=0 */15 * * * *"

# Morning peak only (~$27/month):
az functionapp config appsettings set -g rg-railpulse-cloud -n func-railpulse-XXXX \
  --settings "INGEST_SCHEDULE=0 */15 6-9 * * 1-5"
```

The code has no opinion about the value. That is the point of it being a setting:
the cost/coverage trade is an operational decision, made with the bill in view,
not a deployment. **Verified**: changing the setting and waiting for the restart
changed the schedule the host runs, with no redeploy — confirmed via
`/api/ping`'s `timer_schedule` field and a timer firing on the new cadence.

⚠ **The setting must exist.** The decorator uses the host's `%INGEST_SCHEDULE%`
indirection, which is what makes it changeable at runtime; if the app setting is
ever deleted, `ingest_timer` fails to load. `provision.sh` always sets it. The
obvious-looking alternative — reading `os.environ` in Python and defaulting when
absent — does **not** work on Flex Consumption, because trigger metadata is
generated during the remote build and cached, freezing the value at deploy time.
That bug is written up in
[`deployment_notes.md`](deployment_notes.md#9-the-schedule-setting-that-silently-did-nothing).

---

## The settings that make it cheap, one by one

Everything below is applied by [`azure/provision.sh`](../azure/provision.sh) and
walked through by hand in [`portal_walkthrough.md`](portal_walkthrough.md).

### Azure SQL: `GP_S_Gen5_1`, min 0.5 vCore, auto-pause 60, max 2 GB, LRS

```bash
az sql db create … --edition GeneralPurpose --compute-model Serverless \
  --family Gen5 --capacity 1 --min-capacity 0.5 \
  --auto-pause-delay 60 --max-size 2GB --backup-storage-redundancy Local
```

* **Serverless, not Provisioned.** Provisioned bills 24/7 whether or not anything
  connects. Serverless costs roughly twice as much *per active hour* and can drop
  to zero compute — which is only a win if it actually pauses, hence this whole
  page.
* **`--min-capacity 0.5`** is the floor for Gen5 and the number you are billed
  while awake, whatever the load. Raising it raises the bill linearly for a
  workload that does not need it: the heaviest statement here is a MERGE of 60
  rows.
* **`--auto-pause-delay 60`** — 60 minutes is Azure's minimum. The brief asks for
  exactly this.
* **`--max-size 2GB`** — storage is billed on what is *allocated*, not used. At
  ~5 000 departures/day the fact table needs a few hundred MB a year, so 2 GB is
  comfortable, and shrinking it later is not always possible.
* **`--backup-storage-redundancy Local`** (LRS) — geo-redundant backup costs
  roughly triple to protect a dataset that can be rebuilt from a public API.

### Function App: Flex Consumption (FC1)

`--flexconsumption-location` rather than `--consumption-plan-location` (which
selects the older Y1 plan) is what makes this an FC1 plan: no always-on
instance, billed per execution and per GB-second against a free monthly grant
this workload does not come close to exhausting. A Premium or App Service plan
would add $50–150/month for warm starts nobody here needs.

**On the arithmetic:** the timer fires ~32×/weekday ⇒ ~700 runs/month, each
~30 s at 2 048 MB ⇒ ~42,000 GB-s. Both are inside the free grant, so compute is
$0. Even switching to 24/7 polling (~2,900 runs, ~173,000 GB-s) would cost
about **$1/month** — still a rounding error against the SQL bill, which is the
only number worth managing.

*Why not the classic Y1 Consumption plan?* It was built on Y1 first. The host's
key-management API never worked there, so no function key could be issued and
every protected endpoint was unusable; cold starts also measured 42–60 s. See
[`deployment_notes.md`](deployment_notes.md).

### One resource group

Every resource is in `rg-railpulse-cloud`, which is what makes
`./azure/teardown.sh delete` a single command — and what makes the cost view in
the portal a single line item instead of an archaeology exercise.

---

## The trap that is easy to walk into

**A liveness probe defeats auto-pause.** Point an uptime monitor at an endpoint
that queries the database every five minutes and the database never pauses again;
the monitor becomes the largest line on the bill, and nothing in the portal will
say so.

That is why `/api/ping` is **anonymous and touches no database**: it proves the
app is up without waking anything. `/api/health` *does* query, deliberately, and
is meant to be called when you want to know — not on a schedule.

The same reasoning is why `run_on_startup=False` on the timer trigger. With it
set to `True`, every deployment, scale event and platform patch fires an
unscheduled run and resumes a paused database at an arbitrary moment.

---

## Watching the bill

```bash
make cost          # month-to-date for the resource group, if the API allows it
```

Azure for Students subscriptions sometimes deny the Consumption API, in which
case the portal is authoritative: **Cost Management + Billing → Cost analysis**,
scoped to the resource group. Two things to look at:

1. **Set a budget alert at $20** — Cost Management → Budgets. It is the only
   mechanism that tells you *before* the credit is gone.
2. **Check the database's pause history** — SQL database → Monitoring → Metrics →
   *App CPU billed*. Flat non-zero at 0.5 during the small hours means it is not
   pausing, and something (a probe, a stuck query, a schedule change) is keeping
   it awake.

---

## Friday

```bash
make pause      # stop the Function App, let the database pause. Data kept.
```

Stopping the **Function App** is the step that matters: pausing the database
alone achieves nothing, because the next timer firing resumes it. With the app
stopped, the database goes idle, pauses within the hour, and the ongoing cost is
2 GB of storage — about **$0.28/month**.

```bash
make resume     # the app starts; the database resumes on its first query
```

`make teardown` deletes the resource group instead, and takes every collected
departure with it. **Prefer `pause`** — next week's dashboard is supposed to read
this data.

---

## What a production version would do differently

The architecture here writes to SQL on every poll. That is the right shape for
this project and the wrong one at scale, because it couples "how often do we
sample" to "how long is the expensive resource awake".

The production answer is to **decouple them**: the timer writes each raw payload
to Blob storage (essentially free — the storage account already exists), and a
separate, less frequent flush job MERGEs the accumulated blobs into SQL. Sampling
every 5 minutes then keeps the database awake for perhaps 10 minutes a day
instead of 9.5 hours:

| | direct (built) | buffered (not built) |
|---|---|---|
| sampling resolution | 15 min, peak windows only | 5 min, 24/7 |
| DB awake per day | ~9.5 h | ~0.2 h |
| est. compute | ~$54/month | **~$2/month** |
| moving parts | 1 timer | 2 timers, blob lifecycle, replay logic |
| failure modes | poll fails ⇒ lose one slot | poll fails ⇒ lose one slot; flush fails ⇒ backlog grows silently |

It is not built because it doubles the moving parts for a five-day exercise whose
brief explicitly asks for the direct pipeline — but it is the first thing to build
if this were to keep running, and the schema needs no change to support it: the
MERGE is idempotent, so replaying a backlog of blobs in any order converges to
the same table.
