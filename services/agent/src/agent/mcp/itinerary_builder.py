"""Itinerary Builder MCP — pack POIs into day/time blocks (Phase 1 schema)."""

from __future__ import annotations

import logging
import re

from agent.mcp.geo import city_center, estimate_travel_minutes, haversine_km
from agent.place_identity import PlaceSeen, dedupe_day_plans, dedupe_pois
from agent.preferences import (
    categories_for_interest,
    categories_for_interests,
    interest_match_score,
    normalize_interest,
)
from agent.schemas.itinerary import (
    DayPlan,
    Pace,
    Source,
    Stop,
    TimeBlock,
    TripConstraints,
)
from agent.schemas.specialists import ItineraryDraftResult, POICandidate
from agent.trip_limits import MAX_TRIP_DAYS, MIN_TRIP_DAYS, clamp_trip_days

logger = logging.getLogger(__name__)

DEFAULT_DURATION: dict[str, int] = {
    "heritage": 90,
    "museum": 90,
    "attraction": 75,
    "temple": 60,
    "food": 60,
    "market": 75,
    "shopping": 75,
    "viewpoint": 45,
    "park": 45,
    "other": 60,
}

# Soft day-size hints (relaxed/balanced). Packed has no hard stop cap —
# blocks are filled by time windows (POIs first, then relax/rest notes).
STOPS_PER_DAY: dict[Pace, int] = {
    "relaxed": 4,  # morning 2 · afternoon 1 · evening 1 (dinner)
    "moderate": 6,  # morning 2 · afternoon 2 · evening 2 (soft + dinner)
    "packed": 24,  # generous pool only; densify fills by clock windows
}

# Non-food evening extras allowed before dinner (dinner stays last).
MAX_EVENING_EXTRAS: dict[Pace, int] = {
    "relaxed": 0,
    "moderate": 1,  # one low-priority soft stop + dinner
    "packed": 2,  # two soft stops + dinner
}

def _ideal_block_targets(pace: Pace) -> tuple[int, int, int]:
    """Preferred (morning, afternoon, evening) counts when the day pool is rich enough."""
    if pace == "relaxed":
        return (2, 1, 1)
    if pace == "packed":
        return (3, 3, 2)  # seed denser; densify grows further by time window
    return (2, 2, 2)  # moderate / balanced


def _adaptive_block_targets(pace: Pace, n: int) -> tuple[int, int, int]:
    """Soft-fill toward pace ideals; never invent stops.

    Shrink order when short: evening extras (e>1) → afternoon → morning
    (keep morning protected). Prefer keeping one evening slot when possible
    (e.g. 2-1-1 for four stops) rather than emptying evening (2-2-0).
    With exactly 3 POIs, prefer 1-1-1 so all three panels show rather than 2-1-0.
    Packed grows AM/PM/evening freely (no small hard cap); densify fills by time.
    """
    if n <= 0:
        return (0, 0, 0)
    if n == 1:
        return (1, 0, 0)
    if n == 2:
        return (1, 1, 0)
    if n == 3:
        return (1, 1, 1)

    if pace == "packed":
        # Start 2-2-2; stretch across blocks while POIs last (soft max 8/8/5).
        m, a, e = 2, 2, 2
        cap_m, cap_a, cap_e = 8, 8, 5
        use = min(n, cap_m + cap_a + cap_e)
        spare = use - (m + a + e)
        order = ["m", "a", "e"]
        oi = 0
        while spare > 0:
            slot = order[oi % 3]
            oi += 1
            if slot == "m" and m < cap_m:
                m += 1
                spare -= 1
            elif slot == "a" and a < cap_a:
                a += 1
                spare -= 1
            elif slot == "e" and e < cap_e:
                e += 1
                spare -= 1
            elif m >= cap_m and a >= cap_a and e >= cap_e:
                break
            else:
                continue
        while m + a + e > use:
            if e > 2:
                e -= 1
            elif a > 2:
                a -= 1
            elif m > 2:
                m -= 1
            elif e > 1:
                e -= 1
            elif a > 0:
                a -= 1
            elif m > 1:
                m -= 1
            else:
                break
        return (m, a, e)

    ideal_m, ideal_a, ideal_e = _ideal_block_targets(pace)
    use = min(n, ideal_m + ideal_a + ideal_e)
    m, a, e = ideal_m, ideal_a, ideal_e
    # Shrink extras first, then afternoon, then morning — keep a single
    # evening slot when possible (prefer 2-1-1 over 2-2-0 for n=4).
    while m + a + e > use:
        if e > 1:
            e -= 1
        elif a > 1:
            a -= 1
        elif m > 1:
            m -= 1
        elif e > 0:
            e -= 1
        elif a > 0:
            a -= 1
        else:
            break
    return (m, a, e)


def _breakfast_first(block: list[POICandidate]) -> list[POICandidate]:
    """Ensure the first stop is breakfast food when a food stop is in the block."""
    foods = [p for p in block if _is_food(p)]
    if not foods:
        return block
    rest = [p for p in block if not _is_food(p)]
    return [foods[0]] + rest + foods[1:]


def _dinner_last(block: list[POICandidate]) -> list[POICandidate]:
    """Ensure dinner food is the last stop when a food stop is in the evening block."""
    foods = [p for p in block if _is_food(p)]
    if not foods:
        return block
    rest = [p for p in block if not _is_food(p)]
    return rest + [foods[0]] + foods[1:]


def _duration_for(poi: POICandidate, pace: Pace, *, meal_slot: bool = False) -> int:
    """Visit length varies by pace — relaxed linger, packed move quicker.

    Meal-slot foods stay shorter so breakfast/lunch/dinner + sights fit the day window.
    """
    base = DEFAULT_DURATION.get(poi.category or "other", 60)
    if meal_slot and (poi.category or "").lower() == "food":
        return max(45, min(75, base))
    if pace == "relaxed":
        return min(180, int(base * 1.4))
    if pace == "packed":
        return max(30, int(base * 0.7))
    return base


def _order_nearest(
    pois: list[POICandidate],
    start: tuple[float, float] | None = None,
) -> list[POICandidate]:
    """Greedy nearest-neighbor ordering to reduce travel."""
    if start is None:
        # Use first POI with coords, else a neutral India centroid
        for p in pois:
            if p.lat is not None and p.lon is not None:
                start = (p.lat, p.lon)
                break
        if start is None:
            start = city_center("Delhi")
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


def _poi_key(poi: POICandidate) -> str:
    return f"{poi.osm_type}/{poi.osm_id}"


def _dedupe_pois(pois: list[POICandidate]) -> list[POICandidate]:
    """Keep first of each place (OSM id or near-duplicate name like Albert Hall)."""
    return dedupe_pois(pois)  # type: ignore[return-value]


def _diversify_for_interests(
    ranked: list[POICandidate],
    interests: list[str],
    *,
    max_total: int,
) -> tuple[list[POICandidate], list[str]]:
    """Reserve fair stops per interest (best match first), then round-robin fill."""
    from agent.mcp.poi_search import _MUST_SEE_NAME_RE, _is_low_signal_poi

    notes: list[str] = []
    if not ranked or max_total <= 0:
        return [], notes
    if not interests:
        return ranked[:max_total], notes

    keys = [
        k
        for k in (normalize_interest(raw) or raw.strip().lower() for raw in interests)
        if k
    ]
    keys = list(dict.fromkeys(keys))
    used: set[str] = set()
    picks: list[POICandidate] = []
    covered: list[str] = []
    missing_interests: list[str] = []

    def _pick_score(p: POICandidate) -> tuple[int, float, str]:
        name = p.name or ""
        must = 1 if _MUST_SEE_NAME_RE.search(name) else 0
        return (must, float(p.rank_score or 0), name)

    # Seed ≥1 best-scoring POI per interest (must-sees win ties).
    for key in keys:
        cats = categories_for_interest(key)
        candidates = [
            p
            for p in ranked
            if _poi_key(p) not in used
            and (p.category or "").lower() in cats
            and not _is_low_signal_poi(
                p.name or "", p.tags or {}, (p.category or "").lower()
            )
        ]
        if not candidates:
            missing_interests.append(key)
            continue
        chosen = max(candidates, key=_pick_score)
        picks.append(chosen)
        used.add(_poi_key(chosen))
        covered.append(key)

    # Fill remaining slots by round-robin so parks cannot dominate heritage/shopping.
    buckets: dict[str, list[POICandidate]] = {k: [] for k in keys}
    other: list[POICandidate] = []
    for p in ranked:
        ref = _poi_key(p)
        if ref in used:
            continue
        if _is_low_signal_poi(p.name or "", p.tags or {}, (p.category or "").lower()):
            continue
        cat = (p.category or "").lower()
        matched: str | None = None
        for key in keys:
            if cat in categories_for_interest(key):
                matched = key
                break
        if matched is not None:
            buckets[matched].append(p)
        else:
            other.append(p)

    # Within each interest bucket, must-sees first.
    for key in keys:
        buckets[key] = sorted(buckets.get(key) or [], key=_pick_score, reverse=True)

    while len(picks) < max_total:
        took = False
        for key in keys:
            if len(picks) >= max_total:
                break
            bucket = buckets.get(key) or []
            while bucket:
                p = bucket.pop(0)
                ref = _poi_key(p)
                if ref in used:
                    continue
                picks.append(p)
                used.add(ref)
                took = True
                break
        if not took:
            break

    for p in other:
        if len(picks) >= max_total:
            break
        ref = _poi_key(p)
        if ref in used:
            continue
        picks.append(p)
        used.add(ref)

    if covered:
        notes.append(
            "Diversity rule: ensured ≥1 stop for interests "
            f"{', '.join(covered)} when live MCP candidates existed."
        )
    if missing_interests:
        notes.append(
            "No live POI candidate matched interest(s): "
            f"{', '.join(missing_interests)}."
        )
    return picks[:max_total], notes


def _quota_select_for_pack(
    ranked: list[POICandidate],
    interests: list[str],
    *,
    max_total: int,
) -> tuple[list[POICandidate], list[str]]:
    """Hybrid pack select: keep interest quotas through the final stop count."""
    from agent.mcp.poi_shortlist import shortlist_pois

    if max_total <= 0:
        return [], []
    if not interests:
        return ranked[:max_total], ["Hybrid pack: no interests — took top by score."]
    result = shortlist_pois(
        city="Jaipur",
        candidate_pois=ranked,
        interests=interests,
        num_days=1,
        pace="relaxed",
        target_size=max_total,
    )
    notes = [
        f"Hybrid quota pack: selected {len(result.pois)}/{max_total} with interest balance."
    ]
    if result.notes:
        notes.append(result.notes)
    return list(result.pois)[:max_total], notes


