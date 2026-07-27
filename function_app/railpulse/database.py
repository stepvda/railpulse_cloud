"""Connections, retries and batch execution against Azure SQL.

THE PROBLEM THIS MODULE EXISTS TO SOLVE
A serverless Azure SQL database with a one-hour auto-pause is *not* a normally
available database, and code written as if it were will fail every morning.
When the first timer run of the day connects:

  * the database is PAUSED. The connection attempt does not queue — it fails,
    typically with error 40613 ("Database ... is not currently available"),
    while the platform starts a resume that takes roughly 30-60 seconds;
  * for a few seconds after resuming it may throttle (40501, 10928/10929);
  * mid-statement, a platform reconfiguration can drop the connection (40197,
    40143, 4060).

All of those are *transient*: the correct response is a fresh connection and a
retry with backoff, not an alert. None of them are bugs, and none of them should
lose a poll. That is what :func:`connect_with_retry` and :func:`run_with_retry`
implement, and it is the single most important piece of cloud-specific code in
this project.

Distinguishing transient from permanent is the whole trick. A wrong password
(18456) or a missing table (208) is not transient, and retrying it five times
with backoff only turns a clear failure into a slow one.
"""

from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Sequence, TypeVar

import pyodbc

from . import config

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ==========================================================================
# Transient-fault classification
# ==========================================================================
#: SQL Server / Azure SQL error numbers that mean "try again", from Microsoft's
#: published transient-fault list plus the two that matter most for serverless.
TRANSIENT_SQL_ERROR_NUMBERS = frozenset({
    4060,   # cannot open database (may be mid-resume)
    40197,  # service error processing the request; reconnect
    40501,  # service is busy (engine throttling)
    40613,  # database not currently available — THE serverless resume error
    40143,  # connection-processing error
    49918,  # cannot process request: not enough resources
    49919,  # cannot process create/update request (too many operations)
    49920,  # cannot process request: too many operations
    4221,   # login to read-secondary failed (replica lag)
    1205,   # deadlock victim — a legitimate retry, see MERGE + HOLDLOCK below
    233,    # connection forcibly closed by the transport layer
    64,     # connection was broken during login
    10053,  # transport-level error: connection aborted
    10054,  # transport-level error: connection reset by peer
    10060,  # network-related or instance-specific error (timeout on connect)
    10928,  # resource id limit reached
    10929,  # resource limit: min guarantee not met
    20,     # instance does not support encryption / handshake failure
    0,      # driver-level timeouts surface with no SQL number
})

#: ODBC SQLSTATEs for connection-level failures. Checked as well as the error
#: number because a connect timeout during a serverless resume often arrives as
#: HYT00/08001 with error number 0.
TRANSIENT_SQLSTATES = frozenset({
    "08001",  # client unable to establish connection
    "08002",  # connection name in use
    "08003",  # connection does not exist
    "08004",  # server rejected the connection
    "08006",  # connection failure
    "08S01",  # communication link failure
    "HYT00",  # timeout expired
    "HYT01",  # connection timeout expired
    "40001",  # serialization failure / deadlock
})

_ERROR_NUMBER_RE = re.compile(r"\((\d+)\)")


def is_transient(exc: BaseException) -> bool:
    """True when *exc* is worth retrying with a fresh connection.

    pyodbc exposes the SQLSTATE as ``args[0]`` and puts the driver message in
    ``args[1]``; the SQL Server error number appears inside that message as
    ``(40613)``. Both are inspected because either alone misses cases: number 0
    with SQLSTATE HYT00 is a connect timeout during a resume, while error 1205
    (deadlock) arrives with a SQLSTATE that is not in the list above.
    """
    if not isinstance(exc, pyodbc.Error):
        return False

    args = getattr(exc, "args", ())
    sqlstate = str(args[0]).strip() if args else ""
    message = str(args[1]) if len(args) > 1 else str(exc)

    if sqlstate in TRANSIENT_SQLSTATES:
        return True
    for candidate in _ERROR_NUMBER_RE.findall(message):
        if int(candidate) in TRANSIENT_SQL_ERROR_NUMBERS:
            return True
    # A paused database sometimes reports itself in words before it reports a
    # number, and the words are stable across driver versions.
    lowered = message.lower()
    return any(
        needle in lowered
        for needle in (
            "is not currently available",
            "database is unavailable",
            "resuming",
            "login timeout expired",
            "timeout expired",
            "server is not found or was not accessible",
        )
    )


