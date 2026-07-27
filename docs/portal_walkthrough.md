# Building it by hand in the Azure Portal

The brief asks for a **manual portal deployment**, and this is that walkthrough:
every blade, every field, every value. [`azure/provision.sh`](../azure/provision.sh)
creates exactly the same resources with exactly the same settings — use whichever
you prefer, and use this page to *verify* the script's work if you took the fast
route.

Every cost-critical field is marked **⚠ COST** and is the reason that field is
mentioned at all. If you change nothing else, get those right.

**Time:** about 25 minutes, of which ~5 are Azure creating the SQL server and
~4 the first deployment.

---

## 0. Activate Azure for Students

1. Sign in to [portal.azure.com](https://portal.azure.com) with the
   `@becode.education` account.
2. Search **Education** in the top bar → **Azure for Students** → *Activate*.
   No credit card. $100 of credit, valid 12 months.
3. Verify: **Subscriptions** → your subscription → *Overview*. The **Offer** must
   read *Azure for Students*, and *Credit remaining* should show ~$100.

> If it says **Free Trial** or **Pay-As-You-Go**, stop. Those bill a real payment
> method once the trial credit is gone. `provision.sh` prints a warning when it
> detects this; the portal shows it under Subscriptions → Overview.

---

## 1. Resource group

**All resources go in one group.** That is what makes Friday's cleanup a single
delete, and the cost view a single line.

1. Search **Resource groups** → **+ Create**.
2. | field | value |
   |---|---|
   | Subscription | *Azure for Students* |
   | Resource group | `rg-railpulse-cloud` |
   | Region | **France Central** (see the region note below) |
3. *Tags* tab (optional but useful): `project = railpulse-cloud`,
   `cost-control = delete-on-friday`.
4. **Review + create** → **Create**.

> **Region:** keep every resource in the SAME region — a Function App in one
> region talking to SQL in another adds 20–50 ms to every round trip *and* charges
> for egress. It is the cheapest performance decision available.
>
> ⚠ **West Europe is probably blocked for you.** Azure applies an "Allowed
> resource deployment regions" policy to Student subscriptions. On this one the
> allowed set was `italynorth`, `francecentral`, `germanywestcentral`,
> `polandcentral`, `spaincentral` — West Europe (Amsterdam), the obvious choice
> for Belgian data, is **not** in it, and the failure comes late and unhelpfully
> (`RequestDisallowedByAzure`, without naming the permitted regions). Check yours
> first, then pick the closest to Belgium — `francecentral` (Paris) here:
>
> ```bash
> az policy assignment list --disable-scope-strict-match \
>   --query "[].parameters.listOfAllowedLocations.value"
> ```

---

## 2. Azure SQL Database — the ⚠ COST-critical blade

Search **SQL databases** → **+ Create**.

### Basics tab

| field | value |
|---|---|
| Resource group | `rg-railpulse-cloud` |
| Database name | `railpulse` |
| Server | **Create new** → see below |
| Want to use SQL elastic pool? | **No** |
| Workload environment | **Development** |

**Create new server:**

| field | value |
|---|---|
| Server name | `sql-railpulse-<something-unique>` (globally unique) |
| Location | **France Central** (or your nearest allowed region) |
| Authentication method | **Use SQL authentication** |
| Server admin login | `railpulse_admin` |
| Password | generate a long one and put it straight in a password manager |

> **Why SQL authentication and not Microsoft Entra?** Entra-only auth with a
> managed identity is the better end state and is described in
> [Security](#7-security-notes-and-the-better-end-state) below. The brief asks
> for a connection string in Application Settings, and SQL auth is what makes
> that a 10-second step instead of a detour through `CREATE USER … FROM EXTERNAL
> PROVIDER`. Get it working, then improve it.

Now, still on the Basics tab, click **Configure database** under *Compute +
storage*. This is the blade that decides your bill:

| field | value | why |
|---|---|---|
| Service tier | **General Purpose — Serverless** | ⚠ COST. *Provisioned* bills 24/7 whether or not anything connects. |
| Hardware | **Standard-series (Gen5)** | the default; the only one with a 0.5 vCore floor |
| Max vCores | **1** | ⚠ COST. The ceiling. A 60-row MERGE does not need more. |
| Min vCores | **0.5** | ⚠ COST. **This is what you are billed while awake**, whatever the load. |
| **Enable auto-pause** | **checked** | ⚠ COST. Without it, serverless is just an expensive provisioned database. |
| **Auto-pause delay** | **1 hour** | ⚠ COST. Azure's minimum, and what the brief asks for. |
| Data max size | **2 GB** | ⚠ COST. Billed on what is *allocated*, not used. |

Click **Apply**. The estimated cost shown on this blade assumes the database is
awake continuously — see [cost_control.md](cost_control.md) for what it actually
costs when it pauses.

### Networking tab

| field | value |
|---|---|
| Connectivity method | **Public endpoint** |
| **Allow Azure services and resources to access this server** | **Yes** ← required |
| **Add current client IP address** | **Yes** |
| Minimum TLS version | 1.2 |

> **The first toggle is what makes the whole project work.** It creates a
> firewall rule for `0.0.0.0`, which is *not* an address — it is Azure's sentinel
> for "allow other Azure services". Without it the Function App cannot connect,
> and the failure looks like a generic timeout. It does **not** open the server to
> the internet.
>
> The second adds a rule for the IP you are sitting behind right now, which is
> what lets you use the Query editor and VS Code. It changes when you change
> network — if the Query editor suddenly stops working, this is why (fix:
> SQL server → Networking → *Add your client IPv4 address*).

### Additional settings tab

| field | value |
|---|---|
| Use existing data | **None** |
| Collation | `SQL_Latin1_General_CP1_CI_AS` (default) |
| **Backup storage redundancy** | **Locally-redundant (LRS)** ⚠ COST |

> LRS vs geo-redundant is roughly a 3× difference on backup storage, to protect a
> dataset that can be rebuilt from a public API in an afternoon.

**Review + create** → **Create**. Deployment takes 2–5 minutes.

### Verify

SQL database → **Overview**. Compute tier should read *Serverless*, and there
should be a *Pause/Resume* control (a provisioned database has none). Under
**Settings → Compute + storage**, confirm auto-pause is 1 hour and min vCores 0.5.

---

## 3. Storage account

The Function App needs one for its own bookkeeping — timer schedule state, keys,
the deployment package. Creating it explicitly (rather than letting the Function
App wizard do it) is how you control the redundancy.

Search **Storage accounts** → **+ Create**.

| field | value |
|---|---|
| Resource group | `rg-railpulse-cloud` |
| Name | `strailpulse<unique>` (lower-case letters and digits only, ≤24 chars) |
| Region | **France Central** (see the region note below) |
| Primary service | Azure Blob Storage |
| Performance | **Standard** |
| **Redundancy** | **Locally-redundant storage (LRS)** ⚠ COST |

*Advanced* tab: **Allow blob public access → Disabled**, minimum TLS 1.2.

**Review + create** → **Create**.

> Geo-redundant (GRS) costs roughly double for state that is regenerated on every
> deployment.

---

## 4. Function App

Search **Function App** → **+ Create** → **Consumption** hosting option.

> Azure now presents several hosting options on this first screen. Pick
> **Flex Consumption** — serverless, billed per execution and GB-second against a
> free monthly grant. The classic **Consumption** plan is also serverless and is
> what the brief literally names, but this project could not use it: its host's
> key-management API never issued a function key, so every protected endpoint was
> unreachable, and cold starts measured 42–60 s. Full account in
> [`deployment_notes.md`](deployment_notes.md). *Premium* and *App Service* add
> $50–150/month for warm starts this workload does not need.

### Basics tab

| field | value |
|---|---|
| Resource group | `rg-railpulse-cloud` |
| Function App name | `func-railpulse-<unique>` (becomes your hostname) |
| Do you want to deploy code or container image? | **Code** |
| Runtime stack | **Python** |
| Version | **3.11** |
| Region | **France Central** (see the region note below) |
| Operating system | **Linux** (forced for Python) |
| Hosting | **Flex Consumption** ⚠ COST |

### Storage tab

Select the storage account from step 3.

### Monitoring tab

**Enable Application Insights: Yes.** It is free up to 5 GB/month and it is the
only way to read a traceback from a failed timer run. Turning it off to "save
money" saves nothing and costs you the debugging.

**Review + create** → **Create** (2–3 minutes).

---

## 5. Application settings — where the connection string lives

The brief: *"Never hardcode passwords. Save your SQL connection string inside the
Function App's Environment variables."*

### Get the connection string

1. **SQL databases** → `railpulse` → **Settings → Connection strings** → **ODBC**
   tab.
2. Copy it. It looks like:
   ```
   Driver={ODBC Driver 18 for SQL Server};Server=tcp:sql-railpulse-xxx.database.windows.net,1433;Database=railpulse;Uid=railpulse_admin;Pwd={your_password_here};Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;
   ```
3. Replace `{your_password_here}` with the password — **braces removed**.
4. Change `Connection Timeout=30` to **`Connection Timeout=60`**.

> **That last edit is not cosmetic.** This database auto-pauses. The first
> connection after a quiet night has to wait for a cold resume, which routinely
> takes 30–60 seconds. With a 30-second timeout, every morning's first timer run
> fails. (The code retries on top of this — see `railpulse/database.py` — but
> giving it a realistic timeout to retry *within* is the first line of defence.)

### Set the settings

Function App → **Settings → Environment variables** → *App settings* tab →
**+ Add**, once per row:

| name | value |
|---|---|
| `SQL_CONNECTION_STRING` | the ODBC string from above |
| `INGEST_SCHEDULE` | `0 */15 6-9,16-19 * * 1-5` |
| `WEBSITE_TIME_ZONE` | `Europe/Brussels` |
| `IRAIL_USER_AGENT` | `RailPulseCloud/1.0 (BeCode exercise; your.name@becode.education)` |
| `IRAIL_LANG` | `en` |
| `SQL_LOGIN_TIMEOUT` | `60` |
| `SQL_MAX_ATTEMPTS` | `5` |
| `SCM_DO_BUILD_DURING_DEPLOYMENT` | `true` |
| `ENABLE_ORYX_BUILD` | `true` |

**Click Apply**, then confirm the restart. Settings do not take effect until you
do, and this is the single most common reason a deployment "still has the old
value".

Notes on three of them:

* **`INGEST_SCHEDULE`** is 6-field NCRONTAB (*seconds* first, unlike Unix cron):
  every 15 minutes, hours 06–09 and 16–19, Mon–Fri. Why not round the clock:
  [cost_control.md](cost_control.md). The app reads this at start-up with a
  default, so a typo degrades to the default rather than breaking every function.
* **`WEBSITE_TIME_ZONE`** makes that schedule Belgian local rather than UTC.
  Without it the peak window drifts by an hour twice a year. On **Linux** it takes
  an IANA name; `Romance Standard Time` is the Windows spelling.
* **`SCM_DO_BUILD_DURING_DEPLOYMENT`** tells the platform to run
  `pip install -r requirements.txt` remotely, so nobody has to cross-compile
  `pyodbc` for Linux locally.

### Also set

**Settings → Configuration → General settings → HTTPS Only: On.** Function keys
travel in URLs; those should never cross plain HTTP.

---

## 6. Deploy the code

The portal cannot upload a Python function app from a folder — the *App files*
blade edits files but will not install dependencies. Pick one:

**A. From this repository (recommended)**

```bash
az login
./azure/deploy.sh          # reads .azure-railpulse.env, or set FUNCTION_APP + RESOURCE_GROUP
```

**B. VS Code** — install the *Azure Functions* extension, open the
`function_app/` folder, `F1` → *Azure Functions: Deploy to Function App*. Note
that this deploys `function_app/` only, so `sql/` will be missing and
`POST /api/migrate` will report `not found`; apply the schema with the
Query editor (step 7, option B) in that case.

**C. Zip deploy in the portal** — build the zip yourself
(`function_app/` contents + a copy of `sql/` at the root), then
**Deployment Center → Zip Deploy**.

### Verify

Function App → **Overview → Functions**. After a minute you should see:

| function | trigger |
|---|---|
| `ping` | HTTP |
| `ingest` | HTTP |
| `health` | HTTP |
| `stats` | HTTP |
| `admin_migrate` | HTTP (route `migrate`) |
| `admin_seed_stations` | HTTP (route `seed-stations`) |
| `ingest_timer` | Timer |

If the list is empty, the app failed to index — almost always a syntax error or a
missing dependency. Read it: **Log stream**, or

```bash
az functionapp log tail -n func-railpulse-xxx -g rg-railpulse-cloud
```

Then open `https://<your-app>.azurewebsites.net/api/ping` in a browser. It is
anonymous and touches no database, so it answers instantly.

---

## 7. Create the schema and load data

### Get a function key

Function App → **Functions → App keys** → *default* → copy. Or:

```bash
az functionapp keys list -n func-railpulse-xxx -g rg-railpulse-cloud \
  --query functionKeys.default -o tsv
```

### Option A — through the app (no local SQL client needed)

```bash
BASE=https://func-railpulse-xxx.azurewebsites.net
KEY=<the key>

curl -X POST -H "x-functions-key: $KEY" "$BASE/api/migrate"        # tables, indexes, views
curl -X POST -H "x-functions-key: $KEY" "$BASE/api/seed-stations"  # ~714 stations
curl -X POST -H "x-functions-key: $KEY" "$BASE/api/ingest?hubs=all"      # poll every hub
curl        -H "x-functions-key: $KEY" "$BASE/api/stats"                 # what landed
```

The first of those may take up to a minute — it is waking the database.

Or run all of it with the checks included:

```bash
./azure/smoke_test.sh
```

### Option B — through the portal Query editor

SQL database → **Query editor (preview)** → sign in as `railpulse_admin`. Paste
the contents of [`sql/01_schema.sql`](../sql/01_schema.sql), then `02`, `03`, `04`
in order, running each one.

> The editor **does not understand `GO`** (it is a client-side batch separator,
> not T-SQL). Run each `GO`-separated section separately, or use the VS Code
> **mssql** extension, which does understand it and bundles its own driver — no
> ODBC install needed.

Then check:

```sql
SELECT * FROM dbo.v_data_quality;
SELECT * FROM dbo.v_ingestion_health;
SELECT TOP 20 * FROM dbo.v_departures ORDER BY scheduled_departure_local DESC;
```

---

## 8. Confirm the timer is running

The timer fires on its own inside the configured windows. To confirm without
waiting:

1. **Function App → Functions → `ingest_timer` → Monitor** — invocation history
   (allow a few minutes for Application Insights to catch up).
2. Or ask the database, which is the more direct evidence:
   ```sql
   SELECT TOP 20 run_id, trigger_source, station_id, status,
          departures_returned, rows_inserted, rows_updated, started_utc
   FROM   dbo.ingestion_runs
   ORDER BY run_id DESC;
   ```
   `trigger_source = 'timer'` rows are the scheduled runs. `'http'` rows are yours.

---

## 9. Security notes, and the better end state

What is in place:

* Every endpoint except `/api/ping` needs a function key.
* HTTPS only.
* The connection string is an Application Setting, never in the repository.
  `.gitignore` excludes `.azure-railpulse.env` and `local.settings.json` — the
  two files that ever hold the password.
* The SQL firewall allows Azure services and one IP, not the internet.
* TLS 1.2 minimum, `Encrypt=yes`, `TrustServerCertificate=no` (so the certificate
  is actually verified — `yes` there would silently accept a
  man-in-the-middle).

**What would be better, and is not done here: a managed identity.** Then there is
no password at all.

```bash
az functionapp identity assign -n func-railpulse-xxx -g rg-railpulse-cloud
```

```sql
-- run as the Entra admin on the SQL server:
CREATE USER [func-railpulse-xxx] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [func-railpulse-xxx];
ALTER ROLE db_datawriter ADD MEMBER [func-railpulse-xxx];
ALTER ROLE db_ddladmin  ADD MEMBER [func-railpulse-xxx];  -- only for /api/migrate
```

Then the connection string loses `Uid`/`Pwd` and gains
`Authentication=ActiveDirectoryMsi`. It is strictly better — nothing to leak,
nothing to rotate. It is not the default here because it requires an Entra admin
on the SQL server, which a shared `@becode.education` tenant may not grant, and
because the brief explicitly asks for the connection-string approach. Worth doing
once the pipeline works.

**Also worth knowing:** the admin login used here is the *server* admin, which can
do anything to any database on that server. A production setup would create a
contained database user with only `db_datareader`/`db_datawriter` and use that for
ingestion.

---

## 10. Friday

```bash
make pause      # stops the Function App; the database then pauses by itself
```

Or in the portal: **Function App → Overview → Stop**, then **SQL database →
Overview → Pause**.

Stopping the Function App is the step that matters — pausing the database alone
achieves nothing, because the next timer firing resumes it.

Ongoing cost while paused: 2 GB of storage, about **$0.28/month**. Every collected
departure is still there for next week's dashboard.

To delete everything instead: **Resource groups → `rg-railpulse-cloud` → Delete
resource group**. Irreversible, and it takes the data with it.
