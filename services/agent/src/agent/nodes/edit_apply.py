"""Scoped voice-edit applicator — only the target day/block changes."""

from __future__ import annotations

import logging
import re
from typing import Any

from agent.place_identity import PlaceSeen, allow_repeat_requested, dedupe_day_plans
from agent.schemas.edits import EditPatch, EditTarget
from agent.schemas.itinerary import DayPlan, Itinerary, Stop, TimeBlock, TimeOfDay
from agent.schemas.specialists import POICandidate

logger = logging.getLogger(__name__)

INDOOR_CATEGORIES = {"museum", "food", "market", "temple", "heritage", "art"}
OUTDOOR_CATEGORIES = {"park", "garden", "viewpoint", "nature", "adventure"}

CATEGORY_ALIASES: dict[str, set[str]] = {
    "food": {"food"},
    "restaurant": {"food"},
    "cafe": {"food"},
    "outdoor": OUTDOOR_CATEGORIES,
    "outdoors": OUTDOOR_CATEGORIES,
    "park": {"park", "nature", "viewpoint"},
    "nature": {"park", "nature", "viewpoint"},
    "viewpoint": {"viewpoint", "park"},
    "heritage": {"heritage"},
    "museum": {"museum"},
    "market": {"market", "shopping"},
    "shopping": {"shopping", "market"},
    "temple": {"temple"},
}


def _category_set(category: str) -> set[str]:
    key = (category or "").lower().strip()
    return set(CATEGORY_ALIASES.get(key, {key} if key else set()))


def _normalize_alias_cat(category: str) -> str:
    key = (category or "").lower().strip()
    key = re.sub(r"^musu?e?ums?$", "museum", key)
    if key in {"restaurant", "cafe", "eatery"}:
        return "food"
    if key == "shopping":
        return "market"
    if key in {"outdoors"}:
        return "park"
    return key


def _poi_to_stop(poi: POICandidate, *, reason: str, duration_min: int = 60) -> Stop:
    return Stop(
        name=poi.name,
        osm_type=poi.osm_type,  # type: ignore[arg-type]
        osm_id=int(poi.osm_id),
        lat=poi.lat,
        lon=poi.lon,
        category=poi.category,
        duration_min=duration_min,
        travel_to_next_min=None,
        reason=reason,
        citations=[],
        uncertainty="Added via voice edit; OSM id is ground truth.",
    )


def _block_names(day: DayPlan) -> list[TimeOfDay]:
    return ["morning", "afternoon", "evening"]


def _get_block(day: DayPlan, block: TimeOfDay | None) -> list[tuple[TimeOfDay, TimeBlock]]:
    if block:
        return [(block, day.block(block))]
    return [(b, day.block(b)) for b in _block_names(day)]


def _set_block(day: DayPlan, name: TimeOfDay, block: TimeBlock) -> DayPlan:
    data = day.model_dump(mode="python")
    data[name] = block.model_dump(mode="python")
    return DayPlan.model_validate(data)


def _day_travel_min(day: DayPlan) -> int:
    return sum(s.travel_to_next_min or 0 for s in day.all_stops)


def _relax_stops(stops: list[Stop], keep: int) -> list[Stop]:
    if len(stops) <= keep:
        # Soften durations for a relaxed feel
        return [
            s.model_copy(
                update={
                    "duration_min": min(150, int(s.duration_min * 1.15)),
                    "travel_to_next_min": (
                        None
                        if s.travel_to_next_min is None
                        else max(5, int(s.travel_to_next_min * 0.8))
                    ),
                }
            )
            for s in stops
        ]
    kept = stops[:keep]
    if kept:
        kept[-1] = kept[-1].model_copy(
            update={
                "travel_to_next_min": None,
                "travel_to_next_km": None,
                "travel_to_next_mode": None,
            }
        )
    return kept


def _existing_places(itin: Itinerary, extra_day: DayPlan | None = None) -> PlaceSeen:
    places = [s for d in itin.days for s in d.all_stops]
    if extra_day is not None:
        places.extend(extra_day.all_stops)
    return PlaceSeen.from_places(places)


def _patch_allows_repeat(patch: EditPatch) -> bool:
    payload = patch.payload or {}
    if payload.get("allow_repeat"):
        return True
    return allow_repeat_requested(patch.user_utterance)


def _pick_add_block(day: DayPlan, preferred: TimeOfDay | None) -> TimeOfDay:
    if preferred:
        return preferred
    for name in ("afternoon", "morning", "evening"):
        if len(day.block(name).stops) < 3:  # type: ignore[arg-type]
            return name  # type: ignore[return-value]
    return "afternoon"


def _stop_name_match_score(query: str, name: str) -> float:
    """Score how well an utterance place name matches a stop (higher = better)."""
    q = re.sub(r"\s+", " ", (query or "").strip().lower())
    n = re.sub(r"\s+", " ", (name or "").strip().lower())
    if not q or not n:
        return 0.0
    if n == q:
        return 100.0
    if q in n or n in q:
        return 80.0
    q_tokens = set(re.findall(r"[a-z0-9]+", q))
    n_tokens = set(re.findall(r"[a-z0-9]+", n))
    if not q_tokens or not n_tokens:
        return 0.0
    overlap = len(q_tokens & n_tokens)
    if overlap == 0:
        return 0.0
    return 40.0 + 20.0 * overlap / max(len(q_tokens), len(n_tokens))


