"""The only place in this project that touches the network.

The iRail API is a volunteer-run service in front of SNCB/NMBS data. Its terms
ask for three things, and this module is those three things in code:

* **Identify yourself.** A contactable ``User-Agent`` (config.IRAIL_USER_AGENT).
* **Stay well under 3 requests/second.** A per-client minimum-interval gate,
  not a "hope the loop is slow enough" assumption.
* **Do not hammer on failure.** Retries are bounded, backoff is exponential,
  and the server's own ``Retry-After`` wins over our guess when it sends one.

WHAT WAS LEARNED FROM THE LIVE FEED (2026-07-27)
* The documented ``/liveboard/`` path answers **303** with a ``Location`` of
  ``/v1/liveboard``. Following redirects works, but doubles the request count
  against the rate limit, so this client calls v1 directly.
* **Every scalar is a JSON string**, including numbers and booleans:
  ``"delay": "420"``, ``"canceled": "0"``. Nothing here or in transform.py may
  assume an int.
* An unknown platform is the literal string ``"?"``.
* ``vehicleinfo.type`` carries the *line* for suburban trains (``"S32"``), not
  just the class.
These are asserted by the tests against captured payloads, so a change upstream
shows up as a test failure rather than as bad data.
"""

from __future__ import annotations

import email.utils
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import requests

from . import config

logger = logging.getLogger(__name__)


