"""POI shortlist MCP — quota + quality + must-see selection before travel/pack."""

from __future__ import annotations

import logging
import os
import re
from collections import defaultdict

from agent.mcp.itinerary_builder import STOPS_PER_DAY
from agent.place_identity import dedupe_pois
from agent.preferences import (
    categories_for_interest,
    interest_match_score,
    normalize_interest,
)
from agent.schemas.itinerary import Pace
from agent.schemas.specialists import POICandidate, POISearchResult
from agent.trip_limits import clamp_trip_days

logger = logging.getLogger(__name__)

# Soft must-see priors for Jaipur (boost when present in pool).
# Higher scores pin icons ahead of chains / low-signal Nominatim noise.
MUST_SEE_PATTERNS: tuple[tuple[str, float], ...] = (
    (r"\bhawa\s*mahal\b", 40.0),
    (r"\bcity\s*palace\b", 38.0),
    (r"\b(?:amber|amer)\s*(?:fort|palace)\b", 38.0),
    (r"\bjantar\s*mantar\b", 36.0),
    (r"\bnahargarh(?:\s+fort)?\b", 32.0),
    (r"\bjaigarh(?:\s+fort)?\b", 30.0),
    (r"\balbert\s*hall\b", 28.0),
    (r"\bjal\s*mahal\b", 26.0),
    (r"\bgovind\s*dev|\bbirla\s*mandir\b|\bgalta\s*ji\b|\bgaltaji\b", 28.0),
    (r"\brawat\s*misthan\b|\blaxmi\s*misthan\b", 24.0),
    (r"\banokhi\b", 22.0),
    (r"\bjohari\b", 22.0),
    (r"\bbapu\s*bazaar\b", 22.0),
    (r"\bchokhi\s*dhani\b|\btapri\s+central\b", 18.0),
    (r"\bram\s*niwas\b|\bsisodia\b|\bvidyadhar\b|\bkanak\s*vrindavan\b", 16.0),
)

# Demote chains / low-signal Nominatim noise unless shopping/food pool is thin.
LOW_QUALITY_PATTERNS: tuple[str, ...] = (
    r"cafe\s*coffee\s*day",
    r"\bccd\b",
    r"domino'?s?",
    r"mcdonald",
    r"\bkfc\b",
    r"subway",
    r"big\s*baz+a+r",
    r"pizza\s*hut",
    r"starbucks",
    r"गुलाबी\s*नगरी",
    r"pink\s*city",
    r"rooftop\s*view\s*metal",
    r"\bcricket\b",
    r"\bapartment",
    r"sector[-\s]?\d",
    r"^ground$",
    r"\bpark\s*[-#]?\s*\d+\b",
    r"^deer\s+park$",
    r"\bbike\s*park\b",
    r"\bchitrakoot\b",
    r"\bcollege\b",
    r"\bschool\b",
    r"state\s+bank",
    r"\bbank\s+of\b",
    r"\bsbi\b",
    r"\bjewels?\b",
    r"\bshowroom\b",
    r"\bcanteen\b",
    r"\bmess\b",
)

# Famous sights that belong to other cities — never shortlist for Jaipur trips.
OUT_OF_CITY_PATTERNS: tuple[str, ...] = (
    r"\blake\s*palace\b",  # Udaipur
    r"\bcity\s*palace\s*,?\s*udaipur\b",
    r"\btaj\s*mahal\b",
    r"\bred\s*fort\b",
    r"\bgateway\s*of\s*india\b",
    r"\bindia\s*gate\b",
    r"\bqutub\s*minar\b",
    r"\bmeenakshi\b",
    r"\bcharminar\b",
    r"\bhowrah\s*bridge\b",
)


def active_itinerary_strategy() -> str:
    """Return ``legacy`` (default) or ``hybrid`` from env."""
    raw = (os.getenv("ITINERARY_STRATEGY") or "legacy").strip().lower()
    if raw in {"hybrid", "b", "shortlist"}:
        return "hybrid"
    return "legacy"


def shortlist_target_size(*, num_days: int, pace: Pace) -> int:
    """Shortlist larger than final stops so each day can fill morning/afternoon/evening.

    Sized for hard pace floors (incl. evening food) + must-see pins + junk margin,
    without needing a second Overpass fetch.
    """
    num_days = clamp_trip_days(num_days)
    # Seed per day above STOPS_PER_DAY so Day 2–3 still have fillers.
    per_day = {"relaxed": 10, "moderate": 16, "packed": 22}.get(pace, 12)
    panel_floor = num_days * 4  # AM/PM/E + spare
    final = max(panel_floor, num_days * max(STOPS_PER_DAY.get(pace, 4), per_day))
    # Slack: meal swaps, near-dupe drops, evening food reserve, densify.
    return max(final + 20, panel_floor + 16, 28)


def _poi_key(p: POICandidate) -> str:
    return f"{p.osm_type}/{p.osm_id}"


