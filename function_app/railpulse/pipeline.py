"""Orchestration: fetch a liveboard, parse it, load it, record what happened.

TRANSACTION AND FAILURE MODEL
This is the part of the pipeline that has to be honest about failure, so the
boundaries are drawn deliberately:

  1. The audit row is opened **and committed** before iRail is called. If the
     process is killed, or the API is down, or the load explodes, there is still
     a row in `ingestion_runs` saying a run started and never finished. An audit
     log written only on success is not an audit log.
  2. The API call happens **outside** any transaction. Holding a database
     transaction open across a network call to a third party is how you turn
     someone else's slow afternoon into your lock contention.
  3. The load and the audit row's completion commit **together**. Counts in
     `ingestion_runs` can therefore never disagree with the rows in
     `liveboard_records`.
  4. A failure rolls the load back and then writes `status = 'failed'` with the
     error in a **separate** transaction — because the point of recording a
     failure is defeated if recording it is part of what got rolled back.

Stations are independent: one hub failing (iRail 503, a bad station id) must not
cost the other nine. Each gets its own transaction, and the loop keeps going.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

import pyodbc

from . import config, database, hubs, loader, transform
from .irail import ApiResponse, IRailClient, IRailError

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    """Naive UTC, truncated to the second.

    Truncated because the destination columns are DATETIME2(0): rounding on the
    server would otherwise be able to push a `last_seen_utc` a fraction of a
    second *below* the `first_seen_utc` it was copied from and trip the
    ck_lbr_seen_order check constraint.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


@dataclass
class StationResult:
    """Outcome of polling one station. Serialised straight into the HTTP body."""

    requested: str
    station_id: str | None = None
    station_name: str | None = None
    run_id: int | None = None
    status: str = "failed"
    api_status_code: int | None = None
    api_attempts: int | None = None
    feed_timestamp_utc: datetime | None = None
    departures_returned: int = 0
    duplicates_dropped: int = 0
    unusable_dropped: int = 0
    alerts_seen: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    stations_upserted: int = 0
    vehicles_upserted: int = 0
    platforms_upserted: int = 0
    duration_ms: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = dict(self.__dict__)
        payload["feed_timestamp_utc"] = (
            self.feed_timestamp_utc.isoformat() if self.feed_timestamp_utc else None
        )
        return payload


@dataclass
class PipelineResult:
    """Aggregate across every station in one invocation."""

    trigger_source: str
    invocation_id: str | None
    started_utc: datetime
    stations: list[StationResult] = field(default_factory=list)

    @property
    def succeeded(self) -> int:
        return sum(1 for s in self.stations if s.status == "success")

    @property
    def failed(self) -> int:
        return sum(1 for s in self.stations if s.status != "success")

    @property
    def rows_inserted(self) -> int:
        return sum(s.rows_inserted for s in self.stations)

    @property
    def rows_updated(self) -> int:
        return sum(s.rows_updated for s in self.stations)

    @property
    def departures_returned(self) -> int:
        return sum(s.departures_returned for s in self.stations)

    def as_dict(self) -> dict[str, Any]:
        return {
            "trigger_source": self.trigger_source,
            "invocation_id": self.invocation_id,
            "started_utc": self.started_utc.isoformat() + "Z",
            "stations_polled": len(self.stations),
            "stations_succeeded": self.succeeded,
            "stations_failed": self.failed,
            "departures_returned": self.departures_returned,
            "rows_inserted": self.rows_inserted,
            "rows_updated": self.rows_updated,
            "duration_ms": sum(s.duration_ms for s in self.stations),
            "results": [s.as_dict() for s in self.stations],
        }


