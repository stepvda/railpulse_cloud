# The iRail API — contract, quirks, and compliance

Everything on this page was verified against the live API on **2026-07-27** by
calling it and reading the bytes, not by reading the documentation. Where the two
disagree, this page records what the API actually did — and the parser is written
against that.

---

## Which API, and why this one

The brief says: *"pulls real-time **liveboard** metrics from the SNCB API"* and
*"hits the SNCB endpoints for a major hub (like Brussels-Central)"*, with example
tables `stations`, `vehicles`, `liveboard_records`.

That is a description of a **liveboard** endpoint, and the one that exists for
Belgian rail is [iRail](https://docs.irail.be/) — a long-running open project that
fronts NMBS/SNCB's own data. `GET /v1/liveboard?id=…` returns the next ~55
departures from one station with delays, platforms, cancellations and crowding.

The alternative, used by the previous sprint, is the **Belgian Mobility Open Data**
portal's GTFS feeds. They were the right choice there and the wrong one here:

| | iRail liveboard (this project) | GTFS-Realtime (sprint 1) |
|---|---|---|
| shape | one station's departure board | the whole network in one payload |
| size per call | ~20–40 KB | ~2–5 MB |
| API key | none | subscription key, quota-limited |
| "next departures from Brussels-Central" | one call | download everything, filter |
| platform per departure | **yes** | no (needs the static feed joined in) |
| crowding | yes (crowd-sourced) | no |
| suits a serverless function | yes — small, fast, per-hub | poorly: memory and time |

For a Function App on a Consumption plan polling ten hubs, a 30 KB per-station
response beats a 5 MB network dump on every axis that matters.

---

## Endpoints used

Exactly two, both anonymous.

### `GET /v1/liveboard`

```
https://api.irail.be/v1/liveboard?id=BE.NMBS.008813003&format=json&lang=en&arrdep=departure&alerts=true
```

| parameter | value used | note |
|---|---|---|
| `id` | `BE.NMBS.008813003` | station id — **preferred** |
| `station` | `Brussels-Central` | by name; used only if the caller passes a name |
| `format` | `json` | XML is the default in some versions |
| `lang` | `en` | `nl` / `fr` / `de` / `en`; changes `stationinfo.name` |
| `arrdep` | `departure` | **explicit on purpose** — this is the API's default, and relying on a default that could change upstream would silently turn the pipeline into an arrivals collector |
| `alerts` | `true` | annotations, when present |

**Why `id` and not `station`.** Name resolution is fuzzy, names are localised, and
several stations share a stem (`Brugge` / `Brugge-Sint-Pieters`,
`Antwerp-Central` / `Antwerp-Berchem`). A mis-resolution does not raise — it
returns *a perfectly plausible liveboard for the wrong station*. Ids remove the
whole class of bug. `railpulse/hubs.py` holds ten verified ids and
`irail.py:liveboard()` decides which parameter to use based on the `BE.NMBS.`
prefix.

### `GET /v1/stations`

```
https://api.irail.be/v1/stations?format=json&lang=en
```

One call, **714 stations**, each with id, localised name, official name and
coordinates. Called once by `POST /api/seed-stations`, so the station
dimension is complete on day one rather than growing only as places happen to be
polled — which is what lets a map visual show the whole network instead of one
with holes in it.

---

## Six things the feed does that the code has to handle

### 1. The documented path 303-redirects

```
GET /liveboard/?id=… → HTTP/2 303
location: https://api.irail.be/v1/liveboard?id=…
```

Harmless with `curl -L`, but it doubles the request count against the rate limit.
This client calls `/v1/` directly. Pinned by
`test_the_v1_path_is_called_directly`.

### 2. Every scalar is a JSON string

```json
{ "delay": "420", "canceled": "0", "left": "0", "isExtra": "0",
  "time": "1785138120", "platform": "5" }
```

Numbers, booleans, timestamps — all strings. Nothing in `transform.py` may assume
an `int`, and every coercion helper is total: it returns a usable value or a
documented default, and never raises. One malformed field must not cost a poll of
56 departures.

### 3. `time` is the **scheduled** time; `delay` is separate

`time` is the timetabled departure as a POSIX epoch. `delay` is seconds **on top
of it**. The actual departure is `time + delay`, which the schema exposes as the
computed column `actual_departure_utc`.

Folding the delay into the timestamp on the way in would destroy the ability to
measure punctuality at all — which is the entire point of the dataset. Guarded by
`test_scheduled_time_is_the_schedule_and_delay_is_separate`.

### 4. A departure's own `stationinfo` is its **terminus**

This is the most consequential quirk in the payload:

```json
{
  "station": "Brussels-Central",      // the station you QUERIED
  "stationinfo": { "id": "BE.NMBS.008813003", … },
  "departures": { "departure": [ {
      "station": "Antwerp-Central",   // this train's TERMINUS
      "stationinfo": { "id": "BE.NMBS.008821006", … },
      …
  } ] }
}
```

Two levels, both called `station`. Reading the inner one as the departure station
would attribute every Brussels-Central departure to Antwerp, Ghent and Namur —
and the resulting dashboard would look entirely reasonable. The parser names them
`station_id` (where it departs from) and `destination_station_id`, and
`test_polled_station_becomes_the_origin_not_the_destination` fails if that is ever
reversed.

### 5. `vehicleinfo.type` carries the **line** for suburban trains

Observed across four hubs in one afternoon:

```
IC  L  EC  ECD  EUR  ICE  T  CHAR
S1  S2  S3  S8  S10  S32  S33  S34  S35  S51  S52  S53
```

`S32` is not a type — it is service class `S`, line `32`. Stored raw as a "type",
every new S-line would become a new category and *"how do suburban trains
perform"* would need a `LIKE 'S%'` scan. The loader splits it into `type_code`
(`S`) and `service_line` (`32`), keeping `type_raw` for provenance.

`T` deserves a note: it appears with 5-digit numbers (`T 18124`) and no
documentation this project could verify. Rather than guess, `vehicle_types` labels
it *"Other scheduled train — published by the feed without a recognised service
class"*. Inventing a meaning would be worse than admitting the gap.

### 6. `"?"` means "platform not allocated"

The literal string `?` in `platform` and `platforminfo.name`. Normalised to `NULL`
on the fact row (which also disables the composite platform FK — a composite key
with a NULL member is not checked, so the row loads without a fake `'?'`
platform). Reported as `'unknown'` in the views and as a percentage in
`v_data_quality`.

### Also worth knowing

* `platforminfo.normal` is `"0"` when the train has been **moved off its booked
  platform** — a genuine disruption signal that exists nowhere in the static
  timetable. Kept as `platform_is_normal`, and nullable, because *not reported* is
  a third state that must not be collapsed into "moved".
* `occupancy` is a nested object (`{"@id": "…/terms/low", "name": "low"}`) with
  values `low` / `medium` / `high` / `unknown`. It is **crowd-sourced from iRail's
  app users**, so it is sparse and self-selecting. The MERGE never lets a later
  `unknown` overwrite an earlier real reading, and `v_data_quality` reports
  `pct_occupancy_unknown` so a reader can judge whether it is worth using.
* `departureConnection` is a URI of the form
  `http://irail.be/connections/8813003/20260727/S11958` — station, date, vehicle.
  Its resolution is the *date*, not the minute, so it is stored as an attribute
  and not used as a key (see [schema.md](schema.md#the-natural-key-and-why-it-is-those-three-columns)).
* `isExtra = "1"` marks an unscheduled extra service, i.e. a departure that is
  not in the published timetable at all.
* An empty liveboard omits `departures.departure` entirely, and iRail has
  historically collapsed a one-element array to a bare object. Both are handled
  (`_departure_entries`) and tested.

---

## Rate limiting and courtesy

iRail is **free and volunteer-run**. It asks for a contactable `User-Agent` and
no more than ~3 requests/second. What this project does:

| measure | value |
|---|---|
| minimum interval between calls from one client | **0.4 s** (`IRAIL_MIN_INTERVAL_SECONDS`) — under half the stated limit |
| requests per timer firing | 10 (one per hub) |
| firings per day (default schedule) | ~32 |
| **requests per day** | **~320** |
| retries | 3 attempts max, exponential backoff, `Retry-After` honoured |
| retried statuses | 408, 425, 429, 500, 502, 503, 504 — **and nothing else** |
| `User-Agent` | `RailPulseCloud/1.0 (BeCode data-engineering exercise; <contact>)` |

Two of those are worth spelling out:

**Only transient statuses are retried.** A 404 on a station id we constructed is a
bug in our request; retrying it five times with backoff is abuse dressed up as
resilience. It fails immediately and loudly.

**Backoff only happens if another attempt will follow.** Sleeping after the final
failure delays the exception for no benefit — and on a Consumption plan, that
sleep is billed.

**Set a real contact address** in `IRAIL_USER_AGENT`. It is the difference between
the operator emailing you and the operator blocking you.

---

## Licence and attribution

iRail republishes NMBS/SNCB data under **CC BY 4.0** and asks that the source be
credited. Any published output of this pipeline should carry:

> Data: [iRail](https://irail.be) / NMBS-SNCB, CC BY 4.0.

The `v1` API also has no authentication, which is a courtesy rather than an
invitation. Everything above is what keeps it that way.

---

## Testing against the real thing, offline

Three real responses are committed under `tests/fixtures/`:

| fixture | what it is |
|---|---|
| `liveboard_brussels_central.json` | 56 departures, 6 platforms, delays of 0/60/360/420 s, all four occupancy values |
| `liveboard_brussels_midi.json` | 59 departures including international services (EUR, ICE, EC) and a cancellation |
| `stations_subset.json` | 7 stations including a Dutch one, for the UIC country derivation |

They are **recorded**, not hand-written, which is what makes the tests meaningful:
every quirk above is present because the feed really does it. The suite runs in
0.2 seconds with no network and no Azure subscription.

To refresh them after an upstream change:

```bash
curl -sSL -A "RailPulseCloud/1.0 (contact@example.com)" \
  "https://api.irail.be/v1/liveboard?id=BE.NMBS.008813003&format=json&lang=en&alerts=true" \
  -o tests/fixtures/liveboard_brussels_central.json
```

If the tests then fail, the API changed — which is exactly what a fixture is for.
