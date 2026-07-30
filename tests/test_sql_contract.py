"""Static consistency checks between the .sql files and the Python that uses them.

WHY THIS FILE IS WORTH ITS WEIGHT
The riskiest edit in this project is renaming a column. The DDL lives in
sql/01_schema.sql, the INSERT and UPDATE lists live in Python strings in
loader.py, and nothing connects them — a rename that misses one side fails at
run time, in the cloud, inside a MERGE, with an error that names a column and
not a file. Verifying it locally needs a SQL Server.

So these tests parse both sides and compare them. They cannot prove the SQL is
*correct* (only a real database can), but they catch every drift between the
schema and the loader, which is the failure that actually happens — and they run
in milliseconds with no Azure subscription and no ODBC driver.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from railpulse import database, loader, migrations, reporting

# ==========================================================================
# Minimal T-SQL parsing. Deliberately narrow: it understands this project's
# formatting conventions and nothing else, and every convention it relies on
# (lowercase column names, uppercase keywords, a closing `);` on its own line)
# is checked by the tests below rather than assumed.
# ==========================================================================
CREATE_TABLE_RE = re.compile(
    r"CREATE TABLE (?:dbo\.)?(#?\w+)\s*\((.*?)\n\s*\)\s*;", re.DOTALL | re.IGNORECASE
)
NON_COLUMN_PREFIXES = (
    "CONSTRAINT", "PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "INDEX",
)
COLUMN_NAME_RE = re.compile(r"^([a-z][a-z0-9_]*)\s+\S")

REPO_ROOT = Path(__file__).resolve().parents[1]

BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
LINE_COMMENT_RE = re.compile(r"--[^\n]*")


def strip_comments(script: str) -> str:
    """Remove /* */ and -- comments before parsing.

    Not cosmetic. The DDL in this project is heavily commented, and a prose line
    inside a block comment ("a name loosely, which is exactly ...") matches the
    shape of a column definition — so without this the parser invents columns
    called `a` and `when`, and every "does this column exist" assertion below
    becomes weaker than it looks while still passing.
    """
    return LINE_COMMENT_RE.sub("", BLOCK_COMMENT_RE.sub("", script))


def table_columns(script: str) -> dict[str, set[str]]:
    """Extract {table_name: {column, ...}} from CREATE TABLE statements."""
    tables: dict[str, set[str]] = {}
    for name, body in CREATE_TABLE_RE.findall(strip_comments(script)):
        columns: set[str] = set()
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped or stripped.upper().startswith(NON_COLUMN_PREFIXES):
                continue
            match = COLUMN_NAME_RE.match(stripped)
            if match:
                columns.add(match.group(1))
        tables[name.lower()] = columns
    return tables


def referenced(alias: str, statement: str) -> set[str]:
    """Every column referenced through an alias, e.g. `t.delay_seconds`."""
    return set(re.findall(rf"\b{alias}\.(\w+)", statement))


def insert_column_list(statement: str) -> set[str]:
    """The column list of the INSERT clause of a MERGE."""
    match = re.search(r"INSERT\s*\(([^)]*)\)", statement, re.IGNORECASE)
    assert match, "MERGE statement has no INSERT column list"
    return {token.strip() for token in match.group(1).split(",") if token.strip()}


@pytest.fixture(scope="module")
def sql_dir() -> Path:
    directory = migrations.sql_directory()
    assert directory.is_dir(), f"sql directory not found at {directory}"
    return directory


@pytest.fixture(scope="module")
def schema_sql(sql_dir: Path) -> str:
    return (sql_dir / "01_schema.sql").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def schema_tables(schema_sql: str) -> dict[str, set[str]]:
    return table_columns(schema_sql)


@pytest.fixture(scope="module")
def staging_tables() -> dict[str, set[str]]:
    return table_columns(loader.STAGING_DDL)


# ==========================================================================
# The files exist and are shaped as the migration runner expects.
# ==========================================================================
class TestMigrationFiles:
    def test_every_declared_migration_file_exists(self, sql_dir: Path):
        for file_name in migrations.MIGRATION_FILES:
            assert (sql_dir / file_name).is_file(), f"missing {file_name}"

    def test_no_sql_file_is_silently_left_out_of_the_migration_list(self, sql_dir):
        """A file added to sql/ but not to MIGRATION_FILES would never be
        applied — the kind of omission that shows up as a missing view weeks
        later. (Files under sql/analysis/ are queries, not migrations.)"""
        on_disk = {path.name for path in sql_dir.glob("*.sql")}
        assert on_disk == set(migrations.MIGRATION_FILES), (
            "sql/ contents and migrations.MIGRATION_FILES disagree: "
            f"{on_disk.symmetric_difference(set(migrations.MIGRATION_FILES))}"
        )

    def test_every_file_splits_into_batches_with_no_go_left_behind(self, sql_dir):
        """GO is a client-side separator; a driver sent 'GO' raises a syntax
        error, so the splitter must consume every one of them."""
        for file_name in migrations.MIGRATION_FILES:
            script = (sql_dir / file_name).read_text(encoding="utf-8")
            batches = database.split_batches(script)
            assert batches, f"{file_name} produced no batches"
            for batch in batches:
                for line in batch.splitlines():
                    assert line.strip().upper() != "GO", (
                        f"{file_name}: a GO survived the split")

    def test_views_are_alone_in_their_batch(self, sql_dir: Path):
        """CREATE OR ALTER VIEW must be the first statement of its batch —
        SQL Server rejects it otherwise, and that failure would only appear on
        deploy."""
        script = (sql_dir / "03_views.sql").read_text(encoding="utf-8")
        seen = 0
        for batch in database.split_batches(script):
            body = strip_comments(batch).strip()
            if "CREATE OR ALTER VIEW" not in body.upper():
                continue
            seen += 1
            assert body.upper().count("CREATE OR ALTER VIEW") == 1
            assert body.upper().startswith("CREATE OR ALTER VIEW"), (
                "a statement precedes CREATE OR ALTER VIEW in its batch")
        assert seen >= 7, f"expected the BI views, found {seen}"

    def test_no_filtered_index_predicate_uses_or(self, sql_dir: Path):
        """A filtered-index predicate is not a general boolean expression.

        The documented grammar is `<conjunct> [ AND <conjunct> ]`, so conjuncts
        may be ANDed and a single column may use IN (...) — but OR across two
        columns is not expressible, and SQL Server rejects the CREATE INDEX
        outright. That failure only appears when the migration runs against a
        real database, i.e. after a deploy, which is exactly the kind of mistake
        worth catching in 3 ms here.
        """
        script = strip_comments(
            (sql_dir / "02_indexes.sql").read_text(encoding="utf-8"))
        for match in re.finditer(
            r"CREATE\s+NONCLUSTERED\s+INDEX\s+(\w+)(.*?);", script,
            re.DOTALL | re.IGNORECASE,
        ):
            name, body = match.group(1), match.group(2)
            where = re.search(r"\bWHERE\b(.*)$", body, re.DOTALL | re.IGNORECASE)
            if not where:
                continue
            assert not re.search(r"\bOR\b", where.group(1), re.IGNORECASE), (
                f"{name}: filtered index predicates cannot use OR — "
                "split it into two indexes")

    def test_every_merge_in_the_sql_files_holds_a_lock(self, sql_dir: Path):
        """Two concurrent POSTs to /api/migrate would race the seed MERGE
        exactly as two ingest runs would race the fact MERGE."""
        for path in sql_dir.glob("*.sql"):
            script = strip_comments(path.read_text(encoding="utf-8"))
            for match in re.finditer(r"MERGE\s+(\S+)\s+(\S+)", script,
                                     re.IGNORECASE):
                assert match.group(2).upper().startswith("WITH"), (
                    f"{path.name}: MERGE into {match.group(1)} lacks HOLDLOCK")

    def test_no_cron_expression_sits_inside_a_block_comment(self):
        """`*/` closes a T-SQL block comment — including the one in `*/15`.

        An NCRONTAB schedule (`0 */15 6-9,16-19 * * 1-5`) written inside a
        `/* ... */` comment terminates that comment early, and the remainder of
        the sentence becomes SQL. The failure is a syntax error pointing at a
        digit with no obvious cause ("Incorrect syntax near '6'"), and it only
        appears when the batch actually runs against a server.

        A legitimate comment terminator is followed by whitespace or a newline,
        never by a digit — so that is the signature to forbid. Cron expressions
        belong in `--` line comments.
        """
        for path in sorted((REPO_ROOT / "sql").rglob("*.sql")):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"\*/\d", text):
                line = text[:match.start()].count("\n") + 1
                raise AssertionError(
                    f"{path.relative_to(REPO_ROOT)}:{line} contains '*/' followed "
                    f"by a digit — this closes the block comment early. Use a "
                    f"`--` line comment for cron expressions."
                )

    def test_bi_dimensions_define_what_power_bi_relates_on(self, sql_dir: Path):
        """A Power BI model needs a contiguous date table and the two join keys;
        without them time intelligence silently produces wrong answers."""
        script = strip_comments((sql_dir / "05_bi_dimensions.sql").read_text(encoding="utf-8"))
        tables = table_columns(script)
        assert "dim_date" in tables, "no dim_date — DAX time intelligence cannot work"
        assert "dim_hour" in tables, "no dim_hour — unsampled hours vanish from charts"
        assert {"date_key", "year_month", "year_month_sort", "is_weekend"} <= tables["dim_date"]
        assert {"hour_of_day", "hour_label", "peak_window", "window_sort"} <= tables["dim_hour"]
        # The fact view must expose the relationship keys under unambiguous names.
        assert "CREATE OR ALTER VIEW dbo.v_bi_departures" in script
        assert "AS date_key" in script and "AS hour_of_day" in script

    def test_every_object_granted_to_power_bi_actually_exists(self, sql_dir: Path):
        """scripts/create_bi_reader.py GRANTs SELECT on a fixed list. A name that
        does not exist makes the grant fail at run time; a view added without
        being granted is invisible to Power BI. Both are drift worth catching."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "create_bi_reader", REPO_ROOT / "scripts" / "create_bi_reader.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        defined: set[str] = set()
        for path in sorted(sql_dir.glob("*.sql")):
            script = strip_comments(path.read_text(encoding="utf-8"))
            defined |= set(re.findall(r"CREATE OR ALTER VIEW dbo\.(\w+)", script))
            defined |= set(table_columns(script))

        granted = {obj.split(".")[-1] for obj in module.READABLE_OBJECTS}
        missing = granted - defined
        assert not missing, f"granted on objects that no .sql file defines: {sorted(missing)}"

        # Every BI view should be readable by Power BI, or it may as well not exist.
        bi_views = {v for v in defined if v.startswith("v_")}
        ungranted = bi_views - granted
        assert not ungranted, (
            f"views not granted to powerbi_reader: {sorted(ungranted)} — add them "
            "to READABLE_OBJECTS or they are invisible to the BI layer")

    def test_go_inside_a_comment_or_a_word_is_not_a_separator(self):
        script = (
            "SELECT 1;  -- the GO below is the only real separator\n"
            "GO\n"
            "SELECT 'GOING';\n"
        )
        batches = database.split_batches(script)
        assert len(batches) == 2
        assert "GOING" in batches[1]