def _find_stop_on_day(
    day: DayPlan,
    *,
    name_query: str,
    target_block: TimeOfDay | None,
) -> tuple[TimeOfDay, int, Stop] | None:
    """Best fuzzy match for a named stop on the day (optionally scoped to a block)."""
    best: tuple[float, TimeOfDay, int, Stop] | None = None
    for bname, block in _get_block(day, target_block):
        for i, s in enumerate(block.stops):
            score = _stop_name_match_score(name_query, s.name or "")
            if score < 40.0:
                continue
            if best is None or score > best[0]:
                best = (score, bname, i, s)
    if best is None:
        return None
    return best[1], best[2], best[3]


def _remove_named_stop_day(
    day: DayPlan,
    *,
    name_query: str,
    target_block: TimeOfDay | None,
) -> tuple[DayPlan, list[str]]:
    """Remove a single named stop; leave other same-category stops alone."""
    hit = _find_stop_on_day(day, name_query=name_query, target_block=target_block)
    if hit is None and target_block is not None:
        # Name may be on another block of the same day.
        hit = _find_stop_on_day(day, name_query=name_query, target_block=None)
    if hit is None:
        return day, [
            f"Could not find a stop matching '{name_query}' on Day {day.day_index}."
        ]
    bname, idx, stop = hit
    block = day.block(bname)
    new_stops = [s for i, s in enumerate(block.stops) if i != idx]
    new_day = _set_block(
        day,
        bname,
        TimeBlock(
            time_of_day=bname,
            stops=new_stops,
            notes=block.notes,
        ),
    )
    return new_day, [
        f"Removed {stop.name} from Day {day.day_index} {bname}."
    ]


def _trim_category_day(
    day: DayPlan,
    *,
    category: str,
    keep: int,
    target_block: TimeOfDay | None,
) -> tuple[DayPlan, list[str]]:
    """Keep at most ``keep`` stops matching category on the target day/block."""
    notes: list[str] = []
    cats = _category_set(category)
    keep = max(0, int(keep))
    matched: list[tuple[TimeOfDay, int, Stop]] = []
    for bname, block in _get_block(day, target_block):
        for i, s in enumerate(block.stops):
            if (s.category or "").lower() in cats:
                matched.append((bname, i, s))

    if len(matched) <= keep:
        notes.append(
            f"Day {day.day_index} already has {len(matched)} {category} stop(s) "
            f"(≤ {keep}) — left as-is."
        )
        return day, notes

    # Keep the first ``keep`` matches (stable), drop the rest.
    # keep=0 removes every stop in that category on the targeted day/block.
    drop_keys = {
        (b, i) for b, i, _ in matched[keep:]
    }
    removed_names = [s.name for _, _, s in matched[keep:]]
    new_day = day
    for bname, block in _get_block(day, target_block):
        new_stops = [
            s
            for i, s in enumerate(block.stops)
            if (bname, i) not in drop_keys
        ]
        if new_stops:
            new_stops[-1] = new_stops[-1].model_copy(
                update={
                    "travel_to_next_min": new_stops[-1].travel_to_next_min,
                }
            )
        new_day = _set_block(
            new_day,
            bname,
            TimeBlock(
                time_of_day=bname,
                stops=new_stops,
                notes=(block.notes or "")
                + f" Trimmed {category} stops via voice edit.",
            ),
        )
    if keep == 0:
        notes.append(
            f"Removed {category} stops on Day {day.day_index} "
            f"({', '.join(removed_names)})."
        )
    else:
        notes.append(
            f"Reduced {category} stops on Day {day.day_index} to {keep} "
            f"(removed: {', '.join(removed_names)})."
        )
    return new_day, notes


def resolve_edit_patches(
    itinerary: Itinerary, patches: list[EditPatch]
) -> list[EditPatch]:
    """Resolve deferred targets (e.g. undirected reduce-travel → heaviest day)."""
    out: list[EditPatch] = []
    for patch in patches:
        if patch.operation == "reduce_travel" and (patch.payload or {}).get(
            "auto_day"
        ):
            if not itinerary.days:
                out.append(patch)
                continue
            day_idx = max(
                itinerary.days,
                key=lambda d: (_day_travel_min(d), len(d.all_stops)),
            ).day_index
            payload = {
                k: v
                for k, v in (patch.payload or {}).items()
                if k != "auto_day"
            }
            out.append(
                patch.model_copy(
                    update={
                        "target": EditTarget(
                            day=day_idx, block=patch.target.block
                        ),
                        "payload": payload,
                    }
                )
            )
        else:
            out.append(patch)
    return out