def _is_out_of_city_poi(p: POICandidate, city: str) -> bool:
    """Drop famous landmarks that belong to another city (e.g. Lake Palace)."""
    if (city or "").strip().lower() not in {"jaipur", ""}:
        return False
    low = (p.name or "").lower()
    return any(re.search(pat, low, flags=re.I) for pat in OUT_OF_CITY_PATTERNS)


def _matches_low_quality_name(name: str) -> bool:
    low = (name or "").lower()
    return any(re.search(pat, low, flags=re.I) for pat in LOW_QUALITY_PATTERNS)


def _quality_adjustment(p: POICandidate) -> float:
    name = (p.name or "").strip()
    low = name.lower()
    adj = 0.0
    if len(name) < 3:
        adj -= 25.0
    if re.fullmatch(r"[a-z]", low):
        adj -= 30.0
    for pat in LOW_QUALITY_PATTERNS:
        if re.search(pat, low, flags=re.I):
            adj -= 28.0
            break
    tags = p.tags or {}
    if tags.get("wikidata") or tags.get("wikipedia"):
        adj += 8.0
    if p.lat is not None and p.lon is not None:
        adj += 1.5
    return adj


def _must_see_boost(p: POICandidate) -> float:
    low = (p.name or "").lower()
    boost = 0.0
    for pat, score in MUST_SEE_PATTERNS:
        if re.search(pat, low, flags=re.I):
            boost = max(boost, score)
    return boost


def is_must_see_poi(p: POICandidate) -> bool:
    """True when the place matches a curated Jaipur icon prior."""
    return _must_see_boost(p) >= 16.0


def selection_score(p: POICandidate, interests: list[str]) -> float:
    base = float(p.rank_score or 0.0)
    must = _must_see_boost(p)
    # Must-sees dominate chains / generic cafes even when base OSM score is flat.
    return (
        base
        + interest_match_score(p.category, interests)
        + must
        + (12.0 if must >= 28.0 else 0.0)
        + _quality_adjustment(p)
    )


def _quota_slots(shortlist_size: int, interest_keys: list[str]) -> dict[str, int]:
    if not interest_keys:
        return {}
    from agent.preferences import (
        CULTURE_TIER_INTERESTS,
        SOFT_TIER_INTERESTS,
        culture_soft_mix_active,
        order_interests_by_priority,
    )

    keys = order_interests_by_priority(interest_keys)
    culture = [k for k in keys if k in CULTURE_TIER_INTERESTS]
    soft = [k for k in keys if k in SOFT_TIER_INTERESTS]
    rest = [
        k
        for k in keys
        if k not in CULTURE_TIER_INTERESTS and k not in SOFT_TIER_INTERESTS
    ]

    # Mixed culture+soft trips: ~3× weight for heritage/temple/museum slots.
    if culture_soft_mix_active(keys) and culture and (soft or rest):
        weights = {
            k: (3.0 if k in CULTURE_TIER_INTERESTS else 1.0) for k in keys
        }
        total_w = sum(weights.values()) or 1.0
        quotas = {
            k: max(1, int(shortlist_size * weights[k] / total_w)) for k in keys
        }
        while sum(quotas.values()) > shortlist_size:
            trim_order = soft + rest + culture
            trimmed = False
            for k in reversed(trim_order):
                if quotas.get(k, 0) > 1:
                    quotas[k] -= 1
                    trimmed = True
                    break
            if not trimmed:
                break
        while sum(quotas.values()) < shortlist_size:
            grow = culture[0] if culture else keys[0]
            quotas[grow] = quotas.get(grow, 0) + 1
        return quotas

    n = len(keys)
    base, rem = divmod(shortlist_size, n)
    quotas = {k: base for k in keys}
    prefer = culture or rest or soft or keys
    for i in range(rem):
        quotas[prefer[i % len(prefer)]] = quotas.get(prefer[i % len(prefer)], 0) + 1
    return quotas


