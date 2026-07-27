"""Tests for the write path, using a fake cursor instead of a database.

WHAT THESE CATCH THAT test_sql_contract.py CANNOT
`test_sql_contract` compares the *names* in the SQL against the schema. These
run the loader for real against a recorded payload and check the *statements it
produces*: the order they are issued in (which the foreign keys dictate), the
number of parameters (which must equal columns × rows, or the driver raises
"COUNT field incorrect" at run time), the chunking against SQL Server's
2 100-parameter limit, and the parsing of the MERGE's `$action` output.

A mismatch between `DEPARTURE_COLUMNS` and `_departure_values()` is invisible to
a reviewer, invisible to the type checker, and fatal at run time. It is one
assertion here.

No pyodbc connection is opened, so these run offline in milliseconds like the
rest of the suite.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from railpulse import loader, transform


# ==========================================================================
# A cursor that records instead of executing.
# ==========================================================================
class FakeCursor:
    """Minimal stand-in for pyodbc.Cursor covering what the loader uses."""

    def __init__(self, merge_actions: list[tuple[str, int]] | None = None,
                 rowcount: int = 3) -> None:
        self.calls: list[tuple[str, list | None]] = []
        self.rowcount = rowcount
        self.description: list | None = None
        self._merge_actions = merge_actions if merge_actions is not None else []
        self._pending: list | None = None
        self.closed = False

    def execute(self, statement, params=None):
        self.calls.append((statement, list(params) if params is not None else None))
        if "OUTPUT $action" in statement:
            self.description = [("act",), ("affected",)]
            self._pending = list(self._merge_actions)
        elif "SCOPE_IDENTITY()" in statement:
            self.description = [("run_id",)]
            self._pending = [(4242,)]
        else:
            self.description = None
            self._pending = None
        return self

    def fetchall(self):
        return list(self._pending or [])

    def nextset(self):
        return False

    def close(self):
        self.closed = True

    # -- helpers for the assertions ------------------------------------------
    @property
    def statements(self) -> list[str]:
        return [statement for statement, _ in self.calls]

    def find(self, needle: str) -> list[tuple[str, list | None]]:
        return [call for call in self.calls if needle in call[0]]

    def index_of(self, needle: str) -> int:
        for position, statement in enumerate(self.statements):
            if needle in statement:
                return position
        raise AssertionError(f"no statement containing {needle!r}")


@pytest.fixture()
def batch(brussels_central) -> transform.LiveboardBatch:
    return transform.parse_liveboard(
        brussels_central, seen_utc=datetime(2026, 7, 27, 9, 43, 0))


# ==========================================================================
# Column tuples and value tuples must stay in lockstep.
# ==========================================================================
class TestRowFlattening:
    """The mismatch that is invisible to review and fatal at run time."""

    def test_every_flattener_returns_one_value_per_column(self, batch):
        seen = datetime(2026, 7, 27, 9, 43, 0)
        pairs = [
            (loader.STATION_COLUMNS, loader._station_values(batch.stations[0])),
            (loader.VEHICLE_COLUMNS, loader._vehicle_values(batch.vehicles[0])),
            (loader.PLATFORM_COLUMNS, loader._platform_values(batch.platforms[0])),
            (loader.DEPARTURE_COLUMNS,
             loader._departure_values(batch.departures[0], 1)),
        ]
        for columns, values in pairs:
            assert len(columns) == len(values), (
                f"{len(columns)} columns but {len(values)} values")
        del seen

    def test_departure_values_are_in_the_declared_column_order(self, batch):
        """Positional binding: a reordering here silently writes the vehicle id
        into the station column, and both are VARCHAR so nothing complains."""
        values = loader._departure_values(batch.departures[0], run_id=7)
        by_name = dict(zip(loader.DEPARTURE_COLUMNS, values))
        row = batch.departures[0]
        assert by_name["station_id"] == row.station_id
        assert by_name["vehicle_id"] == row.vehicle_id
        assert by_name["scheduled_departure_utc"] == row.scheduled_departure_utc
        assert by_name["scheduled_departure_local"] == row.scheduled_departure_local
        assert by_name["delay_seconds"] == row.delay_seconds
        assert by_name["run_id"] == 7

    def test_bound_values_are_driver_bindable_types(self, batch):
        """pyodbc binds str/int/float/bool/datetime/None. Anything else — a
        dataclass, an Enum, a Decimal from nowhere — fails at execute time."""
        allowed = (str, int, float, bool, datetime)
        for row in batch.departures:
            for value in loader._departure_values(row, 1):
                assert value is None or isinstance(value, allowed), (
                    f"{value!r} is a {type(value).__name__}")


# ==========================================================================
# The statements the loader issues, in order.
# ==========================================================================
class TestLoadBatchStatements:
    def test_writes_in_foreign_key_order(self, batch):
        """stations -> vehicle_types -> vehicles -> platforms -> departures.

        Not a style preference: a departure cannot reference a platform that does
        not exist yet, and a vehicle cannot reference an unseeded type code.
        Getting this wrong is a foreign-key violation on the first ever load.
        """
        cursor = FakeCursor(merge_actions=[("INSERT", 56)])
        loader.load_batch(cursor, batch, run_id=1)

        order = [
            cursor.index_of("MERGE dbo.stations"),
            cursor.index_of("MERGE dbo.vehicle_types"),
            cursor.index_of("MERGE dbo.vehicles"),
            cursor.index_of("MERGE dbo.platforms"),
            cursor.index_of("MERGE dbo.liveboard_records"),
        ]
        assert order == sorted(order), f"write order is wrong: {order}"

    def test_staging_tables_are_created_before_anything_is_staged(self, batch):
        cursor = FakeCursor(merge_actions=[("INSERT", 56)])
        loader.load_batch(cursor, batch, run_id=1)
        assert cursor.index_of("CREATE TABLE #stg_stations") == 0

    def test_each_insert_binds_columns_times_rows_parameters(self, batch):
        cursor = FakeCursor(merge_actions=[("INSERT", 56)])
        loader.load_batch(cursor, batch, run_id=1)

        expectations = [
            ("INSERT INTO #stg_stations", len(loader.STATION_COLUMNS),
             len(batch.stations)),
            ("INSERT INTO #stg_vehicles", len(loader.VEHICLE_COLUMNS),
             len(batch.vehicles)),
            ("INSERT INTO #stg_platforms", len(loader.PLATFORM_COLUMNS),
             len(batch.platforms)),
            ("INSERT INTO #stg_departures", len(loader.DEPARTURE_COLUMNS),
             len(batch.departures)),
        ]
        for needle, width, rows in expectations:
            calls = cursor.find(needle)
            assert calls, f"nothing staged into {needle}"
            bound = sum(len(params or []) for _, params in calls)
            assert bound == width * rows, (
                f"{needle}: bound {bound} parameters for {rows} rows "
                f"of {width} columns")
            # Every statement's placeholder count must match its parameters, or
            # the driver raises "COUNT field incorrect".
            for statement, params in calls:
                assert statement.count("?") == len(params or [])

    def test_the_fixture_actually_exercises_all_four_tables(self, batch):
        """Guards the tests above: with an empty batch they would pass trivially."""
        assert len(batch.departures) > 20
        assert len(batch.stations) > 5
        assert len(batch.vehicles) > 20
        assert len(batch.platforms) >= 2

    def test_merge_action_counts_are_read_back(self, batch):
        cursor = FakeCursor(merge_actions=[("INSERT", 40), ("UPDATE", 16)])
        counts = loader.load_batch(cursor, batch, run_id=1)
        assert counts.rows_inserted == 40
        assert counts.rows_updated == 16

    @pytest.mark.parametrize("action", ["insert", "Insert", "INSERT"])
    def test_action_matching_is_case_insensitive(self, batch, action):
        """$action's casing is documented as upper case, but reading it back
        case-sensitively would silently report zero inserts if that changed."""
        cursor = FakeCursor(merge_actions=[(action, 7)])
        assert loader.load_batch(cursor, batch, run_id=1).rows_inserted == 7

    def test_no_merge_action_output_means_zero_counts_not_a_crash(self, batch):
        cursor = FakeCursor(merge_actions=[])
        counts = loader.load_batch(cursor, batch, run_id=1)
        assert counts.rows_inserted == 0 and counts.rows_updated == 0

    def test_an_empty_batch_stages_nothing_and_merges_nothing(self):
        cursor = FakeCursor()
        empty = transform.LiveboardBatch(
            station_id=None, station_name=None, feed_timestamp_utc=None)
        counts = loader.load_batch(cursor, empty, run_id=1)
        assert counts.rows_inserted == 0
        assert cursor.find("MERGE dbo.liveboard_records") == []
        # The staging DDL still runs — harmless, and it keeps the code path
        # identical whether or not the feed returned anything.
        assert cursor.find("CREATE TABLE #stg_departures")

    def test_negative_rowcount_never_becomes_a_negative_count(self, batch):
        """pyodbc reports -1 when the driver cannot determine a row count, and a
        negative 'stations_upserted' in the audit table would be nonsense."""
        cursor = FakeCursor(merge_actions=[("INSERT", 1)], rowcount=-1)
        counts = loader.load_batch(cursor, batch, run_id=1)
        assert counts.stations_upserted == 0
        assert counts.vehicles_upserted == 0
        assert counts.platforms_upserted == 0


# ==========================================================================
# Chunking against SQL Server's parameter ceiling.
# ==========================================================================
class TestParameterChunking:
    def test_a_large_insert_is_split_below_the_parameter_limit(self):
        """SQL Server accepts at most 2 100 parameters per statement. The station
        seed is 714 rows × 10 columns = 7 140, so it must be split — and each
        piece must stay under the limit."""
        from railpulse.database import MAX_PARAMETERS_PER_STATEMENT, insert_rows

        cursor = FakeCursor()
        columns = loader.STATION_COLUMNS
        rows = [tuple(range(len(columns))) for _ in range(714)]
        written = insert_rows(cursor, "#stg_stations", columns, rows)

        assert written == 714
        assert len(cursor.calls) > 1, "714 rows should not fit in one statement"
        for statement, params in cursor.calls:
            assert len(params) <= MAX_PARAMETERS_PER_STATEMENT
            assert statement.count("?") == len(params)
        # Nothing lost or duplicated in the split.
        assert sum(len(params) for _, params in cursor.calls) == 714 * len(columns)

    def test_an_empty_row_list_issues_no_statement(self):
        from railpulse.database import insert_rows

        cursor = FakeCursor()
        assert insert_rows(cursor, "#stg_stations", ("a", "b"), []) == 0
        assert cursor.calls == []


# ==========================================================================
# Run bookkeeping
# ==========================================================================
class TestRunBookkeeping:
    def test_open_run_returns_the_identity_it_was_given(self):
        cursor = FakeCursor()
        run_id = loader.open_run(
            cursor, trigger_source="timer", requested_station="Leuven",
            started_utc=datetime(2026, 7, 27, 9, 0), invocation_id="abc")
        assert run_id == 4242
        statement, params = cursor.calls[0]
        assert "INSERT INTO dbo.ingestion_runs" in statement
        assert "SCOPE_IDENTITY()" in statement   # not @@IDENTITY
        assert params[0] == "timer" and params[2] == "Leuven"

    def test_open_run_fails_loudly_when_no_id_comes_back(self):
        """Silently continuing with run_id = None would violate the fact table's
        NOT NULL lineage columns much later, with a far worse error."""
        cursor = FakeCursor()
        cursor.execute = lambda *a, **k: cursor  # type: ignore[method-assign]
        cursor.description = None
        with pytest.raises(RuntimeError, match="run_id"):
            loader.open_run(cursor, trigger_source="http",
                            requested_station="x",
                            started_utc=datetime(2026, 7, 27))

    def test_close_run_truncates_a_long_error_to_the_column_width(self):
        cursor = FakeCursor()
        loader.close_run(
            cursor, 1, status="failed", finished_utc=datetime(2026, 7, 27),
            duration_ms=10, error_message="x" * 5000)
        _, params = cursor.calls[0]
        assert len(params[-2]) == 1000    # NVARCHAR(1000)

    def test_close_run_passes_none_rather_than_an_empty_error(self):
        cursor = FakeCursor()
        loader.close_run(cursor, 1, status="success",
                         finished_utc=datetime(2026, 7, 27), duration_ms=10)
        _, params = cursor.calls[0]
        assert params[-2] is None
