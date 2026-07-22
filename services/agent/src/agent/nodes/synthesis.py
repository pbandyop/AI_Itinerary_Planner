"""Synthesis Agent (Response Composer) — presentation only.

Combines the Itinerary Agent's optimized draft with citations, sources,
schema validation, and a user-friendly narrative. Does **not** move, skip,
or reorder stops — that ownership belongs to the Itinerary Agent.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from agent.formatters import format_spend_duration
from agent.mcp.geo import estimate_travel_minutes
from agent.mcp.itinerary_builder import (
    annotate_block_flex_time,
    refresh_day_themes,
    stamp_schedule_clocks,
)
from agent.nodes.llm_utils import chat_json, compact_itinerary, llm_enabled
from agent.nodes.state_utils import (
    as_dispatch,
    as_draft,
    as_edit_patch,
    as_edit_patches,
    as_itinerary,
    as_knowledge,
    as_travel,
    as_trip,
    as_weather,
    dump,
)
from agent.place_identity import PlaceSeen
from agent.preferences import profile_label
from agent.rag.retrieve import sources_from_knowledge
from agent.schemas.itinerary import Itinerary, Source, Stop, TimeBlock, TripConstraints
from agent.schemas.specialists import KnowledgeResult
from agent.schemas.state import GraphState
from agent.schemas.validation import validate_grounding_rules, validate_itinerary

logger = logging.getLogger(__name__)


def _scope_notes(trip: TripConstraints | None) -> list[str]:
    if not trip:
        return []
    notes: list[str] = []
    for c in trip.constraints or []:
        raw = str(c).strip()
        if raw.lower().startswith("scope:"):
            notes.append(raw.split(":", 1)[1].strip())
    return notes


# Soft / hard day budgets by pace. Soft = "feels packed"; hard = overloaded.
# Schema uses "moderate"; user-facing copy says "balanced".
_PACE_DAY_BUDGETS: dict[str, dict[str, int]] = {
    "relaxed": {"soft_min": 360, "hard_min": 450, "soft_stops": 4, "hard_stops": 5},
    "moderate": {"soft_min": 480, "hard_min": 540, "soft_stops": 6, "hard_stops": 7},
    "packed": {"soft_min": 600, "hard_min": 720, "soft_stops": 11, "hard_stops": 12},
}


def _pace_label(pace: str | None) -> str:
    p = (pace or "moderate").lower()
    if p == "moderate":
        return "balanced"
    return p


def _fmt_hours(mins: int) -> str:
    hours = mins / 60.0
    if hours >= 9:
        return f"~{int(hours)}+ hours"
    if abs(hours - round(hours)) < 0.15:
        return f"~{int(round(hours))} hours"
    return f"~{hours:.1f} hours"


def _join_day_indexes(indexes: list[int]) -> str:
    if not indexes:
        return "Other days"
    if len(indexes) == 1:
        return f"Day {indexes[0]}"
    return f"Days {indexes[0]}–{indexes[-1]}"


def _assess_doability(itin: Itinerary) -> str:
    """Honest feasibility from this itinerary only — no RAG, no edit notes."""
    pace = (itin.trip.pace if itin.trip else "moderate") or "moderate"
    pace_label = _pace_label(pace)
    budget = _PACE_DAY_BUDGETS.get(pace, _PACE_DAY_BUDGETS["moderate"])
    window = (itin.trip.daily_time_window_min if itin.trip else 540) + 60

    packed: list[tuple[int, int, int]] = []  # day, mins, stops
    tight: list[tuple[int, int, int]] = []
    ok: list[int] = []
    stats: list[str] = []

    for day in itin.days:
        mins = day.total_duration_min
        n = len(day.all_stops)
        stats.append(f"Day {day.day_index}: {n} stops · {_fmt_hours(mins)}")
        over_hard = mins > max(budget["hard_min"], window) or n > budget["hard_stops"]
        # Both soft limits breached (or ~9h+) → call it packed, not merely full.
        over_soft_both = (
            mins > budget["soft_min"] and n > budget["soft_stops"]
        ) or mins >= 540
        over_soft = mins > budget["soft_min"] or n > budget["soft_stops"]
        if over_hard or over_soft_both:
            packed.append((day.day_index, mins, n))
        elif over_soft:
            tight.append((day.day_index, mins, n))
        else:
            ok.append(day.day_index)

    if not packed and not tight:
        return (
            f"At a {pace_label} pace this plan looks doable — "
            + "; ".join(stats)
            + "."
        )

    heavy = packed or tight
    d0, m0, n0 = heavy[0]
    adjective = "packed" if packed else "full"
    bits: list[str] = [
        f"At a {pace_label} pace, Day {d0} looks {adjective} "
        f"({_fmt_hours(m0)} with {n0} stops)."
    ]
    if len(packed) > 1:
        others = ", ".join(f"Day {d}" for d, _, _ in packed[1:])
        bits.append(f"{others} also look heavy.")
    elif len(tight) > 1 and not packed:
        others = ", ".join(f"Day {d}" for d, _, _ in tight[1:])
        bits.append(f"{others} are also fairly full.")
    if ok:
        bits.append(f"{_join_day_indexes(ok)} look more manageable.")
    elif tight and packed:
        bits.append(
            f"{_join_day_indexes([d for d, _, _ in tight])} "
            "are full but workable."
        )

    focus = packed[0][0] if packed else tight[0][0]
    if pace != "packed":
        bits.append(
            f"I wouldn’t add more to Day {focus} without dropping a stop "
            f"or switching that day to packed pace."
        )
    else:
        bits.append(
            f"I wouldn’t add more to Day {focus} without dropping a stop."
        )
    return " ".join(bits)


def _planner_why_for_stop(named: Stop, itin: Itinerary) -> str:
    """Itinerary-owned why only — interest/category/day; no guidebook paste."""
    from agent.preferences import categories_for_interest, normalize_interest

    trip = itin.trip
    interests = list(trip.interests or []) if trip else []
    pace = _pace_label(trip.pace if trip else None)
    day_idx = next(
        (
            d.day_index
            for d in itin.days
            if any(
                s is named or (s.osm_id == named.osm_id and s.name == named.name)
                for s in d.all_stops
            )
        ),
        None,
    )
    cat = (named.category or "place").lower()
    day_bit = f" on Day {day_idx}" if day_idx else ""

    # Prefer interest that equals the stop category; only then use related maps.
    # Avoids “food, heritage” on a palace / “culture + heritage” when heritage fits.
    exact: list[str] = []
    related_hits: list[str] = []
    for interest in interests:
        key = normalize_interest(interest) or interest.lower().strip()
        if not key:
            continue
        related = categories_for_interest(interest)
        if cat == key:
            if key not in exact:
                exact.append(key)
        elif cat in related:
            if key not in related_hits:
                related_hits.append(key)
    matched = exact or related_hits

    if matched:
        if len(matched) == 1:
            return (
                f"I included {named.name}{day_bit} because it matches "
                f"your interest in {matched[0]}."
            )
        return (
            f"I included {named.name}{day_bit} because it matches "
            f"your interests in {', '.join(matched[:-1])} and {matched[-1]}."
        )
    return (
        f"I included {named.name}{day_bit} as a live OpenStreetMap "
        f"{cat} stop for this {pace} plan."
    )


def _audience_reply(trip: TripConstraints, stop_count: int) -> str:
    label = profile_label(trip.traveler_profile)
    interests = ", ".join(trip.interests) if trip.interests else "general interests"
    if label:
        core = (
            f"Here is your {trip.num_days}-day {trip.pace} itinerary for {trip.city}, "
            f"designed as a {label} trip focused on {interests}."
        )
    else:
        core = (
            f"Here is your {trip.num_days}-day {trip.pace} itinerary for {trip.city}, "
            f"focused on {interests}."
        )
    scope = _scope_notes(trip)
    if scope:
        core = f"{scope[0]} {core}"
    return f"{core} {stop_count} stops planned."


def _attach_knowledge_to_stops(days: list, knowledge: KnowledgeResult | None) -> list:
    """Attach place-matched RAG citations; never invent unmatched tips."""
    if not knowledge or not knowledge.snippets:
        return days

    # Pair each snippet with its citations; prefer place-name match per stop.
    snip_rows: list[tuple[str, list[Source]]] = []
    for snip in knowledge.snippets:
        text = (snip.text or "").lower()
        cites = list(snip.citations or [])
        if cites:
            snip_rows.append((text, cites))
    if not snip_rows:
        return days

    out = []
    for day in days:
        d = day.model_copy(deep=True)
        for block_name in ("morning", "afternoon", "evening"):
            block: TimeBlock = getattr(d, block_name)
            new_stops: list[Stop] = []
            for stop in block.stops:
                s = stop.model_copy(deep=True)
                name = (s.name or "").strip().lower()
                matched: list[Source] = []
                if name:
                    for text, cites in snip_rows:
                        if name in text or any(
                            tok in text
                            for tok in name.split()
                            if len(tok) >= 4
                        ):
                            matched.extend(cites)
                            break
                existing_ids = {c.source_id for c in s.citations if c.source_id}
                for rag in matched:
                    if rag.source_id and rag.source_id in existing_ids:
                        continue
                    s.citations = list(s.citations) + [rag]
                    if rag.source_id:
                        existing_ids.add(rag.source_id)
                if not s.citations and not s.uncertainty:
                    s.uncertainty = (
                        "No place-matched Wikivoyage tip; OSM grounding only."
                    )
                new_stops.append(s)
            setattr(
                d,
                block_name,
                TimeBlock(time_of_day=block_name, stops=new_stops, notes=block.notes),
            )
        out.append(d)
    return out


def _apply_travel_times(days: list, travel) -> list:
    """Attach travel minutes onto existing stop order — no reordering."""
    if not travel or not travel.legs:
        return days
    leg_map = {
        (leg.from_name.lower(), leg.to_name.lower()): leg.duration_min
        for leg in travel.legs
    }
    out = []
    for day in days:
        d = day.model_copy(deep=True)
        for block_name in ("morning", "afternoon", "evening"):
            block: TimeBlock = getattr(d, block_name)
            stops = list(block.stops)
            for i in range(len(stops) - 1):
                key = (stops[i].name.lower(), stops[i + 1].name.lower())
                if key in leg_map:
                    stops[i] = stops[i].model_copy(
                        update={"travel_to_next_min": leg_map[key]}
                    )
            setattr(
                d,
                block_name,
                TimeBlock(time_of_day=block_name, stops=stops, notes=block.notes),
            )
        out.append(d)
    return out


def _stamp_travel_via_mcp(
    days: list,
    *,
    mutate_days: set[int] | None = None,
    pace: str | None = None,
) -> tuple[list, Any]:
    """Run Travel Time Estimator MCP on each day's ordered stops (grounded only).

    When ``mutate_days`` is set, only those day indices get travel fields rewritten;
    other days are deep-copied unchanged (scoped edits must not rewrite Day 2+).
    Legs are still computed for every day so the travel summary stays complete.
    After travel is stamped, arrive/depart clocks are applied on mutated days.
    """
    from agent.mcp.travel_time import (
        display_mode_label,
        estimate_leg,
        estimate_travel_times,
    )
    from agent.schemas.specialists import TravelTimeResult

    pace_key = pace if pace in ("relaxed", "moderate", "packed") else "moderate"
    out_days: list = []
    all_legs: list = []
    missing_any = False
    notes: list[str] = []

    for day in days:
        d = day.model_copy(deep=True)
        should_mutate = mutate_days is None or day.day_index in mutate_days

        # Flatten morning → afternoon → evening, drop duplicate places in-day
        flat_stops: list[Stop] = []
        seen_places = PlaceSeen()
        block_ranges: list[tuple[str, int, int]] = []
        for block_name in ("morning", "afternoon", "evening"):
            block: TimeBlock = getattr(d, block_name)
            start = len(flat_stops)
            for s in block.stops:
                if seen_places.contains(s):
                    continue
                seen_places.add(s)
                flat_stops.append(s)
            block_ranges.append((block_name, start, len(flat_stops)))

        if len(flat_stops) < 2:
            if should_mutate:
                for block_name, start, end in block_ranges:
                    block = getattr(d, block_name)
                    stamped = [
                        s.model_copy(
                            update={
                                "travel_to_next_min": None,
                                "travel_to_next_km": None,
                                "travel_to_next_mode": None,
                            }
                        )
                        for s in flat_stops[start:end]
                    ]
                    setattr(
                        d,
                        block_name,
                        TimeBlock(
                            time_of_day=block_name, stops=stamped, notes=block.notes
                        ),
                    )
                d = stamp_schedule_clocks([d], pace=pace_key)[0]  # type: ignore[arg-type]
            out_days.append(d if should_mutate else day.model_copy(deep=True))
            continue

        result = estimate_travel_times(
            points=[s.model_dump(mode="json") for s in flat_stops],
            mode="auto",
        )
        missing_any = missing_any or bool(result.missing_data)
        if result.notes:
            notes.append(result.notes)
        all_legs.extend(list(result.legs or []))

        if not should_mutate:
            out_days.append(day.model_copy(deep=True))
            continue

        leg_by_pair = {
            (
                (leg.from_name or "").strip().lower(),
                (leg.to_name or "").strip().lower(),
            ): leg
            for leg in (result.legs or [])
        }
        stamped: list[Stop] = []
        for i, stop in enumerate(flat_stops):
            travel = None
            travel_km = None
            travel_mode = None
            if i < len(flat_stops) - 1:
                nxt = flat_stops[i + 1]
                key = (
                    (stop.name or "").strip().lower(),
                    (nxt.name or "").strip().lower(),
                )
                leg = leg_by_pair.get(key)
                if leg is None:
                    leg = estimate_leg(
                        stop.model_dump(mode="json"),
                        nxt.model_dump(mode="json"),
                        mode="auto",
                    )
                if leg is not None and getattr(leg, "duration_min", 0) > 0:
                    travel = int(leg.duration_min)
                    travel_km = getattr(leg, "distance_km", None)
                    travel_mode = display_mode_label(getattr(leg, "mode", None))
                else:
                    mins = estimate_travel_minutes(
                        stop.lat, stop.lon, nxt.lat, nxt.lon
                    )
                    travel = mins if mins > 0 else None
            stamped.append(
                stop.model_copy(
                    update={
                        "travel_to_next_min": travel,
                        "travel_to_next_km": travel_km,
                        "travel_to_next_mode": travel_mode,
                    }
                )
            )

        for block_name, start, end in block_ranges:
            block = getattr(d, block_name)
            setattr(
                d,
                block_name,
                TimeBlock(
                    time_of_day=block_name,
                    stops=stamped[start:end],
                    notes=block.notes,
                ),
            )
        out_days.append(stamp_schedule_clocks([d], pace=pace_key)[0])  # type: ignore[arg-type]

    out_days, _ = annotate_block_flex_time(
        out_days, pace=pace_key  # type: ignore[arg-type]
    )
    out_days = refresh_day_themes(out_days)

    travel_result = TravelTimeResult(
        legs=all_legs,
        total_duration_min=sum(
            int(getattr(leg, "duration_min", 0) or 0) for leg in all_legs
        ),
        missing_data=missing_any and not all_legs,
        notes="; ".join(dict.fromkeys(notes))
        if notes
        else (
            "Travel Time Estimator MCP (haversine walk/city heuristic)."
            if all_legs
            else "Travel times unavailable — need coordinates for consecutive stops."
        ),
    )
    return out_days, travel_result


def _format_travel_to_next_line(stop: Stop) -> str | None:
    """Inline 'Travel to next' with minutes, distance, and mode when known."""
    if stop.travel_to_next_min is None or stop.travel_to_next_min <= 0:
        return None
    bits = [f"about {stop.travel_to_next_min} minutes"]
    extras: list[str] = []
    if stop.travel_to_next_km is not None and stop.travel_to_next_km >= 0.05:
        extras.append(f"{stop.travel_to_next_km:.1f} km")
    if stop.travel_to_next_mode:
        extras.append(f"by {stop.travel_to_next_mode}")
    if extras:
        bits.append("(" + " ".join(extras) + ")")
    return "       Travel to next: " + " ".join(bits)


def _format_clock_ampm(hhmm: str | None) -> str:
    """Convert 'HH:MM' (24h) to a short 12-hour label like '9:00 AM'."""
    if not hhmm or ":" not in hhmm:
        return ""
    try:
        hour_s, min_s = hhmm.split(":", 1)
        hour = int(hour_s)
        minute = int(min_s)
    except ValueError:
        return hhmm
    suffix = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d} {suffix}"


def _format_readable_itinerary(
    itinerary: Itinerary,
    *,
    travel: Any = None,
    preface: str | None = None,
) -> str:
    """Human-readable day-by-day itinerary (Morning / Afternoon)."""
    del preface
    trip = itinerary.trip
    pace = trip.pace or "moderate"
    pace_label = "balanced" if pace == "moderate" else pace
    interests = ", ".join(trip.interests) if trip.interests else "your interests"

    lines: list[str] = [
        f"{trip.num_days}-day {pace_label} plan for {trip.city} "
        f"({interests}).",
        "",
    ]

    block_labels = {
        "morning": "Morning",
        "afternoon": "Afternoon",
        "evening": "Evening",
    }
    for day in itinerary.days:
        lines.append(f"Day {day.day_index}")
        flat = list(day.all_stops)
        last_key = (
            f"{flat[-1].osm_type}/{flat[-1].osm_id}" if flat else None
        )
        for block_name in ("morning", "afternoon", "evening"):
            block: TimeBlock = getattr(day, block_name)
            if not block.stops and not (block.notes or "").strip():
                continue
            lines.append(f"  {block_labels[block_name]}")
            for i, stop in enumerate(block.stops):
                cat = f" ({stop.category})" if stop.category else ""
                clock = ""
                if stop.arrive_time:
                    clock = f"{_format_clock_ampm(stop.arrive_time)} — "
                lines.append(
                    f"    {i + 1}. {clock}{stop.name}{cat} — "
                    f"{format_spend_duration(stop.duration_min)}"
                    + (
                        f" (until {_format_clock_ampm(stop.depart_time)})"
                        if stop.depart_time
                        else ""
                    )
                )
                stop_key = f"{stop.osm_type}/{stop.osm_id}"
                if stop_key != last_key:
                    travel_line = _format_travel_to_next_line(stop)
                    if travel_line:
                        lines.append(travel_line)
            if (block.notes or "").strip():
                lines.append(f"    · {block.notes.strip()}")
        lines.append("")

    if travel and getattr(travel, "legs", None):
        from agent.mcp.travel_time import display_mode_label

        real_legs = [
            leg
            for leg in travel.legs
            if getattr(leg, "duration_min", 0)
            and (getattr(leg, "distance_km", None) is None or leg.distance_km >= 0.05)
            and (leg.from_name or "").strip().lower()
            != (leg.to_name or "").strip().lower()
        ]
        if real_legs:
            total = sum(int(leg.duration_min) for leg in real_legs)
            lines.append("Travel between stops")
            for leg in real_legs[:14]:
                dist = getattr(leg, "distance_km", None)
                mode = display_mode_label(getattr(leg, "mode", None))
                extras: list[str] = []
                if dist is not None:
                    extras.append(f"{dist:.1f} km")
                if mode:
                    extras.append(f"by {mode}")
                extra_bit = f" ({' '.join(extras)})" if extras else ""
                lines.append(
                    f"  • {leg.from_name} → {leg.to_name}: "
                    f"about {leg.duration_min} minutes{extra_bit}"
                )
            lines.append(f"  Total travel time: about {total} minutes")
    elif travel and getattr(travel, "missing_data", False):
        lines.append(
            "Travel times: data not available from Travel Time Estimator MCP."
        )

    return "\n".join(lines).strip()


def _is_osm_citation(c: Source) -> bool:
    if (c.dataset or "") == "openstreetmap":
        return True
    return bool(re.match(r"^(node|way|relation)/\d+$", c.source_id or ""))


def _collect_sources(
    *,
    knowledge,
    weather,
    travel,
    itinerary: Itinerary,
    include_rag: bool,
) -> list[Source]:
    """Build References panel sources.

    Plan/edit: displayed-stop OSM links + weather (if shown) + travel-time
    attribution. Explain/RAG turns: include knowledge citations.
    """
    sources: list[Source] = []
    seen: set[str] = set()

    def add(src: Source) -> None:
        key = src.source_id or f"{src.title}|{src.url}|{src.snippet}"
        if key in seen:
            return
        seen.add(key)
        sources.append(src)

    if include_rag and knowledge:
        for s in sources_from_knowledge(knowledge):
            add(s)

    if weather and not weather.missing_data and getattr(weather, "days", None):
        add(
            Source(
                title="Open-Meteo Forecast",
                url="https://open-meteo.com/",
                dataset="open-meteo",
                snippet="Daily weather used for rain-risk context.",
                source_id="open-meteo",
            )
        )

    if travel and getattr(travel, "legs", None):
        add(
            Source(
                title="Travel time estimates",
                url="https://www.openstreetmap.org/",
                dataset="openstreetmap",
                snippet="Leg times from OSM coordinates (haversine / mode heuristics).",
                source_id="travel-time-mcp",
            )
        )

    # POI sources: only stops on the displayed itinerary (OSM grounding).
    for day in itinerary.days:
        for stop in day.all_stops:
            osm_cites = [
                c for c in (stop.citations or []) if _is_osm_citation(c)
            ]
            if osm_cites:
                c0 = osm_cites[0]
                add(
                    Source(
                        title=stop.name or c0.title,
                        url=c0.url,
                        dataset="openstreetmap",
                        snippet=c0.snippet
                        or f"{stop.name} ({stop.category or 'poi'})",
                        source_id=c0.source_id
                        or f"{stop.osm_type}/{stop.osm_id}",
                    )
                )
            elif stop.osm_type and stop.osm_id is not None:
                add(
                    Source(
                        title=stop.name or f"OSM {stop.osm_type}/{stop.osm_id}",
                        url=(
                            f"https://www.openstreetmap.org/"
                            f"{stop.osm_type}/{stop.osm_id}"
                        ),
                        dataset="openstreetmap",
                        snippet=f"{stop.name} ({stop.category or 'poi'})",
                        source_id=f"{stop.osm_type}/{stop.osm_id}",
                    )
                )
    return sources


def _strip_rag_citations_from_days(days: list) -> list:
    """Keep OSM stop grounding only — drop leftover RAG cites from prior turns."""
    out = []
    for day in days:
        d = day.model_copy(deep=True)
        for block_name in ("morning", "afternoon", "evening"):
            block: TimeBlock = getattr(d, block_name)
            new_stops: list[Stop] = []
            for stop in block.stops:
                osm_only = [c for c in (stop.citations or []) if _is_osm_citation(c)]
                new_stops.append(stop.model_copy(update={"citations": osm_only}))
            setattr(
                d,
                block_name,
                TimeBlock(time_of_day=block_name, stops=new_stops, notes=block.notes),
            )
        out.append(d)
    return out


def _trace_append(state: GraphState, entry: dict[str, Any]) -> list[dict[str, Any]]:
    del state
    return [entry]


def _ensure_stop_uncertainty(itinerary: Itinerary) -> Itinerary:
    days = []
    for day in itinerary.days:
        d = day.model_copy(deep=True)
        for bn in ("morning", "afternoon", "evening"):
            block = getattr(d, bn)
            fixed = []
            for s in block.stops:
                if not s.citations and not s.uncertainty:
                    s = s.model_copy(
                        update={
                            "uncertainty": (
                                "Citation pending; OSM id is the place ground truth."
                            )
                        }
                    )
                fixed.append(s)
            setattr(d, bn, TimeBlock(time_of_day=bn, stops=fixed, notes=block.notes))
        days.append(d)
    return itinerary.model_copy(update={"days": days})


def _narrative_polish(
    itinerary: Itinerary,
    *,
    intent: str,
) -> tuple[str | None, str | None, str]:
    """Optional LLM narrative only — never changes days/stops."""
    if not llm_enabled("SYNTHESIS_LLM") and not llm_enabled("MERGER_LLM"):
        return itinerary.summary, None, "heuristic"
    trip = itinerary.trip
    audience = profile_label(getattr(trip, "traveler_profile", None))
    scope = _scope_notes(trip)
    system = (
        "You are the Synthesis / Presentation Agent. The itinerary structure is FINAL. "
        "Do not move, skip, or reorder stops. Write a short summary and user_reply. "
        "Do not invent free-time filler like 'open for exploration' for empty slots — "
        "describe the stops that exist. "
        "Mention the traveler audience (kid/senior/couple/friends) and interests when present. "
        "If planning_notes include a day-count scope cap, mention it briefly first. "
        'Return ONLY JSON: {"summary":"...","user_reply":"..."}'
    )
    data = chat_json(
        system=system,
        human=json_dumps_safe(
            {
                "intent": intent,
                "traveler_profile": getattr(trip, "traveler_profile", None),
                "audience_label": audience,
                "planning_notes": scope,
                "interests": list(trip.interests or []),
                "itinerary": compact_itinerary(itinerary),
            }
        ),
        model_env="SYNTHESIS_MODEL"
        if os.getenv("SYNTHESIS_MODEL")
        else "MERGER_MODEL",
    )
    if not data:
        return itinerary.summary, None, "heuristic"
    summary = data.get("summary") if isinstance(data.get("summary"), str) else None
    reply = data.get("user_reply") if isinstance(data.get("user_reply"), str) else None
    return (
        (summary.strip()[:600] if summary else itinerary.summary),
        (reply.strip()[:500] if reply else None),
        "llm",
    )


def json_dumps_safe(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


def synthesis_node(state: GraphState) -> dict[str, Any]:
    intent = state.get("intent") or "plan"
    plan = as_dispatch(state.get("dispatch_plan"))
    trip = as_trip(state.get("trip_constraints"))
    knowledge = as_knowledge(state.get("knowledge_results"))
    weather = as_weather(state.get("weather_results"))
    travel = as_travel(state.get("travel_time_results"))
    draft = as_draft(state.get("itinerary_draft"))
    prev = as_itinerary(state.get("previous_itinerary"))
    patches = as_edit_patches(state.get("edit_patches"))
    if not patches:
        one = as_edit_patch(state.get("edit_patch") or (plan.edit_patch if plan else None))
        patches = [one] if one else []
    patch = patches[0] if patches else None

    if intent == "explain":
        from agent.nodes.llm_utils import compose_grounded_reply
        from agent.rag.retrieve import knowledge_search

        base = as_itinerary(state.get("merged_itinerary")) or prev
        msg = (state.get("user_message") or "").lower()
        city = (
            (base.trip.city if base and base.trip else None)
            or (trip.city if trip else None)
            or "Jaipur"
        )
        snippets = list(knowledge.snippets) if knowledge else []
        sources = sources_from_knowledge(knowledge) if knowledge else []
        reply_parts: list[str] = []
        used_rag_llm = False

        # “Why did you pick X?” — fuzzy-match itinerary stops (same helper as hours Q&A).
        planner_why_q = bool(
            re.search(r"\bwhy (did you |do you )?(pick|choose|include)\b", msg)
        )
        planner_why_answered = False
        if base and re.search(r"\bwhy\b", msg):
            from agent.rag.itinerary_place import match_itinerary_place

            named: Stop | None = None
            itin_match = match_itinerary_place(msg, base, city=city)
            if itin_match and not itin_match.needs_confirm:
                for day in base.days:
                    for s in day.all_stops:
                        if (s.name or "").strip().lower() == itin_match.stop_name.lower():
                            named = s
                            break
                    if named:
                        break
            elif itin_match and itin_match.needs_confirm:
                reply_parts.append(
                    f"Did you mean **{itin_match.stop_name}** on this itinerary? "
                    "Say yes and I’ll explain why it was included."
                )
                planner_why_answered = True
            if named:
                # Plan-first only — no RAG paste on “why did you pick …”.
                reply_parts.append(_planner_why_for_stop(named, base))
                planner_why_answered = True
            elif not reply_parts and planner_why_q:
                reply_parts.append(
                    "I don’t see that place on this itinerary — "
                    "ask about a stop that’s listed, or say "
                    "“tell me more about …” for a general guide tip."
                )
                planner_why_answered = True

        doability_q = bool(
            re.search(
                r"\b(doable|feasible|packed|finish|too much|manageable|"
                r"realistic|overwhelming|stretch)\b",
                msg,
            )
        )
        # Doability: itinerary-only assessment — no edit notes, no free RAG.
        if base and doability_q:
            reply_parts.append(_assess_doability(base))

        # Rain: itinerary day + Weather MCP (never invent outdoor risk).
        if re.search(r"\b(rain|weather)\b", msg) and base:
            from agent.nodes.edit_apply import OUTDOOR_CATEGORIES

            _day_words = {
                "one": 1,
                "first": 1,
                "two": 2,
                "second": 2,
                "three": 3,
                "third": 3,
                "four": 4,
                "fourth": 4,
            }
            day_m_obj = re.search(
                r"\bday\s*(?:([1-4])|(one|two|three|four|first|second|third|fourth))\b",
                msg,
            )
            day_m: int | None = None
            rain_day = 1
            if day_m_obj:
                if day_m_obj.group(1):
                    day_m = int(day_m_obj.group(1))
                    rain_day = day_m
                elif day_m_obj.group(2):
                    day_m = _day_words.get(day_m_obj.group(2), 1)
                    rain_day = day_m
            if state.get("rain_day_index"):
                rain_day = int(state.get("rain_day_index"))
            day_plan = next(
                (d for d in base.days if d.day_index == rain_day), None
            )
            if day_plan is None and day_m is None:
                reply_parts.append(
                    "Which day should I check for rain (Day 1–4), and what is "
                    "your trip start date so I can use the Weather MCP forecast?"
                )
            elif day_plan is None:
                reply_parts.append(
                    f"Day {rain_day} isn’t in this itinerary — tell me which day to check."
                )
            else:
                outdoor = [
                    s.name
                    for s in day_plan.all_stops
                    if (s.category or "").lower() in OUTDOOR_CATEGORIES
                    or "park" in s.name.lower()
                    or "garden" in s.name.lower()
                ]
                indoor = [s.name for s in day_plan.all_stops if s.name not in outdoor]
                wday = None
                if weather and getattr(weather, "days", None):
                    idx = max(0, int(rain_day) - 1)
                    if idx < len(weather.days):
                        wday = weather.days[idx]
                if outdoor:
                    reply_parts.append(
                        f"Day {rain_day} still has outdoor stop(s): "
                        f"{', '.join(outdoor)}. Prefer indoor backups if it rains."
                    )
                else:
                    cats = sorted(
                        {
                            (s.category or "indoor").lower()
                            for s in day_plan.all_stops
                        }
                    )
                    reply_parts.append(
                        f"Day {rain_day} is already indoor-friendly "
                        f"({', '.join(cats) or 'indoor stops'}"
                        f"{': ' + ', '.join(indoor[:4]) if indoor else ''}). "
                        "Rain should not disrupt that day."
                    )
                if wday is not None:
                    risk = getattr(wday, "rain_risk", None) or "unknown"
                    prob = getattr(wday, "precip_probability_max", None)
                    rec = getattr(wday, "recommendation", None) or ""
                    prob_bit = (
                        f", rain chance about {prob:.0f}%"
                        if isinstance(prob, (int, float))
                        else ""
                    )
                    reply_parts.append(
                        f"Weather MCP for itinerary Day {rain_day}: "
                        f"rain risk {risk}{prob_bit}. {rec}".strip()
                    )
                elif weather and weather.missing_data:
                    reply_parts.append(
                        "Weather MCP data is missing right now — "
                        "share a trip start date and I can retry the forecast."
                    )
                else:
                    reply_parts.append(
                        "Share your trip start date for a dated Open-Meteo forecast; "
                        "without it I can still judge indoor vs outdoor from the plan."
                    )
                if weather and not weather.missing_data:
                    sources = list(sources) + [
                        Source(
                            title="Open-Meteo forecast",
                            dataset="open-meteo",
                            snippet="Daily weather used for rain-risk context.",
                        )
                    ]

        # Additional RAG via LLM (avoid dumping raw catalog lines twice).
        # Doability / plan-why answers stay itinerary-led — never pad with
        # unrelated tips.
        if (
            snippets
            and not used_rag_llm
            and not doability_q
            and not planner_why_answered
            and not re.search(r"\b(why|rain)\b", msg)
        ):
            rag_inputs = []
            for snip in snippets[:4]:
                if not snip.citations:
                    continue
                c0 = snip.citations[0]
                rag_inputs.append(
                    {"text": snip.text, "title": c0.title, "url": c0.url}
                )
            grounded = compose_grounded_reply(
                user_query=state.get("user_message") or msg,
                sources=rag_inputs,
                role_hint="travel tip",
            )
            if grounded:
                reply_parts.append(grounded)
                used_rag_llm = True
            else:
                from agent.nodes.llm_utils import format_source_cite

                for snip in snippets[:2]:
                    if not snip.citations:
                        continue
                    cite = snip.citations[0]
                    cite_bit = format_source_cite(cite, text=snip.text)
                    tip = re.sub(r"\s+", " ", (snip.text or "")).strip()
                    tip = re.sub(r"^\d+\s+", "", tip)[:240]
                    if tip:
                        reply_parts.append(tip + cite_bit)

        if knowledge and knowledge.missing_data and not reply_parts:
            reply = knowledge.notes or "I don't have cited tips for that (data missing)."
        elif reply_parts:
            reply = " ".join(reply_parts)
        else:
            reply = (
                "No grounded explanation available yet — "
                "ask a safety/etiquette tip, or plan a trip first for stop justifications."
            )

        itinerary_owned = doability_q or planner_why_answered
        explain_mode = (
            "doability"
            if doability_q
            else "planner_why"
            if planner_why_answered
            else "grounded"
        )
        grounding_docs: list[dict[str, Any]] = []
        if not itinerary_owned:
            for snip in snippets[:4]:
                if not snip.citations:
                    continue
                cite = snip.citations[0]
                grounding_docs.append(
                    {
                        "title": cite.title,
                        "url": cite.url,
                        "dataset": cite.dataset,
                        "source_id": cite.source_id,
                        "text": re.sub(r"\s+", " ", (snip.text or "")).strip(),
                    }
                )
        out: dict[str, Any] = {
            "user_reply": reply[:1200],
            # Why-pick / doability answers are itinerary-owned — do not log RAG sources.
            "sources": [] if itinerary_owned else [dump(s) for s in sources],
            "grounding_documents": [] if itinerary_owned else grounding_docs,
            "knowledge_results": None if itinerary_owned else state.get("knowledge_results"),
            "agent_trace": _trace_append(
                state,
                {
                    "agent": "synthesis",
                    "mode": explain_mode,
                    "action": "explain",
                    "used_itinerary": bool(base),
                    "used_weather": bool(weather) and not itinerary_owned,
                    "used_rag_llm": used_rag_llm and not itinerary_owned,
                    "snippet_count": 0 if itinerary_owned else len(snippets),
                },
            ),
        }
        if base:
            out["merged_itinerary"] = dump(base)
        logger.info("NODE synthesis explain grounded")
        return out

    if prev and patch and (intent == "edit" or bool(patches)):
        # Structural edit already applied by Itinerary Agent when routed there.
        # Presentation: compose narrative from latest draft if present, else prev.
        # Prefer merged days from draft so scoped stops stick even after revise.
        merged_live = as_itinerary(state.get("merged_itinerary"))
        trip_for_edit = (
            (merged_live.trip if merged_live and merged_live.trip else None)
            or (prev.trip if prev else None)
        )
        source_itin = (
            Itinerary(
                trip=trip_for_edit,
                days=list(draft.days),
                sources=[],
                reasoning=list(getattr(draft, "optimization_reasoning", None) or []),
            )
            if draft and draft.days
            else (merged_live or prev).model_copy(deep=True)
        )
        # Pace may have changed via relax/balance/pack voice edits.
        edit_pace = (
            getattr(draft, "pace", None)
            or getattr(getattr(source_itin, "trip", None), "pace", None)
            or getattr(prev.trip, "pace", None)
            or "moderate"
        )
        if source_itin.trip and edit_pace in ("relaxed", "moderate", "packed"):
            if source_itin.trip.pace != edit_pace:
                source_itin = source_itin.model_copy(
                    update={
                        "trip": source_itin.trip.model_copy(
                            update={"pace": edit_pace, "pace_known": True}
                        )
                    }
                )
        days = _strip_rag_citations_from_days(list(source_itin.days))
        mutate_days = {int(p.target.day) for p in patches}
        # Only restamp travel on edited day(s) — other days stay byte-identical.
        days, travel_stamped = _stamp_travel_via_mcp(
            days, mutate_days=mutate_days, pace=edit_pace
        )
        if travel and getattr(travel, "legs", None) and not travel_stamped.legs:
            days = _apply_travel_times(days, travel)
            days = stamp_schedule_clocks(
                days, pace=edit_pace  # type: ignore[arg-type]
            )
            days, _ = annotate_block_flex_time(
                days,
                pace=edit_pace,  # type: ignore[arg-type]
            )
            days = refresh_day_themes(days)
            travel_stamped = travel
        # Hard preserve: restore non-target days from previous itinerary exactly.
        prev_by_day = {d.day_index: d for d in prev.days}
        days = [
            prev_by_day[d.day_index].model_copy(deep=True)
            if d.day_index not in mutate_days and d.day_index in prev_by_day
            else d
            for d in days
        ]
        # Rebuild travel summary legs from final days without mutating others.
        _, travel_stamped = _stamp_travel_via_mcp(
            days, mutate_days=mutate_days, pace=edit_pace
        )
        ops = "+".join(p.operation for p in patches)
        days_label = ", ".join(f"Day {d}" for d in sorted(mutate_days))
        reasoning = list(getattr(draft, "optimization_reasoning", None) or []) or [
            f"Presented scoped edit on {days_label} ({ops}).",
            "Itinerary Agent owns structural changes; Synthesis composes the response.",
        ]
        composed = source_itin.model_copy(
            update={
                "days": days,
                "summary": (
                    f"Updated {days_label} ({ops}); other days unchanged."
                ),
                "reasoning": reasoning,
            }
        )
        composed = _ensure_stop_uncertainty(composed)
        sources = _collect_sources(
            knowledge=None,
            weather=weather,
            travel=travel_stamped,
            itinerary=composed,
            include_rag=False,
        )
        composed = composed.model_copy(
            update={"sources": sources, "summary": None}
        )
        reply = _format_readable_itinerary(composed, travel=travel_stamped)
        edit_notes = [
            n
            for n in (getattr(draft, "optimization_reasoning", None) or [])
            if isinstance(n, str)
        ]
        fail_notes = [
            n
            for n in edit_notes
            if n.lower().startswith("could not ")
        ]
        if fail_notes:
            reply = fail_notes[0] + "\n\n" + reply
        else:
            lead = [
                n
                for n in edit_notes
                if n.lower().startswith(
                    (
                        "added ",
                        "swapped ",
                        "relaxed ",
                        "packed ",
                        "balanced ",
                        "removed ",
                        "day ",
                    )
                )
            ]
            if lead:
                reply = lead[0] + " Other days unchanged.\n\n" + reply

        # Rain edits: append Weather MCP risk for the touched day.
        umsg = (state.get("user_message") or "").lower()
        if weather and re.search(r"\brain\b", umsg) and mutate_days:
            rain_day = sorted(mutate_days)[0]
            if getattr(weather, "days", None):
                idx = max(0, rain_day - 1)
                if idx < len(weather.days):
                    wday = weather.days[idx]
                    risk = getattr(wday, "rain_risk", None) or "unknown"
                    prob = getattr(wday, "precip_probability_max", None)
                    prob_bit = (
                        f", rain chance about {prob:.0f}%"
                        if isinstance(prob, (int, float))
                        else ""
                    )
                    reply = (
                        f"Weather MCP for Day {rain_day}: rain risk {risk}"
                        f"{prob_bit}. Outdoor stops were swapped indoors where needed.\n\n"
                        + reply
                    )
        logger.info("NODE synthesis EDIT day=%s mode=table", patch.target.day)
        return {
            "merged_itinerary": dump(composed),
            "previous_itinerary": dump(composed),
            "sources": [dump(s) for s in sources],
            "travel_time_results": dump(travel_stamped),
            "user_reply": reply[:4500],
            "agent_trace": _trace_append(
                state,
                {
                    "agent": "synthesis",
                    "mode": "table",
                    "action": f"present_edit:{patch.operation}",
                    "reasoning": reasoning,
                    "travel_legs": len(getattr(travel_stamped, "legs", None) or []),
                },
            ),
        }

    if trip is None or draft is None:
        logger.warning("NODE synthesis missing trip/draft")
        return {
            "user_reply": state.get("user_reply")
            or "I couldn't compose a response — missing trip details or itinerary.",
            "agent_trace": _trace_append(
                state,
                {"agent": "synthesis", "mode": "heuristic", "action": "missing"},
            ),
        }

    uncertainty: list[str] = []
    if draft.notes:
        uncertainty.append(draft.notes)
    # Plan path: do not surface topic-RAG notes (knowledge is explain-only).
    if weather and weather.notes:
        uncertainty.append(weather.notes)
    if travel and travel.notes:
        uncertainty.append(travel.notes)

    # Presentation only: OSM-grounded stops + travel labels; no RAG attach on plan.
    days = _strip_rag_citations_from_days(list(draft.days))
    days, travel_stamped = _stamp_travel_via_mcp(days, pace=trip.pace)
    if travel and getattr(travel, "legs", None) and not travel_stamped.legs:
        days = _apply_travel_times(days, travel)
        days = stamp_schedule_clocks(
            days, pace=trip.pace or "moderate"  # type: ignore[arg-type]
        )
        days, _ = annotate_block_flex_time(
            days, pace=trip.pace or "moderate"  # type: ignore[arg-type]
        )
        days = refresh_day_themes(days)
        travel_stamped = travel
    reasoning = list(draft.optimization_reasoning or [])

    audience = profile_label(trip.traveler_profile)
    summary_core = (
        f"{trip.num_days}-day {trip.pace} "
        f"{(audience + ' ') if audience else ''}plan for {trip.city} "
        f"covering {', '.join(trip.interests) or 'general interests'}."
    )
    scope = _scope_notes(trip)
    if scope:
        summary_core = f"{scope[0]} {summary_core}"
    itinerary = Itinerary(
        trip=trip.model_copy(update={"confirmed": True}),
        days=days,
        summary=summary_core,
        uncertainty_notes=[u for u in uncertainty if u],
        sources=[],
        reasoning=reasoning,
    )
    itinerary = _ensure_stop_uncertainty(itinerary)
    sources = _collect_sources(
        knowledge=None,
        weather=weather,
        travel=travel_stamped,
        itinerary=itinerary,
        include_rag=False,
    )
    itinerary = itinerary.model_copy(update={"sources": sources})

    result = validate_itinerary(itinerary)
    if not result.ok or result.itinerary is None:
        logger.warning("NODE synthesis schema invalid: %s", result.errors)
        return {
            "user_reply": "Itinerary failed schema validation during presentation.",
            "merged_itinerary": dump(itinerary),
            "sources": [dump(s) for s in sources],
            "travel_time_results": dump(travel_stamped),
            "agent_trace": _trace_append(
                state,
                {
                    "agent": "synthesis",
                    "mode": "heuristic",
                    "action": "schema_invalid",
                    "reasoning": reasoning,
                },
            ),
        }

    out_itin = result.itinerary
    grounding = validate_grounding_rules(out_itin)
    if grounding:
        notes = list(out_itin.uncertainty_notes) + grounding
        out_itin = out_itin.model_copy(update={"uncertainty_notes": notes})

    # Table-only presentation — no LLM narrative summary.
    out_itin = out_itin.model_copy(update={"summary": None})
    reply = _format_readable_itinerary(out_itin, travel=travel_stamped)

    logger.info(
        "NODE synthesis → days=%d stops=%d sources=%d travel_legs=%d mode=table",
        len(out_itin.days),
        sum(len(d.all_stops) for d in out_itin.days),
        len(sources),
        len(getattr(travel_stamped, "legs", None) or []),
    )
    return {
        "merged_itinerary": dump(out_itin),
        "previous_itinerary": dump(out_itin),
        "sources": [dump(s) for s in sources],
        "travel_time_results": dump(travel_stamped),
        "user_reply": reply[:4500],
        "agent_trace": _trace_append(
            state,
            {
                "agent": "synthesis",
                "mode": "table",
                "action": "compose",
                "reasoning": reasoning,
                "travel_legs": len(getattr(travel_stamped, "legs", None) or []),
                "tool": "travel_time_estimator_mcp",
            },
        ),
    }


# Backward-compatible alias
merger_node = synthesis_node
