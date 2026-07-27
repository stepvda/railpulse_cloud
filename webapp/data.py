"""Azure SQL access for the Streamlit app. Read-only by construction.

WHY pymssql AND NOT pyodbc
The Function App uses pyodbc, because the Azure Functions Python runtime image
ships the Microsoft ODBC driver. The **App Service** Python image does not, and
installing it on every cold start would be slow and fragile. `pymssql` is a pip
wheel with FreeTDS statically bundled, so it needs no system driver at all — the
whole dependency is `pip install pymssql`.

That is a deliberate split, not an inconsistency: each side uses the client that
its runtime already supports. The cost is that the two use different parameter
markers (`?` for pyodbc, `%s` for pymssql), which is why no SQL string is shared
between them. The *definitions* are shared — both go through the views in
sql/03_views.sql — and that is the part that matters.

WHY THE APP CANNOT WRITE
Every statement issued here is a SELECT, the connection is never given a
transaction to commit, and :func:`query` refuses anything that does not begin
with SELECT or WITH. The dashboard is a renderer; the only thing on any page that
changes state is the "run ingest" button, and that goes through the Function
App's own key-protected HTTP endpoint, not through this connection.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

import pandas as pd
import pymssql
import streamlit as st

#: Serverless Azure SQL auto-pauses after an hour idle. The first query after a
#: quiet spell has to wait for a cold resume, which routinely takes 30-60 s — so
#: the login timeout is generous and transient failures are retried rather than
#: shown to the visitor as an error.
LOGIN_TIMEOUT_SECONDS = int(os.environ.get("SQL_LOGIN_TIMEOUT", "90"))
QUERY_TIMEOUT_SECONDS = int(os.environ.get("SQL_QUERY_TIMEOUT", "120"))
MAX_ATTEMPTS = int(os.environ.get("SQL_MAX_ATTEMPTS", "4"))

#: How long a result is reused before the database is asked again. 60 s keeps the
#: page responsive while staying honest about "live": the timer writes at most
#: every 15 minutes, so a minute of staleness is invisible, and it stops a
#: reloading browser tab from waking a paused database over and over.
DEFAULT_TTL_SECONDS = 60

_SELECT_ONLY = re.compile(r"^\s*(?:WITH|SELECT)\b", re.IGNORECASE)


@dataclass(frozen=True)
class SqlCredentials:
    server: str
    database: str
    user: str
    password: str
    port: int = 1433

    @property
    def safe_description(self) -> str:
        """Printable form with no secret in it — safe for the sidebar."""
        return f"{self.database} on {self.server}"


def parse_connection_string(raw: str) -> SqlCredentials:
    """Pull pymssql credentials out of the ODBC string the rest of the project uses.

    One secret, one format. `provision.sh` writes a single
    `SQL_CONNECTION_STRING` in ODBC form for the Function App; rather than
    introduce a second set of app settings (and a second thing to rotate), this
    parses that same string. `Server=tcp:host,1433` is the ODBC spelling, hence
    the stripping of the `tcp:` prefix and the trailing port.
    """
    if not raw:
        raise RuntimeError(
            "SQL_CONNECTION_STRING is not set. In Azure: App Service -> Settings "
            "-> Environment variables. Locally: export it, or "
            "`set -a && source .azure-railpulse.env && set +a`."
        )

    parts: dict[str, str] = {}
    for chunk in raw.split(";"):
        if "=" in chunk:
            key, _, value = chunk.partition("=")
            parts[key.strip().lower()] = value.strip()

    server = parts.get("server", "")
    server = server[4:] if server.lower().startswith("tcp:") else server
    port = 1433
    if "," in server:
        server, _, port_text = server.partition(",")
        port = int(port_text) if port_text.isdigit() else 1433

    missing = [
        name for name, value in (
            ("Server", server),
            ("Database", parts.get("database")),
            ("Uid", parts.get("uid") or parts.get("user id")),
            ("Pwd", parts.get("pwd") or parts.get("password")),
        ) if not value
    ]
    if missing:
        raise RuntimeError(
            f"SQL_CONNECTION_STRING is missing: {', '.join(missing)}. "
            "Expected the ODBC form: "
            "Driver={...};Server=tcp:host,1433;Database=db;Uid=user;Pwd=secret;..."
        )

    return SqlCredentials(
        server=server,
        database=parts["database"],
        user=parts.get("uid") or parts["user id"],
        password=parts.get("pwd") or parts["password"],
        port=port,
    )


def credentials() -> SqlCredentials:
    return parse_connection_string(os.environ.get("SQL_CONNECTION_STRING", "").strip())


class Database:
    """A lazily-opened connection that reopens itself when it goes stale.

    Streamlit keeps this object alive across reruns via ``@st.cache_resource``,
    which is what avoids paying a login on every widget interaction. But a
    connection held across a serverless auto-pause is dead, and every subsequent
    query on it fails identically — so a failed query drops the handle and the
    next attempt gets a fresh one.
    """

    def __init__(self, creds: SqlCredentials) -> None:
        self._creds = creds
        self._connection: pymssql.Connection | None = None

    def _connect(self) -> pymssql.Connection:
        if self._connection is None:
            self._connection = pymssql.connect(
                server=self._creds.server,
                user=self._creds.user,
                password=self._creds.password,
                database=self._creds.database,
                port=str(self._creds.port),
                login_timeout=LOGIN_TIMEOUT_SECONDS,
                timeout=QUERY_TIMEOUT_SECONDS,
            )
        return self._connection

    def _drop(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:  # pragma: no cover - already broken
                pass
            self._connection = None

    def query(self, sql: str, params: tuple = ()) -> pd.DataFrame:
        """Run a read-only statement and wrap the rows for rendering.

        The DataFrame is built straight from ``cursor.description`` and the
        fetched rows. Nothing is derived, filtered, joined or aggregated here —
        every number on every page is computed by SQL Server, exactly as in
        sprint 1. pandas is a carrier from the driver to Altair and nothing else.
        """
        if not _SELECT_ONLY.match(sql):
            # A hard stop rather than a convention: this app is a renderer, and
            # the connection it holds has write permission on the database.
            raise ValueError("only SELECT/WITH statements may be run from the app")

        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                cursor = self._connect().cursor()
                try:
                    cursor.execute(sql, params) if params else cursor.execute(sql)
                    columns = [c[0] for c in cursor.description or ()]
                    rows = [tuple(r) for r in cursor.fetchall()]
                finally:
                    cursor.close()
                return pd.DataFrame(rows, columns=columns)
            except Exception as exc:  # noqa: BLE001 - retried or re-raised below
                last_error = exc
                self._drop()
                if attempt == MAX_ATTEMPTS:
                    raise
                # Exponential backoff sized for a serverless resume.
                time.sleep(2.0 * attempt)
        raise RuntimeError("unreachable") from last_error


@st.cache_resource(show_spinner=False)
def get_database() -> Database:
    return Database(credentials())


@st.cache_data(ttl=DEFAULT_TTL_SECONDS, show_spinner="Querying Azure SQL…")
def run_sql(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Cached query. The cache key is (sql, params), so filters work naturally."""
    return get_database().query(sql, params)


def scalar(frame: pd.DataFrame, column: str, default=None):
    """First value of *column*, or *default* when the frame is empty.

    Exists because an empty warehouse is a normal state — before the first
    ingest, every KPI query returns no rows, and a dashboard that raises on that
    is a dashboard nobody can use to find out why it is empty.
    """
    if frame.empty or column not in frame.columns:
        return default
    value = frame.iloc[0][column]
    return default if pd.isna(value) else value
