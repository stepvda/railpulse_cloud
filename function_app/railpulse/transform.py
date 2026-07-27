"""iRail JSON -> typed rows. Pure functions, no I/O, no database, no clock.

WHY THIS MODULE IS ISOLATED
Everything here is a function from a payload to rows. That makes the risky part
of the pipeline — the part that has to cope with a feed where every value is a
string, where a platform can be ``"?"``, and where a "type" is sometimes a line
number — testable against captured payloads without an Azure subscription. The
tests in tests/test_transform.py run offline in milliseconds.

WHAT THE FEED ACTUALLY LOOKS LIKE
A liveboard response nests three different things that all call themselves a
station, and confusing them is the easiest way to publish a wrong dashboard:

    {
      "station": "Brussels-Central",          <- the station you POLLED
      "stationinfo": {...},                   <-   ... and its metadata
      "departures": {"departure": [{
          "station": "Antwerp-Central",       <- this train's TERMINUS
          "stationinfo": {...},               <-   ... and its metadata
          "time": "1785138120",               <- SCHEDULED departure (epoch)
          "delay": "420",                     <- seconds ON TOP of `time`
          "platform": "5", "canceled": "0", "left": "0", "isExtra": "0",
          "vehicle": "BE.NMBS.S11958",
          "vehicleinfo": {"shortname": "S1 1958", "type": "S1", ...},
          "occupancy": {"name": "low"}
      }]}
    }

So `time` is never the actual departure time, and the inner `station` is never
the station you asked about. Both are named explicitly in the output rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from . import config

# ==========================================================================
# UIC country prefixes. The feed has no country field, yet 137 of its 714
# stations are foreign — so without this, every network-wide average silently
# includes Amsterdam and Lille. Digits 3-4 of the UIC code are the country.
# ==========================================================================
UIC_COUNTRY_PREFIXES: Mapping[str, str] = {
    "88": "BE",  # Belgium
    "84": "NL",  # Netherlands
    "80": "DE",  # Germany
    "87": "FR",  # France
    "82": "LU",  # Luxembourg
    "85": "CH",  # Switzerland
    "83": "IT",  # Italy
    "81": "AT",  # Austria
    "70": "GB",  # Great Britain (Eurostar to London)
    "54": "CZ",  # Czechia
    "78": "HR",
    "79": "SI",
    "76": "NO",
    "74": "SE",
    "86": "DK",
    "71": "ES",
    "94": "PT",
    "75": "TR",
    "72": "RS",
}

UNKNOWN_COUNTRY = "XX"

#: The feed's own sentinel for "platform not allocated yet".
UNKNOWN_PLATFORM_TOKEN = "?"

#: Fallback service class when the feed reports none. Matches iRail's own
#: placeholder and is seeded in 04_seed_reference.sql, so the FK holds.
FALLBACK_TYPE_CODE = "TRN"


# ==========================================================================
# Row types. Frozen dataclasses rather than dicts so that a typo in a column
# name is an AttributeError at parse time, not a NULL in the warehouse.
# ==========================================================================
@dataclass(frozen=True)
class StationRow:
    station_id: str
    uic_code: str
    country_code: str
    name: str
    standard_name: str | None
    latitude: float | None
    longitude: float | None
    irail_url: str | None
    is_hub: bool
    seen_utc: datetime


@dataclass(frozen=True)
class VehicleRow:
    vehicle_id: str
    short_name: str | None
    vehicle_number: str | None
    type_raw: str | None
    type_code: str
    service_line: str | None
    irail_url: str | None
    seen_utc: datetime


@dataclass(frozen=True)
class PlatformRow:
    station_id: str
    platform_code: str
    seen_utc: datetime


@dataclass(frozen=True)
class DepartureRow:
    station_id: str
    vehicle_id: str
    scheduled_departure_utc: datetime
    scheduled_departure_local: datetime
    destination_station_id: str | None
    platform_code: str | None
    platform_is_normal: bool | None
    delay_seconds: int
    is_canceled: bool
    has_left: bool
    is_extra: bool
    occupancy: str | None
    departure_connection: str | None
    seen_utc: datetime

    @property
    def natural_key(self) -> tuple[str, str, datetime]:
        return (self.station_id, self.vehicle_id, self.scheduled_departure_utc)


@dataclass
class LiveboardBatch:
    """Everything one liveboard response contributes to the warehouse."""

    station_id: str | None
    station_name: str | None
    feed_timestamp_utc: datetime | None
    stations: list[StationRow] = field(default_factory=list)
    vehicles: list[VehicleRow] = field(default_factory=list)
    platforms: list[PlatformRow] = field(default_factory=list)
    departures: list[DepartureRow] = field(default_factory=list)

    #: Departures returned by the feed, before de-duplication.
    departures_returned: int = 0
    #: Rows dropped because another row in the SAME payload had the same natural
    #: key. Reported rather than hidden: MERGE fails outright on a duplicated
    #: source key, so this is both a correctness guard and a feed-quality signal.
    duplicates_dropped: int = 0
    #: Departures the feed sent that could not be used at all (no vehicle id, no
    #: parseable time). Counted so the loss is visible in the run log.
    unusable_dropped: int = 0
    #: Alert annotations present on departures. Counted only — see README.
    alerts_seen: int = 0


# ==========================================================================
# Scalar coercion. Every one of these takes "whatever the feed sent" and returns
# something bindable, never raising: one malformed field must not cost a poll.
# ==========================================================================
def _text(value: Any, *, limit: int | None = None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit] if limit else text


def _int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _float(value: Any) -> float | None:
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    # The feed uses "0" for "unknown" on vehicle coordinates. For a *station*
    # 0.0 is not a plausible Belgian coordinate either, so treat it as missing
    # rather than placing the station in the Gulf of Guinea.
    return None if result == 0.0 else result


def _flag(value: Any, default: bool | None = None) -> bool | None:
    """Feed booleans are the strings "0"/"1"; be liberal about the rest."""
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes"}:
        return True
    if text in {"0", "false", "no"}:
        return False
    return default


def _epoch_to_utc(value: Any) -> datetime | None:
    """POSIX epoch -> naive UTC datetime, or None if unusable.

    Naive-in-UTC rather than tz-aware because the destination column is
    DATETIME2, which stores no offset: binding an aware value would rely on the
    driver's conversion rules, and the one thing a timestamp column must not be
    is driver-dependent.
    """
    epoch = _int(value)
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def _to_local(moment: datetime) -> datetime:
    """Naive UTC -> naive Europe/Brussels, via the real tz database.

    A fixed +1 offset would be wrong for half the year, and a fixed +2 for the
    other half; either way the "busiest hour" answer would move.
    """
    return (
        moment.replace(tzinfo=timezone.utc)
        .astimezone(config.LOCAL_TIMEZONE)
        .replace(tzinfo=None)
    )


def country_from_uic(uic_code: str | None) -> str:
    """'008400319' -> 'NL'. Digits 3-4 are the UIC country prefix."""
    text = (uic_code or "").strip()
    if len(text) < 4:
        return UNKNOWN_COUNTRY
    return UIC_COUNTRY_PREFIXES.get(text[2:4], UNKNOWN_COUNTRY)


def uic_from_station_id(station_id: str | None) -> str:
    """'BE.NMBS.008813003' -> '008813003'.

    Left-padded to nine characters because `stations.uic_code` is CHAR(9) and a
    short code there would compare unequal to a padded one elsewhere.
    """
    tail = (station_id or "").rsplit(".", 1)[-1].strip()
    return tail.rjust(9, "0")[:9] if tail else ""


def split_vehicle_type(type_raw: str | None) -> tuple[str, str | None]:
    """Split a published type into (family code, service line).

    The feed reports 'IC', 'L', 'EUR' — but for suburban services it reports the
    *line*: 'S1', 'S10', 'S32'. Left as-is, every new S-line would become a new
    "type", and "how do suburban trains perform overall" would need a LIKE scan.

        'S32' -> ('S', '32')      'IC' -> ('IC', None)
        'S'   -> ('S', None)      ''   -> ('TRN', None)
    """
    text = (type_raw or "").strip().upper()
    if not text:
        return FALLBACK_TYPE_CODE, None
    if text.startswith("S") and text[1:].isdigit():
        return "S", text[1:][:8]
    return text[:8], None


# ==========================================================================
# Station catalogue (/v1/stations)
# ==========================================================================
def parse_station_catalogue(
    payload: Mapping[str, Any], *, seen_utc: datetime,
    hub_ids: Iterable[str] = (),
) -> list[StationRow]:
    """Parse the full station list. One call, ~714 rows, coordinates included.

    Seeding from this rather than only from liveboards means the station
    dimension is complete on day one — which is what lets a Power BI map show
    the whole network instead of only the places we happen to have polled.
    """
    hubs = set(hub_ids)
    rows: dict[str, StationRow] = {}
    for entry in payload.get("station") or []:
        if not isinstance(entry, Mapping):
            continue
        row = _station_row(entry, seen_utc=seen_utc, is_hub=False)
        if row is None:
            continue
        if row.station_id in hubs:
            row = replace(row, is_hub=True)
        rows[row.station_id] = row      # last wins; ids are unique in practice
    return list(rows.values())


def _station_row(
    info: Mapping[str, Any], *, seen_utc: datetime, is_hub: bool
) -> StationRow | None:
    """Build a StationRow from any `stationinfo`-shaped object."""
    station_id = _text(info.get("id"), limit=24)
    if not station_id:
        return None
    # `name` is localised by the lang parameter; `standardname` is the
    # operator's official (for Brussels, bilingual) form. Falling back keeps
    # NOT NULL satisfied without inventing a name.
    name = _text(info.get("name"), limit=120) or _text(
        info.get("standardname"), limit=120) or station_id
    uic = uic_from_station_id(station_id)
    return StationRow(
        station_id=station_id,
        uic_code=uic,
        country_code=country_from_uic(uic),
        name=name,
        standard_name=_text(info.get("standardname"), limit=120),
        # locationY is latitude and locationX is longitude — the reverse of the
        # x/y intuition, and worth naming explicitly at the boundary.
        latitude=_float(info.get("locationY")),
        longitude=_float(info.get("locationX")),
        irail_url=_text(info.get("@id"), limit=200),
        is_hub=is_hub,
        seen_utc=seen_utc,
    )


# ==========================================================================
# Liveboard (/v1/liveboard)
# ==========================================================================
def parse_liveboard(
    payload: Mapping[str, Any], *, seen_utc: datetime, is_hub: bool = True
) -> LiveboardBatch:
    """Turn one liveboard response into the rows it implies.

    De-duplicates on the fact table's natural key
    (station, vehicle, scheduled time), keeping the LAST occurrence, and reports
    how many it dropped. This is not defensive decoration: SQL Server's MERGE
    raises error 8672 and abandons the whole statement if two source rows match
    the same target row, so a payload that ever repeats a departure would take
    down the entire load rather than that one row.
    """
    station_info = payload.get("stationinfo")
    origin = (
        _station_row(station_info, seen_utc=seen_utc, is_hub=is_hub)
        if isinstance(station_info, Mapping) else None
    )

    batch = LiveboardBatch(
        station_id=origin.station_id if origin else None,
        station_name=(origin.name if origin
                      else _text(payload.get("station"), limit=120)),
        feed_timestamp_utc=_epoch_to_utc(payload.get("timestamp")),
    )
    if origin is None:
        # No station identity means nothing can be keyed. Return the empty batch
        # and let the caller record a run with zero rows — an honest "the feed
        # answered but said nothing usable" is better than a crash.
        return batch

    stations: dict[str, StationRow] = {origin.station_id: origin}
    vehicles: dict[str, VehicleRow] = {}
    platforms: dict[tuple[str, str], PlatformRow] = {}
    departures: dict[tuple[str, str, datetime], DepartureRow] = {}

    raw_departures = _departure_entries(payload)
    batch.departures_returned = len(raw_departures)

    for entry in raw_departures:
        if not isinstance(entry, Mapping):
            batch.unusable_dropped += 1
            continue
        if entry.get("alerts"):
            batch.alerts_seen += 1

        scheduled_utc = _epoch_to_utc(entry.get("time"))
        vehicle = _vehicle_row(entry, seen_utc=seen_utc)
        if scheduled_utc is None or vehicle is None:
            # Without a time or a vehicle there is no natural key, so the row
            # could not be de-duplicated or updated later even if stored.
            batch.unusable_dropped += 1
            continue
        vehicles[vehicle.vehicle_id] = vehicle

        destination = (
            _station_row(entry["stationinfo"], seen_utc=seen_utc, is_hub=False)
            if isinstance(entry.get("stationinfo"), Mapping) else None
        )
        if destination is not None:
            # setdefault: never let a destination sighting downgrade the hub
            # flag on a station already recorded as the polled origin.
            stations.setdefault(destination.station_id, destination)

        platform_info = entry.get("platforminfo")
        platform_info = platform_info if isinstance(platform_info, Mapping) else {}
        platform_code = _text(platform_info.get("name")
                              or entry.get("platform"), limit=8)
        if platform_code == UNKNOWN_PLATFORM_TOKEN:
            platform_code = None
        if platform_code:
            platforms[(origin.station_id, platform_code)] = PlatformRow(
                station_id=origin.station_id,
                platform_code=platform_code,
                seen_utc=seen_utc,
            )

        occupancy_info = entry.get("occupancy")
        occupancy = (
            _text(occupancy_info.get("name"), limit=10)
            if isinstance(occupancy_info, Mapping) else _text(occupancy_info, limit=10)
        )

        row = DepartureRow(
            station_id=origin.station_id,
            vehicle_id=vehicle.vehicle_id,
            scheduled_departure_utc=scheduled_utc,
            scheduled_departure_local=_to_local(scheduled_utc),
            destination_station_id=(destination.station_id if destination else None),
            platform_code=platform_code,
            platform_is_normal=_flag(platform_info.get("normal")),
            # Absent delay means "no reported delay", i.e. 0 — not unknown. The
            # column is NOT NULL for exactly this reason: a NULL here would
            # silently vanish from every AVG().
            delay_seconds=_int(entry.get("delay"), 0) or 0,
            is_canceled=bool(_flag(entry.get("canceled"), False)),
            has_left=bool(_flag(entry.get("left"), False)),
            is_extra=bool(_flag(entry.get("isExtra"), False)),
            occupancy=(occupancy.lower() if occupancy else None),
            departure_connection=_text(entry.get("departureConnection"), limit=200),
            seen_utc=seen_utc,
        )
        if row.natural_key in departures:
            batch.duplicates_dropped += 1
        departures[row.natural_key] = row

    batch.stations = list(stations.values())
    batch.vehicles = list(vehicles.values())
    batch.platforms = list(platforms.values())
    batch.departures = list(departures.values())
    return batch


def _departure_entries(payload: Mapping[str, Any]) -> list[Any]:
    """Extract the departure array, tolerating the feed's shape variations.

    ``departures`` is normally ``{"number": "56", "departure": [...]}`` but is
    absent at a station with nothing scheduled, and iRail has historically
    collapsed a single-element array to a bare object. Both must load as 0 and 1
    rows respectively, not as an exception.
    """
    container = payload.get("departures")
    if not isinstance(container, Mapping):
        return []
    entries = container.get("departure")
    if entries is None:
        return []
    if isinstance(entries, Mapping):
        return [entries]
    if isinstance(entries, list):
        return entries
    return []


def _vehicle_row(entry: Mapping[str, Any], *, seen_utc: datetime) -> VehicleRow | None:
    info = entry.get("vehicleinfo")
    info = info if isinstance(info, Mapping) else {}
    vehicle_id = _text(info.get("name") or entry.get("vehicle"), limit=40)
    if not vehicle_id:
        return None
    type_raw = _text(info.get("type"), limit=12)
    type_code, service_line = split_vehicle_type(type_raw)
    return VehicleRow(
        vehicle_id=vehicle_id,
        short_name=_text(info.get("shortname"), limit=40),
        vehicle_number=_text(info.get("number"), limit=12),
        type_raw=type_raw,
        type_code=type_code,
        service_line=service_line,
        irail_url=_text(info.get("@id"), limit=200),
        seen_utc=seen_utc,
    )
