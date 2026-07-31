"""RailPulse Cloud — the live dashboard over Azure SQL.

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT
It is a **renderer**, exactly as sprint 1's dashboard was. It contains no
analysis. Every figure on every page is produced by a statement in queries.py
running on SQL Server, and every one of those statements reads a **view** from
sql/03_views.sql — where this project's definitions live (what counts as on time,
whether a cancellation belongs in the punctuality denominator, which local hour a
departure falls in). The dashboard cannot disagree with the warehouse because it
never computes anything itself.

`pandas` appears in exactly one role: carrying already-aggregated rows from the
driver to Streamlit and Altair. There is no groupby, no merge, no pivot and no
boolean-mask filtering anywhere in this file. The two `.astype(float)` calls are
type coercion for a chart library that will not accept `Decimal`, and they are
commented as such.

WHAT IS NEW COMPARED WITH SPRINT 1
Sprint 1 reported on a static timetable: 2.17 M scheduled departures, no delays,
no cancellations, no platform changes. This reports on what actually happened,
which makes four questions askable for the first time:

  * punctuality at all — the static feed contains no delay data;
  * **delay evolution**, because the pipeline polls the same departure repeatedly
    and keeps its first and latest reading;
  * disruptions — cancellations and trains moved off their booked platform;
  * whether the pipeline itself is healthy, which is a question a dashboard
    reading a file on disk never has to ask.

The database is opened read-only by construction: data.query refuses anything
that is not a SELECT. The one state-changing control in the whole app is the
"run ingest now" button on the Pipeline page, which calls the Function App's own
key-protected endpoint over HTTPS — the key lives in an App Service setting and
never reaches the browser.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import altair as alt
import pandas as pd
import requests
import streamlit as st

import queries
from data import credentials, run_sql, scalar

st.set_page_config(
    page_title="RailPulse Cloud — live rail delays",
    page_icon="🚉",
    layout="wide",
    initial_sidebar_state="expanded",
)

#: Set by azure/provision_webapp.sh so the Pipeline page can trigger an ingest.
#: Absent locally, in which case the button explains itself and does nothing.
FUNCTION_APP_URL = os.environ.get("FUNCTION_APP_URL", "").strip().rstrip("/")
FUNCTION_KEY = os.environ.get("FUNCTION_KEY", "").strip()

BRUSSELS_CENTRAL = "BE.NMBS.008813003"


# ==========================================================================
# Presentation helpers
# ==========================================================================
def show_sql(sql: str, params: tuple = (), *, label: str = "Show the SQL") -> None:
    """Render the exact statement behind the figure above it.

    Carried over from sprint 1's dashboard, and the reason both are defensible:
    any number here can be checked by pasting the statement into the portal's
    Query editor. Bound values are listed separately rather than interpolated,
    because that is how they reach the server.
    """
    with st.expander(label):
        st.code(sql.strip(), language="sql")
        if params:
            st.caption(f"Bound parameters: `{params}`")


def kpis(items: list[tuple[str, str, str | None]]) -> None:
    """A row of metrics. Each item is (label, value, help)."""
    columns = st.columns(len(items))
    for column, (label, value, helptext) in zip(columns, items):
        column.metric(label, value, help=helptext)


def number(value, suffix: str = "", digits: int = 0, dash: str = "—") -> str:
    """Format a scalar for a metric tile, tolerating None and NaN."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return dash
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)
    formatted = f"{numeric:,.{digits}f}" if digits else f"{numeric:,.0f}"
    return f"{formatted}{suffix}"


def empty_warehouse_notice() -> bool:
    """Explain an empty warehouse instead of rendering a page full of dashes.

    Returns True when there is nothing to show. Before the first ingest this is
    the normal state, and a dashboard that raises here is one nobody can use to
    find out why it is empty.
    """
    counts = run_sql(queries.TABLE_COUNTS)
    departures = 0
    if not counts.empty:
        matching = counts.loc[counts["table_name"] == "liveboard_records", "row_count"]
        departures = int(matching.iloc[0]) if len(matching) else 0
    if departures:
        return False
    st.info(
        "**No departures collected yet.** The warehouse schema exists but "
        "`liveboard_records` is empty.\n\n"
        "Trigger a first load from the **Pipeline** page, or run "
        "`make ingest` from the repository.",
        icon="🪹",
    )
    return True


@st.cache_data(ttl=300, show_spinner=False)
def station_options() -> pd.DataFrame:
    return run_sql(queries.ORIGIN_STATIONS)


def station_picker(key: str, *, all_label: str = "All hubs") -> tuple[str, str]:
    """Sidebar-style station selector. Returns (station_id, display name).

    An empty station_id means "no filter" — the queries are written so that a
    bound empty string disables their WHERE clause, which keeps one statement
    doing both jobs instead of concatenating SQL.
    """
    frame = station_options()
    labels = [all_label] + frame["station_name"].tolist()
    chosen = st.selectbox("Station", labels, key=key)
    if chosen == all_label:
        return "", all_label
    row = frame.loc[frame["station_name"] == chosen].iloc[0]
    return str(row["station_id"]), chosen