def _cluster_day_groups(
    pois: list[POICandidate],
    *,
    num_days: int,
    stops_cap: int,
) -> list[list[POICandidate]]:
    """Split shortlist into geographic day themes (capacity-aware)."""
    if num_days <= 1 or len(pois) <= stops_cap:
        return [list(pois[:stops_cap])]

    coords = [p for p in pois if p.lat is not None and p.lon is not None]
    if len(coords) < num_days:
        # Fall back to sequential chunks when we lack coordinates.
        groups: list[list[POICandidate]] = []
        for d in range(num_days):
            start = d * stops_cap
            groups.append(list(pois[start : start + stops_cap]))
        return [g for g in groups if g] or [[]]

    # Seed: first POI, then farthest-from-existing seeds.
    seeds: list[POICandidate] = [coords[0]]
    while len(seeds) < num_days:
        best: POICandidate | None = None
        best_d = -1.0
        for p in coords:
            if any(_poi_key(p) == _poi_key(s) for s in seeds):
                continue
            dmin = min(
                haversine_km(p.lat or 0, p.lon or 0, s.lat or 0, s.lon or 0)
                for s in seeds
            )
            if dmin > best_d:
                best_d = dmin
                best = p
        if best is None:
            break
        seeds.append(best)

    groups = [[] for _ in range(len(seeds))]
    # Assign higher-value must-sees first for stable day themes.
    ordered = sorted(
        pois,
        key=lambda p: (
            0 if p in seeds else 1,
            -float(p.rank_score or 0),
            p.name or "",
        ),
    )
    for p in ordered:
        if p.lat is None or p.lon is None:
            # Park no-coord for fill at end
            continue
        dists = [
            (
                i,
                haversine_km(p.lat, p.lon, s.lat or p.lat, s.lon or p.lon),
                len(groups[i]),
            )
            for i, s in enumerate(seeds)
        ]
        # Prefer nearer seed with free capacity.
        dists.sort(key=lambda t: (t[1], t[2]))
        placed = False
        for i, _, _ in dists:
            if len(groups[i]) < stops_cap:
                groups[i].append(p)
                placed = True
                break
        if not placed:
            # Overflow onto least-full day
            i = min(range(len(groups)), key=lambda j: len(groups[j]))
            groups[i].append(p)

    used = {_poi_key(p) for g in groups for p in g}
    leftovers = [p for p in pois if _poi_key(p) not in used]
    for p in leftovers:
        i = min(range(len(groups)), key=lambda j: len(groups[j]))
        if len(groups[i]) < stops_cap:
            groups[i].append(p)

    # Trim and drop empties
    groups = [g[:stops_cap] for g in groups if g]
    while len(groups) < num_days:
        groups.append([])
    groups = groups[:num_days]
    return _rebalance_day_interest_mix(groups, stops_cap=stops_cap)


def _rebalance_day_interest_mix(
    groups: list[list[POICandidate]],
    *,
    stops_cap: int,
) -> list[list[POICandidate]]:
    """Prefer each day to keep ≥1 food-ish and ≥1 sight-ish stop when possible."""
    if len(groups) < 2:
        return groups

    def is_food(p: POICandidate) -> bool:
        return (p.category or "").lower() == "food"

    def is_shop(p: POICandidate) -> bool:
        return (p.category or "").lower() in {"market", "shopping"}

    def is_sight(p: POICandidate) -> bool:
        return (p.category or "").lower() in {
            "heritage",
            "museum",
            "temple",
            "attraction",
            "viewpoint",
        }

    # If day A has 2+ restaurants and day B has 0, swap one food for a sight/shop.
    for _ in range(4):
        changed = False
        for i, gi in enumerate(groups):
            for j, gj in enumerate(groups):
                if i == j:
                    continue
                foods_i = [p for p in gi if is_food(p)]
                foods_j = [p for p in gj if is_food(p)]
                sights_j = [p for p in gj if is_sight(p) or is_shop(p)]
                sights_i = [p for p in gi if is_sight(p)]
                if len(foods_i) >= 2 and not foods_j and sights_j:
                    a = foods_i[-1]
                    b = sights_j[-1]
                    gi.remove(a)
                    gj.remove(b)
                    gi.append(b)
                    gj.append(a)
                    changed = True
                elif len(sights_i) >= 3 and len([p for p in gj if is_sight(p)]) <= 1 and foods_j:
                    a = sights_i[-1]
                    b = foods_j[-1]
                    gi.remove(a)
                    gj.remove(b)
                    gi.append(b)
                    gj.append(a)
                    changed = True
        if not changed:
            break
        groups = [g[:stops_cap] for g in groups]
    return groups