# ==========================================================================
# The schema contains what the rest of the project believes it contains.
# ==========================================================================
class TestSchemaShape:
    #: Table -> columns that must exist, whatever else changes. Spelled out
    #: rather than counted, so that this test guards the *parser* (a regex that
    #: matched nothing would make every subset assertion below vacuously true)
    #: as well as the schema.
    EXPECTED_COLUMNS = {
        "ingestion_runs": {"run_id", "trigger_source", "requested_station",
                           "station_id", "started_utc", "status",
                           "departures_returned", "rows_inserted"},
        "stations": {"station_id", "uic_code", "country_code", "name",
                     "latitude", "longitude", "is_hub"},
        "vehicle_types": {"type_code", "label", "is_seeded"},
        "vehicles": {"vehicle_id", "type_code", "service_line", "short_name"},
        "platforms": {"station_id", "platform_code", "first_seen_utc",
                      "last_seen_utc"},
        "liveboard_records": {"record_id", "station_id", "vehicle_id",
                              "scheduled_departure_utc",
                              "scheduled_departure_local", "delay_seconds",
                              "is_canceled", "platform_code",
                              "destination_station_id", "occupancy"},
    }
    EXPECTED_TABLES = set(EXPECTED_COLUMNS)

    def test_all_expected_tables_are_declared(self, schema_tables):
        assert self.EXPECTED_TABLES <= set(schema_tables)

    def test_every_expected_column_is_present_and_parsed(self, schema_tables):
        for table, columns in self.EXPECTED_COLUMNS.items():
            missing = columns - schema_tables[table]
            assert not missing, f"{table} is missing {sorted(missing)}"

    def test_the_parser_invents_no_columns_from_prose(self, schema_tables):
        """Every parsed name must look like a column, not like a word from a
        comment. Without strip_comments() this fails on `a` and `when`."""
        suspicious = {
            column
            for columns in schema_tables.values()
            for column in columns
            if len(column) < 3 or column in {"the", "and", "when", "which"}
        }
        assert not suspicious, f"parser picked up prose: {sorted(suspicious)}"

    def test_the_fact_table_declares_its_natural_key_as_unique(self, schema_sql):
        match = re.search(
            r"CONSTRAINT uq_liveboard_records\s+UNIQUE\s*\(([^)]*)\)",
            schema_sql, re.IGNORECASE)
        assert match, "the fact table has no unique natural key"
        columns = [token.strip() for token in match.group(1).split(",")]
        assert columns == ["station_id", "vehicle_id", "scheduled_departure_utc"]

    def test_the_fact_table_has_lineage_back_to_the_run_log(self, schema_tables):
        assert {"first_seen_run_id", "last_seen_run_id"} <= \
            schema_tables["liveboard_records"]

    def test_observation_history_columns_are_present(self, schema_tables):
        """The compromise that makes a current-state fact table still able to
        answer "did this delay grow" — see the header of 01_schema.sql."""
        assert {"first_seen_utc", "last_seen_utc", "observation_count",
                "delay_first_seen_s"} <= schema_tables["liveboard_records"]

    def test_platforms_are_scoped_to_a_station(self, schema_sql):
        assert re.search(
            r"CONSTRAINT pk_platforms PRIMARY KEY \(station_id, platform_code\)",
            schema_sql, re.IGNORECASE)

    def test_every_table_is_created_conditionally(self, schema_sql):
        """The migration endpoint is idempotent, which only holds if every
        CREATE is guarded."""
        creates = len(re.findall(r"CREATE TABLE ", schema_sql, re.IGNORECASE))
        guards = len(re.findall(r"IF OBJECT_ID\('dbo\.\w+', 'U'\) IS NULL",
                                schema_sql, re.IGNORECASE))
        assert creates == guards == len(TestSchemaShape.EXPECTED_TABLES)


