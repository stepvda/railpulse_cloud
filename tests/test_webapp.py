"""Offline tests for the Streamlit dashboard.

These run without Azure, without a database and without Streamlit's runtime.
They cover the two things about `webapp/` that can be checked statically and that
would otherwise only fail in the cloud:

* the connection-string parser, which turns the project's single ODBC secret into
  pymssql credentials — and which must never lose or leak the password;
* the query catalogue's invariants: read-only, parameterised, and reading the
  views rather than the base tables.

The third thing worth checking is not code at all but ORDERING: the App Service
startup command must not reference a file until that file is deployed. Getting
that wrong cost a day of the F1 Free plan's CPU quota (docs/deployment_notes.md
§9), so there is a test for it here too.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WEBAPP_DIR = REPO_ROOT / "webapp"

# webapp/ is its own deployment root — in App Service these modules sit together
# at /home/site/wwwroot, imported as top-level names, so the tests import them
# the same way.
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

pytest.importorskip("pandas", reason="webapp extras not installed")

import queries  # noqa: E402
from data import parse_connection_string  # noqa: E402


# ==========================================================================
# The one secret, parsed rather than duplicated
# ==========================================================================
ODBC = (
    "Driver={ODBC Driver 18 for SQL Server};"
    "Server=tcp:sql-railpulse-abc123.database.windows.net,1433;"
    "Database=railpulse;Uid=railpulse_admin;Pwd=s3cr#t!Value;"
    "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=60;"
)


class TestConnectionStringParsing:
    def test_parses_the_odbc_form_the_rest_of_the_project_uses(self):
        """One secret, one format: the web app reads the SAME app setting the
        Function App does rather than introducing a second pair to rotate."""
        creds = parse_connection_string(ODBC)
        assert creds.server == "sql-railpulse-abc123.database.windows.net"
        assert creds.database == "railpulse"
        assert creds.user == "railpulse_admin"
        assert creds.password == "s3cr#t!Value"
        assert creds.port == 1433

    def test_strips_the_odbc_tcp_prefix_and_port(self):
        """`Server=tcp:host,1433` is ODBC spelling. pymssql wants host and port
        separately, and passing the raw value fails DNS resolution."""
        creds = parse_connection_string(ODBC)
        assert not creds.server.startswith("tcp:")
        assert "," not in creds.server

    def test_a_password_containing_odbc_punctuation_survives(self):
        """provision.sh generates passwords with symbols. A parser that split
        naively would corrupt them and the failure would look like a wrong
        password rather than a parsing bug."""
        creds = parse_connection_string(
            "Server=tcp:h,1433;Database=d;Uid=u;Pwd=a=b{c}d!e;Encrypt=yes;")
        assert creds.password == "a=b{c}d!e"

    def test_accepts_the_user_id_and_password_spellings(self):
        creds = parse_connection_string(
            "Server=tcp:h,1433;Database=d;User Id=u2;Password=p2;")
        assert (creds.user, creds.password) == ("u2", "p2")

    def test_the_safe_description_never_contains_the_password(self):
        """It is rendered in the sidebar of a public dashboard."""
        creds = parse_connection_string(ODBC)
        assert "s3cr#t!Value" not in creds.safe_description
        assert creds.database in creds.safe_description

    @pytest.mark.parametrize("raw, missing", [
        ("Server=tcp:h,1433;Database=d;Uid=u;", "Pwd"),
        ("Server=tcp:h,1433;Uid=u;Pwd=p;", "Database"),
        ("Database=d;Uid=u;Pwd=p;", "Server"),
    ])
    def test_names_what_is_missing_rather_than_failing_obscurely(self, raw, missing):
        with pytest.raises(RuntimeError, match=missing):
            parse_connection_string(raw)

    def test_an_empty_setting_says_where_to_set_it(self):
        with pytest.raises(RuntimeError, match="SQL_CONNECTION_STRING"):
            parse_connection_string("")


# ==========================================================================
# The query catalogue
# ==========================================================================
def statements() -> dict[str, str]:
    """Every module-level SQL constant in queries.py."""
    return {
        name: value for name, value in vars(queries).items()
        if name.isupper() and isinstance(value, str) and value.strip()
    }


class TestQueryCatalogue:
    def test_there_are_queries_to_check(self):
        """Guards the tests below from passing vacuously."""
        assert len(statements()) >= 18

    @pytest.mark.parametrize("name", sorted(statements()))
    def test_every_statement_is_read_only(self, name):
        """data.query refuses non-SELECT statements at run time; this catches it
        at commit time, and covers the whole catalogue rather than the paths a
        given page happens to exercise."""
        sql = statements()[name]
        assert re.match(r"^\s*(WITH|SELECT)\b", sql, re.IGNORECASE), \
            f"{name} does not start with SELECT/WITH"
        forbidden = ("INSERT ", "UPDATE ", "DELETE ", "MERGE ", "DROP ",
                     "ALTER ", "TRUNCATE ", "CREATE ", "EXEC ", "GRANT ")
        upper = sql.upper()
        for keyword in forbidden:
            assert keyword not in upper, f"{name} contains {keyword.strip()}"

    def test_no_statement_interpolates_a_value(self):
        """Values are bound, never formatted in. An f-string or a `%` format in a
        SQL constant is the shape of an injection, even when today's caller
        happens to pass an int."""
        source = (WEBAPP_DIR / "queries.py").read_text(encoding="utf-8")
        assert 'f"""' not in source and "f'''" not in source
        assert ".format(" not in source
        # `%s` is pymssql's parameter marker and is expected; `%d`/`%(name)s`
        # style interpolation is not used anywhere.
        assert not re.search(r"%\((\w+)\)s", source)

    #: The ONLY statements permitted to read a base table, and which ones. Every
    #: other statement must go through a view. Enumerated per query rather than
    #: as a global allowlist, because a global one would silently permit a future
    #: metric query to read `liveboard_records` and re-derive a definition — the
    #: exact drift this seam exists to prevent.
    BASE_TABLE_EXEMPTIONS = {
        # Provenance, not a measure: the run log has no view by design.
        "RECENT_RUNS": {"ingestion_runs"},
        "RUN_TOTALS_BY_TRIGGER": {"ingestion_runs"},
        # Inventory for the map: a dimension read, no metric derived.
        "STATION_MAP": {"stations"},
        # Row counts for the data-quality page — the one place the fact table
        # itself is legitimately named, and only to be counted.
        "TABLE_COUNTS": {"ingestion_runs", "liveboard_records", "platforms",
                         "stations", "vehicle_types", "vehicles"},
    }

    def test_the_dashboard_reads_views_not_base_tables(self):
        """The anti-drift seam. Every metric definition — on-time thresholds,
        whether cancellations are in the denominator, which local hour a
        departure falls in — lives in sql/03_views.sql, and the dashboard must
        not re-derive any of them.
        """
        for name, sql in statements().items():
            allowed = self.BASE_TABLE_EXEMPTIONS.get(name, set())
            base_tables = {t for t in re.findall(r"\bdbo\.(\w+)", sql)
                           if not t.startswith("v_")}
            unexpected = base_tables - allowed
            assert not unexpected, (
                f"{name} reads base table(s) {sorted(unexpected)} instead of a "
                "view. If that is deliberate, add it to BASE_TABLE_EXEMPTIONS "
                "with a reason — the point of this test is that going round the "
                "views is a decision, not an accident."
            )

    def test_the_exemption_list_has_no_stale_entries(self):
        """A named exemption that no longer applies would quietly widen the rule
        for a future query that reuses the name."""
        for name, tables in self.BASE_TABLE_EXEMPTIONS.items():
            assert name in statements(), f"{name} no longer exists in queries.py"
            actual = {t for t in re.findall(r"\bdbo\.(\w+)", statements()[name])
                      if not t.startswith("v_")}
            assert actual, f"{name} no longer reads any base table — drop its exemption"
            assert actual <= tables, f"{name} reads more than its exemption allows"

    def test_the_fact_table_is_only_ever_counted_never_aggregated(self):
        """`liveboard_records` is exempted for TABLE_COUNTS alone, and there only
        to be counted. An AVG or SUM over it would be a metric computed outside
        the views."""
        sql = statements()["TABLE_COUNTS"].upper()
        for aggregate in ("AVG(", "SUM(", "MIN(", "MAX(", "PERCENTILE"):
            assert aggregate not in sql, \
                f"TABLE_COUNTS uses {aggregate} — it must only COUNT(*)"

    def test_metric_definitions_are_not_re_derived_from_raw_delay_seconds(self):
        """`delay_seconds < 360` in the dashboard would be a second definition of
        "on time" that could drift from the views'. The KPI header aggregates the
        view's own flags instead."""
        for name, sql in statements().items():
            assert not re.search(r"delay_seconds\s*<\s*\d", sql), (
                f"{name} re-derives an on-time threshold; use is_on_time_*min "
                "from the views")

    def test_bit_columns_are_never_summed_directly(self):
        """T-SQL cannot SUM a BIT — the same trap the warehouse views have a test
        for, repeated here because these statements are equally exposed to it."""
        for name, sql in statements().items():
            for match in re.finditer(r"SUM\(([^()]*)\)", sql):
                inner = match.group(1)
                assert not re.match(r"^\s*\w*\.?(is_|has_)\w+\s*$", inner), \
                    f"{name}: SUM({inner}) sums a BIT column directly"