def _make_stop(
    poi: POICandidate,
    *,
    pace: Pace,
    interests: list[str],
    travel_to_next_min: int | None = None,
    meal_slot: bool = False,
) -> Stop:
    match = interest_match_score(poi.category, interests)
    if match >= 10:
        why = f"Matches your interest in {', '.join(interests)}"
    elif match > 0:
        why = f"Related to your interests ({', '.join(interests)})"
    elif interests:
        why = "Grounded OSM place (fill-in for day balance)"
    else:
        why = "Grounded OSM place"
    return Stop(
        name=poi.name,
        osm_type=poi.osm_type,
        osm_id=poi.osm_id,
        lat=poi.lat,
        lon=poi.lon,
        category=poi.category,
        duration_min=_duration_for(poi, pace, meal_slot=meal_slot),
        travel_to_next_min=travel_to_next_min,
        reason=f"{why}; OSM {poi.osm_type}/{poi.osm_id}.",
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


def _stamp_day_travel(stops: list[Stop]) -> list[Stop]:
    """Travel ETA on every stop except the last of the day (grounded distances only)."""
    if not stops:
        return stops
    from agent.mcp.travel_time import display_mode_label, estimate_leg

    out: list[Stop] = []
    for i, stop in enumerate(stops):
        travel = None
        travel_km = None
        travel_mode = None
        if i < len(stops) - 1:
            nxt = stops[i + 1]
            leg = estimate_leg(
                stop.model_dump(mode="json"),
                nxt.model_dump(mode="json"),
                mode="auto",
            )
            if leg is not None and leg.duration_min > 0:
                travel = int(leg.duration_min)
                travel_km = leg.distance_km
                travel_mode = display_mode_label(leg.mode)
            else:
                mins = estimate_travel_minutes(stop.lat, stop.lon, nxt.lat, nxt.lon)
                # Skip same-place / zero-distance — do not invent travel
                if mins > 0:
                    travel = mins
        out.append(
            stop.model_copy(
                update={
                    "travel_to_next_min": travel,
                    "travel_to_next_km": travel_km,
                    "travel_to_next_mode": travel_mode,
                }
            )
        )
    return out


# Day-start minutes from midnight by pace.
DAY_START_MIN: dict[Pace, int] = {
    "relaxed": 10 * 60,  # 10:00
    "moderate": 9 * 60,  # 09:00
    "packed": 8 * 60 + 30,  # 08:30
}

# Soft paces + packed: next block does not start before these anchors so
# Morning / Afternoon / Evening labels match real clock windows.
BLOCK_START_MIN: dict[str, int] = {
    "afternoon": 13 * 60,  # 1:00 PM
    "evening": 17 * 60,  # 5:00 PM
}

# Soft end of evening window (for packed densify + flex notes).
EVENING_FLEX_END_MIN = 21 * 60  # 9:00 PM

# Semantic block ends for relax notes (= next block start).
BLOCK_FLEX_END_MIN: dict[str, int] = {
    "morning": BLOCK_START_MIN["afternoon"],
    "afternoon": BLOCK_START_MIN["evening"],
    "evening": EVENING_FLEX_END_MIN,
}


def _format_clock(minutes_from_midnight: int) -> str:
    """Format minutes-from-midnight as HH:MM (wraps past midnight for safety)."""
    mins = int(minutes_from_midnight) % (24 * 60)
    return f"{mins // 60:02d}:{mins % 60:02d}"


def stamp_day_clocks(stops: list[Stop], *, day_start_min: int) -> list[Stop]:
    """Stamp arrive/depart HH:MM from duration + travel_to_next (pure arithmetic)."""
    if not stops:
        return stops
    cursor = int(day_start_min)
    out: list[Stop] = []
    for stop in stops:
        arrive = cursor
        dwell = max(15, int(stop.duration_min or 15))
        depart = arrive + dwell
        travel = int(stop.travel_to_next_min or 0)
        out.append(
            stop.model_copy(
                update={
                    "arrive_time": _format_clock(arrive),
                    "depart_time": _format_clock(depart),
                }
            )
        )
        cursor = depart + max(0, travel)
    return out


def _stamp_block_clocks(
    stops: list[Stop], *, start_min: int
) -> tuple[list[Stop], int]:
    """Stamp one block; return stops and cursor after last stop's travel_to_next."""
    if not stops:
        return [], int(start_min)
    stamped = stamp_day_clocks(stops, day_start_min=start_min)
    last = stamped[-1]
    depart = _parse_clock_min(last.depart_time) or start_min
    cursor = int(depart) + max(0, int(last.travel_to_next_min or 0))
    return stamped, cursor


def stamp_schedule_clocks(
    days: list[DayPlan],
    *,
    pace: Pace = "moderate",
) -> list[DayPlan]:
    """Stamp arrive/depart clocks.

    All paces: afternoon/evening do not start before block anchors (1:00 PM /
    5:00 PM) so Morning / Afternoon / Evening labels match the clocks.
    Leftover window time becomes relax/free notes via annotate_block_flex_time.
    """
    day_start = DAY_START_MIN.get(pace, DAY_START_MIN["moderate"])
    out: list[DayPlan] = []
    for day in days:
        morning = list(day.morning.stops)
        afternoon = list(day.afternoon.stops)
        evening = list(day.evening.stops)

        morning_s, cursor = _stamp_block_clocks(morning, start_min=day_start)
        if afternoon:
            cursor = max(cursor, BLOCK_START_MIN["afternoon"])
        afternoon_s, cursor = _stamp_block_clocks(afternoon, start_min=cursor)
        if evening:
            cursor = max(cursor, BLOCK_START_MIN["evening"])
        evening_s, _ = _stamp_block_clocks(evening, start_min=cursor)

        out.append(
            day.model_copy(
                update={
                    "morning": TimeBlock(
                        time_of_day="morning",
                        stops=morning_s,
                        notes=day.morning.notes,
                    ),
                    "afternoon": TimeBlock(
                        time_of_day="afternoon",
                        stops=afternoon_s,
                        notes=day.afternoon.notes,
                    ),
                    "evening": TimeBlock(
                        time_of_day="evening",
                        stops=evening_s,
                        notes=day.evening.notes,
                    ),
                }
            )
        )
    return out


def restamp_day_travel_and_clocks(
    day: DayPlan, *, pace: Pace = "moderate"
) -> DayPlan:
    """Recompute travel legs then clock times for one day (after pack edits)."""
    morning = list(day.morning.stops)
    afternoon = list(day.afternoon.stops)
    evening = list(day.evening.stops)
    flat = _stamp_day_travel([*morning, *afternoon, *evening])
    m_n, a_n = len(morning), len(afternoon)
    rebuilt = day.model_copy(
        update={
            "morning": TimeBlock(
                time_of_day="morning", stops=flat[:m_n], notes=day.morning.notes
            ),
            "afternoon": TimeBlock(
                time_of_day="afternoon",
                stops=flat[m_n : m_n + a_n],
                notes=day.afternoon.notes,
            ),
            "evening": TimeBlock(
                time_of_day="evening",
                stops=flat[m_n + a_n :],
                notes=day.evening.notes,
            ),
        }
    )
    return stamp_schedule_clocks([rebuilt], pace=pace)[0]


# Packed floors: prefer at least this many stops when POIs exist.
PACKED_BLOCK_FLOOR: dict[str, int] = {
    "morning": 2,
    "afternoon": 2,
    "evening": 1,
}
# Soft per-block safety (avoid runaway loops); not a product "cap".
PACKED_BLOCK_SOFT_CAP: dict[str, int] = {
    "morning": 8,
    "afternoon": 8,
    "evening": 5,
}

# Default travel guess when densifying before legs are stamped.
_PACKED_DEFAULT_TRAVEL_MIN = 18


def _packed_block_window(bname: str, pace: Pace) -> tuple[int, int]:
    """Return (window_start, window_end) minutes for a packed block."""
    day_start = DAY_START_MIN.get(pace, DAY_START_MIN["packed"])
    if bname == "morning":
        return day_start, BLOCK_START_MIN["afternoon"]
    if bname == "afternoon":
        return BLOCK_START_MIN["afternoon"], BLOCK_START_MIN["evening"]
    return BLOCK_START_MIN["evening"], EVENING_FLEX_END_MIN


def _estimate_block_fill_mins(stops: list[Stop]) -> int:
    """Rough minutes consumed by stops (+ default travel between them)."""
    if not stops:
        return 0
    total = 0
    for i, s in enumerate(stops):
        total += max(15, int(s.duration_min or 45))
        if i < len(stops) - 1:
            total += max(
                0, int(s.travel_to_next_min or _PACKED_DEFAULT_TRAVEL_MIN)
            )
    return total


def densify_packed_am_pm(
    days: list[DayPlan],
    *,
    interests: list[str],
    candidate_pois: list[POICandidate],
    pace: Pace = "packed",
) -> tuple[list[DayPlan], list[str]]:
    """Packed only: fill morning/afternoon/evening by time window.

    Adds interest-matching live POIs until the block's clock window is roughly
    full (no day-level stop cap). Leftover time is labeled as relax/rest later
    by annotate_block_flex_time after clocks are stamped.
    """
    notes: list[str] = []
    if pace != "packed" or not days:
        return days, notes

    interest_keys = list(
        dict.fromkeys(
            (normalize_interest(i) or i).lower()
            for i in (interests or [])
            if (normalize_interest(i) or i)
        )
    )
    working = [d.model_copy(deep=True) for d in days]
    pool = sorted(
        list(candidate_pois or []),
        key=lambda p: (-(p.rank_score or 0.0), p.name or ""),
    )

    def _used() -> PlaceSeen:
        seen = PlaceSeen()
        for day in working:
            for s in day.all_stops:
                seen.add(s)
        return seen

    def _pick(used: PlaceSeen, *, block_stops: list[Stop]) -> POICandidate | None:
        prefer_non_food = _has_non_food_interest(interest_keys or interests)
        block_has_food = any(
            (s.category or "").lower() in {"food", "cafe", "restaurant"}
            for s in block_stops
        )
        require_non_food = prefer_non_food and block_has_food
        non_food_keys = [k for k in interest_keys if k != "food"]

        def _ok(p: POICandidate) -> bool:
            cat = (p.category or "").lower()
            is_food = cat in {"food", "cafe", "restaurant"} or _is_food(p)
            if require_non_food and is_food:
                return False
            if require_non_food and non_food_keys:
                return any(_stop_covers_interest(p, key) for key in non_food_keys)
            if interest_keys and not any(
                _stop_covers_interest(p, key) for key in interest_keys
            ):
                return not is_food
            return True

        for p in pool:
            if used.contains(p):
                continue
            if _ok(p):
                return p
        if require_non_food:
            for p in pool:
                if used.contains(p):
                    continue
                cat = (p.category or "").lower()
                if cat not in {"food", "cafe", "restaurant"} and not _is_food(p):
                    return p
        for p in pool:
            if not used.contains(p):
                return p
        return None

    for day in working:
        for bname in ("morning", "afternoon", "evening"):
            floor = PACKED_BLOCK_FLOOR[bname]
            soft_cap = PACKED_BLOCK_SOFT_CAP[bname]
            win_start, win_end = _packed_block_window(bname, pace)
            window_mins = max(60, win_end - win_start)
            # Leave a little slack so flex notes can still appear.
            fill_target = max(45, window_mins - 25)

            block: TimeBlock = getattr(day, bname)
            stops = list(block.stops)

            # Grow until floor met (if POIs exist) and/or window is roughly full.
            while len(stops) < soft_cap:
                used_mins = _estimate_block_fill_mins(stops)
                need_floor = len(stops) < floor
                need_time = used_mins < fill_target
                if not need_floor and not need_time:
                    break
                # Next stop estimate (~dwell + travel)
                next_cost = 45 + _PACKED_DEFAULT_TRAVEL_MIN
                if not need_floor and used_mins + next_cost > window_mins + 10:
                    break
                used = _used()
                pick = _pick(used, block_stops=stops)
                if pick is None:
                    notes.append(
                        f"Day {day.day_index} {bname}: no more interest POIs — "
                        f"leftover slot time will be relax/rest."
                    )
                    break
                new_stop = _make_stop(
                    pick, pace=pace, interests=interest_keys or interests
                ).model_copy(
                    update={
                        "reason": (
                            f"Packed {bname} fill for interests "
                            f"{', '.join(interest_keys) or 'general'} "
                            f"(live OSM place)."
                        )
                    }
                )
                stops.append(new_stop)
                notes.append(
                    f"Day {day.day_index} {bname}: added {new_stop.name} "
                    f"to fill {_format_clock_ampm(win_start)}–"
                    f"{_format_clock_ampm(win_end)} window."
                )

            setattr(
                day,
                bname,
                TimeBlock(
                    time_of_day=bname,  # type: ignore[arg-type]
                    stops=stops,
                    notes=block.notes,
                ),
            )

    return working, notes


def _cat_of_stop(stop: Stop) -> str:
    return (stop.category or "").lower()


def _parse_clock_min(hhmm: str | None) -> int | None:
    if not hhmm or ":" not in hhmm:
        return None
    try:
        h_s, m_s = hhmm.split(":", 1)
        return int(h_s) * 60 + int(m_s)
    except ValueError:
        return None


def _format_clock_ampm(hhmm_or_min: str | int) -> str:
    if isinstance(hhmm_or_min, int):
        hhmm = _format_clock(hhmm_or_min)
    else:
        hhmm = hhmm_or_min
    parsed = _parse_clock_min(hhmm)
    if parsed is None:
        return str(hhmm_or_min)
    hour, minute = divmod(parsed, 60)
    suffix = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d} {suffix}"


def _human_flex_duration(mins: int) -> str:
    mins = max(0, int(mins))
    if mins < 45:
        return f"about {max(15, mins)} minutes"
    hours = round(mins / 60)
    if hours <= 1:
        return "about 1 hour"
    return f"about {hours} hours"


def _strip_flex_note(note: str | None) -> str | None:
    """Remove prior relax/buffer sentences so re-annotation stays clean."""
    if not note:
        return None
    parts = [p.strip() for p in re.split(r"\s*[·|]\s*|\.\s+", note) if p.strip()]
    kept: list[str] = []
    for p in parts:
        low = p.lower()
        if low.startswith("relax") or "free time" in low or "free morning" in low:
            continue
        if "free afternoon" in low:
            continue
        kept.append(p if p.endswith(".") else p)
    if not kept:
        return None
    text = " · ".join(kept)
    return text


def _merge_block_note(existing: str | None, flex: str) -> str:
    base = _strip_flex_note(existing)
    # Put relax/free first so it isn't buried under "Packed via voice edit."
    if base:
        return f"{flex} · {base}"
    return flex


def annotate_block_flex_time(
    days: list[DayPlan],
    *,
    pace: Pace = "moderate",
) -> tuple[list[DayPlan], list[str]]:
    """Label leftover morning/afternoon/evening window as explicit relax time.

    Does not invent POIs. Remaining time is measured until the next block's
    first arrive (after block-anchor stamping). Applies to all paces — packed
    uses rest/relax notes when interest POIs cannot fill the slot.
    """
    notes: list[str] = []
    if not days:
        return days, notes

    next_block_name = {
        "morning": "afternoon",
        "afternoon": "evening",
        "evening": None,
    }
    out: list[DayPlan] = []
    for day in days:
        d = day.model_copy(deep=True)
        for bname, until_label in (
            ("morning", "afternoon"),
            ("afternoon", "evening"),
            ("evening", "end of evening"),
        ):
            block: TimeBlock = getattr(d, bname)
            nxt_name = next_block_name[bname]
            nxt: TimeBlock | None = getattr(d, nxt_name) if nxt_name else None
            next_arrive = None
            if nxt and nxt.stops:
                next_arrive = _parse_clock_min(nxt.stops[0].arrive_time)
            window_end = (
                next_arrive
                if next_arrive is not None
                else BLOCK_FLEX_END_MIN[bname]
            )
            end_label = _format_clock_ampm(window_end)
            if not block.stops:
                flex = (
                    f"Relax / free {bname} — no stops scheduled; "
                    f"enjoy downtime until {until_label} (around {end_label})."
                )
                setattr(
                    d,
                    bname,
                    TimeBlock(
                        time_of_day=bname,  # type: ignore[arg-type]
                        stops=[],
                        notes=_merge_block_note(block.notes, flex),
                    ),
                )
                notes.append(f"Day {day.day_index} {bname}: {flex}")
                continue

            last = block.stops[-1]
            depart = _parse_clock_min(last.depart_time) or _parse_clock_min(
                last.arrive_time
            )
            if depart is None:
                depart = DAY_START_MIN.get(pace, 9 * 60)
                if bname == "afternoon":
                    depart = BLOCK_START_MIN["afternoon"]
                elif bname == "evening":
                    depart = BLOCK_START_MIN["evening"]
                for s in block.stops:
                    depart += max(15, int(s.duration_min or 15))
                    depart += int(s.travel_to_next_min or 0)
                depart -= int(last.travel_to_next_min or 0)

            travel = max(0, int(last.travel_to_next_min or 0))
            gap_until_next = int(window_end) - int(depart)
            if gap_until_next < 30:
                cleaned = _strip_flex_note(block.notes)
                if cleaned != block.notes:
                    setattr(
                        d,
                        bname,
                        TimeBlock(
                            time_of_day=bname,  # type: ignore[arg-type]
                            stops=list(block.stops),
                            notes=cleaned,
                        ),
                    )
                continue

            free_mins = (
                gap_until_next - travel
                if travel >= 10 and (gap_until_next - travel) >= 20
                else gap_until_next
            )
            if bname != "evening" and travel >= 10 and (gap_until_next - travel) >= 20:
                flex = (
                    f"Relax / free time {_human_flex_duration(free_mins)} "
                    f"after {last.name}, then about {travel} min travel to "
                    f"{until_label} (around {end_label})."
                )
            else:
                flex = (
                    f"Relax / free time {_human_flex_duration(free_mins)} "
                    f"after {last.name} until {until_label} (around {end_label})."
                )
            setattr(
                d,
                bname,
                TimeBlock(
                    time_of_day=bname,  # type: ignore[arg-type]
                    stops=list(block.stops),
                    notes=_merge_block_note(block.notes, flex),
                ),
            )
            notes.append(f"Day {day.day_index} {bname}: {flex}")
        out.append(d)
    return out, notes


