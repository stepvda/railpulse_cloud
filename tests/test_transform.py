"""Tests for the JSON -> rows layer.

These run offline in milliseconds and need no Azure subscription, which is the
whole reason transform.py has no I/O in it. They are grouped by the *class of
mistake* they exist to prevent, rather than by function, because the risk here
is not "does the code run" but "does it quietly mean something else".
"""

from __future__ import annotations

from datetime import datetime

import pytest

from railpulse import transform


# ==========================================================================
# Scalar coercion: the feed sends every value as a string.
# ==========================================================================
class TestScalarCoercion:
    @pytest.mark.parametrize("raw, expected", [
        ("S1", ("S", "1")),
        ("S32", ("S", "32")),
        ("S10", ("S", "10")),
        ("IC", ("IC", None)),
        ("L", ("L", None)),
        ("EUR", ("EUR", None)),
        ("ICE", ("ICE", None)),
        ("CHAR", ("CHAR", None)),
        ("T", ("T", None)),
        ("S", ("S", None)),          # bare S: family, no line
        ("ic", ("IC", None)),        # normalised to upper
        (" IC ", ("IC", None)),
        ("", ("TRN", None)),         # missing type must still satisfy the FK
        (None, ("TRN", None)),
        ("SX", ("SX", None)),        # 'S' + non-digits is NOT an S-line
    ])
    def test_vehicle_type_is_split_into_family_and_line(self, raw, expected):
        assert transform.split_vehicle_type(raw) == expected

    @pytest.mark.parametrize("uic, expected", [
        ("008813003", "BE"),
        ("008400319", "NL"),
        ("008000001", "DE"),
        ("008700001", "FR"),
        ("008200001", "LU"),
        ("007000001", "GB"),
        ("009900001", "XX"),   # unknown prefix must not raise
        ("", "XX"),
        (None, "XX"),
        ("12", "XX"),          # too short to hold a prefix
    ])
    def test_country_is_derived_from_the_uic_prefix(self, uic, expected):
        assert transform.country_from_uic(uic) == expected

    def test_uic_is_extracted_and_padded_to_nine_characters(self):
        # CHAR(9) compares padded; a short code stored unpadded would silently
        # fail to join against a padded one.
        assert transform.uic_from_station_id("BE.NMBS.008813003") == "008813003"
        assert transform.uic_from_station_id("BE.NMBS.8813003") == "008813003"
        assert transform.uic_from_station_id("") == ""

    def test_zero_coordinates_are_treated_as_missing(self):
        # The feed uses "0" for unknown. Storing it would place the station in
        # the Gulf of Guinea, which a map visual would happily draw.
        row = transform._station_row(
            {"id": "BE.NMBS.008813003", "name": "X",
             "locationX": "0", "locationY": "0"},
            seen_utc=datetime(2026, 7, 27, 10, 0), is_hub=False,
        )
        assert row is not None
        assert row.latitude is None and row.longitude is None