# ==========================================================================
# Pages
# ==========================================================================
def page_overview() -> None:
    st.title("🚉 RailPulse Cloud")
    st.caption(
        "Live departure boards from the Belgian rail network, ingested every "
        "15 minutes through the weekday peaks by an Azure Function and "
        "normalised into Azure SQL."
    )

    if empty_warehouse_notice():
        return

    header = run_sql(queries.KPI_HEADER)
    kpis([
        ("Departures observed", number(scalar(header, "departures")),
         "One row per scheduled departure event, deduplicated on "
         "(station, vehicle, scheduled time)."),
        ("On time (<6 min)", number(scalar(header, "pct_on_time_6min"), "%", 1),
         "SNCB's own published threshold. Cancellations are excluded from the "
         "denominator — a cancelled train is absent, not late."),
        ("On time (<2 min)", number(scalar(header, "pct_on_time_2min"), "%", 1),
         "The stricter threshold used in sprint 1, shown alongside so neither "
         "definition is silently picked for you."),
        ("Mean delay", number(scalar(header, "avg_delay_seconds"), " s", 1),
         "Running trains only."),
        ("Cancellations", number(scalar(header, "cancellations")), None),
        ("Platform changes", number(scalar(header, "platform_changes")),
         "Trains moved off their booked platform — a disruption signal that "
         "exists only in the live feed, not in the timetable."),
    ])

    earliest, latest = scalar(header, "earliest_local"), scalar(header, "latest_local")
    st.caption(
        f"Covering **{number(scalar(header, 'stations_covered'))} hubs** · "
        f"**{number(scalar(header, 'distinct_vehicles'))} distinct services** · "
        f"**{number(scalar(header, 'days_covered'))} day(s)** · "
        f"scheduled departures from `{earliest}` to `{latest}` (Europe/Brussels)."
    )
    show_sql(queries.KPI_HEADER)

    left, right = st.columns(2)

    with left:
        st.subheader("Departures by local hour")
        hourly = run_sql(queries.OVERVIEW_BY_HOUR)
        if hourly.empty:
            st.caption("No data yet.")
        else:
            st.altair_chart(
                alt.Chart(hourly).mark_bar().encode(
                    x=alt.X("hour_local:O", title="Hour (Europe/Brussels)"),
                    y=alt.Y("departures:Q", title="Departures observed"),
                    tooltip=["hour_local", "departures", "departures_per_day",
                             "avg_delay_seconds"],
                ).properties(height=280),
                use_container_width=True,
            )
            st.caption(
                "Counts, not a peak-hour ranking. The timer samples the weekday "
                "peaks harder than the small hours, so ranking raw counts would "
                "report the *capture schedule* as the peak — see the Peak hours "
                "page, which normalises by days observed."
            )
        show_sql(queries.OVERVIEW_BY_HOUR)

    with right:
        st.subheader("Delay distribution")
        buckets = run_sql(queries.DELAY_BUCKETS)
        if buckets.empty:
            st.caption("No data yet.")
        else:
            st.altair_chart(
                alt.Chart(buckets).mark_bar().encode(
                    x=alt.X("departures:Q", title="Departures"),
                    y=alt.Y("delay_bucket:N", title=None,
                            sort=alt.EncodingSortField(field="delay_bucket_order")),
                    tooltip=["delay_bucket", "departures"],
                ).properties(height=280),
                use_container_width=True,
            )
            st.caption(
                "Buckets are sorted by severity, not alphabetically — the "
                "`delay_bucket_order` companion column exists for exactly that."
            )
        show_sql(queries.DELAY_BUCKETS)

    st.subheader("The network")
    hubs_only = st.toggle("Polled hubs only", value=False, key="map_hubs")
    stations = run_sql(queries.STATION_MAP, (1 if hubs_only else 0,))
    if stations.empty:
        st.caption("No station coordinates — run the station seed first.")
    else:
        # Type coercion for the map widget, which rejects Decimal. Not analysis.
        plottable = stations.assign(
            latitude=stations["latitude"].astype(float),
            longitude=stations["longitude"].astype(float),
        )
        st.map(plottable, latitude="latitude", longitude="longitude", size=120)
        st.caption(
            f"{len(stations):,} stations with coordinates. The dimension is "
            "seeded from iRail's full catalogue, so it covers the whole network "
            "and not just the ten polled hubs."
        )
    show_sql(queries.STATION_MAP, (1 if hubs_only else 0,))


def page_live() -> None:
    st.title("Live departures")
    st.caption(
        "One row per scheduled departure event. Repeated polls revise a row "
        "rather than duplicating it, so `observation_count` is how many times "
        "the pipeline has seen this departure."
    )
    if empty_warehouse_notice():
        return

    controls = st.columns([2, 1, 1, 1])
    with controls[0]:
        station_id, station_name = station_picker("live_station")
    with controls[1]:
        limit = st.number_input("Rows", min_value=25, max_value=1000, value=200, step=25)
    with controls[2]:
        cancelled_only = st.toggle("Cancelled only", value=False)
    with controls[3]:
        delayed_only = st.toggle("Late by 6+ min", value=False)

    params = (int(limit), station_id, station_id,
              1 if cancelled_only else 0, 1 if delayed_only else 0)
    frame = run_sql(queries.LIVE_DEPARTURES, params)

    st.caption(f"**{len(frame):,} row(s)** · {station_name}")
    st.dataframe(
        frame,
        use_container_width=True,
        hide_index=True,
        column_config={
            "scheduled_departure_local": st.column_config.DatetimeColumn(
                "Scheduled (local)", format="YYYY-MM-DD HH:mm"),
            "delay_minutes": st.column_config.NumberColumn("Delay (min)", format="%.1f"),
            "is_canceled": st.column_config.CheckboxColumn("Cancelled"),
            "platform_is_normal": st.column_config.CheckboxColumn("Booked platform"),
            "delay_growth_s": st.column_config.NumberColumn(
                "Delay growth (s)",
                help="Latest delay minus the delay when first seen."),
            "observation_count": st.column_config.NumberColumn("Seen"),
            "last_seen_utc": st.column_config.DatetimeColumn(
                "Last seen (UTC)", format="YYYY-MM-DD HH:mm"),
        },
    )
    st.caption(
        "`Booked platform` unticked means the train was moved off its scheduled "
        "platform. An empty platform is shown as `unknown` — the feed reports "
        "`?` when none has been allocated yet, and dropping those rows would "
        "understate the station total."
    )
    show_sql(queries.LIVE_DEPARTURES, params)