def refresh_day_themes(days: list[DayPlan]) -> list[DayPlan]:
    """Set each day's theme from categories of stops actually on that day."""
    out: list[DayPlan] = []
    for day in days:
        cats = [s.category for s in day.all_stops if s.category]
        theme = ", ".join(dict.fromkeys(cats)) if cats else (day.theme or f"Day {day.day_index}")
        out.append(day.model_copy(update={"theme": theme[:80]}))
    return out


_EVENING_CATEGORIES = frozenset({"food", "market", "shopping", "nightlife"})
_FOOD_CATEGORIES = frozenset({"food"})
_SHOP_CATEGORIES = frozenset({"market", "shopping"})
_SIGHT_CATEGORIES = frozenset(
    {"heritage", "museum", "temple", "attraction", "viewpoint", "park", "garden", "art"}
)


def _cat(poi: POICandidate) -> str:
    return (poi.category or "other").lower()


def _is_food(poi: POICandidate) -> bool:
    return _cat(poi) in _FOOD_CATEGORIES


def _is_shop(poi: POICandidate) -> bool:
    return _cat(poi) in _SHOP_CATEGORIES


def _is_sight(poi: POICandidate) -> bool:
    return _cat(poi) in _SIGHT_CATEGORIES


def _is_evening_friendly(poi: POICandidate) -> bool:
    return _cat(poi) in _EVENING_CATEGORIES


def _evening_soft_categories(interests: list[str] | None) -> frozenset[str]:
    """Non-food evening soft categories — only when the user chose matching interests.

    Shopping/market, nightlife, and park/viewpoint are never always-on.
    """
    cats: set[str] = set()
    for raw in interests or []:
        key = (normalize_interest(raw) or raw).lower()
        if not key:
            continue
        if key in {"shopping", "market"}:
            cats.update({"market", "shopping"})
        if key == "nightlife":
            cats.add("nightlife")
        # Explicit park / nature / garden / viewpoint interests unlock sunset-style soft stops.
        if key in {"park", "garden", "nature", "viewpoint", "outdoor"}:
            cats.update({"park", "garden", "viewpoint"})
        for c in categories_for_interest(key):
            if c in {"park", "garden", "viewpoint"} and key in {
                "park",
                "garden",
                "nature",
                "outdoor",
                "viewpoint",
                "adventure",
            }:
                cats.add(c)
    return frozenset(cats)


def _is_evening_soft(poi: POICandidate, interests: list[str] | None = None) -> bool:
    """Soft evening stop (not dinner): only categories unlocked by user interests."""
    if _is_food(poi):
        return False
    return _cat(poi) in _evening_soft_categories(interests)

def _has_food_interest(interests: list[str]) -> bool:
    return any((normalize_interest(i) or i).lower() == "food" for i in interests if i)


def _has_non_food_interest(interests: list[str]) -> bool:
    for i in interests:
        key = (normalize_interest(i) or i).lower()
        if key and key != "food":
            return True
    return False


def _pick_nearest(
    pool: list[POICandidate],
    *,
    used: set[str],
    anchor: POICandidate | None = None,
) -> POICandidate | None:
    avail = [p for p in pool if _poi_key(p) not in used]
    if not avail:
        return None
    if anchor is None or anchor.lat is None or anchor.lon is None:
        return avail[0]
    avail.sort(
        key=lambda p: (
            haversine_km(
                anchor.lat or 0,
                anchor.lon or 0,
                p.lat if p.lat is not None else anchor.lat or 0,
                p.lon if p.lon is not None else anchor.lon or 0,
            )
            if p.lat is not None and p.lon is not None
            else 9_999.0,
            -(p.rank_score or 0),
            p.name or "",
        )
    )
    return avail[0]


def _dedupe_foods_in_block(pois: list[POICandidate]) -> list[POICandidate]:
    """Keep at most one food stop in a time block (first wins)."""
    out: list[POICandidate] = []
    seen_food = False
    for p in pois:
        if _is_food(p):
            if seen_food:
                continue
            seen_food = True
        out.append(p)
    return out


def _pack_meal_template(
    day_pois: list[POICandidate],
    *,
    interests: list[str],
    food_only: bool,
    pace: Pace = "moderate",
) -> tuple[list[POICandidate], list[POICandidate], list[POICandidate], str | None]:
    """Fill morning/afternoon/evening toward pace block targets.

    Mixed interests only (food chosen *with* other interests):
      morning   = food FIRST (breakfast stop) + sight(s)  — all paces
      afternoon = sights and optional food; food may sit in *any* order
      evening   = soft stops (0/1/2 by pace), then food LAST (dinner stop)
      and ≤1 food per block.

    "Breakfast" / "dinner" here mean food-category stops, not other POIs.

    Food-only trips: foods may stack within a block up to the block budget.
    """
    ordered = _order_nearest(list(day_pois), start=None) if day_pois else []
    m_budget, a_budget, e_budget = _adaptive_block_targets(pace, len(ordered))
    foods = [p for p in ordered if _is_food(p)]
    shops = [p for p in ordered if _is_shop(p)]
    sights = [p for p in ordered if _is_sight(p)]
    other = [
        p
        for p in ordered
        if not _is_food(p) and not _is_shop(p) and not _is_sight(p)
    ]
    non_food = sights + shops + other
    max_eve_extras = MAX_EVENING_EXTRAS.get(pace, 1)

    morning: list[POICandidate] = []
    afternoon: list[POICandidate] = []
    evening: list[POICandidate] = []
    note: str | None = None
    used: set[str] = set()

    def _take(
        pool: list[POICandidate],
        *,
        anchor: POICandidate | None = None,
        allow_food: bool = True,
    ) -> POICandidate | None:
        filt = pool if allow_food else [p for p in pool if not _is_food(p)]
        pick = _pick_nearest(filt, used=used, anchor=anchor)
        if pick:
            used.add(_poi_key(pick))
        return pick

    if food_only:
        buckets = [
            (morning, m_budget),
            (afternoon, a_budget),
            (evening, e_budget),
        ]
        idx = 0
        for p in ordered:
            placed = False
            for _ in range(3):
                block, budget = buckets[idx % 3]
                idx += 1
                if len(block) < budget:
                    block.append(p)
                    placed = True
                    break
            if not placed:
                break
        if e_budget > 0 and not evening and ordered:
            note = "Free evening — not enough food stops to fill all three blocks."
        return morning, afternoon, evening, note

    # Mixed: reserve dinner slot; soft evening extras are low-priority before dinner.
    eve_slots = e_budget
    dinner_slots = 1 if eve_slots >= 1 else 0
    extras_budget = min(max_eve_extras, max(0, eve_slots - dinner_slots))

    anchor: POICandidate | None = None

    def _avail(pool: list[POICandidate]) -> list[POICandidate]:
        return [p for p in pool if _poi_key(p) not in used]

    def _non_food_pool(*, prefer_day_sights: bool = True) -> list[POICandidate]:
        if prefer_day_sights:
            day = _avail(sights) or _avail(other)
            if day:
                return day
        return _avail(sights) or _avail(shops) or _avail(other) or _avail(non_food)

    def _evening_soft_pool() -> list[POICandidate]:
        soft_cats = _evening_soft_categories(interests)
        interest_keys = {
            (normalize_interest(i) or i).lower() for i in (interests or []) if i
        }
        prefer_sunset = bool(interest_keys & {"park", "garden", "nature", "viewpoint", "outdoor"})
        seen: set[str] = set()
        preferred: list[POICandidate] = []
        rest: list[POICandidate] = []
        for p in _avail(shops) + _avail(non_food):
            key = _poi_key(p)
            if key in seen or not _is_evening_soft(p, interests):
                continue
            seen.add(key)
            if prefer_sunset and _cat(p) in {"park", "garden", "viewpoint"}:
                preferred.append(p)
            else:
                rest.append(p)
        return preferred + rest

    # Morning food first, then reserve evening softs (parks/viewpoints when interest-
    # matched), then finish morning sights so soft stops are not stolen by AM/PM.
    if m_budget >= 1:
        bfast = _take(foods)
        if bfast:
            morning.append(bfast)
            anchor = bfast

    if extras_budget > 0:
        prefer_sunset = bool(
            {
                (normalize_interest(i) or i).lower()
                for i in (interests or [])
                if i
            }
            & {"park", "garden", "nature", "viewpoint", "outdoor"}
        )
        while len(evening) < extras_budget:
            soft_pool = _evening_soft_pool()
            if prefer_sunset:
                sunset = [p for p in soft_pool if _cat(p) in {"park", "garden", "viewpoint"}]
                extra = _take(sunset, anchor=anchor, allow_food=False)
                if extra is None:
                    extra = _take(soft_pool, anchor=anchor, allow_food=False)
            else:
                extra = _take(soft_pool, anchor=anchor, allow_food=False)
            if extra is None:
                break
            evening.append(extra)
            anchor = extra

    if m_budget >= 1:
        while len(morning) < m_budget:
            sight_am = _take(
                _non_food_pool(prefer_day_sights=True),
                anchor=anchor,
                allow_food=False,
            )
            if sight_am is None:
                break
            morning.append(sight_am)
            anchor = sight_am
        morning[:] = _breakfast_first(morning)

    # Afternoon: fill sights; optional food may sit in any position (not ordered).
    if a_budget >= 1:
        while len(afternoon) < a_budget:
            sight_pm = _take(
                _non_food_pool(prefer_day_sights=True),
                anchor=anchor,
                allow_food=False,
            )
            if sight_pm is None:
                if not any(not _is_food(p) for p in afternoon):
                    sight_pm = _take(
                        _non_food_pool(prefer_day_sights=False),
                        anchor=anchor,
                        allow_food=False,
                    )
                    if sight_pm is not None:
                        afternoon.append(sight_pm)
                        anchor = sight_pm
                        continue
                break
            afternoon.append(sight_pm)
            anchor = sight_pm

        # Include one afternoon food when budget allows — any order vs sights.
        if (
            a_budget >= 2
            and not any(_is_food(p) for p in afternoon)
            and len(_avail(foods)) > dinner_slots
        ):
            lunch = _take(foods, anchor=anchor)
            if lunch:
                if len(afternoon) >= a_budget:
                    # Swap the last sight for food (keeps count; food not forced first).
                    displaced = afternoon.pop()
                    used.discard(_poi_key(displaced))
                    afternoon.append(lunch)
                elif len(afternoon) >= 1:
                    # Place after the first sight (middle-ish); order is free.
                    afternoon.insert(1, lunch)
                    if len(afternoon) > a_budget:
                        displaced = afternoon.pop()
                        if not _is_food(displaced):
                            used.discard(_poi_key(displaced))
                else:
                    afternoon.append(lunch)
                afternoon[:] = afternoon[:a_budget]
                anchor = lunch

    if eve_slots >= 1 and dinner_slots:
        # Dinner = food stop; always last in the evening block.
        dinner = _take(foods, anchor=anchor)
        if dinner:
            evening.append(dinner)
            anchor = dinner
        evening[:] = _dinner_last(evening)
    leftovers = [p for p in ordered if _poi_key(p) not in used]
    for block, budget in (
        (morning, m_budget),
        (afternoon, a_budget),
        (evening, eve_slots),
    ):
        while len(block) < budget and leftovers:
            if block is evening:
                soft_count = sum(1 for p in block if not _is_food(p))
                has_dinner = any(_is_food(p) for p in block)
                if soft_count < extras_budget:
                    soft_pick = next(
                        (
                            p
                            for p in leftovers
                            if _is_evening_soft(p, interests)
                        ),
                        None,
                    )
                    if soft_pick is not None:
                        if has_dinner:
                            dinner_pois = [p for p in block if _is_food(p)]
                            block[:] = [p for p in block if not _is_food(p)] + [
                                soft_pick
                            ] + dinner_pois
                        else:
                            block.append(soft_pick)
                        leftovers = [
                            p for p in leftovers if _poi_key(p) != _poi_key(soft_pick)
                        ]
                        used.add(_poi_key(soft_pick))
                        continue
                if not has_dinner and dinner_slots:
                    food_pick = next((p for p in leftovers if _is_food(p)), None)
                    if food_pick is not None:
                        block.append(food_pick)
                        leftovers = [
                            p for p in leftovers if _poi_key(p) != _poi_key(food_pick)
                        ]
                        used.add(_poi_key(food_pick))
                        continue
                break

            if block is morning and any(_is_food(p) for p in block):
                prefer_non_food = [p for p in leftovers if not _is_food(p)]
                pick = prefer_non_food[0] if prefer_non_food else None
                if pick is None:
                    break
                block.append(pick)
                leftovers = [p for p in leftovers if _poi_key(p) != _poi_key(pick)]
                used.add(_poi_key(pick))
                continue

            prefer_non_food = [p for p in leftovers if not _is_food(p)]
            pick = prefer_non_food[0] if prefer_non_food else None
            if pick is None:
                if any(_is_food(p) for p in block):
                    break
                food_left = [p for p in leftovers if _is_food(p)]
                evening_still_needs_food = dinner_slots > 0 and not any(
                    _is_food(p) for p in evening
                )
                reserve = 1 if evening_still_needs_food else 0
                if len(food_left) <= reserve:
                    break
                pick = leftovers[0] if leftovers else None
            if pick is None:
                break
            block.append(pick)
            leftovers = [p for p in leftovers if _poi_key(p) != _poi_key(pick)]
            used.add(_poi_key(pick))

    morning = _breakfast_first(_dedupe_foods_in_block(morning))[:m_budget]
    afternoon = _dedupe_foods_in_block(afternoon)[:a_budget]
    evening = _dinner_last(_dedupe_foods_in_block(evening))[:eve_slots]

    if eve_slots > 0 and not evening:
        note = (
            "Free evening / rest near your stay — not enough grounded "
            "dinner/food stops left after morning and afternoon."
        )
    return morning, afternoon, evening, note