# ==========================================================================
# The loader's SQL agrees with the schema. This is the drift these tests exist
# to catch.
# ==========================================================================
class TestLoaderMatchesSchema:
    def test_staging_tables_cover_the_python_column_tuples(self, staging_tables):
        pairs = (
            (loader.STAGE_STATIONS, loader.STATION_COLUMNS),
            (loader.STAGE_VEHICLES, loader.VEHICLE_COLUMNS),
            (loader.STAGE_PLATFORMS, loader.PLATFORM_COLUMNS),
            (loader.STAGE_DEPARTURES, loader.DEPARTURE_COLUMNS),
        )
        for table, columns in pairs:
            declared = staging_tables[table.lower()]
            assert set(columns) == declared, (
                f"{table}: Python tuple and DDL disagree: "
                f"{set(columns).symmetric_difference(declared)}")

    @pytest.mark.parametrize("statement_name, target", [
        ("MERGE_STATIONS", "stations"),
        ("MERGE_VEHICLES", "vehicles"),
        ("MERGE_PLATFORMS", "platforms"),
        ("MERGE_DEPARTURES", "liveboard_records"),
        ("MERGE_VEHICLE_TYPES", "vehicle_types"),
    ])
    def test_merge_targets_only_columns_that_exist(
        self, statement_name, target, schema_tables
    ):
        statement = getattr(loader, statement_name)
        declared = schema_tables[target]
        for column in referenced("t", statement) | insert_column_list(statement):
            assert column in declared, (
                f"{statement_name} references {target}.{column}, "
                "which the schema does not declare")

    @pytest.mark.parametrize("statement_name, staging", [
        ("MERGE_STATIONS", "#stg_stations"),
        ("MERGE_VEHICLES", "#stg_vehicles"),
        ("MERGE_PLATFORMS", "#stg_platforms"),
        ("MERGE_DEPARTURES", "#stg_departures"),
    ])
    def test_merge_reads_only_columns_the_staging_table_has(
        self, statement_name, staging, staging_tables
    ):
        statement = getattr(loader, statement_name)
        declared = staging_tables[staging.lower()]
        for column in referenced("s", statement):
            assert column in declared, (
                f"{statement_name} reads {staging}.{column}, "
                "which the staging DDL does not declare")

    def test_the_fact_merge_keys_on_the_unique_constraint(self, schema_sql):
        """If the ON clause and the UNIQUE constraint ever disagree, MERGE stops
        being idempotent and starts inserting duplicates until the constraint
        rejects them."""
        on_clause = loader.MERGE_DEPARTURES.split("USING", 1)[1].split("WHEN", 1)[0]
        keyed = set(re.findall(r"t\.(\w+)\s*=\s*s\.\1", on_clause))
        assert keyed == {"station_id", "vehicle_id", "scheduled_departure_utc"}

    def test_every_merge_holds_a_lock_on_its_target(self):
        """MERGE without HOLDLOCK is documented as racy: two concurrent runs can
        both miss a row and both insert it. The timer and a manual HTTP call can
        genuinely overlap, so this is not theoretical."""
        for name in ("MERGE_STATIONS", "MERGE_VEHICLES", "MERGE_PLATFORMS",
                     "MERGE_DEPARTURES", "MERGE_VEHICLE_TYPES"):
            statement = getattr(loader, name)
            assert "WITH (HOLDLOCK)" in statement, f"{name} lacks HOLDLOCK"

    def test_first_seen_columns_are_never_updated(self):
        """The whole point of keeping them: they record the first observation.
        An UPDATE touching them would silently erase the delay-growth signal."""
        update_clause = loader.MERGE_DEPARTURES.split("UPDATE SET", 1)[1] \
                                              .split("WHEN NOT MATCHED", 1)[0]
        for column in ("first_seen_utc", "first_seen_run_id", "delay_first_seen_s"):
            assert f"t.{column}" not in update_clause, (
                f"{column} must not be modified on a repeat sighting")

    def test_the_update_is_guarded_against_replaying_the_same_run(self):
        assert "t.last_seen_run_id <> s.run_id" in loader.MERGE_DEPARTURES

    def test_run_bookkeeping_uses_only_declared_columns(self, schema_tables):
        """open_run/close_run write ingestion_runs with hand-written SQL, so the
        `column = ?` assignments in close_run are scanned for drift too."""
        declared = schema_tables["ingestion_runs"]
        block = Path(loader.__file__).read_text(encoding="utf-8") \
                                    .split("def open_run", 1)[1]
        # Both assignment forms close_run uses: a plain parameter, and
        # `COALESCE(?, column)` for the fields that keep whatever the run
        # already recorded. The optional SET prefix catches the first
        # assignment, which shares its line with the SET keyword.
        assignments = re.findall(r"^\s+(?:SET\s+)?(\w+)\s+= (?:\?|COALESCE)",
                                 block, re.MULTILINE)
        assert len(assignments) >= 14, (
            f"close_run's UPDATE list looks unparsed (found {assignments})")
        for column in assignments:
            assert column in declared, f"ingestion_runs.{column} does not exist"


