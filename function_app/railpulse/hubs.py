"""The hubs this pipeline polls, and how a request names a station.

WHY THE IDS ARE HARD-CODED AND VERIFIED
iRail resolves ``?station=Brussels-Central`` by name, which is convenient and
fragile: names are localised, several stations share a stem
('Brugge' / 'Brugge-Sint-Pieters', 'Antwerp-Central' / 'Antwerp-Berchem'), and a
mis-resolution shows up not as an error but as a *plausible liveboard for the
wrong station*. Polling by stable id removes that class of bug entirely.

Every UIC code below was resolved against iRail's own /stations feed on
2026-07-27 rather than assumed — which is how one wrong guess was caught:
008844008 is **Verviers-Central**, not Charleroi. Charleroi-Central is
008872009. The test suite pins these ids so a future edit cannot silently
re-introduce that mistake.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config


@dataclass(frozen=True)
class Hub:
    """A station the pipeline polls on a schedule."""

    station_id: str
    label: str
    #: Short note on why this hub is in the default set — the set is a
    #: deliberate sample of the network, not a top-10 by size.
    rationale: str

    @property
    def uic_code(self) -> str:
        return self.station_id.rsplit(".", 1)[-1]


#: The default ingestion set: ten stations chosen to cover the three Brussels
#: cross-city stations (the network's structural bottleneck), the four regional
#: capitals, and both language communities. The brief names Antwerp, Ghent and
#: Liège explicitly; the rest are here so that "is Brussels worse than
#: everywhere else" is answerable rather than assumed.
DEFAULT_HUBS: tuple[Hub, ...] = (
    Hub("BE.NMBS.008813003", "Brussels-Central",
        "Six platforms carrying the whole north-south link; the sprint-1 "
        "analysis identified it as the network's tightest constraint."),
    Hub("BE.NMBS.008814001", "Brussels-South/Midi",
        "International gateway (Eurostar, ICE, EC) — the counterexample to "
        "Central: more platforms, more slack."),
    Hub("BE.NMBS.008812005", "Brussels-North",
        "Third leg of the cross-city axis; a delay at Central propagates here "
        "within minutes, which is what makes the pair interesting."),
    Hub("BE.NMBS.008821006", "Antwerp-Central",
        "Named in the brief. Terminus-shaped operation: trains reverse, so "
        "dwell time absorbs delay differently."),
    Hub("BE.NMBS.008892007", "Ghent-Sint-Pieters",
        "Named in the brief. Busiest station in Flanders by passenger count."),
    Hub("BE.NMBS.008841004", "Liège-Guillemins",
        "Named in the brief. Eastern hub and the ICE entry point."),
    Hub("BE.NMBS.008872009", "Charleroi-Central",
        "Largest Walloon hub. UIC 008872009 — NOT 008844008, which is "
        "Verviers-Central."),
    Hub("BE.NMBS.008833001", "Leuven",
        "High-frequency commuter feed into Brussels; the sprint-1 static data "
        "put it second among morning destinations."),
    Hub("BE.NMBS.008891009", "Brugge",
        "Coastal line terminus — seasonal demand pattern unlike any other hub."),
    Hub("BE.NMBS.008863008", "Namur",
        "Walloon capital and junction of three lines."),
)

#: Extra stations that are interesting but not polled by default. Kept here so
#: that widening coverage is an edit to RAILPULSE_HUBS, not a code change.
OPTIONAL_HUBS: tuple[Hub, ...] = (
    Hub("BE.NMBS.008819406", "Brussels Airport-Zaventem",
        "Airport spur: delay here has a different cost to the passenger."),
    Hub("BE.NMBS.008822004", "Mechelen", "Dense Antwerp-Brussels corridor stop."),
    Hub("BE.NMBS.008896008", "Kortrijk", "West-Flemish junction."),
    Hub("BE.NMBS.008881000", "Mons", "Western Walloon hub."),
    Hub("BE.NMBS.008831005", "Hasselt", "Limburg terminus, no through traffic."),
)

ALL_KNOWN_HUBS: tuple[Hub, ...] = DEFAULT_HUBS + OPTIONAL_HUBS

_BY_ID = {hub.station_id: hub for hub in ALL_KNOWN_HUBS}
_BY_UIC = {hub.uic_code: hub for hub in ALL_KNOWN_HUBS}
_BY_LABEL = {hub.label.casefold(): hub for hub in ALL_KNOWN_HUBS}


def resolve(token: str) -> Hub | None:
    """Best-effort lookup of a known hub by id, UIC code or label."""
    token = token.strip()
    if not token:
        return None
    return (
        _BY_ID.get(token)
        or _BY_UIC.get(token)
        or _BY_LABEL.get(token.casefold())
    )


def configured_hubs() -> tuple[Hub, ...]:
    """The hubs the timer trigger polls.

    Driven by the ``RAILPULSE_HUBS`` Application Setting: a comma-separated
    list of ids, UIC codes or labels. Anything not in :data:`ALL_KNOWN_HUBS` is
    still accepted and passed through to the API as-is, so the setting can
    reach a station this module has never heard of — the point of the setting
    is to make coverage an operational decision, not a deployment.
    """
    if not config.HUBS_SETTING:
        return DEFAULT_HUBS

    resolved: list[Hub] = []
    for token in config.HUBS_SETTING.split(","):
        token = token.strip()
        if not token:
            continue
        known = resolve(token)
        resolved.append(
            known
            if known
            else Hub(token, token, "supplied via the RAILPULSE_HUBS setting")
        )
    return tuple(resolved) or DEFAULT_HUBS
