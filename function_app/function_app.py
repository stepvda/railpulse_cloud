"""RailPulse Cloud — Azure Functions app (Python v2 programming model).

FIVE HTTP ENDPOINTS AND ONE TIMER
    GET  /api/ping                  liveness. Anonymous, touches no database.
    GET  /api/ingest                poll one station (default Brussels-Central)
    POST /api/ingest?hubs=all       poll the whole configured hub set
    GET  /api/health                is the pipeline running and fresh?
    GET  /api/stats                 row counts, data quality, leaderboard
    POST /api/migrate               apply sql/*.sql (idempotent)
    POST /api/seed-stations         load the ~714-station catalogue
    timer ingest_timer              the scheduled pull (NCRONTAB, see below)

AUTHORISATION
Everything except /api/ping requires a function key (`?code=...` or the
`x-functions-key` header). That is the default for a reason: /api/ingest writes
to the database and calls a third-party API, so an anonymous route would be an
open invitation to have someone else's traffic billed to this subscription and
attributed to our User-Agent at iRail. /api/ping is anonymous so an uptime
monitor needs no secret.

THE SCHEDULE, AND WHY IT IS NOT EVERY 15 MINUTES ROUND THE CLOCK
`INGEST_SCHEDULE` defaults to `0 */15 6-9,16-19 * * 1-5` — every 15 minutes
during the weekday morning and evening peaks, in Belgian local time (the app
setting WEBSITE_TIME_ZONE=Europe/Brussels makes NCRONTAB local rather than UTC).

That is a cost decision, and it is the most consequential one in the project.
A timer firing every 15 minutes all day would never let the serverless database
reach its one-hour idle threshold, so it would never auto-pause, so it would
bill ~0.5 vCore continuously — around $190/month, which exhausts the $100
student credit in about two weeks. The brief asks for both a 15-minute timer and
a one-hour auto-pause; those two requirements are in direct tension, and this is
where the tension is resolved. Sampling the peaks hard and letting the database
sleep the rest of the time keeps the interesting data and roughly a fifth of the
cost. Full reasoning and arithmetic: docs/cost_control.md.

To collect round the clock instead, set INGEST_SCHEDULE to `0 */15 * * * *` and
accept the bill — the code does not care, which is the point of it being a
setting.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import azure.functions as func

from railpulse import config, database, hubs, migrations, pipeline, reporting

logger = logging.getLogger("railpulse")

#: The NCRONTAB schedule, as the literal `%SETTING%` indirection the Functions
#: host resolves from Application Settings AT RUNTIME.
#:
#: This was originally written as `os.environ.get("INGEST_SCHEDULE", default)`,
#: on the reasoning that a missing app setting would then degrade to a default
#: instead of breaking the app. That reasoning was wrong in a way that only shows
#: up in the cloud: on Flex Consumption the trigger metadata is generated during
#: the remote build and CACHED, so a value computed at import time is frozen at
#: deploy time. Changing INGEST_SCHEDULE afterwards changed nothing, and
#: `az functionapp function show` kept reporting the build-time value — which
#: silently broke the one operational lever this project documents (the
#: cost/coverage trade in docs/cost_control.md).
#:
#: `%INGEST_SCHEDULE%` is resolved by the host on every start, so the setting
#: works as documented. The cost is real and accepted: if the setting is ever
#: absent, this function fails to load. azure/provision.sh always sets it, and
#: function_app/local.settings.json.example carries it for local runs.
INGEST_SCHEDULE = "%INGEST_SCHEDULE%"

#: What provision.sh sets the setting to, kept here only so /api/ping can report
#: the intended cadence in a human-readable form.
DEFAULT_SCHEDULE = "0 */15 6-9,16-19 * * 1-5"

#: The station a bare GET /api/ingest polls, per the brief's "a major hub
#: (like Brussels-Central)". Keeping the default to ONE station means a curious
#: click cannot accidentally fire ten API calls.
DEFAULT_STATION = "BE.NMBS.008813003"

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


# ==========================================================================
# Helpers
# ==========================================================================
def _json_response(payload: Any, status_code: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload, indent=2, default=reporting.jsonable,
                   ensure_ascii=False),
        status_code=status_code,
        mimetype="application/json",
    )


def _error_response(exc: Exception, status_code: int = 500) -> func.HttpResponse:
    """Return a diagnosable error without leaking the connection string.

    `str(exc)` on a pyodbc failure includes the driver message but not the
    credentials; config.describe_sql_target() is written to be safe to publish.
    The distinction matters because this body is what a teammate will paste into
    chat when asking why the deployment is broken.
    """
    logger.exception("request failed")
    return _json_response(
        {
            "status": "error",
            "error": " ".join(str(exc).split())[:800],
            "error_type": type(exc).__name__,
            "sql_target": config.describe_sql_target(),
            "hint": (
                "Check the SQL_CONNECTION_STRING app setting, that the SQL "
                "server firewall has 'Allow Azure services and resources to "
                "access this server' enabled, and that POST /api/migrate "
                "has been run at least once."
            ),
        },
        status_code=status_code,
    )


def _param(req: func.HttpRequest, name: str) -> str | None:
    """Read a parameter from the query string or a JSON body, in that order."""
    value = req.params.get(name)
    if value:
        return value.strip()
    try:
        body = req.get_json()
    except ValueError:
        return None
    if isinstance(body, dict):
        raw = body.get(name)
        if raw is not None:
            return str(raw).strip()
    return None


# ==========================================================================
# Liveness
# ==========================================================================
@app.route(route="ping", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def ping(req: func.HttpRequest) -> func.HttpResponse:
    """Prove the app is up without waking the database.

    Anonymous and database-free on purpose: an uptime check that resumed a
    paused serverless database every five minutes would defeat auto-pause and
    quietly become the largest line on the bill.
    """
    return _json_response({
        "status": "ok",
        "app": "railpulse-cloud",
        "sql_target": config.describe_sql_target(),
        # Read from the environment here rather than reporting the literal
        # "%INGEST_SCHEDULE%" the decorator uses: at request time the worker has
        # the resolved value, which is what an operator actually wants to see.
        "timer_schedule": os.environ.get("INGEST_SCHEDULE", "").strip()
                          or f"{DEFAULT_SCHEDULE} (setting absent — using the default)",
        "configured_hubs": [hub.label for hub in hubs.configured_hubs()],
    })


# ==========================================================================
# Ingestion — the must-have HTTP trigger
# ==========================================================================
@app.route(route="ingest", methods=["GET", "POST"])
def ingest(req: func.HttpRequest) -> func.HttpResponse:
    """Fetch liveboard data and load it into Azure SQL.

        GET  /api/ingest                        -> Brussels-Central
        GET  /api/ingest?station=Leuven         -> one station by name or id
        POST /api/ingest?hubs=all               -> every configured hub

    Returns 200 when every requested station loaded, and **207 Multi-Status**
    when some did and some did not. A partial failure genuinely is neither
    success nor failure, and collapsing it into 200 would make a monitor blind
    to a single hub that has been failing for a week.
    """
    try:
        station = _param(req, "station")
        hubs_param = (_param(req, "hubs") or "").lower()

        if hubs_param in {"all", "true", "1", "default"}:
            result = pipeline.ingest_configured_hubs(
                trigger_source="http",
                invocation_id=_invocation_id(req),
            )
        else:
            result = pipeline.ingest_stations(
                [station or DEFAULT_STATION],
                trigger_source="http",
                invocation_id=_invocation_id(req),
            )

        body = result.as_dict()
        body["status"] = "ok" if result.failed == 0 else "partial"
        return _json_response(body, 200 if result.failed == 0 else 207)
    except Exception as exc:  # noqa: BLE001 - the HTTP boundary owes a response
        return _error_response(exc)


@app.timer_trigger(schedule=INGEST_SCHEDULE, arg_name="timer",
                   run_on_startup=False, use_monitor=True)
def ingest_timer(timer: func.TimerRequest) -> None:
    """The scheduled pull — the nice-to-have, and where the history comes from.

    `run_on_startup=False` because a restart (deployment, scale event, platform
    patch) would otherwise fire an unscheduled run and, worse, resume a paused
    database at an arbitrary moment. `use_monitor=True` keeps the schedule
    durable across restarts, so a missed slot is caught up rather than skipped.

    Exceptions are logged, not raised: the per-station error handling in
    pipeline.py has already recorded what failed, and letting an exception out
    here would only add a retry storm from the platform on top of it.
    """
    if timer.past_due:
        logger.warning("timer is past due — the previous slot was missed")

    try:
        result = pipeline.ingest_configured_hubs(
            trigger_source="timer", invocation_id=None
        )
        logger.info("timer run complete: %s", json.dumps({
            "stations_succeeded": result.succeeded,
            "stations_failed": result.failed,
            "rows_inserted": result.rows_inserted,
            "rows_updated": result.rows_updated,
        }))
    except Exception:  # noqa: BLE001
        logger.exception("timer run failed outright")


# ==========================================================================
# Observability
# ==========================================================================
@app.route(route="health", methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    """Row counts and per-station freshness. Wakes the database — by design."""
    try:
        def _read(connection: Any) -> dict[str, Any]:
            cursor = connection.cursor()
            try:
                return reporting.health_snapshot(cursor)
            finally:
                cursor.close()

        snapshot = database.run_with_retry(_read)
        stale = [s for s in snapshot["stations"] if s.get("is_stale")]
        snapshot["status"] = "degraded" if stale else "ok"
        snapshot["stale_stations"] = [s["station_id"] for s in stale]
        return _json_response(snapshot, 200 if not stale else 207)
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc, 503)


@app.route(route="stats", methods=["GET"])
def stats(req: func.HttpRequest) -> func.HttpResponse:
    """Data-quality summary and hub leaderboard — proof the warehouse is usable."""
    try:
        def _read(connection: Any) -> dict[str, Any]:
            cursor = connection.cursor()
            try:
                return reporting.stats_snapshot(cursor)
            finally:
                cursor.close()

        return _json_response({"status": "ok", **database.run_with_retry(_read)})
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc, 503)


# ==========================================================================
# Administration
# ==========================================================================
@app.route(route="migrate", methods=["POST"])
def admin_migrate(req: func.HttpRequest) -> func.HttpResponse:
    """Apply sql/*.sql. Idempotent, so it is safe to call after every deploy.

    POST rather than GET because it changes the database, even though what it
    changes is usually nothing.
    """
    try:
        def _migrate(connection: Any) -> list[dict[str, Any]]:
            with database.transaction(connection) as cursor:
                return [
                    {"file": r.file_name, "batches": r.batches,
                     "skipped": r.skipped, "reason": r.reason}
                    for r in migrations.apply_all(cursor)
                ]

        applied = database.run_with_retry(_migrate)
        return _json_response({
            "status": "ok",
            "sql_directory": str(migrations.sql_directory()),
            "applied": applied,
        })
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc)


@app.route(route="seed-stations", methods=["POST"])
def admin_seed_stations(req: func.HttpRequest) -> func.HttpResponse:
    """Load iRail's full station catalogue into the stations dimension."""
    try:
        return _json_response({"status": "ok",
                               **pipeline.seed_station_catalogue()})
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc)


def _invocation_id(req: func.HttpRequest) -> str | None:
    """Best-effort correlation id, for joining a run to Application Insights.

    Truncated to 64 characters because that is the width of
    `ingestion_runs.invocation_id`. Both real sources fit (a GUID is 36, a
    `traceparent` 55), but `traceparent` arrives from the caller — and a header
    long enough to overflow the column would fail the INSERT and cost the whole
    poll, which is an absurd way to lose data to a diagnostic field.
    """
    raw = (
        req.headers.get("x-azure-functions-invocationid")
        or req.headers.get("traceparent")
        or ""
    ).strip()
    return raw[:64] or None
