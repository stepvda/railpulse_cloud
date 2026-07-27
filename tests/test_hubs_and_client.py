"""Tests for the hub catalogue and the HTTP client.

The hub tests exist because of a real mistake caught during development: UIC
008844008 was assumed to be Charleroi-Central and is in fact Verviers-Central.
That class of error does not raise — it silently produces a perfectly plausible
liveboard for the wrong city — so the ids are pinned here.

The client tests use a fake session rather than the network. A test that calls
api.irail.be is not a test: it is slow, it consumes somebody else's free quota,
and it fails on a train.
"""

from __future__ import annotations

import importlib

import pytest
import requests

from railpulse import hubs, irail


# ==========================================================================
# Hub catalogue
# ==========================================================================
class TestHubCatalogue:
    #: Verified against iRail's /v1/stations feed on 2026-07-27.
    EXPECTED = {
        "Brussels-Central": "008813003",
        "Brussels-South/Midi": "008814001",
        "Brussels-North": "008812005",
        "Antwerp-Central": "008821006",
        "Ghent-Sint-Pieters": "008892007",
        "Liège-Guillemins": "008841004",
        "Charleroi-Central": "008872009",
        "Leuven": "008833001",
        "Brugge": "008891009",
        "Namur": "008863008",
    }

    def test_default_hub_uic_codes_are_the_verified_ones(self):
        actual = {hub.label: hub.uic_code for hub in hubs.DEFAULT_HUBS}
        assert actual == self.EXPECTED

    def test_charleroi_is_not_verviers(self):
        """The specific mistake this file exists to prevent."""
        charleroi = hubs.resolve("Charleroi-Central")
        assert charleroi is not None
        assert charleroi.uic_code == "008872009"
        assert charleroi.uic_code != "008844008"   # Verviers-Central

    def test_hub_ids_are_unique(self):
        ids = [hub.station_id for hub in hubs.ALL_KNOWN_HUBS]
        assert len(set(ids)) == len(ids)

    def test_all_hubs_are_belgian_and_well_formed(self):
        for hub in hubs.ALL_KNOWN_HUBS:
            assert hub.station_id.startswith("BE.NMBS.")
            assert len(hub.uic_code) == 9 and hub.uic_code.isdigit()
            assert hub.uic_code[2:4] == "88", f"{hub.label} is not Belgian"
            assert hub.rationale, f"{hub.label} has no documented reason to exist"

    @pytest.mark.parametrize("token", [
        "BE.NMBS.008813003", "008813003", "Brussels-Central",
        "brussels-central", "  Brussels-Central  ",
    ])
    def test_resolution_accepts_id_uic_or_label(self, token):
        assert hubs.resolve(token) is hubs.DEFAULT_HUBS[0]

    def test_unknown_token_resolves_to_none(self):
        assert hubs.resolve("Hogwarts") is None
        assert hubs.resolve("") is None

    def test_configured_hubs_defaults_to_the_ten(self, monkeypatch):
        monkeypatch.delenv("RAILPULSE_HUBS", raising=False)
        importlib.reload(hubs.config)
        importlib.reload(hubs)
        assert hubs.configured_hubs() == hubs.DEFAULT_HUBS

    def test_setting_overrides_the_hub_set(self, monkeypatch):
        monkeypatch.setenv("RAILPULSE_HUBS", "Leuven, 008891009 ,BE.NMBS.008863008")
        importlib.reload(hubs.config)
        reloaded = importlib.reload(hubs)
        try:
            assert [h.label for h in reloaded.configured_hubs()] == [
                "Leuven", "Brugge", "Namur"]
        finally:
            monkeypatch.delenv("RAILPULSE_HUBS", raising=False)
            importlib.reload(reloaded.config)
            importlib.reload(reloaded)

    def test_unknown_station_in_the_setting_is_passed_through(self, monkeypatch):
        """The setting must be able to reach a station the code has never heard
        of, otherwise widening coverage means a redeploy."""
        monkeypatch.setenv("RAILPULSE_HUBS", "BE.NMBS.008895802")
        importlib.reload(hubs.config)
        reloaded = importlib.reload(hubs)
        try:
            configured = reloaded.configured_hubs()
            assert len(configured) == 1
            assert configured[0].station_id == "BE.NMBS.008895802"
        finally:
            monkeypatch.delenv("RAILPULSE_HUBS", raising=False)
            importlib.reload(reloaded.config)
            importlib.reload(reloaded)


# ==========================================================================
# HTTP client
# ==========================================================================
class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None,
                 content=b"{}"):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}
        self.content = content

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


