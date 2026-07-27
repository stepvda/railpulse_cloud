"""Applying the .sql files to the database.

WHY THE FUNCTION APP CAN MIGRATE ITSELF
Applying DDL to Azure SQL from a laptop needs the Microsoft ODBC driver
installed locally *and* a firewall rule for whatever IP the laptop has today.
Neither is hard, but both are a detour, and the second silently breaks every
time you change network. The Function App, meanwhile, already has a working
driver in its runtime image and is already allowed through the firewall by
"Allow Azure services and resources to access this server".

So the deployed app can apply its own schema, via a key-protected
``POST /api/migrate``. The same files can equally be run from
scripts/apply_schema.py, from the portal's Query editor, or from VS Code — this
is one route, not the only route, and the SQL is plain files either way.

IDEMPOTENT BY CONSTRUCTION
Every file guards its own objects (``IF OBJECT_ID(...) IS NULL``,
``CREATE OR ALTER VIEW``, ``MERGE`` for reference data), so re-running the whole
set against a live database is a no-op and a safe way to bring a stale
deployment up to date. What this deliberately does NOT do is destructive
migration: dropping or retyping a column that already holds data is a decision
for a human with a backup, not for an HTTP endpoint.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import pyodbc

from .database import apply_script

logger = logging.getLogger(__name__)

#: Files applied by :func:`apply_all`, in order. Named explicitly rather than
#: globbed so that adding a file to sql/ is a conscious act and the order can
#: never depend on how the filesystem sorts.
MIGRATION_FILES: tuple[str, ...] = (
    "01_schema.sql",
    "02_indexes.sql",
    "03_views.sql",
    "04_seed_reference.sql",
)


@dataclass
class MigrationResult:
    file_name: str
    batches: int
    skipped: bool = False
    reason: str | None = None


def sql_directory() -> Path:
    """Locate sql/ in both layouts this code runs in.

    Deployed, the package sits at /home/site/wwwroot/railpulse and the SQL at
    /home/site/wwwroot/sql (azure/deploy.sh copies it into the zip). Locally,
    the package is function_app/railpulse and the SQL is at the repository root.
    Both are checked, plus an explicit override for anything unforeseen.
    """
    override = os.environ.get("RAILPULSE_SQL_DIR", "").strip()
    if override:
        return Path(override)

    here = Path(__file__).resolve()
    candidates = (
        here.parent / "sql",        # railpulse/sql (if ever vendored inside)
        here.parents[1] / "sql",    # function_app/sql  <- the deployed layout
        here.parents[2] / "sql",    # repository root   <- the local layout
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    # Return the deployed-layout path so the error message names something real.
    return here.parents[1] / "sql"


def apply_all(cursor: pyodbc.Cursor) -> list[MigrationResult]:
    """Apply every migration file in order. Caller owns the transaction.

    A missing file is reported as skipped rather than raised: a deployment that
    somehow shipped without 04_seed_reference.sql should still get its tables,
    and the response body should say exactly what was and was not applied.
    """
    directory = sql_directory()
    results: list[MigrationResult] = []

    for file_name in MIGRATION_FILES:
        path = directory / file_name
        if not path.is_file():
            logger.warning("migration file missing: %s", path)
            results.append(MigrationResult(file_name, 0, skipped=True,
                                           reason=f"not found at {path}"))
            continue
        script = path.read_text(encoding="utf-8")
        batches = apply_script(cursor, script, label=file_name)
        logger.info("applied %s (%d batches)", file_name, batches)
        results.append(MigrationResult(file_name, batches))

    return results