# ==========================================================================
# Liveboard parsing against a real captured payload.
# ==========================================================================
class TestLiveboardParsing:
    SEEN = datetime(2026, 7, 27, 9, 43, 0)

    def test_polled_station_becomes_the_origin_not_the_destination(
        self, brussels_central
    ):
        """The single most consequential distinction in the payload.

        Each departure entry's own `stationinfo` is the train's TERMINUS. Reading
        it as the departure station would attribute every Brussels-Central
        departure to Antwerp, Ghent, Namur ... and the error would look entirely
        plausible in a dashboard.
        """
        batch = transform.parse_liveboard(brussels_central, seen_utc=self.SEEN)

        assert batch.station_id == "BE.NMBS.008813003"
        assert batch.station_name == "Brussels-Central"
        # Every fact row departs FROM the polled station.
        assert {row.station_id for row in batch.departures} == {"BE.NMBS.008813003"}
        # ... and at least one goes somewhere else.
        destinations = {row.destination_station_id for row in batch.departures}
        assert "BE.NMBS.008821006" in destinations       # Antwerp-Central
        assert destinations - {"BE.NMBS.008813003"}

    def test_every_returned_departure_is_accounted_for(self, brussels_central):
        batch = transform.parse_liveboard(brussels_central, seen_utc=self.SEEN)
        assert batch.departures_returned == 56
        # Nothing may vanish silently: kept + dropped must equal returned.
        assert (len(batch.departures)
                + batch.duplicates_dropped
                + batch.unusable_dropped) == batch.departures_returned

    def test_scheduled_time_is_the_schedule_and_delay_is_separate(
        self, brussels_central
    ):
        """`time` is the timetabled departure; `delay` is added on top of it.

        Folding the delay into the timestamp would destroy the ability to
        measure punctuality at all — which is the entire point of the dataset.
        """
        batch = transform.parse_liveboard(brussels_central, seen_utc=self.SEEN)
        delayed = [d for d in batch.departures if d.delay_seconds > 0]
        assert delayed, "fixture should contain at least one delayed train"
        for row in delayed:
            # The scheduled time is untouched; actual is derived in SQL.
            assert row.scheduled_departure_utc.second == 0
            assert row.delay_seconds % 60 == 0     # feed reports whole minutes

    def test_utc_is_converted_to_belgian_local_time(self, brussels_central):
        batch = transform.parse_liveboard(brussels_central, seen_utc=self.SEEN)
        row = batch.departures[0]
        # July: Europe/Brussels is UTC+2. A fixed offset would be right now and
        # wrong in January, which is why zoneinfo is used.
        offset = row.scheduled_departure_local - row.scheduled_departure_utc
        assert offset.total_seconds() == 7200

    def test_local_time_handles_the_winter_offset_too(self):
        """The same conversion in January must be +1, not +2.

        This is the test that would fail if someone "simplified" the conversion
        to a constant, and it is the reason the local hour is stored rather than
        computed in the BI layer.
        """
        winter_epoch = int(datetime(2026, 1, 15, 12, 0).timestamp())
        payload = _minimal_payload(epoch=winter_epoch)
        batch = transform.parse_liveboard(payload, seen_utc=self.SEEN)
        row = batch.departures[0]
        offset = row.scheduled_departure_local - row.scheduled_departure_utc
        assert offset.total_seconds() == 3600

    def test_unknown_platform_becomes_null_and_no_platform_row(self):
        """'?' is the feed's sentinel, not a platform.

        NULL on the fact row also disables the composite foreign key (a
        composite FK with a NULL member is not checked), which is what lets an
        unallocated departure load without inventing a fake '?' platform.
        """
        payload = _minimal_payload(platform="?")
        batch = transform.parse_liveboard(payload, seen_utc=self.SEEN)
        assert batch.departures[0].platform_code is None
        assert batch.platforms == []

    def test_real_platforms_produce_dimension_rows_scoped_to_the_station(
        self, brussels_central
    ):
        batch = transform.parse_liveboard(brussels_central, seen_utc=self.SEEN)
        assert batch.platforms
        assert all(p.station_id == "BE.NMBS.008813003" for p in batch.platforms)
        # Brussels-Central has six platforms; the fixture should see several.
        assert 1 < len(batch.platforms) <= 6

    def test_flags_are_parsed_from_string_zero_and_one(self, brussels_central):
        batch = transform.parse_liveboard(brussels_central, seen_utc=self.SEEN)
        for row in batch.departures:
            assert isinstance(row.is_canceled, bool)
            assert isinstance(row.has_left, bool)
            assert isinstance(row.is_extra, bool)

    def test_cancellation_is_read_as_true(self):
        payload = _minimal_payload(canceled="1")
        batch = transform.parse_liveboard(payload, seen_utc=self.SEEN)
        assert batch.departures[0].is_canceled is True

    def test_occupancy_is_lifted_out_of_its_nested_object(self, brussels_central):
        batch = transform.parse_liveboard(brussels_central, seen_utc=self.SEEN)
        values = {row.occupancy for row in batch.departures}
        assert values <= {"low", "medium", "high", "unknown", None}
        assert values & {"low", "medium", "high", "unknown"}

    def test_the_polled_station_is_flagged_as_a_hub_and_destinations_are_not(
        self, brussels_central
    ):
        batch = transform.parse_liveboard(brussels_central, seen_utc=self.SEEN,
                                         is_hub=True)
        by_id = {row.station_id: row for row in batch.stations}
        assert by_id["BE.NMBS.008813003"].is_hub is True
        others = [row for sid, row in by_id.items() if sid != "BE.NMBS.008813003"]
        assert others and all(row.is_hub is False for row in others)

    def test_dimension_rows_are_deduplicated(self, brussels_midi):
        """Two trains to the same terminus must not stage that station twice.

        MERGE raises error 8672 and abandons the whole statement when two source
        rows match one target row, so this is a correctness requirement, not
        tidiness: one repeated destination would fail the entire load.
        """
        batch = transform.parse_liveboard(brussels_midi, seen_utc=self.SEEN)
        assert len({r.station_id for r in batch.stations}) == len(batch.stations)
        assert len({r.vehicle_id for r in batch.vehicles}) == len(batch.vehicles)
        assert len({(r.station_id, r.platform_code)
                    for r in batch.platforms}) == len(batch.platforms)
        keys = [r.natural_key for r in batch.departures]
        assert len(set(keys)) == len(keys)

    def test_duplicate_departures_in_one_payload_are_dropped_and_counted(self):
        entry = _departure_entry()
        payload = {
            "station": "Test", "timestamp": "1785138180",
            "stationinfo": {"id": "BE.NMBS.008813003", "name": "Test"},
            "departures": {"number": "2", "departure": [entry, dict(entry)]},
        }
        batch = transform.parse_liveboard(payload, seen_utc=self.SEEN)
        assert len(batch.departures) == 1
        assert batch.duplicates_dropped == 1