def _ensure_meal_candidate_floor(
    ranked: list[POICandidate],
    full_pool: list[POICandidate],
    *,
    interests: list[str],
    num_days: int,
    max_total: int,
) -> list[POICandidate]:
    """Guarantee enough foods/shops for meal+evening templates when mixed interests.

    When the ranked list is already at ``max_total``, swap in needed foods/shops
    by evicting lower-priority sights so evenings can stay dinner-first.
    """
    if not _has_food_interest(interests) or not _has_non_food_interest(interests):
        return ranked
    per_day = max(1, max_total // max(1, num_days))
    # Relaxed (~4): breakfast + dinner. Balanced/packed (≥5): + lunch.
    foods_per_day = 2 if per_day <= 4 else 3
    need_food = min(foods_per_day * num_days, max_total)
    interest_keys = {
        (normalize_interest(i) or i).lower() for i in interests if i
    }
    want_shop = bool(interest_keys & {"shopping", "market"})
    want_park_eve = bool(
        interest_keys & {"park", "garden", "nature", "viewpoint", "outdoor"}
    )
    # Soft evening extras (balanced 1 / packed 2). Prefer park/viewpoint when chosen;
    # otherwise fall back to markets/shops.
    if per_day >= 8:
        need_eve_soft = 2 * num_days
    elif per_day >= 6:
        need_eve_soft = 1 * num_days
    else:
        need_eve_soft = 0
    need_park_eve = need_eve_soft if want_park_eve else 0
    need_shop = num_days if want_shop else 0
    if need_eve_soft and not want_park_eve:
        need_shop = max(need_shop, need_eve_soft)
    need_sight = min(
        2 * num_days,
        max(0, max_total - need_food - need_shop - need_park_eve),
    )

    selected = list(ranked[:max_total])
    used = {_poi_key(p) for p in selected}

    def _is_park_soft(p: POICandidate) -> bool:
        return _cat(p) in {"park", "garden", "viewpoint"}

    def _evict_for(
        incoming_is_food: bool,
        incoming_is_shop: bool,
        *,
        incoming_is_park_soft: bool = False,
    ) -> bool:
        """Drop a surplus stop so a needed meal/soft candidate can enter."""
        nonlocal selected, used
        if not selected:
            return False
        food_n = sum(1 for p in selected if _is_food(p))
        shop_n = sum(1 for p in selected if _is_shop(p))
        park_n = sum(1 for p in selected if _is_park_soft(p))
        sight_n = sum(1 for p in selected if _is_sight(p) and not _is_park_soft(p))

        def _can_drop(p: POICandidate) -> bool:
            if _is_food(p):
                return food_n > need_food
            if _is_park_soft(p):
                return park_n > need_park_eve and not incoming_is_park_soft
            if _is_shop(p):
                return shop_n > need_shop and not incoming_is_shop
            if _is_sight(p):
                return (
                    sight_n > need_sight
                    or incoming_is_food
                    or incoming_is_shop
                    or incoming_is_park_soft
                )
            return True

        candidates = [p for p in selected if _can_drop(p)]
        if not candidates:
            if incoming_is_food:
                candidates = [p for p in selected if not _is_food(p)]
            elif incoming_is_park_soft:
                candidates = [
                    p
                    for p in selected
                    if not _is_food(p) and not _is_park_soft(p)
                ]
            elif incoming_is_shop:
                candidates = [
                    p for p in selected if not _is_food(p) and not _is_shop(p)
                ]
        if not candidates:
            return False
        victim = min(
            candidates,
            key=lambda p: (
                0
                if (_is_sight(p) and not _is_park_soft(p))
                or not (_is_food(p) or _is_shop(p) or _is_park_soft(p))
                else 1,
                p.rank_score or 0,
                p.name or "",
            ),
        )
        selected.remove(victim)
        used.discard(_poi_key(victim))
        return True

    def _add_from(pool: list[POICandidate], pred, need: int) -> None:
        nonlocal selected, used
        have = sum(1 for p in selected if pred(p))
        if have >= need:
            return
        for p in pool:
            if have >= need:
                break
            if _poi_key(p) in used:
                continue
            if not pred(p):
                continue
            if len(selected) >= max_total:
                if not _evict_for(
                    _is_food(p),
                    _is_shop(p),
                    incoming_is_park_soft=_is_park_soft(p),
                ):
                    break
            selected.append(p)
            used.add(_poi_key(p))
            have += 1

    pool = _dedupe_pois(list(full_pool))
    foods = sorted(
        [p for p in pool if _is_food(p)],
        key=lambda p: (-(p.rank_score or 0), p.name or ""),
    )
    shops = sorted(
        [p for p in pool if _is_shop(p)],
        key=lambda p: (-(p.rank_score or 0), p.name or ""),
    )
    parks = sorted(
        [p for p in pool if _is_park_soft(p)],
        key=lambda p: (-(p.rank_score or 0), p.name or ""),
    )
    _add_from(foods, _is_food, need_food)
    if need_park_eve:
        _add_from(parks, _is_park_soft, need_park_eve)
    if need_shop:
        _add_from(shops, _is_shop, need_shop)
    if need_sight > 0:
        sights = sorted(
            [p for p in pool if _is_sight(p) and not _is_park_soft(p)],
            key=lambda p: (-(p.rank_score or 0), p.name or ""),
        )
        _add_from(sights, lambda p: _is_sight(p) and not _is_park_soft(p), need_sight)

    if len(selected) > max_total:
        foods_keep = [p for p in selected if _is_food(p)][:need_food]
        parks_keep = [p for p in selected if _is_park_soft(p)][: max(need_park_eve, 0)]
        shops_keep = [p for p in selected if _is_shop(p)][: max(need_shop, 0)]
        kept = {_poi_key(x) for x in foods_keep + parks_keep + shops_keep}
        rest = [p for p in selected if _poi_key(p) not in kept]
        rest.sort(key=lambda p: (-(p.rank_score or 0), p.name or ""))
        room = max_total - len(foods_keep) - len(parks_keep) - len(shops_keep)
        selected = foods_keep + parks_keep + shops_keep + rest[: max(0, room)]
    return selected[:max_total]


def _distribute_days_for_meals(
    ranked: list[POICandidate],
    *,
    num_days: int,
    stops_cap: int,
) -> list[list[POICandidate]]:
    """Round-robin foods then shops then sights so each day can fill meal slots.

    Aim for ≥2 foods/day when available (breakfast + dinner); allow a 3rd when
    the day still has room (lunch on balanced/packed).
    """
    foods = [p for p in ranked if _is_food(p)]
    shops = [p for p in ranked if _is_shop(p)]
    rest = [p for p in ranked if not _is_food(p) and not _is_shop(p)]
    groups: list[list[POICandidate]] = [[] for _ in range(num_days)]
    min_foods_per_day = 2 if len(foods) >= 2 * num_days else 1

    def _food_count(d: int) -> int:
        return sum(1 for x in groups[d] if _is_food(x))

    def _place(p: POICandidate, *, is_food: bool = False) -> None:
        if is_food:
            # Prefer days still below the breakfast+dinner floor, then thinnest.
            day = min(
                range(num_days),
                key=lambda d: (
                    0 if _food_count(d) < min_foods_per_day else 1,
                    _food_count(d),
                    len(groups[d]),
                ),
            )
        else:
            day = min(
                range(num_days),
                key=lambda d: (
                    len(groups[d]),
                    _food_count(d),
                    sum(1 for x in groups[d] if _is_shop(x)),
                ),
            )
        if len(groups[day]) < stops_cap:
            groups[day].append(p)
            return
        for d in range(num_days):
            if len(groups[d]) < stops_cap:
                groups[d].append(p)
                return

    for p in foods:
        _place(p, is_food=True)
    for p in shops:
        _place(p)
    for p in rest:
        _place(p)
    return [g[:stops_cap] for g in groups if g]


def _assign_blocks(
    day_pois: list[POICandidate],
    *,
    pace: Pace,
    day_index: int,
    interests: list[str] | None = None,
) -> DayPlan:
    """Pack a day with Morning + Afternoon + Evening always present.

    Targets by pace (soft, adaptive to available POIs):
      relaxed  → 2 · 1 · 1
      moderate → 2 · 2 · 1  (balanced)
      packed   → ≥2 · ≥2 · 2  (stretch to 3 · 2 · 2 when pool allows)

    When food is paired with other interests, use a meal-slot template and
    allow at most one food stop per block. Food-only trips may stack foods.
    """
    interests = list(interests or [])
    unique = _dedupe_pois(day_pois)
    food_interest = _has_food_interest(interests)
    mixed = food_interest and _has_non_food_interest(interests)
    food_only = food_interest and not _has_non_food_interest(interests)

    morning_pois: list[POICandidate] = []
    afternoon_pois: list[POICandidate] = []
    evening_pois: list[POICandidate] = []
    evening_note: str | None = None
    am_note = None
    pm_note = None

    if not unique:
        evening_note = (
            "Free evening — no grounded places available for this day "
            "(catalog too thin to fill morning/afternoon/evening)."
        )
    elif len(unique) == 1:
        morning_pois = [unique[0]]
        evening_note = (
            "Free evening — only one grounded place for this day; "
            "left afternoon/evening open rather than inventing stops."
        )
    elif food_interest:
        morning_pois, afternoon_pois, evening_pois, evening_note = _pack_meal_template(
            unique, interests=interests, food_only=food_only, pace=pace
        )
        if mixed:
            morning_pois = _dedupe_foods_in_block(morning_pois)
            afternoon_pois = _dedupe_foods_in_block(afternoon_pois)
            evening_pois = _dedupe_foods_in_block(evening_pois)
    else:
        # No food interest: geo order + adaptive block budgets.
        ordered = _order_nearest(unique, start=None)
        m_budget, a_budget, e_budget = _adaptive_block_targets(pace, len(ordered))
        used: set[str] = set()
        remaining = list(ordered)

        def _pull(
            prefer: list[POICandidate] | None = None,
            *,
            evening_only: bool = False,
        ) -> POICandidate | None:
            pool = prefer if prefer is not None else remaining
            for p in pool:
                if _poi_key(p) in used:
                    continue
                if evening_only and not _is_evening_soft(p, interests):
                    continue
                used.add(_poi_key(p))
                if p in remaining:
                    remaining.remove(p)
                return p
            return None

        # Reserve evening soft stops first so afternoon packing does not
        # consume markets/shopping (same idea as meal-template soft reserve).
        soft_budget = min(e_budget, MAX_EVENING_EXTRAS.get(pace, 1))
        if soft_budget > 0:
            soft_pool = [p for p in remaining if _is_evening_soft(p, interests)]
            while len(evening_pois) < soft_budget:
                pick = _pull(soft_pool, evening_only=True)
                if pick is None:
                    break
                evening_pois.append(pick)

        while len(morning_pois) < m_budget:
            pick = _pull()
            if pick is None:
                break
            morning_pois.append(pick)

        while len(afternoon_pois) < a_budget:
            pick = _pull()
            if pick is None:
                break
            afternoon_pois.append(pick)

        if len(evening_pois) < e_budget:
            soft = [p for p in remaining if _is_evening_soft(p, interests)]
            while len(evening_pois) < e_budget:
                pick = _pull(soft, evening_only=True)
                if pick is None:
                    break
                evening_pois.append(pick)

        if e_budget > 0 and not evening_pois:
            evening_note = (
                "Free evening / rest near your stay — no evening-friendly "
                "grounded stop available for this day."
            )

    morning_stops = [
        _make_stop(p, pace=pace, interests=interests, meal_slot=food_interest)
        for p in morning_pois
    ]
    afternoon_stops = [
        _make_stop(p, pace=pace, interests=interests, meal_slot=food_interest)
        for p in afternoon_pois
    ]
    evening_stops = [
        _make_stop(p, pace=pace, interests=interests, meal_slot=food_interest)
        for p in evening_pois
    ]

    flat = _stamp_day_travel(morning_stops + afternoon_stops + evening_stops)
    m_n, a_n, e_n = len(morning_stops), len(afternoon_stops), len(evening_stops)
    morning_stops = flat[:m_n]
    afternoon_stops = flat[m_n : m_n + a_n]
    evening_stops = flat[m_n + a_n : m_n + a_n + e_n]

    # Theme from places actually scheduled — not the unused day pool.
    placed = [*morning_stops, *afternoon_stops, *evening_stops]
    theme_bits = [s.category for s in placed if s.category]
    theme = (
        ", ".join(dict.fromkeys(theme_bits)) if theme_bits else f"Day {day_index}"
    )
    if m_n == 0:
        am_note = "No grounded place available for morning."
    if a_n == 0:
        pm_note = (
            "No grounded place available for afternoon "
            "(catalog had fewer unique POIs than needed)."
        )
    if e_n == 0 and evening_note is None:
        evening_note = (
            "Free evening / rest near your stay — no evening-friendly "
            "grounded stop available for this day."
        )

    return DayPlan(
        day_index=day_index,
        theme=theme[:80],
        morning=TimeBlock(
            time_of_day="morning", stops=morning_stops, notes=am_note
        ),
        afternoon=TimeBlock(
            time_of_day="afternoon", stops=afternoon_stops, notes=pm_note
        ),
        evening=TimeBlock(
            time_of_day="evening", stops=evening_stops, notes=evening_note
        ),
    )



def build_itinerary(
    *,
    candidate_pois: list[POICandidate],
    num_days: int = 3,
    pace: Pace = "relaxed",
    daily_time_window_min: int = 540,
    interests: list[str] | None = None,
    city: str = "Jaipur",
    revision_constraints: list[str] | None = None,
    preserve_days: dict[int, DayPlan] | None = None,
    selection_mode: str | None = None,
) -> ItineraryDraftResult:
    """MCP: build a day-wise draft itinerary from candidate POIs.

    ``selection_mode``:
      - ``legacy``: old ≥1-per-interest diversify then score fill
      - ``hybrid``: interest quotas + geographic day clusters (default when env set)
      - ``preselected``: candidates already shortlisted; quota trim + clusters only

    Optional ``revision_constraints`` come from the Reviewer Agent
    (e.g. \"Reduce travel\", \"Keep museum\", \"Preserve Day 1\").
    """
    import re

    from agent.mcp.poi_shortlist import active_itinerary_strategy

    num_days = clamp_trip_days(num_days)
    notes: list[str] = []
    missing = False
    constraints_l = [c.lower().strip() for c in (revision_constraints or []) if c]
    mode = (selection_mode or active_itinerary_strategy()).strip().lower()
    if mode not in {"legacy", "hybrid", "preselected"}:
        mode = "hybrid"
    notes.append(f"Itinerary selection_mode={mode}.")

    # Honor Reviewer constraints
    effective_pace: Pace = pace
    stops_cap = STOPS_PER_DAY[pace]
    if any("relax" in c or "reduce stop" in c or "reduce travel" in c for c in constraints_l):
        effective_pace = "relaxed"
        stops_cap = max(2, STOPS_PER_DAY["relaxed"] - 1)
        notes.append(
            "Applied Reviewer constraints: reduced stops/travel for feasibility."
        )
    if any("respect relaxed" in c for c in constraints_l):
        effective_pace = "relaxed"
        stops_cap = min(stops_cap, STOPS_PER_DAY["relaxed"])

    # Traveler-profile constraints from Orchestrator
    if any("kid_friendly" in c or "senior_friendly" in c for c in constraints_l):
        effective_pace = "relaxed"
        stops_cap = min(stops_cap, 3)
        notes.append(
            "Audience profile: fewer stops and relaxed pacing "
            "(kid-friendly or senior-friendly)."
        )
    elif any("friends_friendly" in c for c in constraints_l):
        if pace != "relaxed":
            effective_pace = "packed"
            stops_cap = max(stops_cap, STOPS_PER_DAY["packed"])
        notes.append("Audience profile: friends-friendly — denser day packing.")
    elif any("couple_friendly" in c for c in constraints_l):
        if pace == "packed":
            effective_pace = "moderate"
            stops_cap = STOPS_PER_DAY["moderate"]
        notes.append("Audience profile: couple-friendly — moderate scenic pacing.")

    # Drop avoided categories when profile asks
    filtered_pois = list(candidate_pois)
    if any("avoid nightlife" in c or "kid_friendly" in c or "senior_friendly" in c for c in constraints_l):
        before = len(filtered_pois)
        filtered_pois = [
            p for p in filtered_pois if (p.category or "").lower() != "nightlife"
        ]
        if len(filtered_pois) < before:
            notes.append("Filtered nightlife stops for family/senior audience.")
    candidate_pois = filtered_pois

    keep_terms = []
    for c in constraints_l:
        m = re.match(r"keep\s+(.+)", c)
        if m:
            keep_terms.append(m.group(1).strip())

    preserve_idx: set[int] = set()
    for c in constraints_l:
        m = re.search(r"preserve\s+day\s*(\d)", c)
        if m:
            preserve_idx.add(int(m.group(1)))
    # Also honor any day keys explicitly passed in preserve_days.
    if preserve_days:
        preserve_idx.update(int(k) for k in preserve_days.keys())

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
            pace=effective_pace,
            daily_time_window_min=daily_time_window_min,
            days=empty_days,
            missing_data=True,
            notes="; ".join(notes),
        )

    # Prefer higher interest match + rank_score; then Reviewer "Keep X"
    interest_list = list(interests or [])
    preferred_cats = categories_for_interests(interest_list)

    def _rank_key(p: POICandidate) -> tuple:
        boost = interest_match_score(p.category, interest_list)
        cat = (p.category or "").lower()
        name = (p.name or "").lower()
        for term in keep_terms:
            if term in cat or term in name:
                boost += 5.0
        return (-((p.rank_score or 0) + boost), p.name)

    ranked = sorted(candidate_pois, key=_rank_key)
    # Pull stated-interest matches to the front of the pack
    if preferred_cats:
        preferred = [
            p for p in ranked if (p.category or "").lower() in preferred_cats
        ]
        rest = [p for p in ranked if p not in preferred]
        ranked = preferred + rest
        if preferred:
            notes.append(
                f"Weighted POIs matching interests {', '.join(interest_list)} "
                f"higher ({len(preferred)} preferred stops)."
            )
    # If Keep terms present, ensure those POIs are near the front
    if keep_terms:
        kept = [p for p in ranked if any(
            t in (p.category or "").lower() or t in (p.name or "").lower()
            for t in keep_terms
        )]
        rest = [p for p in ranked if p not in kept]
        ranked = kept + rest
        if kept:
            notes.append(
                f"Prioritized Reviewer keep constraints: {', '.join(keep_terms)}."
            )

    # Unique POIs only — never cycle the same place into multiple days.
    ranked = _dedupe_pois(ranked)

    # Exclude places already on preserved days so rebuilds don't re-add them.
    if preserve_days and preserve_idx:
        reserved = PlaceSeen.from_places(
            [
                s
                for i, day in preserve_days.items()
                if int(i) in preserve_idx
                for s in day.all_stops
            ]
        )
        before = len(ranked)
        ranked = [p for p in ranked if not reserved.contains(p)]
        if len(ranked) < before:
            notes.append(
                f"Excluded {before - len(ranked)} place(s) already on preserved days."
            )

    # Prefer ≥2 unique stops per day so Morning + Afternoon each get at least one.
    # Cap per day by pace so relaxed/balanced/packed actually differ.
    min_per_day = 2
    max_total = num_days * stops_cap
    if mode == "legacy":
        full_pool = list(ranked)
        ranked, diversity_notes = _diversify_for_interests(
            ranked, interest_list, max_total=max_total
        )
        notes.extend(diversity_notes)
        before_meal = len(ranked)
        ranked = _ensure_meal_candidate_floor(
            ranked,
            full_pool + list(candidate_pois),
            interests=interest_list,
            num_days=num_days,
            max_total=max_total,
        )
        if len(ranked) != before_meal:
            notes.append(
                f"Meal template floor: adjusted pool to {len(ranked)} places "
                f"(enough foods/shops for pace block targets)."
            )
    else:
        # hybrid / preselected: enforce quotas through final stop count
        ranked, quota_notes = _quota_select_for_pack(
            ranked, interest_list, max_total=max_total
        )
        notes.extend(quota_notes)
    if len(ranked) > max_total:
        ranked = ranked[:max_total]
        notes.append(
            f"Pace={effective_pace}: capped at {stops_cap} stops/day "
            f"({max_total} places across {num_days} days)."
        )

    if len(ranked) < num_days * min_per_day:
        missing = True
        notes.append(
            f"Only {len(ranked)} unique grounded POIs for a {num_days}-day plan "
            f"(need at least {num_days * min_per_day} for morning+afternoon each day). "
            "Some blocks may be thinner; places are not duplicated."
        )
        day_groups = [[] for _ in range(num_days)]
        for idx, poi in enumerate(ranked):
            day_groups[idx % num_days].append(poi)
    elif _has_food_interest(interest_list):
        day_groups = _distribute_days_for_meals(
            ranked, num_days=num_days, stops_cap=stops_cap
        )
        notes.append(
            f"Meal-aware day split @ {effective_pace} "
            f"(≤{stops_cap}/day; foods distributed for breakfast/lunch/dinner)."
        )
    elif mode in {"hybrid", "preselected"} and any(
        p.lat is not None and p.lon is not None for p in ranked
    ):
        day_groups = _cluster_day_groups(
            ranked, num_days=num_days, stops_cap=stops_cap
        )
        notes.append(
            f"Geographic day clusters @ {effective_pace} "
            f"(≤{stops_cap}/day; {sum(len(g) for g in day_groups)} stops)."
        )
        # Ensure thin days get a second stop when possible
        for d in range(len(day_groups)):
            while len(day_groups[d]) < min_per_day:
                donor = max(
                    range(len(day_groups)),
                    key=lambda i: len(day_groups[i]) if i != d else -1,
                )
                if len(day_groups[donor]) <= min_per_day:
                    break
                day_groups[d].append(day_groups[donor].pop())
    elif len(ranked) < num_days * stops_cap:
        # Not enough POIs to fill every day to the pace cap — distribute evenly
        # (still ≤ stops_cap) so later days are not left nearly empty.
        base, rem = divmod(len(ranked), num_days)
        day_groups = []
        idx = 0
        for d in range(num_days):
            take = min(stops_cap, base + (1 if d < rem else 0))
            day_groups.append(list(ranked[idx : idx + take]))
            idx += take
        for d in range(num_days):
            while len(day_groups[d]) < min_per_day:
                donor = max(
                    range(num_days),
                    key=lambda i: len(day_groups[i]) if i != d else -1,
                )
                if len(day_groups[donor]) <= min_per_day:
                    break
                day_groups[d].append(day_groups[donor].pop())
        notes.append(
            f"Assigned all {len(ranked)} unique POIs across {num_days} days "
            f"(pace {effective_pace} cap {stops_cap}/day; catalog smaller than full cap)."
        )
    else:
        # Contiguous chunks sized by pace cap (not "use every POI").
        per = stops_cap
        day_groups = []
        for d in range(num_days):
            start = d * per
            day_groups.append(list(ranked[start : start + per]))
        for d in range(num_days):
            while len(day_groups[d]) < min_per_day:
                donor = max(
                    range(num_days),
                    key=lambda i: len(day_groups[i]) if i != d else -1,
                )
                if len(day_groups[donor]) <= min_per_day:
                    break
                day_groups[d].append(day_groups[donor].pop())
            if len(day_groups[d]) > stops_cap:
                overflow = day_groups[d][stops_cap:]
                day_groups[d] = day_groups[d][:stops_cap]
                for extra in overflow:
                    for j in range(num_days):
                        if j != d and len(day_groups[j]) < stops_cap:
                            day_groups[j].append(extra)
                            break
        notes.append(
            f"Assigned {sum(len(g) for g in day_groups)} unique grounded POIs "
            f"across {num_days} days @ {effective_pace} pace "
            f"(≤{stops_cap}/day; morning / afternoon / evening)."
        )

    # Cluster within each day's list for travel locality
    day_groups = [
        _order_nearest(g, start=city_center(city)) if g else g for g in day_groups
    ]

    days: list[DayPlan] = []
    for i, group in enumerate(day_groups[:num_days], start=1):
        if preserve_days and i in preserve_idx and i in preserve_days:
            days.append(preserve_days[i].model_copy(deep=True))
            notes.append(f"Preserved Day {i} per Reviewer constraint.")
            continue
        if not group:
            missing = True
            notes.append(f"Day {i} has no POIs — data insufficient for a full plan.")
            days.append(
                DayPlan(
                    day_index=i,
                    theme="Light day — limited grounded POIs",
                    morning=TimeBlock(
                        time_of_day="morning",
                        notes="No grounded POI available for this slot.",
                    ),
                    afternoon=TimeBlock(
                        time_of_day="afternoon",
                        notes="No grounded POI available for this slot.",
                    ),
                    evening=TimeBlock(time_of_day="evening"),
                )
            )
            continue
        day = _assign_blocks(
            group, pace=effective_pace, day_index=i, interests=interest_list
        )
        if not day.morning.stops or not day.afternoon.stops:
            missing = True
        total = day.total_duration_min
        if total > daily_time_window_min:
            notes.append(
                f"Day {i} draft duration {total}m exceeds window "
                f"{daily_time_window_min}m — Reviewer should flag or trim."
            )
        days.append(day)

    # Final cross-day uniqueness (near-duplicate names + OSM ids).
    days, dedupe_notes = dedupe_day_plans(days)
    notes.extend(dedupe_notes)

    # Post-pack: ensure every stated interest still appears when a live POI exists.
    days, coverage_notes = ensure_interest_coverage(
        days,
        interests=interest_list,
        candidate_pois=list(candidate_pois) + list(ranked),
        pace=effective_pace,
    )
    notes.extend(coverage_notes)

    # Packed: densify thin morning/afternoon with interest POIs (no fake places).
    days, densify_notes = densify_packed_am_pm(
        days,
        interests=interest_list,
        candidate_pois=list(candidate_pois) + list(ranked),
        pace=effective_pace,
    )
    notes.extend(densify_notes)

    # Recompute travel after coverage/densify, then stamp arrive/depart clocks.
    days = [
        restamp_day_travel_and_clocks(d, pace=effective_pace) for d in days
    ]
    notes.append(
        f"Schedule clocks stamped from "
        f"{_format_clock(DAY_START_MIN.get(effective_pace, 9 * 60))} "
        f"({effective_pace} day start) using duration + travel_to_next."
    )

    # Relaxed/balanced: explicit relax/buffer for leftover AM/PM window time.
    days, flex_notes = annotate_block_flex_time(days, pace=effective_pace)
    notes.extend(flex_notes)
    days = refresh_day_themes(days)

    logger.info(
        "Itinerary builder: %d POIs → %d days @ pace=%s constraints=%s",
        len(candidate_pois),
        num_days,
        effective_pace,
        revision_constraints or [],
    )
    if interests:
        notes.append(f"Interests considered: {', '.join(interests)}.")
    if constraints_l:
        notes.append(f"Reviewer constraints: {'; '.join(revision_constraints or [])}.")

    return ItineraryDraftResult(
        pace=effective_pace,
        daily_time_window_min=daily_time_window_min,
        days=days,
        missing_data=missing,
        notes="; ".join(notes) if notes else None,
    )


