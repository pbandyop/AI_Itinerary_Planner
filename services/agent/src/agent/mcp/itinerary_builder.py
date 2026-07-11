"""Itinerary Builder MCP — pack POIs into day/time blocks (Phase 1 schema)."""

from __future__ import annotations

import logging

from agent.mcp.geo import JAIPUR_CENTER, estimate_travel_minutes, haversine_km
from agent.schemas.itinerary import (
    DayPlan,
    Pace,
    Source,
    Stop,
    TimeBlock,
    TripConstraints,
)
from agent.schemas.specialists import ItineraryDraftResult, POICandidate

logger = logging.getLogger(__name__)

DEFAULT_DURATION: dict[str, int] = {
    "heritage": 90,
    "museum": 90,
    "attraction": 75,
    "temple": 60,
    "food": 60,
    "market": 75,
    "viewpoint": 45,
    "park": 45,
    "other": 60,
}

# Max activity stops per day by pace (excluding tiny connectors)
STOPS_PER_DAY: dict[Pace, int] = {
    "relaxed": 4,
    "moderate": 5,
    "packed": 7,
}


def _duration_for(poi: POICandidate, pace: Pace) -> int:
    base = DEFAULT_DURATION.get(poi.category or "other", 60)
    if pace == "relaxed":
        return min(150, int(base * 1.15))
    if pace == "packed":
        return max(30, int(base * 0.85))
    return base


def _order_nearest(
    pois: list[POICandidate],
    start: tuple[float, float] = JAIPUR_CENTER,
) -> list[POICandidate]:
    """Greedy nearest-neighbor ordering to reduce travel."""
    remaining = [p for p in pois if p.lat is not None and p.lon is not None]
    no_coords = [p for p in pois if p.lat is None or p.lon is None]
    ordered: list[POICandidate] = []
    lat, lon = start
    while remaining:
        remaining.sort(
            key=lambda p: haversine_km(lat, lon, p.lat or lat, p.lon or lon)
        )
        nxt = remaining.pop(0)
        ordered.append(nxt)
        lat, lon = nxt.lat or lat, nxt.lon or lon
    return ordered + no_coords


def _split_days(
    ordered: list[POICandidate],
    *,
    num_days: int,
    pace: Pace,
) -> list[list[POICandidate]]:
    per_day = STOPS_PER_DAY[pace]
    needed = num_days * per_day
    selected = ordered[:needed]
    days: list[list[POICandidate]] = [[] for _ in range(num_days)]
    for idx, poi in enumerate(selected):
        days[idx % num_days].append(poi)
    # Rebalance: prefer contiguous chunks for travel locality
    chunked: list[list[POICandidate]] = []
    for d in range(num_days):
        start = d * per_day
        chunked.append(selected[start : start + per_day])
    # Drop empty trailing if not enough POIs
    return [c for c in chunked if c] or [[]]


def _assign_blocks(
    day_pois: list[POICandidate],
    *,
    pace: Pace,
    day_index: int,
) -> DayPlan:
    """Split a day's POIs into morning / afternoon / evening by cumulative time."""
    morning_cap = 180 if pace != "packed" else 210
    afternoon_cap = 180 if pace != "packed" else 210

    blocks: dict[str, list[Stop]] = {
        "morning": [],
        "afternoon": [],
        "evening": [],
    }
    cursor = "morning"
    used = {"morning": 0, "afternoon": 0, "evening": 0}

    enriched: list[tuple[POICandidate, int]] = [
        (p, _duration_for(p, pace)) for p in day_pois
    ]

    for i, (poi, dur) in enumerate(enriched):
        if cursor == "morning" and used["morning"] >= morning_cap and enriched[i:]:
            cursor = "afternoon"
        if cursor == "afternoon" and used["afternoon"] >= afternoon_cap and enriched[i:]:
            cursor = "evening"

        next_poi = enriched[i + 1][0] if i + 1 < len(enriched) else None
        travel = None
        if next_poi is not None:
            travel = estimate_travel_minutes(poi.lat, poi.lon, next_poi.lat, next_poi.lon)

        stop = Stop(
            name=poi.name,
            osm_type=poi.osm_type,
            osm_id=poi.osm_id,
            lat=poi.lat,
            lon=poi.lon,
            category=poi.category,
            duration_min=dur,
            travel_to_next_min=travel,
            reason=(
                f"Selected for interests {poi.matched_interests or ['general']} "
                f"at a {pace} pace; OSM {poi.osm_type}/{poi.osm_id}."
            ),
            citations=[
                Source(
                    title=f"OpenStreetMap {poi.osm_type}/{poi.osm_id}",
                    url=f"https://www.openstreetmap.org/{poi.osm_type}/{poi.osm_id}",
                    dataset="openstreetmap",
                    snippet=f"{poi.name} ({poi.category or 'poi'})",
                    source_id=f"{poi.osm_type}/{poi.osm_id}",
                )
            ],
            uncertainty=None,
        )
        blocks[cursor].append(stop)
        used[cursor] += dur + (travel or 0)

    # Clear travel_to_next on last stop of each block
    for name, stops in blocks.items():
        if stops:
            stops[-1].travel_to_next_min = None
            # Recompute travel within block only
            for j in range(len(stops) - 1):
                a, b = stops[j], stops[j + 1]
                stops[j].travel_to_next_min = estimate_travel_minutes(
                    a.lat, a.lon, b.lat, b.lon
                )

    theme_bits = [p.category for p in day_pois if p.category]
    theme = ", ".join(dict.fromkeys(theme_bits) ) if theme_bits else f"Day {day_index}"

    return DayPlan(
        day_index=day_index,
        theme=theme[:80],
        morning=TimeBlock(time_of_day="morning", stops=blocks["morning"]),
        afternoon=TimeBlock(time_of_day="afternoon", stops=blocks["afternoon"]),
        evening=TimeBlock(time_of_day="evening", stops=blocks["evening"]),
    )