# ==========================================================================
# Connecting
# ==========================================================================
def connect(connection_string: str | None = None) -> pyodbc.Connection:
    """Open one connection. Explicit transactions (autocommit off) by design.

    Autocommit is off so that a station's dimension upserts and its fact MERGE
    either all land or none do; a half-loaded liveboard with vehicles but no
    departures would be worse than no load at all.
    """
    raw = connection_string or config.require_sql_connection_string()
    connection = pyodbc.connect(
        raw, autocommit=False, timeout=config.SQL_LOGIN_TIMEOUT
    )
    connection.timeout = config.SQL_QUERY_TIMEOUT
    return connection


def connect_with_retry(connection_string: str | None = None) -> pyodbc.Connection:
    """Open a connection, waiting out a serverless resume if necessary.

    The retry budget (5 attempts, 2s base, exponential) is sized for the
    observed worst case: a cold auto-paused database answering in ~60 s. Total
    patience is therefore about 2+4+8+16 = 30 s of sleeping on top of up to five
    60-second login timeouts, which fits inside the 9-minute function timeout
    configured in host.json.
    """
    attempts = max(config.SQL_MAX_ATTEMPTS, 1)
    last_error: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            connection = connect(connection_string)
            if attempt > 1:
                logger.info("SQL connection established on attempt %d/%d "
                            "(database was most likely resuming)",
                            attempt, attempts)
            return connection
        except Exception as exc:  # noqa: BLE001 - classified immediately below
            last_error = exc
            if not is_transient(exc) or attempt == attempts:
                raise
            backoff = config.SQL_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "SQL connect attempt %d/%d failed transiently (%s); "
                "retrying in %.1fs", attempt, attempts, _short(exc), backoff)
            time.sleep(backoff)

    raise RuntimeError("unreachable") from last_error


def run_with_retry(operation: Callable[[pyodbc.Connection], T]) -> T:
    """Run *operation* with a fresh connection, retrying transient failures.

    A new connection per attempt on purpose: after 40613 or 40197 the existing
    handle is dead, and reusing it would fail identically forever. The operation
    must therefore be safe to run twice — which every operation in loader.py is,
    because they are all MERGEs on a natural key.
    """
    attempts = max(config.SQL_MAX_ATTEMPTS, 1)
    last_error: BaseException | None = None

    for attempt in range(1, attempts + 1):
        connection = None
        try:
            connection = connect_with_retry()
            result = operation(connection)
            return result
        except Exception as exc:  # noqa: BLE001 - classified immediately below
            last_error = exc
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:  # pragma: no cover - already failing
                    pass
            if not is_transient(exc) or attempt == attempts:
                raise
            backoff = config.SQL_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
            logger.warning("SQL operation attempt %d/%d failed transiently (%s); "
                           "retrying in %.1fs",
                           attempt, attempts, _short(exc), backoff)
            time.sleep(backoff)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:  # pragma: no cover
                    pass

    raise RuntimeError("unreachable") from last_error


def _short(exc: BaseException) -> str:
    """One-line rendering of a pyodbc error, for logs."""
    return " ".join(str(exc).split())[:240]


# ==========================================================================
# Executing
# ==========================================================================
@contextmanager
def transaction(connection: pyodbc.Connection) -> Iterator[pyodbc.Cursor]:
    """Yield a cursor, committing on success and rolling back on any exception."""
    cursor = connection.cursor()
    try:
        yield cursor
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


#: SQL Server accepts at most 2 100 parameters in one statement. Room is left
#: for the handful a surrounding batch might add.
MAX_PARAMETERS_PER_STATEMENT = 2000