def draft_to_trip_constraints(
    *,
    city: str,
    num_days: int,
    pace: Pace,
    interests: list[str],
    daily_time_window_min: int = 540,
) -> TripConstraints:
    return TripConstraints(
        city=city,
        country="India",
        num_days=num_days,
        interests=interests,
        pace=pace,
        daily_time_window_min=daily_time_window_min,
        confirmed=False,
    )


def _stop_to_poi(stop: Stop) -> POICandidate:
    return POICandidate(
        name=stop.name,
        osm_type=stop.osm_type,
        osm_id=stop.osm_id,
        lat=stop.lat,
        lon=stop.lon,
        category=stop.category,
        rank_score=0.0,
    )


def _stop_covers_interest(stop: Stop | POICandidate, interest: str) -> bool:
    key = normalize_interest(interest) or (interest or "").lower()
    if not key:
        return False
    cat = (getattr(stop, "category", None) or "").lower()
    return cat in categories_for_interest(key)


def _sole_cover_interests(
    stop: Stop, all_stops: list[Stop], interests: list[str]
) -> set[str]:
    """Interests for which this stop is the only covering place on the plan."""
    sole: set[str] = set()
    for raw in interests:
        key = normalize_interest(raw) or (raw or "").lower()
        if not key:
            continue
        matches = [s for s in all_stops if _stop_covers_interest(s, key)]
        if len(matches) == 1 and matches[0].osm_id == stop.osm_id:
            sole.add(key)
    return sole