def build_itinerary(
    *,
    candidate_pois: list[POICandidate],
    num_days: int = 3,
    pace: Pace = "relaxed",
    daily_time_window_min: int = 540,
    interests: list[str] | None = None,
) -> ItineraryDraftResult:
    """MCP: build a day-wise draft itinerary from candidate POIs."""
    num_days = max(2, min(4, num_days))
    notes: list[str] = []
    missing = False

    if not candidate_pois:
        missing = True
        notes.append("No candidate POIs provided — cannot build an itinerary.")
        empty_days = [
            DayPlan(
                day_index=i,
                morning=TimeBlock(time_of_day="morning"),
                afternoon=TimeBlock(time_of_day="afternoon"),
                evening=TimeBlock(time_of_day="evening"),
            )
            for i in range(1, num_days + 1)
        ]
        return ItineraryDraftResult(
            pace=pace,
            days=empty_days,
            missing_data=True,
            notes="; ".join(notes),
        )

    # Prefer higher rank_score first, then nearest-neighbor within top set
    ranked = sorted(
        candidate_pois,
        key=lambda p: (-(p.rank_score or 0), p.name),
    )
    ordered = _order_nearest(ranked)
    day_groups = _split_days(ordered, num_days=num_days, pace=pace)

    # Ensure we always emit num_days (pad empty if needed)
    while len(day_groups) < num_days:
        day_groups.append([])

    days: list[DayPlan] = []
    for i, group in enumerate(day_groups[:num_days], start=1):
        if not group:
            missing = True
            notes.append(f"Day {i} has no POIs — data insufficient for a full plan.")
            days.append(
                DayPlan(
                    day_index=i,
                    theme="Insufficient POI data",
                    morning=TimeBlock(time_of_day="morning"),
                    afternoon=TimeBlock(time_of_day="afternoon"),
                    evening=TimeBlock(time_of_day="evening"),
                )
            )
            continue
        day = _assign_blocks(group, pace=pace, day_index=i)
        # Soft feasibility trim if over window
        total = day.total_duration_min
        if total > daily_time_window_min:
            notes.append(
                f"Day {i} draft duration {total}m exceeds window "
                f"{daily_time_window_min}m — Reviewer should flag or trim."
            )
        days.append(day)

    logger.info(
        "Itinerary builder: %d POIs → %d days @ pace=%s",
        len(candidate_pois),
        num_days,
        pace,
    )
    if interests:
        notes.append(f"Interests considered: {', '.join(interests)}.")

    return ItineraryDraftResult(
        pace=pace,
        days=days,
        missing_data=missing,
        notes="; ".join(notes) if notes else None,
    )


def draft_to_trip_constraints(
    *,
    num_days: int,
    pace: Pace,
    interests: list[str],
    daily_time_window_min: int = 540,
) -> TripConstraints:
    return TripConstraints(
        city="Jaipur",
        num_days=num_days,
        interests=interests,
        pace=pace,
        daily_time_window_min=daily_time_window_min,
        confirmed=False,
    )