def shortlist_pois(
    *,
    city: str,
    candidate_pois: list[POICandidate],
    interests: list[str] | None,
    num_days: int = 2,
    pace: Pace = "relaxed",
    target_size: int | None = None,
) -> POISearchResult:
    """Select a travel-ready shortlist with interest quotas + quality + must-sees.

    Capstone hybrid strategy (light B): discover with ``poi_search``, shortlist
    here, estimate travel on this set, then pack days from it.

    Must-sees are pinned first. Food (when requested) reserves at least one
    dinner-capable slot per day so evening floors can fill without a refetch.
    """
    interests_list = [i for i in (interests or []) if str(i).strip()]
    keys = list(
        dict.fromkeys(
            normalize_interest(i) or i.strip().lower() for i in interests_list if i.strip()
        )
    )
    days_n = clamp_trip_days(num_days)
    size = target_size or shortlist_target_size(num_days=days_n, pace=pace)
    notes: list[str] = [
        f"Shortlist strategy=hybrid target={size} pace={pace} days={days_n}."
    ]

    if not candidate_pois:
        return POISearchResult(
            city=city,
            query_interests=interests_list,
            pois=[],
            missing_data=True,
            notes="; ".join(notes + ["Empty POI pool — nothing to shortlist."]),
        )

    from agent.mcp.poi_search import _is_low_signal_poi

    ranked = dedupe_pois(list(candidate_pois))
    filtered: list[POICandidate] = []
    dropped_low = 0
    dropped_oos = 0
    for p in ranked:
        if _is_out_of_city_poi(p, city):
            dropped_oos += 1
            continue
        if _matches_low_quality_name(p.name or "") and not is_must_see_poi(p):
            dropped_low += 1
            continue
        if _is_low_signal_poi(
            p.name or "", p.tags or {}, (p.category or "").lower()
        ):
            dropped_low += 1
            continue
        filtered.append(p)
    if dropped_oos:
        notes.append(f"Dropped {dropped_oos} out-of-city landmarks (e.g. Lake Palace).")
    if dropped_low:
        notes.append(f"Dropped {dropped_low} low-signal POIs before quotas.")
    scored = sorted(
        filtered,
        key=lambda p: (-selection_score(p, interests_list), p.name or ""),
    )

    buckets: dict[str, list[POICandidate]] = defaultdict(list)
    other: list[POICandidate] = []
    for p in scored:
        cat = (p.category or "").lower()
        matched: str | None = None
        for key in keys:
            if cat in categories_for_interest(key):
                matched = key
                break
        if matched:
            buckets[matched].append(p)
        else:
            other.append(p)

    picks: list[POICandidate] = []
    used: set[str] = set()
    quotas = _quota_slots(size, keys) if keys else {}

    def _try_add(p: POICandidate, *, min_score: float = -8.0) -> bool:
        if len(picks) >= size:
            return False
        ref = _poi_key(p)
        if ref in used:
            return False
        if selection_score(p, interests_list) < min_score and not is_must_see_poi(p):
            return False
        picks.append(p)
        used.add(ref)
        return True

    # 1) Pin must-sees first so quotas cannot crowd out Amber / Hawa Mahal / etc.
    must_pinned = 0
    for p in scored:
        if not is_must_see_poi(p):
            continue
        if _try_add(p, min_score=-40.0):
            must_pinned += 1
    if must_pinned:
        notes.append(f"Pinned {must_pinned} must-see POIs before interest quotas.")

    # 2) Interest quotas (culture weighted heavier when mixed with soft).
    for key, quota in quotas.items():
        need = quota
        for p in buckets.get(key, []):
            if need <= 0 or len(picks) >= size:
                break
            if _try_add(p):
                need -= 1
        if need > 0:
            notes.append(
                f"Quota shortfall for interest={key}: needed {quota}, filled {quota - need}."
            )

    # 3) Evening / meal reserve: ≥1 food POI per day when food is an interest.
    if "food" in keys:
        food_cats = categories_for_interest("food")
        foods_in = [
            p
            for p in picks
            if (p.category or "").lower() in food_cats
        ]
        food_need = max(days_n, quotas.get("food", 0))
        if len(foods_in) < food_need:
            for p in buckets.get("food", []) + [
                x for x in scored if (x.category or "").lower() in food_cats
            ]:
                if len([x for x in picks if (x.category or "").lower() in food_cats]) >= food_need:
                    break
                # Prefer known food icons over random cafes.
                if selection_score(p, interests_list) < -5 and not is_must_see_poi(p):
                    continue
                _try_add(p)
            notes.append(
                f"Food reserve for evenings/breakfasts: want≥{food_need}, "
                f"have={sum(1 for x in picks if (x.category or '').lower() in food_cats)}."
            )

    pinned_names = [p.name for p in picks if is_must_see_poi(p)]
    if pinned_names:
        notes.append("Must-see priors present in shortlist: " + ", ".join(pinned_names[:12]))

    # 4) Fill remaining slots by score (still skip junk).
    for p in scored:
        if len(picks) >= size:
            break
        _try_add(p)

    # Final order: must-sees first, then selection score (travel agent reads prefix).
    picks = sorted(
        picks[:size],
        key=lambda p: (
            0 if is_must_see_poi(p) else 1,
            -selection_score(p, interests_list),
            p.name or "",
        ),
    )

    cat_counts: dict[str, int] = defaultdict(int)
    for p in picks:
        cat_counts[(p.category or "other").lower()] += 1
    notes.append(
        "Quota mix after shortlist: "
        + ", ".join(f"{k}={v}" for k, v in sorted(cat_counts.items()))
    )
    notes.append(f"Shortlisted {len(picks)}/{len(scored)} from live POI pool.")

    logger.info(
        "poi_shortlist city=%s in=%d out=%d interests=%s",
        city,
        len(scored),
        len(picks),
        interests_list,
    )
    return POISearchResult(
        city=city,
        query_interests=interests_list,
        pois=picks,
        missing_data=len(picks) < 2,
        notes="; ".join(notes),
    )