def _reduce_day_travel(day: DayPlan) -> tuple[DayPlan, list[str]]:
    """Remove one stop to shorten travel while preserving other blocks."""
    notes: list[str] = []
    best: tuple[int, TimeOfDay, int] | None = None
    for bname in ("morning", "afternoon", "evening"):
        block = day.block(bname)  # type: ignore[arg-type]
        for i, s in enumerate(block.stops):
            t = int(s.travel_to_next_min or 0)
            if best is None or t > best[0]:
                best = (t, bname, i)  # type: ignore[assignment]

    if best is None or len(day.all_stops) < 2:
        notes.append(
            f"Day {day.day_index} already has few stops — travel left unchanged."
        )
        return day, notes

    _, bname, idx = best
    block = day.block(bname)
    stops = list(block.stops)

    # Prefer removing the destination of the longest same-block hop.
    if len(stops) >= 2 and idx + 1 < len(stops):
        removed = stops.pop(idx + 1)
        if stops:
            stops[-1] = stops[-1].model_copy(
                update={
                    "travel_to_next_min": None,
                    "travel_to_next_km": None,
                    "travel_to_next_mode": None,
                }
            )
        new_day = _set_block(
            day,
            bname,
            TimeBlock(time_of_day=bname, stops=stops, notes=block.notes),
        )
        notes.append(
            f"Removed {removed.name} on Day {day.day_index} to cut travel."
        )
        return new_day, notes

    if len(stops) >= 2:
        removed = stops.pop(min(idx, len(stops) - 1))
        if stops:
            stops[-1] = stops[-1].model_copy(
                update={
                    "travel_to_next_min": None,
                    "travel_to_next_km": None,
                    "travel_to_next_mode": None,
                }
            )
        new_day = _set_block(
            day,
            bname,
            TimeBlock(time_of_day=bname, stops=stops, notes=block.notes),
        )
        notes.append(
            f"Removed {removed.name} on Day {day.day_index} to cut travel."
        )
        return new_day, notes

    # Long hop is between blocks — trim the next block's first stop.
    order: list[TimeOfDay] = ["morning", "afternoon", "evening"]
    try:
        start = order.index(bname)
    except ValueError:
        start = 0
    for next_b in order[start + 1 :]:
        nb = day.block(next_b)
        if not nb.stops:
            continue
        nstops = list(nb.stops)
        removed = nstops.pop(0)
        new_day = _set_block(
            day,
            next_b,
            TimeBlock(time_of_day=next_b, stops=nstops, notes=nb.notes),
        )
        # Clear stale travel on the origin stop.
        origin = list(day.block(bname).stops)
        if origin:
            origin[-1] = origin[-1].model_copy(
                update={
                    "travel_to_next_min": None,
                    "travel_to_next_km": None,
                    "travel_to_next_mode": None,
                }
            )
            new_day = _set_block(
                new_day,
                bname,
                TimeBlock(
                    time_of_day=bname,
                    stops=origin,
                    notes=day.block(bname).notes,
                ),
            )
        notes.append(
            f"Removed {removed.name} on Day {day.day_index} to cut travel."
        )
        return new_day, notes

    # Fallback: drop any remaining extra stop from the densest block.
    densest = max(
        ("morning", "afternoon", "evening"),
        key=lambda b: len(day.block(b).stops),  # type: ignore[arg-type]
    )
    dblock = day.block(densest)  # type: ignore[arg-type]
    if len(dblock.stops) >= 2:
        dstops = list(dblock.stops)
        removed = dstops.pop(-1)
        new_day = _set_block(
            day,
            densest,  # type: ignore[arg-type]
            TimeBlock(
                time_of_day=densest,  # type: ignore[arg-type]
                stops=dstops,
                notes=dblock.notes,
            ),
        )
        notes.append(
            f"Removed {removed.name} on Day {day.day_index} to cut travel."
        )
        return new_day, notes

    notes.append(
        f"Day {day.day_index} already has few stops — travel left unchanged."
    )
    return day, notes


def _apply_pace_day_reshape(
    day: DayPlan,
    *,
    pace: str,
    interests: list[str],
    note_suffix: str,
) -> tuple[DayPlan, list[str]]:
    """Trim/reshape one day to the target pace layout (no invented POIs)."""
    from agent.mcp.itinerary_builder import reassert_meal_pace_layout

    pace_key = pace if pace in ("relaxed", "moderate", "packed") else "moderate"
    fixed, re_notes = reassert_meal_pace_layout(
        [day], pace=pace_key, interests=interests  # type: ignore[arg-type]
    )
    out = fixed[0] if fixed else day.model_copy(deep=True)
    # Tag blocks so UI/debug shows the voice edit source.
    for bname in ("morning", "afternoon", "evening"):
        block = out.block(bname)  # type: ignore[arg-type]
        tagged = (block.notes or "").strip()
        if note_suffix and note_suffix not in tagged:
            tagged = f"{tagged} {note_suffix}".strip() if tagged else note_suffix
        out = _set_block(
            out,
            bname,  # type: ignore[arg-type]
            TimeBlock(
                time_of_day=bname,  # type: ignore[arg-type]
                stops=list(block.stops),
                notes=tagged or None,
            ),
        )
    return out, list(re_notes)


def apply_edit_patches(
    itinerary: Itinerary,
    patches: list[EditPatch],
    *,
    candidate_pois: list[POICandidate] | None = None,
) -> tuple[Itinerary, list[str]]:
    """Apply one or more scoped edits sequentially (compound 'and' utterances)."""
    if not patches:
        return itinerary.model_copy(deep=True), ["No edit patches to apply."]
    notes: list[str] = []
    current = itinerary
    touched_days: set[int] = set()
    for patch in patches:
        current, step_notes = apply_edit_patch(
            current, patch, candidate_pois=candidate_pois
        )
        notes.extend(step_notes)
        touched_days.add(patch.target.day)
    days_label = ", ".join(f"Day {d}" for d in sorted(touched_days))
    ops = "+".join(p.operation for p in patches)
    current = current.model_copy(
        update={
            "summary": (
                f"Updated {days_label} ({ops}); other days unchanged."
            ),
            "reasoning": list(itinerary.reasoning or []) + notes,
        }
    )
    return current, notes