def ensure_interest_coverage(
    days: list[DayPlan],
    *,
    interests: list[str],
    candidate_pois: list[POICandidate],
    pace: Pace = "relaxed",
) -> tuple[list[DayPlan], list[str]]:
    """Post-pack guard: restore missing stated interests when live POIs exist.

    Prefers replacing a non-unique stop (e.g. a second heritage) with a missing
    interest match (e.g. park). Falls back to appending on the thinnest day under
    the pace cap. Does not invent places.
    """
    notes: list[str] = []
    if not days or not interests:
        return days, notes

    interest_keys = list(
        dict.fromkeys(
            (normalize_interest(i) or i).lower()
            for i in interests
            if (normalize_interest(i) or i)
        )
    )
    if not interest_keys:
        return days, notes

    working = [d.model_copy(deep=True) for d in days]
    cap = STOPS_PER_DAY.get(pace, 4)
    pool = sorted(
        list(candidate_pois or []),
        key=lambda p: (-(p.rank_score or 0.0), p.name or ""),
    )
    changed = False

    def _all_stops() -> list[Stop]:
        return [s for d in working for s in d.all_stops]

    def _used() -> PlaceSeen:
        seen = PlaceSeen()
        for s in _all_stops():
            seen.add(s)
        return seen

    def _set_block(day: DayPlan, bname: str, stops: list[Stop], notes_s: str | None) -> None:
        setattr(
            day,
            bname,
            TimeBlock(
                time_of_day=bname,  # type: ignore[arg-type]
                stops=stops,
                notes=notes_s,
            ),
        )

    def _try_place(key: str, pick: POICandidate) -> bool:
        nonlocal changed
        flat = _all_stops()
        new_stop = _make_stop(pick, pace=pace, interests=interest_keys).model_copy(
            update={
                "reason": (
                    f"Restored '{key}' interest coverage after packing "
                    f"(live OSM place)."
                )
            }
        )
        # 1) Replace a non-sole-cover stop (prefer extra heritage).
        for day in working:
            slots: list[tuple[str, int, Stop]] = []
            for bname in ("morning", "afternoon", "evening"):
                block = day.block(bname)  # type: ignore[arg-type]
                for idx, s in enumerate(block.stops):
                    slots.append((bname, idx, s))
            slots.sort(
                key=lambda row: (
                    0 if not _sole_cover_interests(row[2], flat, interest_keys) else 1,
                    0
                    if (row[2].category or "").lower()
                    in {"heritage", "attraction", "museum", "other"}
                    else 1,
                    -(row[2].duration_min or 0),
                )
            )
            for bname, idx, s in slots:
                if _sole_cover_interests(s, flat, interest_keys):
                    continue
                if _stop_covers_interest(s, key):
                    continue
                block = day.block(bname)  # type: ignore[arg-type]
                stops = list(block.stops)
                stops[idx] = new_stop
                _set_block(day, bname, stops, block.notes)
                notes.append(
                    f"Interest coverage: restored '{key}' on Day {day.day_index} "
                    f"(replaced {s.name} with {pick.name})."
                )
                changed = True
                return True

        # 2) Append on the thinnest day under the pace cap.
        for day in sorted(working, key=lambda d: (len(d.all_stops), d.day_index)):
            if len(day.all_stops) >= cap:
                continue
            if not day.morning.stops:
                target = "morning"
            elif not day.afternoon.stops:
                target = "afternoon"
            elif len(day.morning.stops) <= len(day.afternoon.stops):
                target = "morning"
            else:
                target = "afternoon"
            block = day.block(target)  # type: ignore[arg-type]
            _set_block(day, target, [*block.stops, new_stop], block.notes)
            notes.append(
                f"Interest coverage: restored '{key}' on Day {day.day_index} "
                f"(added {pick.name})."
            )
            changed = True
            return True

        notes.append(
            f"Interest coverage: could not place '{key}' "
            f"({pick.name}) without exceeding pace caps."
        )
        return False

    for key in interest_keys:
        if any(_stop_covers_interest(s, key) for s in _all_stops()):
            continue
        used = _used()
        pick = next(
            (
                p
                for p in pool
                if not used.contains(p) and _stop_covers_interest(p, key)
            ),
            None,
        )
        if pick is None:
            notes.append(
                f"Interest coverage: no unused live POI for '{key}' — left uncovered."
            )
            continue
        _try_place(key, pick)

    if changed:
        packed, pack_notes = reassert_meal_pace_layout(
            working, pace=pace, interests=interest_keys
        )
        # If reassert dropped a sole-interest stop, reinject once more then reassert.
        for key in interest_keys:
            if any(
                _stop_covers_interest(s, key) for d in packed for s in d.all_stops
            ):
                continue
            used = PlaceSeen()
            for d in packed:
                for s in d.all_stops:
                    used.add(s)
            pick = next(
                (
                    p
                    for p in pool
                    if not used.contains(p) and _stop_covers_interest(p, key)
                ),
                None,
            )
            if pick is None:
                continue
            working = packed
            if _try_place(key, pick):
                packed, more = reassert_meal_pace_layout(
                    working, pace=pace, interests=interest_keys
                )
                pack_notes = list(pack_notes) + list(more)
        working = packed
        notes.extend(pack_notes)

    return working, notes