def page_leaderboard() -> None:
    st.title("Hub leaderboard")
    st.caption(
        "Which city runs the most reliable station. This question was "
        "unanswerable in sprint 1 — a static timetable contains no delays."
    )
    if empty_warehouse_notice():
        return

    board = run_sql(queries.HUB_LEADERBOARD)
    if board.empty:
        st.caption("No hub data yet.")
        return

    st.dataframe(
        board, use_container_width=True, hide_index=True,
        column_config={
            "pct_on_time_6min": st.column_config.ProgressColumn(
                "On time <6 min", format="%.2f%%", min_value=0, max_value=100),
            "pct_on_time_2min": st.column_config.NumberColumn(
                "On time <2 min", format="%.2f%%"),
            "avg_delay_seconds": st.column_config.NumberColumn(
                "Mean delay (s)", format="%.1f"),
            "reliability_score": st.column_config.NumberColumn(
                "Reliability", format="%.2f",
                help="On-time count minus cancellations, over trains measured. "
                     "Charges a cancellation the same as a 6-minute delay, "
                     "because to a passenger it is worse."),
        },
    )
    st.caption(
        "**Three definitions decide this ranking, and all three are visible.** "
        "Cancellations are excluded from the delay average and from the on-time "
        "denominator (they are absences, not late trains) and counted in their "
        "own column; \"on time\" is <6 min, SNCB's published threshold, with the "
        "sprint-1 2-minute figure beside it; and the reliability score's "
        "weighting is stated rather than buried."
    )
    show_sql(queries.HUB_LEADERBOARD)

    left, right = st.columns(2)
    with left:
        st.subheader("Punctuality")
        st.altair_chart(
            alt.Chart(board).mark_bar().encode(
                x=alt.X("pct_on_time_6min:Q", title="% on time (<6 min)",
                        scale=alt.Scale(domain=[0, 100])),
                y=alt.Y("station_name:N", title=None, sort="-x"),
                tooltip=["station_name", "pct_on_time_6min", "departures"],
            ).properties(height=320),
            use_container_width=True,
        )
    with right:
        st.subheader("Volume against mean delay")
        st.altair_chart(
            alt.Chart(board).mark_circle(size=160).encode(
                x=alt.X("departures:Q", title="Departures observed"),
                y=alt.Y("avg_delay_seconds:Q", title="Mean delay (s)"),
                tooltip=["station_name", "departures", "avg_delay_seconds",
                         "pct_on_time_6min"],
            ).properties(height=320)
            + alt.Chart(board).mark_text(align="left", dx=10, fontSize=11).encode(
                x="departures:Q", y="avg_delay_seconds:Q", text="station_name:N"),
            use_container_width=True,
        )
        st.caption(
            "If busy stations were simply less punctual, this would trend "
            "upwards. With a short capture window, treat it as a hypothesis "
            "rather than a finding."
        )

    st.subheader("Day by day")
    trend = run_sql(queries.HUB_TREND)
    if trend.empty or trend["departure_date_local"].nunique() < 2:
        st.caption(
            "A trend needs at least two days of capture. Come back tomorrow — "
            "the timer is accumulating history in the meantime."
        )
    else:
        st.altair_chart(
            alt.Chart(trend).mark_line(point=True).encode(
                x=alt.X("departure_date_local:T", title=None),
                y=alt.Y("pct_on_time_6min:Q", title="% on time (<6 min)"),
                color=alt.Color("station_name:N", title="Station"),
                tooltip=["station_name", "departure_date_local",
                         "pct_on_time_6min", "departures_observed"],
            ).properties(height=340),
            use_container_width=True,
        )
    show_sql(queries.HUB_TREND)