class IRailError(RuntimeError):
    """A call to iRail failed in a way the caller must record, not swallow."""

    def __init__(self, message: str, *, status_code: int | None = None,
                 url: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url


@dataclass
class ApiResponse:
    """One successful call. Carries the metadata `ingestion_runs` records."""

    url: str
    status_code: int
    payload: dict[str, Any]
    bytes_downloaded: int
    elapsed_ms: int
    attempts: int = 1

    #: Header timestamp of the feed itself (when *they* built it), as a POSIX
    #: epoch string. Distinct from when *we* asked.
    @property
    def feed_timestamp_epoch(self) -> int | None:
        raw = self.payload.get("timestamp")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None


class _MinIntervalGate:
    """Keeps at least ``min_interval`` seconds between calls from one client.

    Per-instance, not global. The pipeline creates one client per invocation and
    polls hubs sequentially through it, so in practice that is one gate per
    invocation — which is the unit that matters, because two concurrent
    invocations are two different processes anyway.
    """

    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(min_interval, 0.0)
        self._last: float | None = None

    def wait(self) -> float:
        if self._last is None:
            self._last = time.monotonic()
            return 0.0
        sleep_for = self.min_interval - (time.monotonic() - self._last)
        if sleep_for > 0:
            time.sleep(sleep_for)
        else:
            sleep_for = 0.0
        self._last = time.monotonic()
        return sleep_for


#: Transient by nature: worth one more try. Everything else (401, 404, 400) is
#: a bug in the request, and retrying it is just abuse of a free service.
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Never sleep longer than this on a server's instruction, however emphatic.
MAX_BACKOFF_SECONDS = 60.0


@dataclass
class IRailClient:
    """Rate-limited, retrying client for the two endpoints this project uses."""

    user_agent: str = config.IRAIL_USER_AGENT
    base_url: str = config.IRAIL_BASE_URL
    lang: str = config.IRAIL_LANG
    timeout: int = config.IRAIL_TIMEOUT
    max_attempts: int = config.IRAIL_MAX_ATTEMPTS
    min_interval: float = config.IRAIL_MIN_INTERVAL_SECONDS
    session: requests.Session = field(default_factory=requests.Session, repr=False)

    def __post_init__(self) -> None:
        self._gate = _MinIntervalGate(self.min_interval)
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
        })

    # -- public API --------------------------------------------------------
    def liveboard(self, station: str, *, alerts: bool | None = None) -> ApiResponse:
        """Fetch the departures liveboard for one station.

        ``station`` may be an iRail id (``BE.NMBS.008813003``) or a name. Ids go
        in the ``id`` parameter and names in ``station``; the distinction is
        made here so callers never have to think about it.
        """
        station = station.strip()
        if not station:
            raise IRailError("liveboard() needs a station id or name")

        params = {
            "format": "json",
            "lang": self.lang,
            # arrdep=departure is the default, but being explicit means an
            # upstream default change cannot silently turn this into arrivals.
            "arrdep": "departure",
            "alerts": "true" if (
                config.IRAIL_REQUEST_ALERTS if alerts is None else alerts
            ) else "false",
        }
        # An id always starts with the operator prefix; a name never does.
        params["id" if station.upper().startswith("BE.NMBS.") else "station"] = station

        return self._get("liveboard", params)

    def stations(self) -> ApiResponse:
        """Fetch the full station catalogue (~714 rows) in one call."""
        return self._get("stations", {"format": "json", "lang": self.lang})

    # -- core request loop -------------------------------------------------
    def _get(self, endpoint: str, params: dict[str, str]) -> ApiResponse:
        url = f"{self.base_url.rstrip('/')}/{endpoint}"
        display_url = f"{url}?{urlencode(params)}"
        last_error: Exception | None = None

        for attempt in range(1, max(self.max_attempts, 1) + 1):
            waited = self._gate.wait()
            if waited:
                logger.debug("rate gate: slept %.2fs before %s", waited, endpoint)

            started = time.monotonic()
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                self._sleep_before_retry(attempt, None, str(exc))
                continue

            elapsed_ms = int((time.monotonic() - started) * 1000)

            if response.status_code in RETRYABLE_STATUSES:
                last_error = IRailError(
                    f"HTTP {response.status_code} from {endpoint}",
                    status_code=response.status_code, url=display_url,
                )
                self._sleep_before_retry(
                    attempt,
                    _parse_retry_after(response.headers.get("Retry-After")),
                    f"HTTP {response.status_code}",
                )
                continue

            if not response.ok:
                # 400/404 on a station id we constructed, 401 on a key we do not
                # send — a request bug. Fail loudly and immediately.
                raise IRailError(
                    f"HTTP {response.status_code} from {endpoint}: "
                    f"{response.text[:200]}",
                    status_code=response.status_code, url=display_url,
                )

            try:
                payload = response.json()
            except ValueError as exc:
                # A 200 that is not JSON is how a captive portal or an outage
                # page announces itself. Retrying is reasonable; parsing is not.
                last_error = IRailError(f"non-JSON 200 from {endpoint}: {exc}",
                                        status_code=200, url=display_url)
                self._sleep_before_retry(attempt, None, "non-JSON 200")
                continue

            if not isinstance(payload, dict):
                raise IRailError(
                    f"unexpected payload type from {endpoint}: "
                    f"{type(payload).__name__}",
                    status_code=response.status_code, url=display_url,
                )

            return ApiResponse(
                url=display_url,
                status_code=response.status_code,
                payload=payload,
                bytes_downloaded=len(response.content),
                elapsed_ms=elapsed_ms,
                attempts=attempt,
            )

        raise IRailError(
            f"giving up on {endpoint} after {self.max_attempts} attempt(s): "
            f"{last_error}",
            status_code=getattr(last_error, "status_code", None),
            url=display_url,
        ) from last_error

    def _sleep_before_retry(
        self, attempt: int, server_backoff: float | None, reason: str
    ) -> None:
        """Back off, but only if another attempt will actually follow.

        Sleeping after the final failure delays the exception for no benefit —
        and on a Consumption plan that sleep is billed.
        """
        if attempt >= self.max_attempts:
            logger.warning("iRail attempt %d/%d failed (%s); giving up",
                           attempt, self.max_attempts, reason)
            return
        # `is None` rather than a truthiness test: 'Retry-After: 0' is a valid
        # instruction meaning "retry now", and `or` would discard it.
        backoff = (server_backoff if server_backoff is not None
                   else min(2.0 ** attempt, MAX_BACKOFF_SECONDS))
        logger.warning("iRail attempt %d/%d failed (%s); retrying in %.1fs",
                       attempt, self.max_attempts, reason, backoff)
        time.sleep(backoff)

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "IRailClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _parse_retry_after(value: str | None) -> float | None:
    """Interpret a ``Retry-After`` header in either RFC-9110 form.

    Returns None when there is no usable instruction, which the caller
    distinguishes from a legitimate 0. ``parsedate_to_datetime`` *raises* on
    unparseable input in Python 3.10+, and letting that propagate would turn a
    cosmetically malformed header into a failed ingestion run.
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return min(float(value), MAX_BACKOFF_SECONDS)
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return min(max(parsed.timestamp() - time.time(), 0.0), MAX_BACKOFF_SECONDS)