def _trim_day_stops_for_pace(
    stops: list[Stop],
    *,
    cap: int,
    pace: Pace,
    interests: list[str],
) -> list[Stop]:
    """Keep up to ``cap`` stops, preferring foods then interest-matched categories.

    Never drops a stop that is the sole cover of a stated interest when another
    non-critical stop can be trimmed instead.
    """
    if len(stops) <= cap:
        return list(stops)
    interest_keys = list(
        dict.fromkeys(
            (normalize_interest(i) or i).lower()
            for i in interests
            if (normalize_interest(i) or i)
        )
    )
    preferred_cats = categories_for_interests(interests)
    protected = [s for s in stops if _sole_cover_interests(s, stops, interest_keys)]
    foods = [
        s
        for s in stops
        if (s.category or "").lower() == "food" and s not in protected
    ]
    soft = [
        s
        for s in stops
        if s not in protected
        and _is_evening_soft(_stop_to_poi(s), interests)
        and (s.category or "").lower() != "food"
    ]
    rest = [
        s
        for s in stops
        if s not in protected and s not in foods and s not in soft
    ]
    rest.sort(
        key=lambda s: (
            0 if (s.category or "").lower() in preferred_cats else 1,
            s.name or "",
        )
    )
    keep: list[Stop] = list(protected)
    room = max(0, cap - len(keep))
    keep_foods = foods[: min(len(foods), min(3, max(2, cap // 2)), room)]
    keep.extend(keep_foods)
    room = max(0, cap - len(keep))
    soft_budget = MAX_EVENING_EXTRAS.get(pace, 0)
    keep.extend(soft[: min(len(soft), soft_budget, room)])
    room = max(0, cap - len(keep))
    keep.extend(rest[:room])
    kept_ids = {id(s) for s in keep}
    return [s for s in stops if id(s) in kept_ids][:cap]



def reassert_meal_pace_layout(
    days: list[DayPlan],
    *,
    pace: Pace = "relaxed",
    interests: list[str] | None = None,
) -> tuple[list[DayPlan], list[str]]:
    """Re-apply pace budgets + meal ordering after optimize/LLM reshuffles.

    Does not invent places. Rebuilds each day's morning/afternoon/evening from the
    day's existing stops using the same packer as ``build_itinerary``.
    """
    interests = list(interests or [])
    notes: list[str] = []
    if not days:
        return days, notes

    cap = STOPS_PER_DAY.get(pace, 4)
    out: list[DayPlan] = []
    for day in days:
        flat = list(day.all_stops)
        if not flat:
            out.append(day.model_copy(deep=True))
            continue

        before_n = len(flat)
        # Deduplicate within the day first.
        seen = PlaceSeen()
        unique: list[Stop] = []
        for s in flat:
            if seen.contains(s):
                continue
            seen.add(s)
            unique.append(s)
        trimmed = _trim_day_stops_for_pace(
            unique, cap=cap, pace=pace, interests=interests
        )
        if len(trimmed) < before_n:
            notes.append(
                f"Day {day.day_index}: reasserted pace={pace} cap ≤{cap} "
                f"({before_n}→{len(trimmed)} stops)."
            )

        by_id = {s.osm_id: s for s in trimmed}
        pois = [_stop_to_poi(s) for s in trimmed]
        packed = _assign_blocks(
            pois, pace=pace, day_index=day.day_index, interests=interests
        )

        def _remap(block: TimeBlock) -> list[Stop]:
            remapped: list[Stop] = []
            for s in block.stops:
                orig = by_id.get(s.osm_id)
                if orig is None:
                    continue
                remapped.append(
                    orig.model_copy(
                        update={
                            "travel_to_next_min": None,
                            "travel_to_next_km": None,
                            "travel_to_next_mode": None,
                        }
                    )
                )
            return remapped

        new_day = DayPlan(
            day_index=day.day_index,
            theme=day.theme or packed.theme,
            morning=TimeBlock(
                time_of_day="morning",
                stops=_remap(packed.morning),
                notes=packed.morning.notes or day.morning.notes,
            ),
            afternoon=TimeBlock(
                time_of_day="afternoon",
                stops=_remap(packed.afternoon),
                notes=packed.afternoon.notes or day.afternoon.notes,
            ),
            evening=TimeBlock(
                time_of_day="evening",
                stops=_remap(packed.evening),
                notes=packed.evening.notes or day.evening.notes,
            ),
        )
        out.append(restamp_day_travel_and_clocks(new_day, pace=pace))
        notes.append(
            f"Day {day.day_index}: restored meal/pace layout "
            f"(M{len(new_day.morning.stops)} "
            f"A{len(new_day.afternoon.stops)} "
            f"E{len(new_day.evening.stops)})."
        )

    out, flex_notes = annotate_block_flex_time(out, pace=pace)
    notes.extend(flex_notes)
    out = refresh_day_themes(out)
    return out, notes