def page_peak_hours() -> None:
    st.title("Peak hours")
    st.caption("The live continuation of sprint 1's Q1 — with a different trap.")
    if empty_warehouse_notice():
        return

    st.info(
        "**Rank on departures per day, never on the raw count.** The timer "
        "samples the weekday peaks every 15 minutes and does not sample the "
        "small hours at all, so a `COUNT(*)` per hour would report the capture "
        "schedule as the peak and be perfectly circular. `days_observed` is "
        "shown so the coverage behind every figure is visible.",
        icon="⚠️",
    )

    station_id, station_name = station_picker("peak_station")
    frame = run_sql(queries.HOURLY_PRESSURE, (station_id, station_id))
    if frame.empty:
        st.caption("No data yet.")
        return

    st.altair_chart(
        alt.Chart(frame).mark_bar().encode(
            x=alt.X("hour_local:O", title="Hour (Europe/Brussels)"),
            y=alt.Y("departures_per_day:Q", title="Departures per day observed"),
            color=alt.Color("day_type:N", title="Day type"),
            xOffset="day_type:N",
            tooltip=["hour_local", "day_type", "departures", "days_observed",
                     "departures_per_day", "avg_delay_seconds"],
        ).properties(height=340, title=f"{station_name} — normalised by days observed"),
        use_container_width=True,
    )

    st.altair_chart(
        alt.Chart(frame).mark_line(point=True).encode(
            x=alt.X("hour_local:O", title="Hour (Europe/Brussels)"),
            y=alt.Y("avg_delay_seconds:Q", title="Mean delay (s)"),
            color=alt.Color("day_type:N", title="Day type"),
            tooltip=["hour_local", "day_type", "avg_delay_seconds",
                     "pct_on_time_6min"],
        ).properties(height=280, title="Does the busiest hour also run the latest?"),
        use_container_width=True,
    )
    st.caption(
        "If the worst delays fall **outside** the busiest hour, congestion is "
        "not the cause and adding peak capacity would not fix it."
    )

    st.dataframe(frame, use_container_width=True, hide_index=True)
    show_sql(queries.HOURLY_PRESSURE, (station_id, station_id))


def page_platforms() -> None:
    st.title("Platform bottlenecks")
    st.caption(
        "Sprint 1 found Brussels-Central handling more annual departures than "
        "Brussels-Midi across six platforms instead of twenty-one — a 3.9× "
        "pressure differential. Here is what live data says."
    )
    if empty_warehouse_notice():
        return

    frame = station_options()
    default_index = 0
    if not frame.empty:
        matches = frame.index[frame["station_id"] == BRUSSELS_CENTRAL].tolist()
        default_index = int(matches[0]) if matches else 0
    chosen = st.selectbox(
        "Station", frame["station_name"].tolist() if not frame.empty else [],
        index=default_index, key="platform_station",
    )
    if not chosen:
        st.caption("No stations yet.")
        return
    station_id = str(frame.loc[frame["station_name"] == chosen, "station_id"].iloc[0])

    platforms = run_sql(queries.PLATFORM_PRESSURE, (station_id,))
    if platforms.empty:
        st.caption("No platform observations for this station yet.")
        return

    st.altair_chart(
        alt.Chart(platforms).mark_bar().encode(
            x=alt.X("platform_label:N", title="Platform", sort="-y"),
            y=alt.Y("departures:Q", title="Departures observed"),
            color=alt.Color("avg_delay_seconds:Q", title="Mean delay (s)",
                            scale=alt.Scale(scheme="orangered")),
            tooltip=["platform_label", "departures", "departures_per_day",
                     "peak_hour_departures", "avg_delay_seconds",
                     "pct_on_time_6min", "platform_changes"],
        ).properties(height=320, title=f"{chosen} — load and mean delay by platform"),
        use_container_width=True,
    )
    st.dataframe(
        platforms, use_container_width=True, hide_index=True,
        column_config={
            "peak_hour_departures": st.column_config.NumberColumn(
                "Busiest hour",
                help="Most departures this platform handled in a single hour — "
                     "the number that decides whether a platform is a "
                     "bottleneck as opposed to merely busy across the day."),
            "pct_of_station": st.column_config.NumberColumn(
                "% of station", format="%.2f%%"),
        },
    )
    show_sql(queries.PLATFORM_PRESSURE, (station_id,))

    st.subheader("The three Brussels stations")
    comparison = run_sql(queries.BRUSSELS_COMPARISON)
    if comparison.empty:
        st.caption("Not enough Brussels data yet.")
    else:
        st.dataframe(
            comparison, use_container_width=True, hide_index=True,
            column_config={
                "departures_per_platform": st.column_config.NumberColumn(
                    "Departures / platform", format="%.2f",
                    help="The pressure differential. Platforms *in use*, not a "
                         "published inventory — the feed publishes none."),
            },
        )
    show_sql(queries.BRUSSELS_COMPARISON)