# ==========================================================================
# Malformed and degenerate payloads: a poll must not crash on a bad response.
# ==========================================================================
class TestDegeneratePayloads:
    SEEN = datetime(2026, 7, 27, 9, 43, 0)

    def test_station_with_no_departures_yields_an_empty_batch(self):
        payload = {"stationinfo": {"id": "BE.NMBS.008831005", "name": "Hasselt"},
                   "departures": {"number": "0"}}
        batch = transform.parse_liveboard(payload, seen_utc=self.SEEN)
        assert batch.station_id == "BE.NMBS.008831005"
        assert batch.departures == []
        assert batch.departures_returned == 0
        # The station itself is still recorded — a quiet station is data.
        assert len(batch.stations) == 1

    def test_a_single_departure_sent_as_an_object_is_accepted(self):
        """iRail has historically collapsed one-element arrays to a bare object."""
        payload = {
            "stationinfo": {"id": "BE.NMBS.008813003", "name": "Test"},
            "departures": {"number": "1", "departure": _departure_entry()},
        }
        batch = transform.parse_liveboard(payload, seen_utc=self.SEEN)
        assert len(batch.departures) == 1

    def test_missing_station_identity_returns_an_empty_batch_not_an_exception(self):
        batch = transform.parse_liveboard({"departures": {}}, seen_utc=self.SEEN)
        assert batch.station_id is None
        assert batch.departures == []

    def test_departure_without_a_usable_key_is_dropped_and_counted(self):
        payload = {
            "stationinfo": {"id": "BE.NMBS.008813003", "name": "Test"},
            "departures": {"number": "3", "departure": [
                _departure_entry(),
                {**_departure_entry(), "time": "not-a-number"},   # no key
                {**_departure_entry(), "vehicle": "", "vehicleinfo": {}},
            ]},
        }
        batch = transform.parse_liveboard(payload, seen_utc=self.SEEN)
        assert len(batch.departures) == 1
        assert batch.unusable_dropped == 2

    def test_absent_delay_means_zero_not_null(self):
        """NOT NULL in the schema, and for a reason: a NULL delay would vanish
        from every AVG() and quietly flatter the punctuality figure."""
        entry = _departure_entry()
        del entry["delay"]
        payload = {"stationinfo": {"id": "BE.NMBS.008813003", "name": "T"},
                   "departures": {"departure": [entry]}}
        batch = transform.parse_liveboard(payload, seen_utc=self.SEEN)
        assert batch.departures[0].delay_seconds == 0

    def test_garbage_scalars_do_not_raise(self):
        entry = {**_departure_entry(), "delay": "banana", "canceled": "maybe",
                 "platforminfo": {"name": "", "normal": "?"},
                 "occupancy": "unexpected-string"}
        payload = {"stationinfo": {"id": "BE.NMBS.008813003", "name": "T"},
                   "departures": {"departure": [entry]}}
        batch = transform.parse_liveboard(payload, seen_utc=self.SEEN)
        row = batch.departures[0]
        assert row.delay_seconds == 0
        assert row.is_canceled is False
        # An unparseable `normal` is unknown, not "abnormal": NULL, not 0.
        assert row.platform_is_normal is None
        # platforminfo.name is blank here, so the flat `platform` field is used.
        # The fallback is deliberate — the two fields carry the same value and
        # either one alone is enough to know the platform.
        assert row.platform_code == "5"

    def test_platform_is_null_only_when_both_fields_are_unusable(self):
        entry = {**_departure_entry(), "platform": "", "platforminfo": {}}
        payload = {"stationinfo": {"id": "BE.NMBS.008813003", "name": "T"},
                   "departures": {"departure": [entry]}}
        batch = transform.parse_liveboard(payload, seen_utc=self.SEEN)
        assert batch.departures[0].platform_code is None
        assert batch.platforms == []

    def test_string_lengths_are_clamped_to_the_column_widths(self):
        entry = {**_departure_entry(),
                 "departureConnection": "x" * 500,
                 "vehicleinfo": {"name": "y" * 200, "shortname": "z" * 200,
                                 "type": "IC"}}
        payload = {"stationinfo": {"id": "BE.NMBS.008813003", "name": "n" * 400},
                   "departures": {"departure": [entry]}}
        batch = transform.parse_liveboard(payload, seen_utc=self.SEEN)
        # Truncating here rather than letting SQL Server raise "String data,
        # right truncation" and lose the whole batch.
        assert len(batch.departures[0].departure_connection) <= 200
        assert len(batch.vehicles[0].vehicle_id) <= 40
        assert len(batch.stations[0].name) <= 120


