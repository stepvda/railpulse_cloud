# Deployment notes — everything that fought back

Getting this pipeline and its dashboard live took ten obstacles that no tutorial mentions. Each
one is written up here with its symptom, its real cause, and where the fix lives
in the repository — because the symptoms are all misleading, and the next person
on a fresh `@becode.education` subscription will hit most of them.

Environment: Azure CLI 2.88.0, Azure for Students, tenant `becode.education`,
Python 3.11 Functions, 2026-07-27.

---

## 1. The subscription has no subscription

**Symptom.** `az login` succeeds, then everything fails:

```
$ az account show
ERROR: Please run 'az login' to setup account.
$ az rest --method get --url ".../subscriptions?api-version=2020-01-01"
{"count": {"value": 0}, "value": []}
```

**Cause.** Signing in gives *tenant-level* access. Azure for Students has to be
activated separately, and until it is there is no subscription to deploy into.

**Fix.** Activate at [azure.microsoft.com/free/students](https://azure.microsoft.com/free/students),
then `az account list --refresh`. Only the account holder can do this; it cannot
be scripted. `provision.sh` checks the offer up front and warns rather than
failing halfway through.

---

## 2. Nothing is registered on a new subscription

**Symptom.** `az group create` succeeds. Then:

```
ERROR: (MissingSubscriptionRegistration) The subscription is not registered
to use namespace 'Microsoft.Sql'
```

**Cause.** A brand-new subscription has **every** resource provider
`NotRegistered`. Registration is per-namespace and takes minutes.

**Fix.** `provision.sh` registers `Microsoft.Sql`, `Microsoft.Web`,
`Microsoft.Storage`, `Microsoft.Insights` and `Microsoft.OperationalInsights` up
front and waits for all five. Took ~300 s here. Turns a confusing mid-script
failure into a visible pause.

---

## 3. West Europe is banned

**Symptom.**

```
ERROR: (RequestDisallowedByAzure) Resource 'sql-railpulse-…' was disallowed by
Azure: This policy maintains a set of best available regions where your
subscription can deploy resources.
```

The message never says *which* regions.

**Cause.** Azure attaches an **Allowed resource deployment regions** policy to
Free/Student subscriptions. For this one the set is `italynorth`,
`francecentral`, `germanywestcentral`, `polandcentral`, `spaincentral` — West
Europe (Amsterdam), the obvious choice for Belgian data, is **not** in it.

**Fix.** The allowed list is readable from the policy itself, so `provision.sh`
reads it instead of hard-coding a region:

```bash
az policy assignment list --disable-scope-strict-match \
  --query "[].parameters.listOfAllowedLocations.value"
```

It then picks the nearest allowed region from a preference list ordered by
distance from Belgium — `francecentral` (Paris) here. The set varies between
subscriptions, which is exactly why it is discovered rather than assumed. Latency
matters: every one of the ~600 daily round trips between the Function App and the
database pays it.

---

## 4. Basic-auth publishing is off by default (Linux Consumption only)

**Symptom.** `az functionapp deployment source config-zip` fails with:

```
ERROR: (ResourceNotFound) The Resource 'Microsoft.Web/sites/func-railpulse-…'
under resource group 'rg-railpulse-cloud' was not found.
```

…while `az functionapp show` returns that exact resource, `state: Running`.

**Cause.** Nothing is missing. `config-zip` publishes through Kudu using
**basic-auth publishing credentials**, and Azure now creates apps with both
`scm` and `ftp` basic auth disabled. The 404 is about the credential policy, not
the site.

```bash
az resource show --ids "$SITE/basicPublishingCredentialsPolicies/scm" \
  --api-version 2023-12-01 --query properties
# {"allow": false}
```

**Fix (superseded).** On Linux Consumption the only route was to re-enable `scm`
(leaving `ftp` off). After the move to Flex Consumption (§6) the deployment uses
an Entra bearer token instead, so **both stay disabled** and there is no
publishing password anywhere in this project.

---

## 5. The Linux Consumption host could not issue function keys — the blocker

**Symptom.** The app works: `/api/ping` returns 200 with the right payload, all
seven functions index, `/api/health` correctly returns 401 without a key. But no
key can ever be obtained:

```
$ az functionapp keys list -n func-railpulse-… -g rg-railpulse-cloud
ERROR: Operation returned an invalid status 'Bad Request'

$ az rest --method post --url ".../host/default/listKeys?api-version=2023-12-01"
{"Code":"BadRequest","Message":"Encountered an error (InternalServerError)
from host runtime."}
```

So every protected endpoint was permanently unreachable. The host also never
created its `azure-webjobs-secrets` blob container.

**What was ruled out**, before blaming the platform:

| hypothesis | test | result |
|---|---|---|
| storage credentials wrong | created `azure-webjobs-secrets` by hand with the app's own `AzureWebJobsStorage` string | container created — credentials fine |
| storage firewall | `networkRuleSet.defaultAction` | `Allow` |
| missing content share | `az storage share list` vs `WEBSITE_CONTENTSHARE` | share present |
| wrong runtime config | `linuxFxVersion`, `FUNCTIONS_EXTENSION_VERSION`, worker | `Python|3.11`, `~4`, `python` — correct |
| cold-start timeout | warmed to 0.1 s, then called `listKeys` immediately | failed identically |
| secret store type | set `AzureWebJobsSecretStorageType=blob`, restarted | no change |
| stale host state | restarted twice, waited | no change |

Cold starts were also 42–60 s, with one 60 s timeout — bad enough on its own.

**Fix.** Rebuild the app on **Flex Consumption**. It issued a key immediately.

---

## 6. Moving to Flex Consumption

The brief says *"Hosting Plan to Consumption (Serverless)"*. Flex Consumption is
that — serverless, scale-to-zero, billed per execution and GB-second against a
free monthly grant — and it is the plan Microsoft now recommends: the CLI prints
a migration notice on **every** Y1 call, and Linux Consumption reaches end of
life on 2028-09-30.

So this is a deviation from the brief's literal wording, made for three concrete
reasons and recorded rather than hidden:

1. the Y1 host could not issue function keys (§5), making the deliverable
   impossible;
2. Y1 cold starts measured 42–60 s against ~0.1 s warm on Flex;
3. Flex has a deployment API that works (§7).

Cost is unchanged in practice — ~700 executions and ~42,000 GB-s/month, inside
the free grant, against a bill that is ~97% Azure SQL either way. And it removed
the need to relax the basic-auth default (§4). `provision.sh` now creates
`--flexconsumption-location` (FC1) and documents the trade at the call site.

---

## 7. `az functionapp deploy` cannot deploy to Flex

**Symptom.**

```
$ az functionapp deploy --src-path pkg.zip --type zip
ERROR: An error occurred during deployment. Status Code: 415
```

415 = Unsupported Media Type, with `--type zip`, without it, and with the file
named `.zip`. Meanwhile the older `config-zip` command answers *"This API isn't
available in this environment yet!"* — it is a Kudu path Flex does not implement.

**Cause.** The CLI sends a content type the OneDeploy endpoint refuses. The
endpoint itself is fine.

**Fix.** `deploy.sh` calls it directly:

```bash
TOKEN=$(az account get-access-token --resource https://management.core.windows.net/ \
        --query accessToken -o tsv)
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/zip" \
  --data-binary @pkg.zip \
  "https://<app>.scm.azurewebsites.net/api/publish?RemoteBuild=true"
# 202 Accepted
```

Then poll `/api/deployments/latest` until `complete` (Kudu status 4 = success,
3 = failed). A bearer token rather than basic auth is what lets §4 stay disabled.
`RemoteBuild=true` makes the platform run `pip install`, so pyodbc's Linux
wheels never have to be cross-compiled locally.

---

## 8. The schedule setting that silently did nothing

**Symptom.** `INGEST_SCHEDULE` was changed from the peak-window cadence to every
five minutes. Nothing fired for eleven minutes. But the timer was not broken:

```bash
$ az functionapp function show ... --function-name ingest_timer \
    --query "config.bindings[0].schedule"
"0 */15 6-9,16-19 * * 1-5"      # <- the BUILD-TIME value, not the app setting
```

**Cause.** This one was mine, not Azure's. The decorator read the setting in
Python at import time:

```python
INGEST_SCHEDULE = os.environ.get("INGEST_SCHEDULE", "").strip() or "0 */15 …"

@app.timer_trigger(schedule=INGEST_SCHEDULE, …)
```

The reasoning was that a missing app setting should degrade to a sensible default
rather than break the app — which is true, and irrelevant, because on Flex
Consumption the **trigger metadata is generated during the remote build and
cached**. A value computed at import time is therefore frozen at deploy time.
Changing the app setting afterwards changed nothing at all.

Two things were wrong as a result: the verification attempt was invalid (12:20 is
not inside a 06–09/16–19 window, so *not* firing was correct behaviour), and the
"change coverage with one app setting, no redeploy" lever documented in
[`cost_control.md`](cost_control.md) did not actually exist.

**Fix.** Use the host's own indirection, which is resolved at every start:

```python
INGEST_SCHEDULE = "%INGEST_SCHEDULE%"
```

The trade is real and accepted: if the app setting is ever deleted, the timer
function fails to load. `provision.sh` always sets it, and
`local.settings.json.example` carries it for local runs. `/api/ping` reports the
*resolved* value by reading the environment at request time, so an operator can
see the effective cadence rather than the literal `%INGEST_SCHEDULE%`.

**Verified after the fix.** With the setting at `0 */5 * * * *`, a scheduled run
fired within 150 s and wrote **10 `trigger_source='timer'` rows** (one per hub):
85 departures inserted, 219 revised. Restoring the peak-window value then took
effect with **no redeploy**, confirmed via `/api/ping`.

The general lesson: on Flex Consumption, anything a trigger decorator needs must
be either a literal or a `%SETTING%` reference. Computing it in Python works
locally, survives the deploy, and then quietly ignores you.

---

## 9. The F1 Free plan bricks itself for a day — and my script caused it

**Symptom.** The Streamlit dashboard deployed, built cleanly (Oryx status 4), and
then would not serve. Then nothing would:

```
$ curl https://web-railpulse-….azurewebsites.net/
Error 403 - This web app is stopped.
$ az webapp start …            # succeeds, changes nothing
$ az webapp show --query "{state:state, usageState:usageState}"
{ "state": "QuotaExceeded", "usageState": "Exceeded" }
```

Kudu is part of the same site, so the deployment API answered 403 too — the app
could no longer be fixed *or* redeployed.

**Cause — a chain, and the first link was mine.**

1. `provision_webapp.sh` set the startup command to
   `bash /home/site/wwwroot/startup.sh` **at provision time**, when no code had
   been deployed and that file did not exist.
2. The container therefore exited **127** ("command not found") after ~16 s. App
   Service's documented response is *"Failed to start site. Revert by stopping
   site"* — which stops Kudu as well.
3. Every restart attempt burned CPU. The plan's usage counter later showed
   **`WP stop requests: 19`**.
4. F1 Free allows **60 CPU-minutes per day**. Between the failed starts and the
   `pip install` of streamlit + pandas + pymssql (188 MB written), it was gone.
5. Exhausted, F1 sets `state = QuotaExceeded` and answers 403 to everything. It
   is **not clearable** — not by `start`, `restart` or `config set`. It resets at
   UTC midnight.

The 127 itself had two causes worth recording separately, because either alone
produces it: the App Service Python image provides **`python3`, not `python`**,
and a *custom* startup command does not run with Oryx's `antenv` virtualenv
activated, so `streamlit` is not importable even once the interpreter is found.
`webapp/startup.sh` now resolves the interpreter with `command -v` and sources
`antenv/bin/activate` when present, and prints both to the container log so the
next failure of this kind is one log read away.

**Fix, in the scripts rather than in a runbook.**

* `provision_webapp.sh` now sets a startup command that **cannot fail and exposes
  nothing** — one line of text served from an empty temp directory, not the
  source tree. A fresh app therefore starts cleanly with no code on it, and Kudu
  stays reachable.
* `deploy_webapp.sh` switches to the real Streamlit command **only after** the
  publish and build have succeeded, i.e. once `startup.sh` is genuinely on disk.
* `deploy_webapp.sh` also checks the site state up front and, on
  `QuotaExceeded`, says so plainly with both remedies (wait for the reset, or
  `--sku B1` at ~\$0.018/hour) instead of failing later on a mystery 403.

**The lesson, which generalises past App Service.** On any platform with a
restart-on-failure loop and a consumption quota, a startup command that
references a not-yet-deployed file is not a small ordering mistake. It is a
denial of service against your own environment, and it removes the access you
need to repair it. Point the entrypoint at something that cannot fail, and move
it only once the thing it needs exists.

---

## 10. Three smaller ones

**`az functionapp show` returns empty strings on Flex.** `defaultHostName`,
`state` and `sku` all come back blank, while `az webapp show` — the same ARM
resource, a different command module — returns them. Trusting the first silently
produces `BASE="https://"` and then `curl: (6) Could not resolve host: api`,
which looks like DNS and is not. Every script now falls back to `az webapp show`.

**`/api/admin/*` routes 404 — the host reserves `admin`.** Five functions
answered; the two whose routes began `admin/` returned 404 **even without a
key** (a 404, not a 401, so the route was never matched). The Functions runtime
reserves `admin` for its own management API. Renamed to `/api/migrate` and
`/api/seed-stations`.

**The Kudu log stream does not exist on Linux Consumption.** Both
`/api/logs/docker` and `az webapp log tail` return 404 *with valid credentials*,
so the usual "just read the logs" step is unavailable — which is why §5 had to be
diagnosed by elimination.

---

## What this cost, and what it bought

Roughly two hours, almost all of it on §5. Three things made it tractable:

* **`/api/ping` being anonymous and database-free.** It proved the app, the
  config parsing and the connection-string handling were all fine while the key
  API was broken. A liveness endpoint that needed a key would have left nothing
  to test with.
* **Refusing to accept a misleading error at face value.** `ResourceNotFound`
  was about a credential policy; `415` was a client-side content type; a `404`
  was a reserved route name. Each cost time precisely because the message pointed
  somewhere else.
* **Getting a local SQL client before iterating on DDL.** Each redeploy is ~4
  minutes. `pip install pymssql` (bundled TDS, no system driver, no Homebrew tap
  to trust) made it possible to test the schema against the live database in
  seconds — which is how the `PERSISTED` determinism problem below got settled
  in one pass instead of four deploys.

### The one bug that was mine, not Azure's

`departure_dow_local` could not be persisted. Error 4936, three times, for three
different reasons — and the rules are not guessable, so they were tested rather
than reasoned about:

| expression | result |
|---|---|
| `DATEPART(WEEKDAY, d)` | rejected — depends on `SET DATEFIRST` |
| `DATEDIFF(DAY, '19000101', d)` | rejected — the **implicit string-to-date conversion** of the anchor is itself non-deterministic |
| `DATEDIFF(DAY, CONVERT(DATE,'1900-01-01',23), d)` | rejected — an explicit style does not rescue a conversion whose target is `DATE` |
| `DATEDIFF(DAY, 0, d)` | **accepted** — integer 0 → 1900-01-01, no locale involved |

Verified: 2026-07-27 (Mon) → 1, 2026-08-01 (Sat) → 6, 2026-08-02 (Sun) → 7.
Two static tests now guard this class of mistake
(`test_no_filtered_index_predicate_uses_or`, and the schema-shape checks in
`test_sql_contract.py`), because a `CREATE TABLE` that only fails in the cloud is
the most expensive kind of failure in this project.