def page_delay_evolution() -> None:
    st.title("Delay evolution")
    st.caption(
        "Questions that exist only because the pipeline polls the same "
        "departure repeatedly and keeps both its first and its latest reading."
    )
    if empty_warehouse_notice():
        return

    st.info(
        "`delay_first_seen_s` is the delay the **first** time a departure "
        "appeared on a liveboard; `delay_seconds` is the **latest** reading. "
        "The difference measures how a delay developed as departure approached — "
        "the difference between \"the 17:42 was late\" and \"the 17:42 was on "
        "time until 20 minutes before it left\".",
        icon="🕰️",
    )

    trajectory = run_sql(queries.DELAY_TRAJECTORY)
    if trajectory.empty:
        st.caption("No data yet.")
        return

    left, right = st.columns([1, 1])
    with left:
        st.altair_chart(
            alt.Chart(trajectory).mark_arc(innerRadius=60).encode(
                theta=alt.Theta("departures:Q"),
                color=alt.Color("trajectory:N", title="Trajectory"),
                tooltip=["trajectory", "departures", "pct_of_all",
                         "avg_first_delay_s", "avg_final_delay_s"],
            ).properties(height=320),
            use_container_width=True,
        )
    with right:
        st.dataframe(
            trajectory, use_container_width=True, hide_index=True,
            column_config={
                "pct_of_all": st.column_config.NumberColumn(
                    "% of all", format="%.2f%%"),
            },
        )
        st.caption(
            "`seen once` means the departure had no chance to be revised — a "
            "high share there means the capture window is too narrow to measure "
            "evolution, and is reported rather than hidden."
        )
    show_sql(queries.DELAY_TRAJECTORY)

    st.subheader("Worst deteriorations")
    st.caption(
        "How much warning did passengers get? A train announced on time that "
        "left 15 minutes late is a different failure from one flagged an hour "
        "ahead."
    )
    limit = st.slider("Rows", 5, 100, 25, key="deterioration_rows")
    worst = run_sql(queries.WORST_DETERIORATIONS, (int(limit),))
    if worst.empty:
        st.caption(
            "No departure has yet been seen twice with a growing delay. This "
            "needs at least two polls covering the same departure — about "
            "15 minutes of pipeline uptime inside a capture window."
        )
    else:
        st.dataframe(
            worst, use_container_width=True, hide_index=True,
            column_config={
                "minutes_of_notice": st.column_config.NumberColumn(
                    "Notice (min)",
                    help="Minutes between our first sighting and the scheduled "
                         "departure."),
                "growth_min": st.column_config.NumberColumn(
                    "Growth (min)", format="%.1f"),
            },
        )
    show_sql(queries.WORST_DETERIORATIONS, (int(limit),))

    st.subheader("Repeat offenders")
    st.caption(
        "One bad day is weather. The same train number late on several days is "
        "a timetabling problem — this is the query that tells them apart."
    )
    offenders = run_sql(queries.REPEAT_OFFENDERS, (20,))
    st.dataframe(offenders, use_container_width=True, hide_index=True)
    show_sql(queries.REPEAT_OFFENDERS, (20,))


def page_services() -> None:
    st.title("Services and destinations")
    if empty_warehouse_notice():
        return

    st.subheader("Service class performance")
    st.caption("Does an InterCity keep time better than an S-train?")
    types = run_sql(queries.VEHICLE_TYPE_PERFORMANCE)
    if not types.empty:
        st.altair_chart(
            alt.Chart(types).mark_circle().encode(
                x=alt.X("departures:Q", title="Departures observed"),
                y=alt.Y("avg_delay_seconds:Q", title="Mean delay (s)"),
                size=alt.Size("distinct_vehicles:Q", title="Distinct services"),
                color=alt.Color("vehicle_type:N", title="Class"),
                tooltip=["vehicle_type", "type_code", "departures",
                         "distinct_vehicles", "avg_delay_seconds",
                         "pct_on_time_6min", "pct_cancelled"],
            ).properties(height=320),
            use_container_width=True,
        )
        st.dataframe(
            types, use_container_width=True, hide_index=True,
            column_config={
                "type_is_documented": st.column_config.CheckboxColumn(
                    "Documented",
                    help="Unticked means the loader discovered this class and "
                         "sql/04_seed_reference.sql does not describe it yet. "
                         "A new service class must never break the ingest, so "
                         "it is admitted and flagged rather than rejected."),
            },
        )
    show_sql(queries.VEHICLE_TYPE_PERFORMANCE)

    st.subheader("Busiest destinations")
    morning_only = st.toggle(
        "Morning only (before 12:00 local)", value=False,
        help="Sprint 1's Q3 asked exactly this of the static timetable and "
             "answered Antwerp-Central, Leuven, Charleroi-Central.",
    )
    limit = st.slider("Rows", 5, 50, 15, key="destination_rows")
    params = (int(limit), 1 if morning_only else 0)
    destinations = run_sql(queries.TOP_DESTINATIONS, params)
    if not destinations.empty:
        st.altair_chart(
            alt.Chart(destinations).mark_bar().encode(
                x=alt.X("departures:Q", title="Departures"),
                y=alt.Y("destination_name:N", title=None, sort="-x"),
                color=alt.Color("destination_country:N", title="Country"),
                tooltip=["destination_name", "destination_country", "departures",
                         "served_from_hubs", "avg_delay_seconds",
                         "pct_on_time_6min"],
            ).properties(height=28 * max(len(destinations), 4)),
            use_container_width=True,
        )
        st.dataframe(destinations, use_container_width=True, hide_index=True)
        st.caption(
            "`country` is derived from UIC digits 3-4 — the feed carries no "
            "country field, yet 137 of its 714 stations are foreign, so without "
            "it every network average would quietly include Amsterdam and Lille."
        )
    show_sql(queries.TOP_DESTINATIONS, params)