def insert_rows(
    cursor: pyodbc.Cursor,
    table: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
) -> int:
    """Insert rows using multi-row VALUES, chunked to the parameter limit.

    WHY NOT ``fast_executemany``
    The obvious choice for bulk insert has a trap that bites precisely this
    workload. fast_executemany binds the whole parameter array with ONE inferred
    type per column, taken from the first row — so a nullable text column that
    happens to be None in row 1 gets bound as a 1-character type, and row 7's
    real value fails with "String data, right truncation". Half the columns
    staged here are legitimately NULL in the first departure of a payload
    (platform not yet allocated, no occupancy report), which makes that failure
    mode likely rather than theoretical. It is fixable with setinputsizes(), but
    that means hand-maintaining an ODBC type list per table in parallel with the
    DDL — a second source of truth that will drift.

    A multi-row ``VALUES`` clause avoids the whole problem: every parameter is
    bound individually with its own inferred type, so NULLs cost nothing, while
    still collapsing 60 inserts into a single round trip. At this scale (60 rows
    per poll, 714 on a station seed) it is as fast as the array bind and cannot
    silently truncate.
    """
    if not rows:
        return 0

    column_list = ", ".join(columns)
    placeholder = f"({', '.join('?' * len(columns))})"
    chunk_size = max(MAX_PARAMETERS_PER_STATEMENT // max(len(columns), 1), 1)

    written = 0
    for start in range(0, len(rows), chunk_size):
        chunk = rows[start:start + chunk_size]
        statement = (
            f"INSERT INTO {table} ({column_list}) VALUES "
            + ", ".join([placeholder] * len(chunk))
        )
        flattened = [value for row in chunk for value in row]
        cursor.execute(statement, flattened)
        written += len(chunk)
    return written


def first_result_set(cursor: pyodbc.Cursor) -> list[Any]:
    """Skip row-count-only results and return the first real result set.

    A batch of the form ``MERGE ... OUTPUT $action INTO @t; SELECT ...`` produces
    a row count before the SELECT unless ``SET NOCOUNT ON`` is in effect. Rather
    than depending on that, this walks forward until it finds a set with a
    description (i.e. actual columns).
    """
    while cursor.description is None:
        if not cursor.nextset():
            return []
    return cursor.fetchall()


def fetch_dicts(cursor: pyodbc.Cursor, statement: str,
                params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    """Run a SELECT and return rows as dicts, for JSON responses."""
    if params:
        cursor.execute(statement, list(params))
    else:
        cursor.execute(statement)
    if cursor.description is None:
        return []
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


# ==========================================================================
# DDL / migrations
# ==========================================================================
_GO_SEPARATOR = re.compile(r"^\s*GO\s*(?:--.*)?$", re.IGNORECASE | re.MULTILINE)


def split_batches(script: str) -> list[str]:
    """Split a T-SQL script on its ``GO`` batch separators.

    ``GO`` is not T-SQL — it is an instruction to the *client* to send what it
    has so far as one batch. Drivers do not understand it, so a script that uses
    it (as ours must: ``CREATE OR ALTER VIEW`` has to be the first statement in
    its batch) has to be split here. The regex matches only a line that is
    nothing but GO, so the word inside a comment or a string is left alone.
    """
    return [
        batch.strip()
        for batch in _GO_SEPARATOR.split(script)
        if batch.strip() and not batch.strip().startswith("--")
    ]


def apply_script(cursor: pyodbc.Cursor, script: str, *, label: str = "script") -> int:
    """Execute every batch of a T-SQL script. Returns the batch count."""
    batches = split_batches(script)
    for index, batch in enumerate(batches, start=1):
        try:
            cursor.execute(batch)
            # DDL batches can return result sets (ours do not, but a future
            # migration might); draining them keeps the cursor usable.
            while cursor.nextset():
                pass
        except pyodbc.Error as exc:
            raise RuntimeError(
                f"{label}: batch {index}/{len(batches)} failed: {_short(exc)}\n"
                f"--- batch starts ---\n{batch[:400]}"
            ) from exc
    return len(batches)