class SqlSession:
    """A lazily-opened connection that can be thrown away and reopened.

    Reusing one connection across ten hubs avoids paying the login round trip
    (and, on the first call of the day, the serverless resume) ten times over.
    But a connection that has seen a transport-level error is dead, and every
    subsequent statement on it fails identically — so :meth:`reset` exists to
    discard it, and the next station transparently gets a fresh one.
    """

    def __init__(self) -> None:
        self._connection: pyodbc.Connection | None = None

    @property
    def connection(self) -> pyodbc.Connection:
        if self._connection is None:
            self._connection = database.connect_with_retry()
        return self._connection

    def reset(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:  # pragma: no cover - already broken
                pass
            self._connection = None

    def close(self) -> None:
        self.reset()

    def __enter__(self) -> "SqlSession":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# ==========================================================================
# One station
# ==========================================================================
def ingest_station(
    session: SqlSession,
    client: IRailClient,
    station_token: str,
    *,
    trigger_source: str,
    invocation_id: str | None = None,
    is_hub: bool = True,
) -> StationResult:
    """Poll one station and load it. Never raises: failures come back as data.

    Not raising is a deliberate contract. The caller is a loop over ten hubs and
    an HTTP handler that owes the operator a response; both need "this one
    failed, here is why" as a value, not as an exception that ends the run.
    """
    started = time.monotonic()
    started_utc = utc_now()
    result = StationResult(requested=station_token)
    known_hub = hubs.resolve(station_token)
    if known_hub:
        result.station_name = known_hub.label

    # ---- 1. open and commit the audit row -------------------------------
    try:
        with database.transaction(session.connection) as cursor:
            result.run_id = loader.open_run(
                cursor,
                trigger_source=trigger_source,
                requested_station=station_token,
                started_utc=started_utc,
                invocation_id=invocation_id,
                station_id=(known_hub.station_id if known_hub else None),
            )
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        session.reset()
        result.error = f"could not open ingestion run: {_short(exc)}"
        result.duration_ms = _elapsed_ms(started)
        logger.error("ingest %s: %s", station_token, result.error)
        return result

    # ---- 2. call the API, outside any transaction ------------------------
    try:
        response: ApiResponse = client.liveboard(station_token)
        result.api_status_code = response.status_code
        result.api_attempts = response.attempts
    except IRailError as exc:
        result.api_status_code = exc.status_code
        result.error = f"iRail: {_short(exc)}"
        _record_failure(session, result, started, started_utc)
        return result
    except Exception as exc:  # noqa: BLE001
        result.error = f"unexpected fetch error: {_short(exc)}"
        _record_failure(session, result, started, started_utc)
        return result

    # ---- 3. parse (pure, cannot touch the network or the database) -------
    batch = transform.parse_liveboard(
        response.payload, seen_utc=started_utc, is_hub=is_hub
    )
    result.station_id = batch.station_id or result.station_id
    result.station_name = batch.station_name or result.station_name
    result.feed_timestamp_utc = batch.feed_timestamp_utc
    result.departures_returned = batch.departures_returned
    result.duplicates_dropped = batch.duplicates_dropped
    result.unusable_dropped = batch.unusable_dropped
    result.alerts_seen = batch.alerts_seen

    if batch.station_id is None:
        result.error = ("feed answered but carried no station identity; "
                        "nothing could be keyed")
        _record_failure(session, result, started, started_utc,
                        api_url=response.url)
        return result

    # ---- 4. load and complete the audit row, in one transaction ---------
    # Retried once on a transient fault: MERGE-on-natural-key means a partially
    # applied attempt converges to the same state on the second, so this is safe
    # in a way a row-by-row insert would not be.
    attempts = 2
    for attempt in range(1, attempts + 1):
        try:
            with database.transaction(session.connection) as cursor:
                counts = loader.load_batch(cursor, batch, result.run_id)
                loader.close_run(
                    cursor, result.run_id,
                    status="success",
                    finished_utc=utc_now(),
                    duration_ms=_elapsed_ms(started),
                    station_id=batch.station_id,
                    api_status_code=response.status_code,
                    api_url=response.url,
                    feed_timestamp_utc=batch.feed_timestamp_utc,
                    departures_returned=batch.departures_returned,
                    rows_skipped=batch.duplicates_dropped + batch.unusable_dropped,
                    counts=counts,
                )
            result.status = "success"
            result.rows_inserted = counts.rows_inserted
            result.rows_updated = counts.rows_updated
            result.stations_upserted = counts.stations_upserted
            result.vehicles_upserted = counts.vehicles_upserted
            result.platforms_upserted = counts.platforms_upserted
            result.duration_ms = _elapsed_ms(started)
            logger.info(
                "ingest %s (%s): %d departures -> %d new, %d revised in %d ms",
                result.station_name or station_token, batch.station_id,
                batch.departures_returned, counts.rows_inserted,
                counts.rows_updated, result.duration_ms,
            )
            return result
        except Exception as exc:  # noqa: BLE001
            transient = database.is_transient(exc)
            session.reset()
            if transient and attempt < attempts:
                logger.warning("load %s failed transiently (%s); retrying",
                               station_token, _short(exc))
                time.sleep(config.SQL_RETRY_BASE_SECONDS)
                continue
            result.error = f"load failed: {_short(exc)}"
            _record_failure(session, result, started, started_utc,
                            api_url=response.url)
            return result

    return result  # pragma: no cover - loop always returns


def _record_failure(
    session: SqlSession,
    result: StationResult,
    started: float,
    started_utc: datetime,
    *,
    api_url: str | None = None,
) -> None:
    """Mark the run failed in its own transaction, and never mask the cause.

    If even *this* write fails there is nothing useful left to do but log: the
    original error is what the operator needs, and replacing it with a
    bookkeeping error would hide it.
    """
    result.duration_ms = _elapsed_ms(started)
    logger.error("ingest %s failed: %s", result.requested, result.error)
    if result.run_id is None:
        return
    try:
        with database.transaction(session.connection) as cursor:
            loader.close_run(
                cursor, result.run_id,
                status="failed",
                finished_utc=utc_now(),
                duration_ms=result.duration_ms,
                station_id=result.station_id,
                api_status_code=result.api_status_code,
                api_url=api_url,
                departures_returned=result.departures_returned,
                error_message=result.error,
            )
    except Exception as exc:  # noqa: BLE001
        session.reset()
        logger.error("could not record the failure of run %s: %s",
                     result.run_id, _short(exc))


# ==========================================================================
# Many stations
# ==========================================================================
def ingest_stations(
    station_tokens: Sequence[str],
    *,
    trigger_source: str,
    invocation_id: str | None = None,
) -> PipelineResult:
    """Poll every requested station in sequence.

    Sequential on purpose. Ten hubs at 0.4 s apart is under five seconds of
    wall clock, and iRail is a free volunteer-run service: parallelising to save
    three seconds while multiplying our request rate by ten would be a poor
    trade for everyone.
    """
    outcome = PipelineResult(
        trigger_source=trigger_source,
        invocation_id=invocation_id,
        started_utc=utc_now(),
    )
    logger.info("ingest run (%s) starting for %d station(s); SQL target: %s",
                trigger_source, len(station_tokens), config.describe_sql_target())

    with SqlSession() as session, IRailClient() as client:
        for token in station_tokens:
            outcome.stations.append(
                ingest_station(
                    session, client, token,
                    trigger_source=trigger_source,
                    invocation_id=invocation_id,
                )
            )

    logger.info("ingest run (%s) finished: %d ok, %d failed, "
                "%d new rows, %d revised",
                trigger_source, outcome.succeeded, outcome.failed,
                outcome.rows_inserted, outcome.rows_updated)
    return outcome


def ingest_configured_hubs(
    *, trigger_source: str, invocation_id: str | None = None
) -> PipelineResult:
    """Poll the hub set from the RAILPULSE_HUBS setting (or the default ten)."""
    return ingest_stations(
        [hub.station_id for hub in hubs.configured_hubs()],
        trigger_source=trigger_source,
        invocation_id=invocation_id,
    )


# ==========================================================================
# Station catalogue seed
# ==========================================================================
def seed_station_catalogue(*, trigger_source: str = "http") -> dict[str, Any]:
    """Load iRail's full station list, so the dimension is complete on day one.

    One API call for ~714 stations with coordinates. Without this, `stations`
    would only ever contain places that happen to be a hub or somebody's
    terminus, and a map visual would show a network with holes in it.
    """
    started = time.monotonic()
    started_utc = utc_now()
    hub_ids = {hub.station_id for hub in hubs.configured_hubs()}

    with IRailClient() as client:
        response = client.stations()
    rows = transform.parse_station_catalogue(
        response.payload, seen_utc=started_utc, hub_ids=hub_ids
    )

    def _write(connection: pyodbc.Connection) -> int:
        with database.transaction(connection) as cursor:
            run_id = loader.open_run(
                cursor, trigger_source=trigger_source,
                requested_station="__station_catalogue__",
                started_utc=started_utc,
            )
            written = loader.seed_stations(cursor, rows)
            loader.close_run(
                cursor, run_id, status="success", finished_utc=utc_now(),
                duration_ms=_elapsed_ms(started),
                api_status_code=response.status_code, api_url=response.url,
                departures_returned=0,
                counts=loader.LoadCounts(stations_upserted=written),
            )
            return written

    written = database.run_with_retry(_write)
    logger.info("station catalogue seeded: %d stations (%d flagged as hubs)",
                written, len(hub_ids))
    return {
        "stations_in_feed": len(response.payload.get("station") or []),
        "stations_written": written,
        "hubs_flagged": sorted(hub_ids),
        "duration_ms": _elapsed_ms(started),
    }


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _short(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:400]