# ==========================================================================
# The views the reporting layer selects from must actually be defined.
# ==========================================================================
class TestViewsExist:
    def test_every_view_queried_by_reporting_is_defined(self, sql_dir: Path):
        views_sql = (sql_dir / "03_views.sql").read_text(encoding="utf-8")
        defined = set(re.findall(r"CREATE OR ALTER VIEW dbo\.(\w+)", views_sql))
        used = set(re.findall(r"dbo\.(v_\w+)",
                              Path(reporting.__file__).read_text(encoding="utf-8")))
        assert used <= defined, f"undefined views referenced: {used - defined}"

    def test_reporting_counts_only_real_tables(self, schema_tables):
        assert set(reporting.COUNTED_TABLES) <= set(schema_tables)

    def test_bit_columns_are_never_summed_directly(self, sql_dir: Path):
        """T-SQL cannot SUM a BIT. The idiom is SUM(CONVERT(INT, flag)), and
        getting it wrong fails at deploy time rather than at write time."""
        views_sql = (sql_dir / "03_views.sql").read_text(encoding="utf-8")
        for match in re.finditer(r"SUM\(([^()]*)\)", views_sql):
            inner = match.group(1)
            assert not re.match(r"^\s*\w+\.(is_|has_)\w+\s*$", inner), (
                f"SUM({inner}) sums a BIT column directly")
