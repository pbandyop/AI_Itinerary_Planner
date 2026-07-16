"""Itinerary Agent optimization — constraint resolution & plan decisions.

Ownership: **Itinerary Agent** decides move / skip / reorder using POI,
weather, travel, knowledge, and user preferences. The Synthesis Agent must
not change the itinerary structure.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from agent.mcp.geo import haversine_km
from agent.mcp.itinerary_builder import reassert_meal_pace_layout
from agent.nodes.llm_utils import chat_json, compact_itinerary, llm_enabled
from agent.place_identity import PlaceSeen, dedupe_day_plans
from agent.schemas.itinerary import DayPlan, Itinerary, Stop, TimeBlock, TimeOfDay

logger = logging.getLogger(__name__)

_OUTDOOR = {
    "heritage",
    "park",
    "viewpoint",
    "attraction",
    "temple",
    "market",
    "fort",
    "palace",
}
_INDOOR = {"museum", "food", "cafe", "indoor"}


def _is_outdoor(stop: Stop) -> bool:
    cat = (stop.category or "").lower()
    name = (stop.name or "").lower()
    if any(k in cat or k in name for k in _INDOOR):
        return False
    if any(k in cat or k in name for k in _OUTDOOR):
        return True
    # Default: treat unknown heritage-ish names as outdoor-ish
    if any(k in name for k in ("fort", "palace", "garden", "mahal", "ghat")):
        return True
    return False


def _clone_days(days: list[DayPlan]) -> list[DayPlan]:
    return [d.model_copy(deep=True) for d in days]


def _find_stop(
    days: list[DayPlan], osm_id: int
) -> tuple[int, TimeOfDay, int, Stop] | None:
    for di, day in enumerate(days):
        for bn in ("morning", "afternoon", "evening"):
            block: TimeBlock = getattr(day, bn)
            for si, stop in enumerate(block.stops):
                if stop.osm_id == osm_id:
                    return di, bn, si, stop  # type: ignore[return-value]
    return None


def _remove_stop(days: list[DayPlan], osm_id: int) -> Stop | None:
    found = _find_stop(days, osm_id)
    if not found:
        return None
    di, bn, si, stop = found
    block: TimeBlock = getattr(days[di], bn)
    stops = list(block.stops)
    removed = stops.pop(si)
    if stops:
        stops[-1] = stops[-1].model_copy(
            update={
                "travel_to_next_min": None,
                "travel_to_next_km": None,
                "travel_to_next_mode": None,
            }
        )
    setattr(
        days[di],
        bn,
        TimeBlock(time_of_day=bn, stops=stops, notes=block.notes),  # type: ignore[arg-type]
    )
    return removed


def _insert_stop(
    days: list[DayPlan],
    stop: Stop,
    *,
    day_index: int,
    block_name: TimeOfDay,
) -> bool:
    for day in days:
        if day.day_index != day_index:
            continue
        block: TimeBlock = getattr(day, block_name)
        stops = list(block.stops)
        # Soft capacity: packed mornings may hold up to 4
        if len(stops) >= 4:
            return False
        stops.append(
            stop.model_copy(
                update={
                    "travel_to_next_min": None,
                    "travel_to_next_km": None,
                    "travel_to_next_mode": None,
                }
            )
        )
        setattr(
            day,
            block_name,
            TimeBlock(time_of_day=block_name, stops=stops, notes=block.notes),
        )
        return True
    return False


def _reorder_block_nearest(stops: list[Stop]) -> list[Stop]:
    if len(stops) <= 2:
        return stops
    with_coords = [s for s in stops if s.lat is not None and s.lon is not None]
    without = [s for s in stops if s.lat is None or s.lon is None]
    if len(with_coords) <= 1:
        return stops
    ordered: list[Stop] = [with_coords[0]]
    remaining = with_coords[1:]
    while remaining:
        last = ordered[-1]
        remaining.sort(
            key=lambda s: haversine_km(
                last.lat or 0, last.lon or 0, s.lat or 0, s.lon or 0
            )
        )
        ordered.append(remaining.pop(0))
    out = ordered + without
    for i in range(len(out) - 1):
        a, b = out[i], out[i + 1]
        if a.lat is not None and b.lat is not None:
            # ~4 min per km city heuristic (matches travel MCP roughly)
            mins = max(5, int(haversine_km(a.lat, a.lon or 0, b.lat, b.lon or 0) * 4))
            out[i] = a.model_copy(
                update={
                    "travel_to_next_min": mins,
                    "travel_to_next_km": None,
                    "travel_to_next_mode": None,
                }
            )
        else:
            out[i] = a.model_copy(update={"travel_to_next_min": a.travel_to_next_min})
    out[-1] = out[-1].model_copy(
        update={
            "travel_to_next_min": None,
            "travel_to_next_km": None,
            "travel_to_next_mode": None,
        }
    )
    return out


def _preserve_meal_block_order(
    stops: list[Stop],
    *,
    block_name: TimeOfDay,
) -> list[Stop]:
    """Keep breakfast food first / dinner food last; ≤1 food per block."""
    foods = [s for s in stops if (s.category or "").lower() == "food"]
    rest = [s for s in stops if (s.category or "").lower() != "food"]
    if not foods:
        return list(stops)
    food = foods[0]
    if block_name == "morning":
        return [food] + rest
    if block_name == "evening":
        return rest + [food]
    out: list[Stop] = []
    seen_food = False
    for s in stops:
        if (s.category or "").lower() == "food":
            if seen_food:
                continue
            seen_food = True
        out.append(s)
    return out


def _trip_interests(itinerary: Itinerary) -> list[str]:
    trip = itinerary.trip
    if trip is None:
        return []
    return list(trip.interests or [])


def _trip_pace(itinerary: Itinerary) -> str:
    trip = itinerary.trip
    if trip is None or not trip.pace:
        return "relaxed"
    return str(trip.pace)


def _finalize_meal_pace(
    itinerary: Itinerary,
    reasoning: list[str],
) -> tuple[Itinerary, list[str]]:
    """Always reassert pace/meal layout after optimize moves."""
    days, notes = reassert_meal_pace_layout(
        list(itinerary.days),
        pace=_trip_pace(itinerary),  # type: ignore[arg-type]
        interests=_trip_interests(itinerary),
    )
    reasoning = list(dict.fromkeys([*reasoning, *notes]))
    return itinerary.model_copy(update={"days": days, "reasoning": reasoning}), reasoning


def _knowledge_morning_names(knowledge: Any) -> set[str]:
    names: set[str] = set()
    if not knowledge or not getattr(knowledge, "snippets", None):
        return names
    for snip in knowledge.snippets:
        text = (snip.text or "").lower()
        if "morning" not in text and "am " not in text:
            continue
        # Capture capitalized place-like phrases near "morning"
        for m in re.finditer(
            r"([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3}).{0,40}morning|"
            r"morning.{0,40}([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3})",
            snip.text or "",
            flags=re.IGNORECASE,
        ):
            g = m.group(1) or m.group(2)
            if g:
                names.add(g.strip().lower())
        # Also match known stop names appearing with morning in same snippet
        if "best visited in morning" in text or "visit in the morning" in text:
            # leave names empty — caller matches stop names inside text
            pass
    return names


def _stop_prefers_morning(stop: Stop, knowledge: Any) -> bool:
    if not knowledge or not getattr(knowledge, "snippets", None):
        return False
    name = (stop.name or "").lower()
    for snip in knowledge.snippets:
        text = (snip.text or "").lower()
        if name and name in text and "morning" in text:
            return True
    return False


def heuristic_synthesize(
    itinerary: Itinerary,
    *,
    weather: Any = None,
    knowledge: Any = None,
    travel: Any = None,
) -> tuple[Itinerary, list[str], str]:
    """Resolve weather / knowledge / travel conflicts without inventing POIs."""
    days = _clone_days(itinerary.days)
    reasoning: list[str] = []

    # --- Weather: move outdoor afternoon stops off rainy afternoons ---
    rain_days: set[int] = set()
    if weather and getattr(weather, "days", None):
        for i, wday in enumerate(weather.days):
            risk = getattr(wday, "rain_risk", None) or ""
            if str(risk).lower() in {"high", "moderate"}:
                # day_index is 1-based; weather.days aligned to itinerary days
                if i < len(days):
                    rain_days.add(days[i].day_index)

    for day in list(days):
        if day.day_index not in rain_days:
            continue
        afternoon = list(day.afternoon.stops)
        kept: list[Stop] = []
        for stop in afternoon:
            if not _is_outdoor(stop):
                kept.append(stop)
                continue
            # Prefer same-day morning, else earliest non-rain morning
            moved = False
            if _insert_stop(
                days, stop, day_index=day.day_index, block_name="morning"
            ):
                reasoning.append(
                    f"Moved {stop.name} to Day {day.day_index} morning "
                    f"to avoid rain on Day {day.day_index} afternoon."
                )
                moved = True
            else:
                for alt in days:
                    if alt.day_index in rain_days:
                        continue
                    if _insert_stop(
                        days, stop, day_index=alt.day_index, block_name="morning"
                    ):
                        reasoning.append(
                            f"Moved {stop.name} to Day {alt.day_index} morning "
                            f"because Day {day.day_index} afternoon has rain risk."
                        )
                        moved = True
                        break
            if not moved:
                # Skip outdoor stop on rainy afternoon — genuine constraint resolve
                reasoning.append(
                    f"Skipped {stop.name} on Day {day.day_index} afternoon "
                    "because heavy/moderate rain makes an outdoor visit impractical."
                )
            # If moved or skipped, do not keep in rainy afternoon
        # Rebuild afternoon without moved/skipped outdoor stops
        day_ref = next(d for d in days if d.day_index == day.day_index)
        day_ref.afternoon = TimeBlock(
            time_of_day="afternoon",
            stops=kept,
            notes=(
                ((day_ref.afternoon.notes or "") + " [synthesis: rain-aware]").strip()
            ),
        )

    # --- Knowledge: pull morning-preferred stops into morning ---
    for day in days:
        for bn in ("afternoon", "evening"):
            block: TimeBlock = getattr(day, bn)
            remaining: list[Stop] = []
            for stop in list(block.stops):
                if _stop_prefers_morning(stop, knowledge):
                    if _insert_stop(
                        days, stop, day_index=day.day_index, block_name="morning"
                    ):
                        reasoning.append(
                            f"Scheduled {stop.name} in the morning "
                            "(Knowledge Agent tip)."
                        )
                        continue
                remaining.append(stop)
            setattr(
                day,
                bn,
                TimeBlock(time_of_day=bn, stops=remaining, notes=block.notes),  # type: ignore[arg-type]
            )

    # --- Travel: cluster stops within each block by nearest neighbor ---
    clustered = False
    for day in days:
        for bn in ("morning", "afternoon", "evening"):
            block: TimeBlock = getattr(day, bn)
            if len(block.stops) < 2:
                continue
            new_stops = _preserve_meal_block_order(
                _reorder_block_nearest(list(block.stops)),
                block_name=bn,  # type: ignore[arg-type]
            )
            if [s.osm_id for s in new_stops] != [s.osm_id for s in block.stops]:
                clustered = True
            setattr(
                day,
                bn,
                TimeBlock(time_of_day=bn, stops=new_stops, notes=block.notes),  # type: ignore[arg-type]
            )
    if clustered:
        reasoning.append("Reduced travel by clustering nearby POIs within each block.")
    elif travel and getattr(travel, "legs", None):
        long_legs = [
            leg
            for leg in travel.legs
            if getattr(leg, "duration_min", 0) and leg.duration_min >= 40
        ]
        if long_legs:
            reasoning.append(
                "Noted longer travel legs from Travel Agent; kept draft order "
                "where clustering could not improve further."
            )

    if not reasoning:
        reasoning.append(
            "Synthesized specialist outputs with no hard weather/knowledge conflicts."
        )

    days, bal_notes = _ensure_am_pm_balance(days)
    reasoning.extend(bal_notes)
    days, dedupe_notes = dedupe_day_plans(days)
    reasoning.extend(dedupe_notes)

    out = itinerary.model_copy(
        update={"days": days, "reasoning": list(dict.fromkeys(reasoning))}
    )
    out, reasoning = _finalize_meal_pace(out, list(out.reasoning))
    return out, list(out.reasoning), "heuristic"


def _ensure_am_pm_balance(days: list[DayPlan]) -> tuple[list[DayPlan], list[str]]:
    """Every day should have ≥1 morning and ≥1 afternoon stop when enough stops exist.

    Does not invent places. Moves a morning stop into afternoon (or vice versa)
    only when that day already has 2+ distinct stops.
    """
    notes: list[str] = []
    days = _clone_days(days)
    for day in days:
        # Deduplicate within day by OSM id or near-duplicate name.
        seen = PlaceSeen()
        for bn in ("morning", "afternoon", "evening"):
            block: TimeBlock = getattr(day, bn)
            kept: list[Stop] = []
            for s in block.stops:
                if seen.contains(s):
                    continue
                seen.add(s)
                kept.append(s)
            setattr(
                day,
                bn,
                TimeBlock(time_of_day=bn, stops=kept, notes=block.notes),  # type: ignore[arg-type]
            )

        am = list(day.morning.stops)
        pm = list(day.afternoon.stops)
        eve = list(day.evening.stops)

        # Prefer filling afternoon from evening soft stops — never steal the
        # only dinner food, and prefer splitting morning before emptying evening.
        if not pm and eve:
            soft = [
                s for s in eve if (s.category or "").lower() != "food"
            ]
            if soft:
                moved = soft[0]
                eve = [s for s in eve if s.osm_id != moved.osm_id]
                pm = [moved]
                notes.append(
                    f"Day {day.day_index}: moved an evening soft stop into afternoon "
                    "so the day has both morning and afternoon."
                )
            elif len(am) >= 2:
                # Keep dinner in evening; pull from morning instead.
                non_bfast = [
                    s
                    for s in am
                    if not (
                        (s.category or "").lower() == "food"
                        and s is am[0]
                        and (am[0].category or "").lower() == "food"
                    )
                ]
                if non_bfast:
                    moved = non_bfast[-1]
                else:
                    moved = am[-1]
                am = [s for s in am if s.osm_id != moved.osm_id]
                pm = [moved]
                notes.append(
                    f"Day {day.day_index}: moved a morning stop into afternoon "
                    "(kept evening dinner)."
                )
            else:
                # Last resort: move evening food to afternoon only if morning exists.
                if am:
                    pm = [eve.pop(0)]
                    notes.append(
                        f"Day {day.day_index}: moved an evening stop into afternoon "
                        "so the day has both morning and afternoon."
                    )
        if not am and pm:
            # Prefer non-food for morning fill; food can still land first via reassert.
            am = [pm.pop(0)]
            notes.append(
                f"Day {day.day_index}: moved an afternoon stop into morning "
                "so the day has both morning and afternoon."
            )
        if not pm and len(am) >= 2:
            # Don't move breakfast food if another stop exists.
            if (am[0].category or "").lower() == "food" and len(am) >= 2:
                pm = [am.pop()]  # last non-breakfast
            else:
                pm = [am.pop()]
            notes.append(
                f"Day {day.day_index}: moved a morning stop into afternoon "
                "so morning and afternoon each have at least one place."
            )
        if not am and len(pm) >= 2:
            am = [pm.pop(0)]
            notes.append(
                f"Day {day.day_index}: moved an afternoon stop into morning "
                "so morning and afternoon each have at least one place."
            )

        day.morning = TimeBlock(time_of_day="morning", stops=am, notes=day.morning.notes)
        day.afternoon = TimeBlock(
            time_of_day="afternoon", stops=pm, notes=day.afternoon.notes
        )
        day.evening = TimeBlock(time_of_day="evening", stops=eve, notes=day.evening.notes)
    return days, notes


def _refill_empty_days(days: list[DayPlan]) -> tuple[list[DayPlan], list[str]]:
    """Move a stop from the fullest day into any empty day so no day is blank."""
    notes: list[str] = []
    days = _clone_days(days)
    for _ in range(len(days)):
        empty_idx = next((i for i, d in enumerate(days) if not d.all_stops), None)
        if empty_idx is None:
            break
        donor_idx = max(
            range(len(days)),
            key=lambda i: len(days[i].all_stops),
        )
        if len(days[donor_idx].all_stops) <= 1:
            break
        # Prefer moving an afternoon/evening stop from the donor
        moved: Stop | None = None
        for bn in ("evening", "afternoon", "morning"):
            block: TimeBlock = getattr(days[donor_idx], bn)
            if not block.stops:
                continue
            stop = block.stops[-1]
            moved = _remove_stop(days, stop.osm_id)
            break
        if moved is None:
            break
        target_day = days[empty_idx].day_index
        _insert_stop(days, moved, day_index=target_day, block_name="morning")
        notes.append(
            f"Refilled Day {target_day} with {moved.name} so no day is left empty."
        )
    return days, notes


def _apply_llm_actions(
    itinerary: Itinerary,
    actions: list[dict[str, Any]],
    reasoning: list[str],
) -> tuple[Itinerary, list[str]]:
    days = _clone_days(itinerary.days)
    valid_ids = {s.osm_id for d in days for s in d.all_stops}
    applied = list(reasoning)

    for raw in actions:
        if not isinstance(raw, dict):
            continue
        typ = str(raw.get("type") or "").lower()
        try:
            osm_id = int(raw.get("osm_id"))
        except (TypeError, ValueError):
            continue
        if osm_id not in valid_ids and typ != "skip":
            # skip may reference already-present id
            if osm_id not in {s.osm_id for d in days for s in d.all_stops}:
                continue

        if typ == "skip":
            removed = _remove_stop(days, osm_id)
            if removed:
                why = str(raw.get("reason") or "constraint conflict").strip()[:160]
                applied.append(f"Skipped {removed.name}: {why}")
            continue

        if typ == "move":
            to_day = int(raw.get("to_day") or 0)
            to_block = str(raw.get("to_block") or "morning").lower()
            if to_block not in {"morning", "afternoon", "evening"}:
                to_block = "morning"
            removed = _remove_stop(days, osm_id)
            if not removed:
                continue
            ok = _insert_stop(
                days,
                removed,
                day_index=to_day,
                block_name=to_block,  # type: ignore[arg-type]
            )
            if ok:
                applied.append(
                    f"Moved {removed.name} to Day {to_day} {to_block} "
                    f"({str(raw.get('reason') or 'synthesis').strip()[:120]})."
                )
            else:
                # Put back if move failed
                _insert_stop(
                    days,
                    removed,
                    day_index=min(to_day, days[-1].day_index),
                    block_name="evening",
                )
                applied.append(
                    f"Could not move {removed.name} as requested; left near original slot."
                )

    out = itinerary.model_copy(
        update={"days": days, "reasoning": list(dict.fromkeys(applied))}
    )
    return out, list(out.reasoning)


def llm_synthesize(
    itinerary: Itinerary,
    *,
    weather: Any = None,
    knowledge: Any = None,
    travel: Any = None,
    poi: Any = None,
) -> tuple[Itinerary, list[str], str] | None:
    """LLM constraint resolver — may move/skip existing stops only."""
    if not llm_enabled("ITINERARY_LLM") and not llm_enabled("MERGER_LLM"):
        return None

    weather_brief = []
    if weather and getattr(weather, "days", None):
        for i, d in enumerate(weather.days):
            weather_brief.append(
                {
                    "day_index": i + 1,
                    "rain_risk": getattr(d, "rain_risk", None),
                    "recommendation": getattr(d, "recommendation", None),
                }
            )
    knowledge_brief = []
    if knowledge and getattr(knowledge, "snippets", None):
        for s in knowledge.snippets[:5]:
            knowledge_brief.append((s.text or "")[:220])
    travel_brief = []
    if travel and getattr(travel, "legs", None):
        for leg in travel.legs[:8]:
            travel_brief.append(
                {
                    "from": getattr(leg, "from_name", None),
                    "to": getattr(leg, "to_name", None),
                    "duration_min": getattr(leg, "duration_min", None),
                }
            )
    poi_names = []
    if poi and getattr(poi, "pois", None):
        poi_names = [p.name for p in poi.pois[:12]]

    system = (
        "You are the Itinerary Agent optimizer for an India city travel planner. "
        "Specialists disagree sometimes (outdoor POI + rain, long travel, morning tips). "
        "You MUST resolve conflicts by rearranging or skipping EXISTING stops only. "
        "Never invent new places or OSM ids. Never rename stops. "
        "Prefer: move outdoor stops before rain; honor morning tips; cluster to cut travel; "
        "skip an outdoor stop on a heavy-rain afternoon if no better slot exists. "
        "CRITICAL: every day in the itinerary must keep at least one stop. "
        "Do not empty a day or leave it for vague 'open exploration'. "
        "MEAL/PACE RULES (must respect when food is among interests): "
        "morning food stop first (breakfast); evening dinner food last; "
        "at most one food per morning/afternoon/evening block; "
        "relaxed evening is dinner-only (do not park forts/heritage in evening); "
        "do not steal the only dinner food to fill afternoon. "
        "Return ONLY JSON: "
        '{"actions":[{"type":"move"|"skip","osm_id":123,"to_day":1,'
        '"to_block":"morning","reason":"..."}],'
        '"reasoning":["Outdoor attractions moved before rain.",'
        '"Reduced travel by clustering nearby POIs."],'
        '"summary":"...","user_reply":"..."}'
    )
    human = json.dumps(
        {
            "itinerary": compact_itinerary(itinerary),
            "stop_osm_ids": [
                {"name": s.name, "osm_id": s.osm_id, "category": s.category}
                for d in itinerary.days
                for s in d.all_stops
            ],
            "weather_days": weather_brief,
            "knowledge_snippets": knowledge_brief,
            "travel_legs": travel_brief,
            "poi_candidates": poi_names,
        },
        ensure_ascii=False,
    )
    data = chat_json(system=system, human=human, model_env="ITINERARY_MODEL")
    if not data:
        data = chat_json(system=system, human=human, model_env="MERGER_MODEL")
    if not data:
        return None

    reasoning = [
        str(r).strip()[:240]
        for r in (data.get("reasoning") or [])
        if str(r).strip()
    ]
    actions = data.get("actions") or []
    synthesized, reasoning = _apply_llm_actions(
        itinerary, actions if isinstance(actions, list) else [], reasoning
    )
    synthesized_days, refill_notes = _refill_empty_days(list(synthesized.days))
    if refill_notes:
        reasoning = list(dict.fromkeys([*reasoning, *refill_notes]))
        synthesized = synthesized.model_copy(
            update={"days": synthesized_days, "reasoning": reasoning}
        )

    updates: dict[str, Any] = {"reasoning": reasoning}
    if isinstance(data.get("summary"), str) and data["summary"].strip():
        updates["summary"] = data["summary"].strip()[:600]
    synthesized = synthesized.model_copy(update=updates)

    # Attach user_reply via summary field side-channel in caller
    synthesized._optimizer_user_reply = (  # type: ignore[attr-defined]
        str(data.get("user_reply") or "").strip()[:500] or None
    )
    logger.info(
        "Itinerary optimizer actions=%d reasoning=%s", len(actions), reasoning
    )
    return synthesized, reasoning, "llm"


def optimize_itinerary(
    itinerary: Itinerary,
    *,
    weather: Any = None,
    knowledge: Any = None,
    travel: Any = None,
    poi: Any = None,
) -> tuple[Itinerary, list[str], str, str | None]:
    """
    Run itinerary optimization: prefer LLM constraint resolver, else heuristic.

    Returns (itinerary, reasoning, mode, optional_user_reply).
    Always reasserts pace/meal layout so breakfast-first / dinner-last / caps stick.
    """
    llm = llm_synthesize(
        itinerary,
        weather=weather,
        knowledge=knowledge,
        travel=travel,
        poi=poi,
    )
    if llm:
        out, reasoning, mode = llm
        reply = getattr(out, "_optimizer_user_reply", None)
        if not any("cluster" in r.lower() or "travel" in r.lower() for r in reasoning):
            out2, r2, _ = heuristic_synthesize(
                out, weather=None, knowledge=None, travel=travel
            )
            extra = [x for x in r2 if "cluster" in x.lower() or "travel" in x.lower()]
            reasoning = list(dict.fromkeys([*reasoning, *extra]))
            out = out2.model_copy(update={"reasoning": reasoning, "summary": out.summary})
        days, bal = _ensure_am_pm_balance(list(out.days))
        reasoning = list(dict.fromkeys([*reasoning, *bal]))
        out = out.model_copy(update={"days": days, "reasoning": reasoning})
        out, reasoning = _finalize_meal_pace(out, reasoning)
        return out, reasoning, mode, reply

    out, reasoning, mode = heuristic_synthesize(
        itinerary, weather=weather, knowledge=knowledge, travel=travel
    )
    # heuristic_synthesize already finalizes; keep idempotent safety net
    out, reasoning = _finalize_meal_pace(out, reasoning)
    return out, reasoning, mode, None
