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
MUST_SEE_PATTERNS: tuple[tuple[str, float], ...] = (
    (r"\bhawa\s*mahal\b", 22.0),
    (r"\bcity\s*palace\b", 20.0),
    (r"\b(?:amber|amer)\s*(?:fort|palace)\b", 20.0),
    (r"\bjantar\s*mantar\b", 18.0),
    (r"\balbert\s*hall\b", 16.0),
    (r"\bgovind\s*dev|\bbirla\s*mandir\b|\bgalta\s*ji\b", 16.0),
    (r"\banokhi\b", 14.0),
    (r"\bjohari\b", 14.0),
    (r"\bbapu\s*bazaar\b", 14.0),
    (r"\bnahargarh\b", 12.0),
    (r"\bjal\s*mahal\b", 10.0),
    (r"\bram\s*niwas\b|\bsisodia\b|\bvidyadhar\b", 10.0),
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


def active_itinerary_strategy() -> str:
    """Return ``legacy`` (default) or ``hybrid`` from env."""
    raw = (os.getenv("ITINERARY_STRATEGY") or "legacy").strip().lower()
    if raw in {"hybrid", "b", "shortlist"}:
        return "hybrid"
    return "legacy"


def shortlist_target_size(*, num_days: int, pace: Pace) -> int:
    """Shortlist a bit larger than final stop count so travel/pack have slack."""
    num_days = clamp_trip_days(num_days)
    final = num_days * STOPS_PER_DAY.get(pace, 4)
    return max(final + 4, 10, min(16, final + 6))


def _poi_key(p: POICandidate) -> str:
    return f"{p.osm_type}/{p.osm_id}"


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
            adj -= 18.0
            break
    tags = p.tags or {}
    if tags.get("wikidata") or tags.get("wikipedia"):
        adj += 6.0
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


def selection_score(p: POICandidate, interests: list[str]) -> float:
    base = float(p.rank_score or 0.0)
    return (
        base
        + interest_match_score(p.category, interests)
        + _must_see_boost(p)
        + _quality_adjustment(p)
    )


def _quota_slots(shortlist_size: int, interest_keys: list[str]) -> dict[str, int]:
    if not interest_keys:
        return {}
    from agent.preferences import CULTURE_TIER_INTERESTS, order_interests_by_priority

    keys = order_interests_by_priority(interest_keys)
    n = len(keys)
    base, rem = divmod(shortlist_size, n)
    # Prefer giving remainder to culture-tier / earlier (priority) interests.
    quotas = {k: base for k in keys}
    culture = [k for k in keys if k in CULTURE_TIER_INTERESTS]
    rest = [k for k in keys if k not in CULTURE_TIER_INTERESTS]
    for i in range(rem):
        target = culture[i % len(culture)] if culture else rest[i % len(rest)]
        quotas[target] = quotas.get(target, 0) + 1
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
    """
    interests_list = [i for i in (interests or []) if str(i).strip()]
    keys = list(
        dict.fromkeys(
            normalize_interest(i) or i.strip().lower() for i in interests_list if i.strip()
        )
    )
    size = target_size or shortlist_target_size(num_days=num_days, pace=pace)
    notes: list[str] = [
        f"Shortlist strategy=hybrid target={size} pace={pace} days={clamp_trip_days(num_days)}."
    ]

    if not candidate_pois:
        return POISearchResult(
            city=city,
            query_interests=interests_list,
            pois=[],
            missing_data=True,
            notes="; ".join(notes + ["Empty POI pool — nothing to shortlist."]),
        )

    ranked = dedupe_pois(list(candidate_pois))
    scored = sorted(
        ranked,
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

    # Must-sees are score-boosted in ``selection_score``; quotas stay hard so
    # shopping/food are not crowded out by too many heritage icons.
    for key, quota in quotas.items():
        need = quota
        for p in buckets.get(key, []):
            if need <= 0 or len(picks) >= size:
                break
            ref = _poi_key(p)
            if ref in used:
                continue
            if selection_score(p, interests_list) < -5:
                continue
            picks.append(p)
            used.add(ref)
            need -= 1
        if need > 0:
            notes.append(f"Quota shortfall for interest={key}: needed {quota}, filled {quota - need}.")

    pinned = [p.name for p in picks if _must_see_boost(p) >= 12.0]
    if pinned:
        notes.append("Must-see priors present in shortlist: " + ", ".join(pinned))

    for p in scored:
        if len(picks) >= size:
            break
        ref = _poi_key(p)
        if ref in used:
            continue
        if selection_score(p, interests_list) < -10:
            continue
        picks.append(p)
        used.add(ref)

    # Final order: selection score (travel agent reads prefix of shortlist).
    picks = sorted(
        picks[:size],
        key=lambda p: (-selection_score(p, interests_list), p.name or ""),
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