def page_quality() -> None:
    st.title("Data quality")
    st.caption(
        "Every dataset has holes. The difference between a trustworthy one and "
        "a misleading one is whether the holes are measured — so this page is "
        "meant to be read, not hidden."
    )

    counts = run_sql(queries.TABLE_COUNTS)
    if not counts.empty:
        kpis([(str(row.table_name), number(row.row_count), None)
              for row in counts.itertuples()])
    show_sql(queries.TABLE_COUNTS)

    quality = run_sql(queries.DATA_QUALITY)
    if quality.empty or not scalar(quality, "row_count"):
        st.info("No departures collected yet.", icon="🪹")
        return

    st.subheader("Completeness")
    kpis([
        ("Unknown platform", number(scalar(quality, "pct_platform_unknown"), "%", 2),
         "The feed reports `?` when no platform has been allocated yet. Stored "
         "as NULL, reported as 'unknown', never dropped."),
        ("Occupancy unreported", number(scalar(quality, "pct_occupancy_unknown"), "%", 2),
         "Occupancy is crowd-sourced from iRail's app users, so it is sparse "
         "and self-selecting. This is the number that says whether it is worth "
         "reading at all."),
        ("Destination missing", number(scalar(quality, "destination_missing")), None),
        ("Seen only once", number(scalar(quality, "observed_once")),
         "These had no chance to have their delay revised, so their figure is a "
         "first impression rather than an outcome."),
        ("Mean observations", number(scalar(quality, "avg_observations"), "", 2), None),
        ("Confirmed departed", number(scalar(quality, "confirmed_departed")),
         "The feed reported the train as having left."),
    ])
    st.dataframe(quality.T.rename(columns={0: "value"}), use_container_width=True)
    show_sql(queries.DATA_QUALITY)


def page_pipeline() -> None:
    st.title("Pipeline")
    st.caption(
        "A dashboard reading a file on disk never has to ask whether its data "
        "is still arriving. This one does."
    )

    st.subheader("Freshness by station")
    health = run_sql(queries.INGESTION_HEALTH)
    if health.empty:
        st.info("No ingestion runs recorded yet.", icon="🪹")
    else:
        stale = int(health["is_stale"].sum()) if "is_stale" in health else 0
        if stale:
            st.warning(
                f"{stale} station(s) have not loaded in over an hour. That is "
                "expected outside the capture window (weekday 06–09 and 16–19, "
                "Europe/Brussels) — the database is deliberately allowed to "
                "auto-pause in between.",
                icon="⏸️",
            )
        else:
            st.success("Every station loaded within the last hour.", icon="✅")
        st.dataframe(
            health, use_container_width=True, hide_index=True,
            column_config={
                "is_stale": st.column_config.CheckboxColumn("Stale"),
                "is_hub": st.column_config.CheckboxColumn("Hub"),
                "minutes_since_last_run": st.column_config.NumberColumn(
                    "Age (min)"),
                "last_run_started_utc": st.column_config.DatetimeColumn(
                    "Last run (UTC)", format="YYYY-MM-DD HH:mm"),
            },
        )
    show_sql(queries.INGESTION_HEALTH)

    st.subheader("Is the deduplication working?")
    totals = run_sql(queries.RUN_TOTALS_BY_TRIGGER)
    if not totals.empty:
        st.dataframe(
            totals, use_container_width=True, hide_index=True,
            column_config={
                "rows_skipped": st.column_config.NumberColumn(
                    "Skipped",
                    help="Duplicate keys within a single payload, dropped before "
                         "the MERGE — which fails outright if two source rows "
                         "match one target row."),
                "avg_duration_ms": st.column_config.NumberColumn(
                    "Mean duration (ms)", format="%.0f"),
            },
        )
        st.caption(
            "**`rows_updated` should dominate `rows_inserted`** once a window "
            "has been polled more than once: the same departures, recognised "
            "and revised rather than duplicated. That is the whole point of the "
            "MERGE on `(station, vehicle, scheduled time)`."
        )
    show_sql(queries.RUN_TOTALS_BY_TRIGGER)

    st.subheader("Recent runs")
    limit = st.slider("Rows", 5, 200, 25, key="run_rows")
    runs = run_sql(queries.RECENT_RUNS, (int(limit),))
    st.dataframe(
        runs, use_container_width=True, hide_index=True,
        column_config={
            "started_utc": st.column_config.DatetimeColumn(
                "Started (UTC)", format="YYYY-MM-DD HH:mm:ss"),
        },
    )
    st.caption(
        "`trigger_source` distinguishes the scheduled timer from manual HTTP "
        "calls. Every departure row carries the run id that first saw it and "
        "the run that last touched it, so any number on any page traces back to "
        "an HTTP response at a point in time."
    )
    show_sql(queries.RECENT_RUNS, (int(limit),))

    # ----------------------------------------------------------------------
    st.subheader("Trigger a load now")
    if not (FUNCTION_APP_URL and FUNCTION_KEY):
        st.info(
            "Set `FUNCTION_APP_URL` and `FUNCTION_KEY` to enable this button. "
            "`azure/provision_webapp.sh` sets both. Without them the page is "
            "read-only — which is the correct default for a public dashboard.",
            icon="🔒",
        )
        return

    st.caption(
        "Polls all ten hubs immediately through the Function App. This **wakes "
        "the serverless database**, which is the only thing in this project "
        "that costs real money — see docs/cost_control.md."
    )
    if st.button("Run ingest now", type="primary"):
        with st.spinner("Polling iRail and loading Azure SQL… (up to 2 minutes "
                        "if the database is resuming from auto-pause)"):
            try:
                response = requests.post(
                    f"{FUNCTION_APP_URL}/api/ingest",
                    params={"hubs": "all"},
                    headers={"x-functions-key": FUNCTION_KEY},
                    timeout=420,
                )
                body = response.json()
            except requests.RequestException as exc:
                st.error(f"Could not reach the Function App: {exc}")
                return
            except ValueError:
                st.error(f"HTTP {response.status_code}: {response.text[:400]}")
                return

        # 207 is the app's documented "some hubs loaded, some did not".
        if response.status_code == 200:
            st.success(
                f"Loaded {body.get('departures_returned', 0)} departures from "
                f"{body.get('stations_succeeded', 0)} hub(s): "
                f"{body.get('rows_inserted', 0)} new, "
                f"{body.get('rows_updated', 0)} revised.",
                icon="✅",
            )
        elif response.status_code == 207:
            st.warning(
                f"Partial: {body.get('stations_succeeded', 0)} hub(s) loaded, "
                f"{body.get('stations_failed', 0)} failed. "
                f"{body.get('rows_inserted', 0)} new, "
                f"{body.get('rows_updated', 0)} revised.",
                icon="⚠️",
            )
        else:
            st.error(f"HTTP {response.status_code}: {str(body)[:400]}")
        st.json(body, expanded=False)
        # The 60-second result cache would otherwise hide the rows just written.
        st.cache_data.clear()
        st.caption("Caches cleared — the pages above now show the new rows.")