def apply_edit_patch(
    itinerary: Itinerary,
    patch: EditPatch,
    *,
    candidate_pois: list[POICandidate] | None = None,
) -> tuple[Itinerary, list[str]]:
    """
    Apply a voice edit. Non-target days are deep-copied unchanged.
    Returns (new_itinerary, reasoning_notes).
    """
    notes: list[str] = []
    target_day = patch.target.day
    target_block = patch.target.block
    op = patch.operation
    pois = list(candidate_pois or [])
    if op == "reduce_travel" and (patch.payload or {}).get("auto_day"):
        # Undirected "reduce travel" → edit the day with the most travel time.
        if itinerary.days:
            target_day = max(
                itinerary.days,
                key=lambda d: (_day_travel_min(d), len(d.all_stops)),
            ).day_index

    days_out: list[DayPlan] = []
    touched = False

    for day in itinerary.days:
        if day.day_index != target_day:
            days_out.append(day.model_copy(deep=True))
            continue

        new_day = day.model_copy(deep=True)
        touched = True

        if op == "relax_block":
            # Packed/busy → relaxed: trim toward ~2-1-1 meal layout (keep dinner).
            # Clocks + flex notes are restamped in Synthesis using trip.pace=relaxed.
            interests = (
                list(itinerary.trip.interests or [])
                if itinerary.trip
                else []
            )
            if target_block:
                # Block-scoped: keep a single softer stop in that block only.
                for bname, block in _get_block(new_day, target_block):
                    new_stops = _relax_stops(list(block.stops), keep=1)
                    new_day = _set_block(
                        new_day,
                        bname,
                        TimeBlock(
                            time_of_day=bname,
                            stops=new_stops,
                            notes=(block.notes or "") + " Relaxed via voice edit.",
                        ),
                    )
                notes.append(
                    f"Relaxed Day {target_day} {target_block} "
                    "(fewer / slower stops)."
                )
            else:
                before_n = len(new_day.all_stops)
                new_day, re_notes = _apply_pace_day_reshape(
                    new_day,
                    pace="relaxed",
                    interests=interests,
                    note_suffix="Relaxed via voice edit.",
                )
                after_n = len(new_day.all_stops)
                notes.append(
                    f"Relaxed Day {target_day} "
                    f"(relaxed pacing; {before_n}→{after_n} stops; "
                    "schedule will use morning/afternoon anchors + free time)."
                )
                notes.extend(re_notes)

        elif op == "balance_block":
            # Toward moderate/balanced (2–4 / 2–4 / 1–3). From packed: trim first.
            # From thin relaxed: densify with unused interest POIs.
            from agent.mcp.itinerary_builder import STOPS_PER_DAY
            from agent.preferences import categories_for_interests

            used = _existing_places(itinerary, new_day)
            interests = (
                list(itinerary.trip.interests or [])
                if itinerary.trip
                else []
            )
            preferred_cats = categories_for_interests(interests) or {
                (c or "").lower() for c in interests if c
            }
            culture_cats = {"museum", "heritage", "temple", "attraction"}
            cap = STOPS_PER_DAY.get("moderate", 10)
            before_n = len(new_day.all_stops)

            # Always reshape to moderate meal/block layout (trims when over-cap).
            new_day, re_notes = _apply_pace_day_reshape(
                new_day,
                pace="moderate",
                interests=interests,
                note_suffix="Balanced via voice edit.",
            )
            notes.extend(re_notes)

            flat = list(new_day.all_stops)
            from agent.mcp.poi_search import _MUST_SEE_NAME_RE, _is_low_signal_poi

            ranked_pool = sorted(
                [
                    p
                    for p in pois
                    if not used.contains(p)
                    and not _is_low_signal_poi(
                        p.name or "", p.tags or {}, (p.category or "").lower()
                    )
                ],
                key=lambda p: (
                    0
                    if _MUST_SEE_NAME_RE.search(p.name or "")
                    or (p.category or "").lower() in culture_cats
                    else 1
                    if (p.category or "").lower() in preferred_cats
                    else 2,
                    -(p.rank_score or 0),
                    p.name or "",
                ),
            )
            # Balanced densify: morning 2 · afternoon 2 · evening 0–1 (~4–5).
            target_n = max(4, min(cap, 5))
            added_names: list[str] = []

            def _take_from_pool(pred=None) -> bool:
                nonlocal ranked_pool, flat
                for i, p in enumerate(ranked_pool):
                    if used.contains(p):
                        continue
                    if pred is not None and not pred(p):
                        continue
                    used.add(p)
                    flat.append(
                        _poi_to_stop(
                            p,
                            reason="Added for a more balanced day via voice edit.",
                            duration_min=60,
                        )
                    )
                    added_names.append(p.name)
                    ranked_pool = ranked_pool[:i] + ranked_pool[i + 1 :]
                    return True
                return False

            want_shop = bool(
                preferred_cats & {"shopping", "market"}
                or {"shopping", "market"} & {(i or "").lower() for i in interests}
            )
            if want_shop and not any(
                (s.category or "").lower() in {"market", "shopping"} for s in flat
            ):
                _take_from_pool(
                    lambda p: (p.category or "").lower() in {"market", "shopping"}
                )
            want_sight = bool(
                preferred_cats & {"museum", "heritage", "temple", "attraction"}
            )
            if want_sight and sum(
                1
                for s in flat
                if (s.category or "").lower()
                in {"museum", "heritage", "temple", "attraction"}
            ) < 2:
                _take_from_pool(
                    lambda p: (p.category or "").lower()
                    in {"museum", "heritage", "temple", "attraction"}
                )
            while len(flat) < target_n:
                def _fill_ok(p) -> bool:
                    cat = (p.category or "").lower()
                    if not want_shop and cat in {"market", "shopping"}:
                        return False
                    if preferred_cats and cat not in preferred_cats:
                        return False
                    return True

                if not _take_from_pool(_fill_ok):
                    if not _take_from_pool(
                        (lambda p: (p.category or "").lower()
                         not in {"market", "shopping"})
                        if not want_shop
                        else None
                    ):
                        break

            if added_names or len(flat) != len(new_day.all_stops):
                temp = DayPlan(
                    day_index=new_day.day_index,
                    theme=new_day.theme,
                    morning=TimeBlock(time_of_day="morning", stops=flat),
                    afternoon=TimeBlock(time_of_day="afternoon", stops=[]),
                    evening=TimeBlock(time_of_day="evening", stops=[]),
                )
                new_day, more_notes = _apply_pace_day_reshape(
                    temp,
                    pace="moderate",
                    interests=interests,
                    note_suffix="Balanced via voice edit.",
                )
                notes.extend(more_notes)

            after_n = len(new_day.all_stops)
            if added_names:
                notes.append(
                    f"Balanced Day {target_day} "
                    f"(moderate pacing; {before_n}→{after_n} stops; "
                    f"added {', '.join(added_names)})."
                )
            else:
                notes.append(
                    f"Balanced Day {target_day} "
                    f"(moderate pacing; {before_n}→{after_n} stops; "
                    "schedule will use block anchors + free time)."
                )

        elif op == "balance_categories":
            raw_cats = list((patch.payload or {}).get("categories") or [])
            want = [_normalize_alias_cat(c) for c in raw_cats]
            want = [c for c in want if c]
            if len(want) < 2:
                notes.append(
                    f"Could not balance Day {target_day} — need at least two categories."
                )
            else:
                used = _existing_places(itinerary, new_day)
                pools: dict[str, list[POICandidate]] = {c: [] for c in want}
                for p in pois:
                    cat = (p.category or "").lower()
                    if used.contains(p):
                        continue
                    for w in want:
                        if cat in _category_set(w) and p not in pools[w]:
                            pools[w].append(p)
                # Flatten day stops with block membership.
                slots: list[tuple[TimeOfDay, Stop]] = []
                for bname in ("morning", "afternoon", "evening"):
                    for s in new_day.block(bname).stops:  # type: ignore[arg-type]
                        slots.append((bname, s))  # type: ignore[arg-type]
                if not slots:
                    notes.append(f"Day {target_day} has no stops to rebalance.")
                else:
                    # Target ~even mix across requested categories; keep length.
                    n = len(slots)
                    per = max(1, n // len(want))
                    targets: list[str] = []
                    for w in want:
                        targets.extend([w] * per)
                    while len(targets) < n:
                        targets.append(want[len(targets) % len(want)])
                    targets = targets[:n]

                    new_by_block: dict[str, list[Stop]] = {
                        "morning": [],
                        "afternoon": [],
                        "evening": [],
                    }
                    swapped: list[str] = []
                    for (bname, stop), target_cat in zip(slots, targets):
                        cur = (stop.category or "").lower()
                        if cur in _category_set(target_cat):
                            new_by_block[bname].append(stop)
                            continue
                        pool = pools.get(target_cat) or []
                        if not pool:
                            new_by_block[bname].append(stop)
                            continue
                        p = pool.pop(0)
                        used.add(p)
                        replacement = _poi_to_stop(
                            p,
                            reason=(
                                f"Swapped in for a more balanced {target_cat} "
                                "mix via voice edit."
                            ),
                            duration_min=max(55, min(90, stop.duration_min or 60)),
                        )
                        new_by_block[bname].append(replacement)
                        swapped.append(f"{stop.name}→{p.name} ({target_cat})")
                    for bname in ("morning", "afternoon", "evening"):
                        bl = new_day.block(bname)  # type: ignore[arg-type]
                        new_day = _set_block(
                            new_day,
                            bname,  # type: ignore[arg-type]
                            TimeBlock(
                                time_of_day=bname,  # type: ignore[arg-type]
                                stops=new_by_block[bname],
                                notes=(bl.notes or "")
                                + " Balanced categories via voice edit.",
                            ),
                        )
                    if swapped:
                        notes.append(
                            f"Balanced Day {target_day} for "
                            f"{' & '.join(want)} "
                            f"(changed {', '.join(swapped[:4])}"
                            f"{'…' if len(swapped) > 4 else ''})."
                        )
                    else:
                        notes.append(
                            f"Day {target_day} already mixes "
                            f"{' & '.join(want)} — left as-is."
                        )

        elif op == "pack_block":
            from agent.mcp.itinerary_builder import (
                BLOCK_FLOOR_BY_PACE,
                BLOCK_SOFT_CAP_BY_PACE,
                _has_non_food_interest,
                _is_evening_eligible,
                _is_food,
                _FREE_EVENING_NOTE,
            )
            from agent.mcp.poi_search import _MUST_SEE_NAME_RE, _is_low_signal_poi
            from agent.preferences import categories_for_interests, culture_soft_mix_active

            used = _existing_places(itinerary, new_day)
            interests = (
                list(itinerary.trip.interests or [])
                if itinerary.trip
                else []
            )
            prefer_non_food = _has_non_food_interest(interests)
            interest_keys = [
                (i or "").lower() for i in interests if i
            ]
            culture_cats = {"heritage", "museum", "temple", "attraction"}
            soft_cats = {"market", "shopping", "food", "cafe", "restaurant", "nightlife"}
            mixed_culture_soft = culture_soft_mix_active(interest_keys)
            want_culture = bool(
                set(interest_keys)
                & {"heritage", "culture", "history", "temple", "museum", "art"}
            )
            non_food_cats = {
                c
                for c in categories_for_interests(interests)
                if c not in {"food", "cafe", "restaurant"}
            }
            # Rank unused POIs: must-sees / culture first, then interest non-food,
            # then other non-food, then food / remainder.
            def _pack_rank(p: POICandidate) -> tuple:
                cat = (p.category or "").lower()
                name = p.name or ""
                is_food = _is_food(p) or cat in {"food", "cafe", "restaurant"}
                must = 1 if _MUST_SEE_NAME_RE.search(name) else 0
                culture = 1 if cat in culture_cats else 0
                if prefer_non_food or want_culture:
                    if must or culture:
                        return (0, -must, -culture, -(p.rank_score or 0), name)
                    if cat in non_food_cats:
                        return (1, 0, 0, -(p.rank_score or 0), name)
                    if not is_food:
                        return (2, 0, 0, -(p.rank_score or 0), name)
                    return (3, 0, 0, -(p.rank_score or 0), name)
                if is_food:
                    return (0, 0, 0, -(p.rank_score or 0), name)
                return (1, 0, 0, -(p.rank_score or 0), name)

            pool = sorted(
                [
                    p
                    for p in pois
                    if not used.contains(p)
                    and not _is_low_signal_poi(
                        p.name or "", p.tags or {}, (p.category or "").lower()
                    )
                ],
                key=_pack_rank,
            )
            added_names: list[str] = []
            floors = BLOCK_FLOOR_BY_PACE.get("packed", BLOCK_FLOOR_BY_PACE["moderate"])
            soft_caps = BLOCK_SOFT_CAP_BY_PACE.get(
                "packed", BLOCK_SOFT_CAP_BY_PACE["moderate"]
            )

            def _pick_for_block(
                stops: list[Stop], *, bname: str
            ) -> POICandidate | None:
                """Avoid stacking a second food; prefer culture when mixed.

                Evening only accepts after-5:00 PM eligible stops.
                """
                block_has_food = any(
                    (s.category or "").lower() in {"food", "cafe", "restaurant"}
                    for s in stops
                )
                block_has_culture = any(
                    (s.category or "").lower() in culture_cats for s in stops
                )
                block_market_n = sum(
                    1
                    for s in stops
                    if (s.category or "").lower() in {"market", "shopping"}
                )
                require_non_food = prefer_non_food and block_has_food
                need_culture = (
                    mixed_culture_soft
                    and want_culture
                    and not block_has_culture
                    and bname != "evening"
                )
                for i, p in enumerate(pool):
                    if used.contains(p):
                        continue
                    if bname == "evening" and not _is_evening_eligible(p, interests):
                        continue
                    cat = (p.category or "").lower()
                    is_food = _is_food(p) or cat in {"food", "cafe", "restaurant"}
                    if require_non_food and is_food:
                        continue
                    if need_culture and cat in soft_cats and cat not in culture_cats:
                        continue
                    if mixed_culture_soft and cat in {"market", "shopping"} and block_market_n >= 1:
                        continue
                    if require_non_food and non_food_cats and cat not in non_food_cats:
                        if cat not in culture_cats and not _MUST_SEE_NAME_RE.search(
                            p.name or ""
                        ):
                            continue
                    return pool.pop(i)
                if need_culture:
                    for i, p in enumerate(pool):
                        if used.contains(p):
                            continue
                        if (p.category or "").lower() in culture_cats:
                            return pool.pop(i)
                if require_non_food:
                    for i, p in enumerate(pool):
                        if used.contains(p):
                            continue
                        if bname == "evening" and not _is_evening_eligible(p, interests):
                            continue
                        cat = (p.category or "").lower()
                        is_food = _is_food(p) or cat in {"food", "cafe", "restaurant"}
                        if not is_food:
                            return pool.pop(i)
                while pool:
                    p = pool.pop(0)
                    if used.contains(p):
                        continue
                    if bname == "evening" and not _is_evening_eligible(p, interests):
                        continue
                    return p
                return None

            for bname, block in _get_block(new_day, target_block):
                floor = floors.get(bname, 2)
                soft_cap = soft_caps.get(bname, 5)
                if bname == "evening":
                    eve_ok = any(_is_evening_eligible(p, interests) for p in pool)
                    if not eve_ok:
                        floor = 0
                        soft_cap = 0
                # Packed day: meet pace floors, then soft-cap when packing a block.
                if target_block:
                    target_count = max(floor, len(block.stops) + 1)
                    target_count = min(soft_cap, max(target_count, floor))
                else:
                    target_count = max(floor, len(block.stops))
                    target_count = min(soft_cap, target_count)
                stops = [
                    s.model_copy(
                        update={
                            "duration_min": max(45, int(s.duration_min * 0.85)),
                        }
                    )
                    for s in block.stops
                ]
                if bname == "evening":
                    from types import SimpleNamespace

                    stops = [
                        s
                        for s in stops
                        if _is_evening_eligible(
                            SimpleNamespace(name=s.name, category=s.category),
                            interests,
                        )
                    ]
                while len(stops) < target_count:
                    p = _pick_for_block(stops, bname=bname)
                    if p is None:
                        break
                    used.add(p)
                    stops.append(
                        _poi_to_stop(
                            p,
                            reason=(
                                "Added to pack the day via voice edit"
                                + (
                                    " (non-food interest fill)."
                                    if prefer_non_food
                                    and (p.category or "").lower()
                                    not in {"food", "cafe", "restaurant"}
                                    else "."
                                )
                            ),
                            duration_min=55,
                        )
                    )
                    added_names.append(p.name)
                eve_note = ""
                if bname == "evening" and not stops:
                    eve_note = " " + _FREE_EVENING_NOTE
                new_day = _set_block(
                    new_day,
                    bname,
                    TimeBlock(
                        time_of_day=bname,
                        stops=stops,
                        notes=(block.notes or "")
                        + " Packed via voice edit."
                        + eve_note,
                    ),
                )
            if added_names:
                notes.append(
                    f"Packed Day {target_day}"
                    + (f" {target_block}" if target_block else "")
                    + f" (added {', '.join(added_names)})."
                )
            else:
                notes.append(
                    f"Packed Day {target_day}"
                    + (f" {target_block}" if target_block else "")
                    + " (tighter timings; no unused places available to add)."
                )

        elif op == "trim_category":
            category = str((patch.payload or {}).get("category") or "food").lower()
            raw_keep = (patch.payload or {}).get("keep")
            keep = 1 if raw_keep is None else int(raw_keep)
            new_day, trim_notes = _trim_category_day(
                new_day, category=category, keep=keep, target_block=target_block
            )
            notes.extend(trim_notes)

        elif op == "make_indoor":
            used = _existing_places(itinerary, new_day)
            indoor_pool = [
                p
                for p in pois
                if (p.category or "").lower() in INDOOR_CATEGORIES
                and not used.contains(p)
            ]
            rain_adjust = bool((patch.payload or {}).get("rain_adjust"))
            # Rain swaps: prefer museums/heritage/temples over yet another cafe.
            if rain_adjust:
                preferred = {"museum", "heritage", "temple", "art", "market"}
                indoor_pool = sorted(
                    indoor_pool,
                    key=lambda p: (
                        0
                        if (p.category or "").lower() in preferred
                        else 1,
                        -(p.rank_score or 0.0),
                        p.name or "",
                    ),
                )
            utterance = (patch.user_utterance or "").lower()
            force_swap = bool(re.search(r"\b(swap|replace|change)\b", utterance))
            # block=None means whole day — do NOT default to evening only
            # (that bug left morning/afternoon parks untouched on rain edits).
            for bname, block in _get_block(new_day, target_block):
                # Explicit "swap … indoors" replaces the whole block plan.
                if force_swap:
                    if indoor_pool:
                        p = indoor_pool.pop(0)
                        used.add(p)
                        new_stops = [
                            _poi_to_stop(
                                p,
                                reason=(
                                    f"Swapped Day {target_day} {bname} plan "
                                    "to an indoor stop via voice edit."
                                ),
                            )
                        ]
                        notes.append(
                            f"Swapped Day {target_day} {bname} plan → "
                            f"{p.name} (indoor)."
                        )
                    elif block.stops and all(
                        (s.category or "").lower() in INDOOR_CATEGORIES
                        and "park" not in s.name.lower()
                        and "garden" not in s.name.lower()
                        for s in block.stops
                    ):
                        new_stops = list(block.stops)
                        notes.append(
                            f"Day {target_day} {bname} is already indoor — left as-is."
                        )
                    else:
                        new_stops = [
                            s
                            for s in block.stops
                            if (s.category or "").lower() in INDOOR_CATEGORIES
                            and "park" not in s.name.lower()
                            and "garden" not in s.name.lower()
                        ]
                        notes.append(
                            f"Could not swap Day {target_day} {bname} to indoor — "
                            "no unused indoor POI candidates."
                        )
                    new_day = _set_block(
                        new_day,
                        bname,
                        TimeBlock(
                            time_of_day=bname, stops=new_stops, notes=block.notes
                        ),
                    )
                    continue

                new_stops: list[Stop] = []
                for s in block.stops:
                    cat = (s.category or "").lower()
                    if cat in OUTDOOR_CATEGORIES or (
                        "park" in s.name.lower() or "garden" in s.name.lower()
                    ):
                        if indoor_pool:
                            p = indoor_pool.pop(0)
                            used.add(p)
                            new_stops.append(
                                _poi_to_stop(
                                    p,
                                    reason=(
                                        f"Swapped indoor for outdoor '{s.name}' "
                                        "after voice edit."
                                    ),
                                )
                            )
                            notes.append(
                                f"Swapped {s.name} → {p.name} (indoor) on Day "
                                f"{target_day} {bname}."
                            )
                        else:
                            notes.append(
                                f"Removed outdoor stop {s.name} on Day "
                                f"{target_day} {bname} (no indoor substitute)."
                            )
                    else:
                        new_stops.append(s)
                if not new_stops and indoor_pool:
                    p = indoor_pool.pop(0)
                    new_stops.append(
                        _poi_to_stop(
                            p,
                            reason="Indoor stop added via voice edit.",
                        )
                    )
                new_day = _set_block(
                    new_day,
                    bname,
                    TimeBlock(time_of_day=bname, stops=new_stops, notes=block.notes),
                )

        elif op == "reduce_travel":
            # Keep morning/afternoon/evening membership; drop one stop tied to
            # the longest hop (or an extra stop when travel fields are missing).
            new_day, cut_notes = _reduce_day_travel(new_day)
            notes.extend(cut_notes)

        elif op == "remove_stop":
            name_q = str((patch.payload or {}).get("name") or "").strip()
            if not name_q:
                notes.append("No place name given to remove.")
            else:
                new_day, rm_notes = _remove_named_stop_day(
                    new_day,
                    name_query=name_q,
                    target_block=target_block,
                )
                notes.extend(rm_notes)

        elif op == "add_stop":
            category = str((patch.payload or {}).get("category") or "food").lower()
            wanted = _category_set(category)
            allow_repeat = _patch_allows_repeat(patch)
            used = _existing_places(itinerary, new_day)

            pool = [
                p
                for p in pois
                if (p.category or "").lower() in wanted
                and (allow_repeat or not used.contains(p))
            ]
            # Never substitute museums/heritage when the user asked for food/shopping.
            if (
                not pool
                and category in {"outdoor", "outdoors", "park", "nature"}
            ):
                pool = [
                    p
                    for p in pois
                    if (p.category or "").lower() in OUTDOOR_CATEGORIES
                    and (allow_repeat or not used.contains(p))
                ]
            bname = _pick_add_block(new_day, target_block)
            block = new_day.block(bname)
            if pool:
                utterance = (patch.user_utterance or "").lower()
                if category == "food" and re.search(
                    r"\b(famous|local food|well[- ]known)\b", utterance
                ):
                    generic = {"restaurant", "cafe", "food", "eatery"}

                    def _food_rank(p: POICandidate) -> tuple:
                        name = (p.name or "").strip()
                        return (
                            -(p.rank_score or 0.0),
                            1 if name.lower() in generic else 0,
                            0
                            if re.search(
                                r"(thali|lassi|spice|rajasthani|dhaba|"
                                r"chokhi|handi|sangeet|niros|lmb)",
                                name,
                                re.I,
                            )
                            else 1,
                            name.lower(),
                        )

                    pool = sorted(pool, key=_food_rank)
                p = pool[0]
                label = "outdoor" if category in {"outdoor", "outdoors"} else category
                stop = _poi_to_stop(
                    p,
                    reason=f"Added {label} place via voice edit.",
                    duration_min=60 if label == "food" else 75,
                )
                # "beginning/start" edits prepend; otherwise append.
                utterance = (patch.user_utterance or "").lower()
                at_start = bool(
                    target_block == "morning"
                    or re.search(r"\b(beginning|start|first)\b", utterance)
                )
                new_stops = (
                    [stop] + list(block.stops)
                    if at_start
                    else list(block.stops) + [stop]
                )
                new_day = _set_block(
                    new_day,
                    bname,
                    TimeBlock(time_of_day=bname, stops=new_stops, notes=block.notes),
                )
                notes.append(
                    f"Added {p.name} to Day {target_day} {bname}"
                    + (" (at the beginning)." if at_start else ".")
                )
            else:
                notes.append(
                    f"Could not add a {category} stop — live POI search found "
                    f"no unused {category} places right now. Please try again."
                )

        else:
            # swap_stop / replace_block — soft fallthrough to relax
            for bname, block in _get_block(new_day, target_block):
                new_stops = _relax_stops(
                    list(block.stops), keep=max(1, len(block.stops) - 1)
                )
                new_day = _set_block(
                    new_day,
                    bname,
                    TimeBlock(time_of_day=bname, stops=new_stops, notes=block.notes),
                )
            notes.append(
                f"Applied fallback scoped edit ({op}) on Day {target_day}."
            )

        days_out.append(new_day)

    if not touched:
        available = sorted({d.day_index for d in itinerary.days}) or [1]
        notes.append(
            f"Day {target_day} is not in this plan "
            f"(available: Day {', Day '.join(str(d) for d in available)}). "
            "Itinerary unchanged."
        )
        return itinerary.model_copy(deep=True), notes

    days_out.sort(key=lambda d: d.day_index)
    # Strip near-duplicates unless the user explicitly asked to repeat a place.
    if not _patch_allows_repeat(patch):
        days_out, dedupe_notes = dedupe_day_plans(days_out)
        notes.extend(dedupe_notes)

    # Pace-changing edits must update trip.pace so Synthesis restamps with
    # soft block anchors + relax/free notes (not continuous packed clocks).
    trip_update: dict[str, Any] = {}
    if itinerary.trip is not None:
        if op == "relax_block" and not target_block:
            trip_update["pace"] = "relaxed"
            trip_update["pace_known"] = True
        elif op == "balance_block":
            trip_update["pace"] = "moderate"
            trip_update["pace_known"] = True
        elif op == "pack_block" and not target_block:
            trip_update["pace"] = "packed"
            trip_update["pace_known"] = True

    new_trip = (
        itinerary.trip.model_copy(update=trip_update)
        if itinerary.trip is not None and trip_update
        else itinerary.trip
    )
    updated = itinerary.model_copy(
        deep=True,
        update={
            "trip": new_trip,
            "days": days_out,
            "summary": (
                f"Updated Day {target_day} ({op}); other days unchanged."
            ),
            "reasoning": list(itinerary.reasoning or []) + notes,
        },
    )
    logger.info(
        "EDIT applied op=%s day=%s block=%s pace=%s notes=%s",
        op,
        target_day,
        target_block,
        getattr(new_trip, "pace", None),
        notes,
    )
    return updated, notes