class _FakeSession:
    """Records calls and replays a scripted list of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []
        self.headers: dict[str, str] = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params or {}))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        pass


def _client(responses, **kwargs) -> irail.IRailClient:
    # min_interval 0: the gate is tested by inspection, not by making the suite
    # sleep. Its arithmetic is trivial; its cost in a test run is not.
    return irail.IRailClient(
        session=_FakeSession(responses), min_interval=0.0, **kwargs)


class TestClientRequestShaping:
    def test_an_id_goes_in_the_id_parameter(self):
        client = _client([_FakeResponse(payload={"station": "x"})])
        client.liveboard("BE.NMBS.008813003")
        _, params = client.session.calls[0]
        assert params["id"] == "BE.NMBS.008813003"
        assert "station" not in params

    def test_a_name_goes_in_the_station_parameter(self):
        client = _client([_FakeResponse(payload={"station": "x"})])
        client.liveboard("Leuven")
        _, params = client.session.calls[0]
        assert params["station"] == "Leuven"
        assert "id" not in params

    def test_arrdep_is_explicit(self):
        """Departures is the API default; relying on a default that could change
        upstream would silently turn this pipeline into an arrivals collector."""
        client = _client([_FakeResponse(payload={})])
        client.liveboard("Leuven")
        assert client.session.calls[0][1]["arrdep"] == "departure"

    def test_the_v1_path_is_called_directly(self):
        """The legacy /liveboard/ path answers 303; following it would double
        our request count against the rate limit."""
        client = _client([_FakeResponse(payload={})])
        client.liveboard("Leuven")
        assert client.session.calls[0][0] == "https://api.irail.be/v1/liveboard"

    def test_a_contactable_user_agent_is_sent(self):
        client = _client([_FakeResponse(payload={})])
        assert "RailPulse" in client.session.headers["User-Agent"]

    def test_empty_station_is_rejected_before_a_request_is_made(self):
        client = _client([])
        with pytest.raises(irail.IRailError):
            client.liveboard("   ")
        assert client.session.calls == []


class TestClientRetryPolicy:
    def test_a_429_is_retried_and_then_succeeds(self, monkeypatch):
        monkeypatch.setattr(irail.time, "sleep", lambda _s: None)
        client = _client([
            _FakeResponse(status_code=429, headers={"Retry-After": "1"}),
            _FakeResponse(payload={"station": "ok"}),
        ])
        response = client.liveboard("Leuven")
        assert response.payload == {"station": "ok"}
        assert response.attempts == 2

    def test_a_404_is_not_retried(self):
        """A bad station id is a bug in our request. Retrying it five times is
        abuse of a free service, not resilience."""
        client = _client([_FakeResponse(status_code=404, text="not found")])
        with pytest.raises(irail.IRailError) as caught:
            client.liveboard("BE.NMBS.999999999")
        assert caught.value.status_code == 404
        assert len(client.session.calls) == 1

    def test_a_connection_error_is_retried(self, monkeypatch):
        monkeypatch.setattr(irail.time, "sleep", lambda _s: None)
        client = _client([
            requests.ConnectionError("reset by peer"),
            _FakeResponse(payload={"station": "ok"}),
        ])
        assert client.liveboard("Leuven").attempts == 2

    def test_retries_are_bounded(self, monkeypatch):
        monkeypatch.setattr(irail.time, "sleep", lambda _s: None)
        client = _client([_FakeResponse(status_code=503)] * 3, max_attempts=3)
        with pytest.raises(irail.IRailError):
            client.liveboard("Leuven")
        assert len(client.session.calls) == 3

    def test_a_non_json_200_is_retried_not_parsed(self, monkeypatch):
        """How a captive portal or an outage page announces itself."""
        monkeypatch.setattr(irail.time, "sleep", lambda _s: None)
        client = _client([
            _FakeResponse(status_code=200, payload=None, text="<html>"),
            _FakeResponse(payload={"station": "ok"}),
        ])
        assert client.liveboard("Leuven").payload == {"station": "ok"}

    def test_a_json_array_is_rejected_rather_than_mis_parsed(self):
        client = _client([_FakeResponse(payload=[1, 2, 3])])
        with pytest.raises(irail.IRailError):
            client.liveboard("Leuven")


class TestRetryAfterParsing:
    @pytest.mark.parametrize("header, expected", [
        ("30", 30.0),
        ("0", 0.0),          # a valid instruction: retry immediately
        (None, None),
        ("", None),
        ("not-a-date", None),
        # Capped at MAX_BACKOFF_SECONDS (60 s). We honour the server's wishes,
        # but not past the point where a Consumption-plan function is paying to
        # sleep — and a misconfigured proxy answering with a date three months
        # out must not park the run until the heat death of the sprint.
        ("120", irail.MAX_BACKOFF_SECONDS),
        ("99999", irail.MAX_BACKOFF_SECONDS),
    ])
    def test_delta_seconds_and_garbage(self, header, expected):
        assert irail._parse_retry_after(header) == expected

    def test_an_http_date_in_the_past_yields_zero_not_a_negative_sleep(self):
        assert irail._parse_retry_after(
            "Wed, 21 Oct 2015 07:28:00 GMT") == 0.0

    def test_a_malformed_date_does_not_raise(self):
        """parsedate_to_datetime raises on garbage in Python 3.10+, and letting
        that propagate would turn a cosmetic header bug into a failed run."""
        assert irail._parse_retry_after("Thu, 99 Xxx 2026") is None