def page_schedule_vs_reality() -> None:
    st.title("Schedule vs reality")
    st.caption(
        "The static SNCB timetable from sprint 1, joined to what the pipeline "
        "actually observed. This is the only page that can tell an empty hour "
        "with no trains apart from an empty hour nobody was watching."
    )
    if empty_warehouse_notice():
        return

    station_id, station_name = station_picker("coverage_station")
    kpi = run_sql(queries.SCHEDULE_COVERAGE_KPI, (station_id, station_id))

    if not scalar(kpi, "scheduled"):
        st.info(
            "**No timetable baseline loaded yet.** The static schedule lives in "
            "sprint 1's SQLite build; load the comparable slice with\n\n"
            "`python scripts/load_schedule_baseline.py`",
            icon="🗓️",
        )
        return

    kpis([
        ("Scheduled", number(scalar(kpi, "scheduled")),
         "Departures the published timetable says should have left these "
         "stations on the days observed."),
        ("Observed", number(scalar(kpi, "observed")),
         "Of those, how many the pipeline actually saw on a liveboard."),
        ("Coverage, watched hours",
         number(scalar(kpi, "coverage_sampled_pct"), "%", 1),
         "The figure that means something: of the trains scheduled in hours the "
         "pipeline WAS polling, how many it saw."),
        ("Coverage, overall", number(scalar(kpi, "coverage_pct"), "%", 1),
         "Includes hours nobody sampled, so it is low by design. Not a quality "
         "measure — shown only so it cannot be confused with the figure beside it."),
        ("Ran unwatched", number(scalar(kpi, "scheduled_unsampled")),
         "Scheduled departures in hours the pipeline never polled. Previously "
         "invisible; the timetable is what makes them countable."),
        ("Days", number(scalar(kpi, "days_covered")), None),
    ])
    show_sql(queries.SCHEDULE_COVERAGE_KPI, (station_id, station_id))

    # ----------------------------------------------------------------------
    st.subheader("Where the blind spot is")
    st.caption(
        f"{station_name} — every hour of the day, what the timetable scheduled "
        "against what was seen. The unwatched hours are the point: the trains "
        "were there, the pipeline was not."
    )
    hourly = run_sql(queries.COVERAGE_BY_HOUR, (station_id, station_id))
    if hourly.empty:
        st.caption("No timetable rows for this station.")
    else:
        # Long form for a grouped bar chart. A rename and a concat — no
        # aggregation, no derivation; the numbers are exactly as SQL returned them.
        scheduled = hourly[["hour_local", "scheduled"]].rename(
            columns={"scheduled": "departures"}).assign(series="scheduled")
        observed = hourly[["hour_local", "observed"]].rename(
            columns={"observed": "departures"}).assign(series="observed")
        chart_data = pd.concat([scheduled, observed], ignore_index=True)

        st.altair_chart(
            alt.Chart(chart_data).mark_bar().encode(
                x=alt.X("hour_local:O", title="Hour (Europe/Brussels)"),
                y=alt.Y("departures:Q", title="Departures"),
                color=alt.Color("series:N", title=None,
                                scale=alt.Scale(domain=["scheduled", "observed"],
                                                range=["#c9ced6", "#2b6cb0"])),
                xOffset="series:N",
                tooltip=["hour_local", "series", "departures"],
            ).properties(height=340),
            use_container_width=True,
        )
        unwatched = hourly.loc[hourly["hour_was_sampled"] == 0, "scheduled"].sum()
        st.caption(
            f"Grey is the timetable, blue is what was observed. **{int(unwatched):,} "
            "scheduled departures fall in hours with no sampling at all** — the "
            "flat grey columns. Before the timetable was joined in, those hours "
            "looked identical to hours with no trains."
        )
        st.dataframe(
            hourly, use_container_width=True, hide_index=True,
            column_config={
                "hour_was_sampled": st.column_config.CheckboxColumn("Watched"),
                "coverage_pct": st.column_config.NumberColumn(
                    "Coverage", format="%.2f%%"),
            },
        )
    show_sql(queries.COVERAGE_BY_HOUR, (station_id, station_id))

    # ----------------------------------------------------------------------
    left, right = st.columns([3, 2])
    with left:
        st.subheader("Coverage by hub, watched hours only")
        by_station = run_sql(queries.COVERAGE_BY_STATION)
        st.dataframe(
            by_station, use_container_width=True, hide_index=True,
            column_config={
                "coverage_pct": st.column_config.ProgressColumn(
                    "Coverage", format="%.1f%%", min_value=0, max_value=100),
                "unseen_while_watching": st.column_config.NumberColumn(
                    "Unseen while watching",
                    help="Scheduled, in an hour the pipeline was polling, yet "
                         "never seen. NOT a cancellation count — see below."),
            },
        )
        show_sql(queries.COVERAGE_BY_STATION)
    with right:
        st.subheader("How confident is the match?")
        quality = run_sql(queries.SCHEDULE_MATCH_QUALITY)
        st.dataframe(
            quality, use_container_width=True, hide_index=True,
            column_config={"pct_of_all": st.column_config.NumberColumn(
                "% of all", format="%.2f%%")},
        )
        st.caption(
            "A timetable row is paired with an observation on station and "
            "scheduled minute, and confirmed by **train number** where both "
            "sides publish one. A time-only match is a weaker claim and is "
            "counted separately rather than averaged in."
        )
        show_sql(queries.SCHEDULE_MATCH_QUALITY)

    # ----------------------------------------------------------------------
    st.subheader("Planned platform vs actual")
    st.caption(
        "**Neither dataset can produce this alone.** The timetable knows which "
        "platform was published; the live feed knows which one the train used."
    )
    limit = st.slider("Rows", 5, 200, 25, key="platform_change_rows")
    changes = run_sql(queries.PLATFORM_PLAN_VS_ACTUAL,
                      (int(limit), station_id, station_id))
    if changes.empty:
        st.caption("No platform differences recorded for this selection.")
    else:
        st.dataframe(
            changes, use_container_width=True, hide_index=True,
            column_config={
                "scheduled_departure_local": st.column_config.DatetimeColumn(
                    "Scheduled (local)", format="YYYY-MM-DD HH:mm"),
                "planned_platform": st.column_config.TextColumn("Published"),
                "observed_platform": st.column_config.TextColumn("Actual"),
                "is_canceled": st.column_config.CheckboxColumn("Cancelled"),
            },
        )
    show_sql(queries.PLATFORM_PLAN_VS_ACTUAL, (int(limit), station_id, station_id))

    # ----------------------------------------------------------------------
    st.subheader("Scheduled, watched for, never seen")
    st.warning(
        "**These are candidates, not cancellations.** A liveboard shows only the "
        "next ~55 departures, so an hour counted as \"watched\" is often only "
        "partly covered — a train scheduled late in the hour may simply never "
        "have been in view. Treat this as a queue to investigate, and read it "
        "next to the coverage percentage above rather than on its own.",
        icon="⚠️",
    )
    unseen = run_sql(queries.UNSEEN_WHILE_WATCHING,
                     (int(limit), station_id, station_id))
    st.dataframe(
        unseen, use_container_width=True, hide_index=True,
        column_config={
            "scheduled_departure_local": st.column_config.DatetimeColumn(
                "Scheduled (local)", format="YYYY-MM-DD HH:mm"),
        },
    )
    show_sql(queries.UNSEEN_WHILE_WATCHING, (int(limit), station_id, station_id))