# ==========================================================================
# Station catalogue
# ==========================================================================
class TestStationCatalogue:
    SEEN = datetime(2026, 7, 27, 9, 43, 0)

    def test_catalogue_rows_carry_coordinates_and_country(self, station_catalogue):
        rows = transform.parse_station_catalogue(
            station_catalogue, seen_utc=self.SEEN)
        assert rows
        by_id = {row.station_id: row for row in rows}
        central = by_id["BE.NMBS.008813003"]
        assert central.country_code == "BE"
        assert central.latitude == pytest.approx(50.845658, abs=1e-6)
        assert central.longitude == pytest.approx(4.356801, abs=1e-6)
        # The bilingual official form is preserved alongside the localised name.
        assert "/" in (central.standard_name or "")

    def test_latitude_and_longitude_are_not_swapped(self, station_catalogue):
        """locationY is latitude and locationX is longitude — the reverse of the
        x/y reading. Belgium is near 50.8 N, 4.4 E, so a swap is detectable."""
        rows = transform.parse_station_catalogue(
            station_catalogue, seen_utc=self.SEEN)
        belgian = [r for r in rows if r.country_code == "BE" and r.latitude]
        assert belgian
        for row in belgian:
            assert 49.0 < row.latitude < 52.0
            assert 2.0 < row.longitude < 7.0

    def test_foreign_stations_are_identified(self, station_catalogue):
        rows = transform.parse_station_catalogue(
            station_catalogue, seen_utc=self.SEEN)
        countries = {row.country_code for row in rows}
        assert "BE" in countries
        # The fixture includes 's Hertogenbosch (UIC prefix 84).
        assert "NL" in countries

    def test_hub_ids_are_flagged_during_the_seed(self, station_catalogue):
        rows = transform.parse_station_catalogue(
            station_catalogue, seen_utc=self.SEEN,
            hub_ids=["BE.NMBS.008813003"])
        by_id = {row.station_id: row for row in rows}
        assert by_id["BE.NMBS.008813003"].is_hub is True
        assert all(row.is_hub is False
                   for sid, row in by_id.items() if sid != "BE.NMBS.008813003")


# ==========================================================================
# Helpers
# ==========================================================================
def _departure_entry(**overrides) -> dict:
    """A minimal but realistically-shaped departure entry."""
    entry = {
        "id": "0",
        "station": "Antwerp-Central",
        "stationinfo": {"@id": "http://irail.be/stations/NMBS/008821006",
                        "id": "BE.NMBS.008821006", "name": "Antwerp-Central",
                        "locationX": "4.421101", "locationY": "51.2172",
                        "standardname": "Antwerpen-Centraal"},
        "time": "1785138120",
        "delay": "0",
        "canceled": "0",
        "left": "0",
        "isExtra": "0",
        "vehicle": "BE.NMBS.S11958",
        "vehicleinfo": {"name": "BE.NMBS.S11958", "shortname": "S1 1958",
                        "number": "1958", "type": "S1",
                        "@id": "http://irail.be/vehicle/S11958"},
        "platform": "5",
        "platforminfo": {"name": "5", "normal": "1"},
        "occupancy": {"@id": "http://api.irail.be/terms/low", "name": "low"},
        "departureConnection": "http://irail.be/connections/8813003/20260727/S11958",
    }
    entry.update(overrides)
    return entry


def _minimal_payload(*, epoch: int | None = None, platform: str = "5",
                     canceled: str = "0") -> dict:
    entry = _departure_entry(canceled=canceled)
    if epoch is not None:
        entry["time"] = str(epoch)
    entry["platform"] = platform
    entry["platforminfo"] = {"name": platform, "normal": "1"}
    return {
        "station": "Brussels-Central",
        "timestamp": "1785138180",
        "stationinfo": {"@id": "http://irail.be/stations/NMBS/008813003",
                        "id": "BE.NMBS.008813003", "name": "Brussels-Central",
                        "locationX": "4.356801", "locationY": "50.845658",
                        "standardname": "Brussel-Centraal/Bruxelles-Central"},
        "departures": {"number": "1", "departure": [entry]},
    }
