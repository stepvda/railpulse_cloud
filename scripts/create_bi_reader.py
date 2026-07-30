#!/usr/bin/env python3
"""Create a least-privilege SQL login for Power BI.

WHY NOT JUST USE THE ADMIN LOGIN
`provision.sh` creates one credential: the SQL **server** administrator. It can
read, write, drop and re-create anything on every database on that server. Typing
it into a BI tool means:

  * the credential is cached in a desktop client, and in the Power BI Service's
    stored data-source credentials, where it is one shared workspace away from
    someone else;
  * a mis-click in a query editor can write to the warehouse;
  * it cannot be revoked without breaking the whole pipeline, because the
    Function App uses it too.

So this creates `powerbi_reader`: SELECT on the **eight BI views and two
dimensions only**, and nothing else. Not `db_datareader` — that would grant read
on every base table, including the fact table.

THAT RESTRICTION IS THE PROJECT'S DESIGN, ENFORCED
`docs/webapp.md` argues that the views are the BI contract: they are where "on
time", "is a cancellation in the denominator" and "which local hour" are defined,
so a consumer that reads base tables can silently disagree with the warehouse.
Granting SELECT on views alone turns that argument into a permission. Power BI
*cannot* read `liveboard_records` even if someone writes a query that tries.

Ownership chaining is what makes it work: the views and the tables share an owner
(`dbo`), so SELECT on the view is sufficient and no permission on the underlying
tables is needed or given.

    python scripts/create_bi_reader.py                 # create or update
    python scripts/create_bi_reader.py --rotate        # new password
    python scripts/create_bi_reader.py --show          # print what it can read

Requires pymssql (`pip install pymssql`) — a pip wheel with TDS bundled, so no
system ODBC driver is needed. Same client the web app uses.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import string
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SECRET_FILE = Path(os.environ.get("SECRET_FILE", REPO_ROOT / ".azure-railpulse.env"))

BI_LOGIN = "powerbi_reader"

#: Exactly what Power BI may read. Views first, then the two BI dimensions.
#: Adding to this list is a deliberate act — which is the point.
READABLE_OBJECTS = (
    "dbo.v_bi_departures",
    "dbo.v_departures",
    "dbo.v_station_punctuality",
    "dbo.v_hourly_pressure",
    "dbo.v_platform_pressure",
    "dbo.v_delay_distribution",
    "dbo.v_vehicle_type_performance",
    "dbo.v_ingestion_health",
    "dbo.v_data_quality",
    "dbo.dim_date",
    "dbo.dim_hour",
)


def load_env() -> dict[str, str]:
    """Read .azure-railpulse.env, honouring the single-quoted values."""
    if not SECRET_FILE.is_file():
        raise SystemExit(f"no {SECRET_FILE} — run ./azure/provision.sh first")
    values: dict[str, str] = {}
    for line in SECRET_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def generate_password() -> str:
    """A password Azure SQL will accept: length, and three character classes.

    `secrets`, not `random`: this is a credential. The trailing fixed characters
    guarantee the complexity policy is met without rejecting-and-retrying, and
    the ODBC-hostile characters (`;` `{` `}`) are excluded so the value can be
    pasted into a connection string without quoting surprises.
    """
    alphabet = string.ascii_letters + string.digits + "-_.~!*"
    return "".join(secrets.choice(alphabet) for _ in range(28)) + "Aa9!"


def persist(env: dict[str, str], password: str) -> None:
    """Record the credential in the gitignored env file, single-quoted."""
    text = SECRET_FILE.read_text(encoding="utf-8")
    entries = {
        "BI_READER_LOGIN": BI_LOGIN,
        "BI_READER_PASSWORD": password,
    }
    for key, value in entries.items():
        quoted = f"{key}='{value}'"
        if re.search(rf"^{key}=", text, re.MULTILINE):
            text = re.sub(rf"^{key}=.*$", quoted, text, flags=re.MULTILINE)
        else:
            text = text.rstrip("\n") + f"\n{quoted}\n"
    SECRET_FILE.write_text(text, encoding="utf-8")
    SECRET_FILE.chmod(0o600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--rotate", action="store_true",
                        help="set a new password for an existing login")
    parser.add_argument("--show", action="store_true",
                        help="only report what the login can currently read")
    args = parser.parse_args(argv)

    try:
        import pymssql
    except ImportError:
        raise SystemExit("pymssql is required: pip install pymssql")

    env = load_env()
    server = env["SQL_FQDN"]
    admin, admin_pw = env["SQL_ADMIN_USER"], env["SQL_ADMIN_PASSWORD"]
    database = env["SQL_DATABASE"]

    def connect(db: str):
        return pymssql.connect(server=server, user=admin, password=admin_pw,
                               database=db, timeout=180, login_timeout=180,
                               autocommit=True)

    if args.show:
        with connect(database) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT o.name, p.permission_name
                FROM sys.database_permissions AS p
                JOIN sys.objects AS o ON o.object_id = p.major_id
                JOIN sys.database_principals AS u ON u.principal_id = p.grantee_principal_id
                WHERE u.name = %s
                ORDER BY o.name
            """, (BI_LOGIN,))
            rows = cur.fetchall()
            if not rows:
                print(f"  {BI_LOGIN} has no object permissions (or does not exist)")
            for name, perm in rows:
                print(f"  {perm:<8} {name}")
        return 0

    password = env.get("BI_READER_PASSWORD") or ""
    if args.rotate or not password:
        password = generate_password()

    # ---- server-level login lives in master -----------------------------
    with connect("master") as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM sys.sql_logins WHERE name = %s", (BI_LOGIN,))
        exists = cur.fetchone()[0] > 0
        if exists and (args.rotate or not env.get("BI_READER_PASSWORD")):
            cur.execute(f"ALTER LOGIN [{BI_LOGIN}] WITH PASSWORD = %s", (password,))
            print(f"  login {BI_LOGIN}: password set")
        elif not exists:
            cur.execute(f"CREATE LOGIN [{BI_LOGIN}] WITH PASSWORD = %s", (password,))
            print(f"  login {BI_LOGIN}: created")
        else:
            print(f"  login {BI_LOGIN}: already present, password unchanged")

    # ---- database user + the only grants it gets -------------------------
    with connect(database) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM sys.database_principals WHERE name = %s",
                    (BI_LOGIN,))
        if cur.fetchone()[0] == 0:
            cur.execute(f"CREATE USER [{BI_LOGIN}] FROM LOGIN [{BI_LOGIN}]")
            print(f"  user  {BI_LOGIN}: created in {database}")
        else:
            print(f"  user  {BI_LOGIN}: already present in {database}")

        # DENY on the fact table as well as withholding the grant. Belt and
        # braces: a future `ALTER ROLE db_datareader ADD MEMBER` — the obvious
        # "just make it work" fix someone reaches for — would otherwise silently
        # hand over every base table. An explicit DENY outranks a role grant.
        for table in ("liveboard_records", "stations", "platforms", "vehicles",
                      "vehicle_types", "ingestion_runs"):
            cur.execute(f"DENY SELECT ON dbo.{table} TO [{BI_LOGIN}]")
        print(f"  denied direct SELECT on 6 base tables (DENY outranks any role)")

        for obj in READABLE_OBJECTS:
            cur.execute(f"GRANT SELECT ON {obj} TO [{BI_LOGIN}]")
        print(f"  granted SELECT on {len(READABLE_OBJECTS)} views/dimensions")

    persist(env, password)
    print(f"  credentials written to {SECRET_FILE.name} (mode 600, gitignored)")
    print()
    print("  Power BI connection details:")
    print(f"    Server    {server}")
    print(f"    Database  {database}")
    print(f"    User      {BI_LOGIN}")
    print("    Password  see BI_READER_PASSWORD in .azure-railpulse.env")
    print("    Mode      IMPORT, not DirectQuery — see docs/powerbi.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