# ==========================================================================
# Sidebar and dispatch
# ==========================================================================
PAGES = {
    "Overview": page_overview,
    "Live departures": page_live,
    "Hub leaderboard": page_leaderboard,
    "Peak hours": page_peak_hours,
    "Platform bottlenecks": page_platforms,
    "Delay evolution": page_delay_evolution,
    "Schedule vs reality": page_schedule_vs_reality,
    "Services & destinations": page_services,
    "Data quality": page_quality,
    "Pipeline": page_pipeline,
}

st.sidebar.title("RailPulse Cloud")
st.sidebar.caption("Sprint 2 — live liveboard data in Azure SQL")
choice = st.sidebar.radio("Page", list(PAGES), label_visibility="collapsed")

st.sidebar.divider()
if st.sidebar.button("Refresh data", use_container_width=True):
    st.cache_data.clear()
    st.rerun()
st.sidebar.caption(
    f"Results are cached for 60 s. Last render "
    f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC."
)

st.sidebar.divider()
try:
    target = credentials()
    st.sidebar.caption(
        f"**Database**\n\n`{target.safe_description}`\n\nOpened read-only — the "
        "app rejects any statement that is not a SELECT."
    )
except Exception as exc:  # noqa: BLE001 - shown, not swallowed
    st.sidebar.error(f"No database configured:\n\n{exc}")

st.sidebar.caption(
    "Data: [iRail](https://irail.be) / NMBS-SNCB, CC BY 4.0. "
    "Every figure is produced by SQL over the views in `sql/03_views.sql`; "
    "each one has a **Show the SQL** expander."
)

try:
    PAGES[choice]()
except Exception as exc:  # noqa: BLE001 - the page boundary owes the reader a message
    st.error(f"**{type(exc).__name__}** — {exc}")
    st.caption(
        "If this mentions a login timeout, the serverless database is most "
        "likely resuming from auto-pause; the first query after a quiet spell "
        "can take up to a minute. Press **Refresh data** and try again. If it "
        "mentions an invalid object name, the schema has not been applied — run "
        "`make migrate`."
    )