# ==========================================================================
# The deployment ordering that cost a day of Free-tier quota
# ==========================================================================
class TestStartupOrdering:
    """See docs/deployment_notes.md §9.

    provision_webapp.sh must NOT point the startup command at a file that only
    exists after a deploy. On App Service the container then exits 127 in a
    restart loop, App Service stops the site (taking Kudu with it, so it cannot
    be redeployed), and on the F1 Free plan the loop exhausts the 60 CPU-minute
    daily quota — after which the app answers 403 to everything until UTC
    midnight and no CLI command can clear it.
    """

    def provision(self) -> str:
        return (REPO_ROOT / "azure" / "provision_webapp.sh").read_text(encoding="utf-8")

    def deploy(self) -> str:
        return (REPO_ROOT / "azure" / "deploy_webapp.sh").read_text(encoding="utf-8")

    def test_provision_does_not_reference_the_undeployed_startup_script(self):
        # Only in comments explaining why not — never as a --startup-file value.
        for line in self.provision().splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "--startup-file" not in stripped or "wwwroot/startup.sh" not in stripped, \
                "provision_webapp.sh points the startup command at a file that " \
                "does not exist until deploy_webapp.sh runs"

    def test_provision_sets_a_placeholder_that_cannot_fail(self):
        assert "--startup-file" in self.provision()
        assert "http.server" in self.provision()

    def test_the_placeholder_does_not_serve_the_source_tree(self):
        """`python3 -m http.server` in /home/site/wwwroot would publish app.py,
        data.py and queries.py to anyone who found the URL during the window
        between provision and deploy."""
        provision = self.provision()
        assert "/tmp/ph" in provision or "mkdir -p /tmp" in provision
        assert "cd /home/site/wwwroot && exec python3 -m http.server" not in provision

    def test_deploy_installs_the_real_startup_command_after_publishing(self):
        deploy = self.deploy()
        assert "wwwroot/startup.sh" in deploy
        # …and does so after the build, not before the package is on disk.
        assert deploy.index("Waiting for the build") < deploy.index(
            'startup-file "bash /home/site/wwwroot/startup.sh"')

    def test_deploy_reports_quota_exhaustion_explicitly(self):
        assert "QuotaExceeded" in self.deploy()

    def test_startup_script_resolves_the_interpreter_and_the_virtualenv(self):
        """The App Service image ships `python3`, not `python`, and a custom
        startup command does not inherit Oryx's activated antenv — either alone
        yields exit 127."""
        startup = (WEBAPP_DIR / "startup.sh").read_text(encoding="utf-8")
        assert "command -v python3" in startup
        assert "antenv/bin/activate" in startup
        assert "exec " in startup, "the server must replace the shell, not be its child"
