"""Every tunable in the pipeline, read from the environment exactly once.

WHY THIS FILE EXISTS
No other module in the package touches ``os.environ``. When a value looks wrong
in production there is one place to look, and when a setting needs adding there
is one place to add it — which matters more than usual here, because in Azure
these values are not files on disk but **Application Settings** on the Function
App, edited in a portal blade by a human under time pressure.

THE ONE SECRET
``SQL_CONNECTION_STRING`` is the only secret, it is never defaulted, and it is
never logged (:func:`describe_sql_target` exists so that start-up logging can
say *where* it is connecting without saying *how*). It lives in the Function
App's Environment variables, as the brief requires — not in this repository.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------
# .env support for local runs. Optional by design: in Azure the settings come
# from the platform, and the pipeline must not depend on a dev convenience.
# --------------------------------------------------------------------------
try:  # pragma: no cover - trivial import guard
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _env(name: str, default: str = "") -> str:
    """Read an environment variable, treating blank as unset.

    Blank matters: an Application Setting that exists with an empty value is a
    very common way to break a deployment, and it should behave like "not set"
    rather than like an empty string.
    """
    return os.environ.get(name, "").strip() or default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


# ==========================================================================
# Azure SQL
# ==========================================================================
#: ODBC connection string, e.g.
#:   Driver={ODBC Driver 18 for SQL Server};Server=tcp:x.database.windows.net,1433;
#:   Database=railpulse;Uid=...;Pwd=...;Encrypt=yes;TrustServerCertificate=no;
#:   Connection Timeout=60;
#: Set as an Application Setting on the Function App. Never committed.
SQL_CONNECTION_STRING = _env("SQL_CONNECTION_STRING")

#: Seconds to wait for a connection. Deliberately generous: this database is
#: serverless with a one-hour auto-pause, so the first connection after a quiet
#: night has to wait for a cold resume, which routinely takes 30-60 seconds.
#: A default 15-second timeout would turn every morning's first timer run into
#: a failure.
SQL_LOGIN_TIMEOUT = _env_int("SQL_LOGIN_TIMEOUT", 60)

#: Query timeout once connected (0 = no limit). The heaviest statement is a
#: MERGE of ~60 rows, so anything beyond a few seconds means trouble.
SQL_QUERY_TIMEOUT = _env_int("SQL_QUERY_TIMEOUT", 120)

#: How many times to retry a *transient* failure (see database.is_transient).
#: Five attempts with exponential backoff covers a serverless resume plus the
#: throttling Azure SQL applies while it is still warming up.
SQL_MAX_ATTEMPTS = _env_int("SQL_MAX_ATTEMPTS", 5)
SQL_RETRY_BASE_SECONDS = _env_float("SQL_RETRY_BASE_SECONDS", 2.0)

# ==========================================================================
# iRail API
# ==========================================================================
#: The v1 base. The legacy /liveboard/ path answers 303 and redirects here, so
#: calling v1 directly saves a round trip per poll.
IRAIL_BASE_URL = _env("IRAIL_BASE_URL", "https://api.irail.be/v1")

#: iRail's terms ask every client to identify itself with a contactable
#: User-Agent. This is not decoration: it is the difference between the
#: operator emailing you and the operator blocking you.
IRAIL_USER_AGENT = _env(
    "IRAIL_USER_AGENT",
    "RailPulseCloud/1.0 (BeCode data-engineering exercise; "
    "https://github.com/stepvda/railpulse_cloud)",
)

#: Language for station names. Belgian stations have up to four names; picking
#: one and sticking to it is what keeps `stations.name` a usable dimension.
#: The bilingual official form is kept separately as `standard_name`.
IRAIL_LANG = _env("IRAIL_LANG", "en")

IRAIL_TIMEOUT = _env_int("IRAIL_TIMEOUT", 20)
IRAIL_MAX_ATTEMPTS = _env_int("IRAIL_MAX_ATTEMPTS", 3)

#: Minimum seconds between two calls from one client instance. iRail asks for
#: no more than 3 requests/second; 0.4 s keeps us at less than half of that
#: even when polling twelve hubs back to back.
IRAIL_MIN_INTERVAL_SECONDS = _env_float("IRAIL_MIN_INTERVAL_SECONDS", 0.4)

#: Ask for alert annotations on the liveboard. Currently stored only as a count
#: in the log, not shredded into tables (see README "Not built, and why").
IRAIL_REQUEST_ALERTS = _env("IRAIL_REQUEST_ALERTS", "1") == "1"

# ==========================================================================
# Ingestion
# ==========================================================================
#: Which hubs the timer polls, as a comma-separated list of station ids (or
#: names — the client passes either through). Empty means "the default hub set"
#: in hubs.py. Overriding this is how you scale the pipeline up or down without
#: a redeploy.
HUBS_SETTING = _env("RAILPULSE_HUBS")

#: All timestamps are stored in UTC *and* in local Belgian time, because
#: "which hour is busiest" is a question about the clock on the platform wall.
#: Held as a real tz database zone so that the two DST switches inside a year
#: of data are handled instead of being an off-by-one.
LOCAL_TIMEZONE_NAME = _env("RAILPULSE_TIMEZONE", "Europe/Brussels")
LOCAL_TIMEZONE = ZoneInfo(LOCAL_TIMEZONE_NAME)

#: Rows per executemany batch when staging a payload. A liveboard returns ~60
#: departures, so this is only ever reached by the 714-row station seed.
INSERT_BATCH_SIZE = _env_int("RAILPULSE_BATCH_SIZE", 500)

# ==========================================================================
# Introspection helpers — safe to log.
# ==========================================================================
@dataclass(frozen=True)
class SqlTarget:
    """The non-secret parts of a connection string, for logs and /api/health."""

    server: str
    database: str
    driver: str
    has_credentials: bool


def parse_sql_target(connection_string: str | None = None) -> SqlTarget:
    """Pull server/database/driver out of an ODBC string, dropping the secret.

    Written defensively rather than with a regex over the whole string: a
    malformed setting must produce a usable diagnostic ('unset', 'unparsed'),
    because the moment this function raises is the moment nobody can find out
    why the app will not start.
    """
    raw = connection_string if connection_string is not None else SQL_CONNECTION_STRING
    if not raw:
        return SqlTarget("unset", "unset", "unset", False)

    parts: dict[str, str] = {}
    for chunk in raw.split(";"):
        if "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        parts[key.strip().lower()] = value.strip()

    return SqlTarget(
        server=parts.get("server", "unparsed"),
        database=parts.get("database", parts.get("initial catalog", "unparsed")),
        driver=parts.get("driver", "unparsed").strip("{}"),
        has_credentials=bool(
            parts.get("pwd") or parts.get("password")
            or "activedirectory" in parts.get("authentication", "").lower()
        ),
    )


def describe_sql_target() -> str:
    """One-line, secret-free description of the SQL target for start-up logs."""
    target = parse_sql_target()
    credentials = "with credentials" if target.has_credentials else "NO CREDENTIALS"
    return (
        f"{target.database} on {target.server} "
        f"via [{target.driver}] ({credentials})"
    )


def require_sql_connection_string() -> str:
    """Return the connection string or fail with an actionable message."""
    if not SQL_CONNECTION_STRING:
        raise RuntimeError(
            "SQL_CONNECTION_STRING is not set. In Azure: Function App -> "
            "Settings -> Environment variables -> App settings. Locally: "
            "function_app/local.settings.json or a .env file. "
            "See .env.example for the expected ODBC format."
        )
    return SQL_CONNECTION_STRING
