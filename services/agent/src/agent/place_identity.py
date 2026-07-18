"""Place identity helpers — block near-duplicate stops unless the user asks to repeat."""

from __future__ import annotations

import re
from typing import Any, Protocol

# Trailing place-type tokens stripped before comparing cores.
_TYPE_TOKENS = frozenset(
    {
        "museum",
        "museums",
        "palace",
        "fort",
        "temple",
        "mandir",
        "park",
        "garden",
        "gardens",
        "gallery",
        "galleries",
        "market",
        "bazaar",
        "bazar",
        "complex",
        "memorial",
        "monument",
        "zoo",
        "observatory",
        "centre",
        "center",
        "hall",  # only when trailing AND another core token remains
    }
)

# Never strip these as a sole remaining token via "hall" alone — handled below.
_LEAD_TOKENS = frozenset({"the", "sri", "shri", "sir"})
# Don't strip a type token if that would leave only a generic core.
_GENERIC_CORES = frozenset(
    {"city", "pink", "art", "royal", "national", "state", "old", "new", "india"}
)

# Spelling variants that mean the same place (Amer Fort ↔ Amber Fort).
_TOKEN_ALIASES: dict[str, str] = {
    "amber": "amer",
    "amer": "amer",
    "jaighar": "jaigarh",
    "nahargar": "nahargarh",
}

_REPEAT_RE = re.compile(
    r"\b("
    r"again|once more|one more time|return to|go back to|"
    r"visit .+ again|see .+ again|repeat|twice|second time"
    r")\b",
    re.I,
)


class _NamedPlace(Protocol):
    name: str | None
    osm_type: str | None
    osm_id: int | None


def normalize_place_name(name: str | None) -> str:
    text = (name or "").lower().strip()
    text = text.replace("&", " and ")
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def core_place_name(name: str | None) -> str:
    """Stable core for near-duplicate matching (Albert Hall Museum → albert hall)."""
    tokens = normalize_place_name(name).split()
    while tokens and tokens[0] in _LEAD_TOKENS:
        tokens.pop(0)
    # Strip trailing type words, but keep "hall" when it's the distinctive noun
    # of a two-token name like "albert hall", and don't reduce "City Palace"
    # to the bare generic "city".
    while len(tokens) >= 2 and tokens[-1] in _TYPE_TOKENS:
        if tokens[-1] == "hall" and len(tokens) == 2:
            break
        candidate = tokens[:-1]
        if len(candidate) == 1 and candidate[0] in _GENERIC_CORES:
            break
        tokens.pop()
    tokens = [_TOKEN_ALIASES.get(t, t) for t in tokens]
    return " ".join(tokens)


def osm_ref(place: Any) -> str | None:
    osm_type = getattr(place, "osm_type", None)
    osm_id = getattr(place, "osm_id", None)
    if osm_type is None or osm_id is None:
        return None
    return f"{osm_type}/{osm_id}"


def places_are_same(a: Any, b: Any) -> bool:
    """True when two POIs/stops are the same place (OSM id or near-duplicate name)."""
    ra, rb = osm_ref(a), osm_ref(b)
    if ra and rb and ra == rb:
        return True
    ca = core_place_name(getattr(a, "name", None))
    cb = core_place_name(getattr(b, "name", None))
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    # Longer name is shorter + only type tokens (already core-equal above covers
    # Albert Hall / Albert Hall Museum). Also catch raw containment of cores.
    shorter, longer = (ca, cb) if len(ca) <= len(cb) else (cb, ca)
    if len(shorter) >= 8 and longer.startswith(shorter + " "):
        rest = longer[len(shorter) :].split()
        if rest and all(t in _TYPE_TOKENS for t in rest):
            return True
    return False


class PlaceSeen:
    """Tracks places already on an itinerary for uniqueness checks."""

    def __init__(self) -> None:
        self.refs: set[str] = set()
        self.cores: set[str] = set()

    @classmethod
    def from_places(cls, places: list[Any]) -> PlaceSeen:
        seen = cls()
        for p in places:
            seen.add(p)
        return seen

    def add(self, place: Any) -> None:
        ref = osm_ref(place)
        if ref:
            self.refs.add(ref)
        core = core_place_name(getattr(place, "name", None))
        if core:
            self.cores.add(core)

    def contains(self, place: Any) -> bool:
        ref = osm_ref(place)
        if ref and ref in self.refs:
            return True
        core = core_place_name(getattr(place, "name", None))
        if not core:
            return False
        if core in self.cores:
            return True
        for existing in self.cores:
            shorter, longer = (
                (core, existing) if len(core) <= len(existing) else (existing, core)
            )
            if len(shorter) >= 8 and longer.startswith(shorter + " "):
                rest = longer[len(shorter) :].split()
                if rest and all(t in _TYPE_TOKENS for t in rest):
                    return True
        return False


def allow_repeat_requested(utterance: str | None) -> bool:
    """User explicitly asked to visit a place again."""
    if not utterance:
        return False
    return bool(_REPEAT_RE.search(utterance))


def dedupe_pois(pois: list[Any]) -> list[Any]:
    """Keep first occurrence of each distinct place (OSM id or near-duplicate name)."""
    out: list[Any] = []
    seen = PlaceSeen()
    for p in pois:
        if seen.contains(p):
            continue
        seen.add(p)
        out.append(p)
    return out


def dedupe_day_plans(days: list[Any]) -> tuple[list[Any], list[str]]:
    """Drop near-duplicate stops across the whole itinerary (first wins)."""
    from agent.schemas.itinerary import DayPlan, TimeBlock

    seen = PlaceSeen()
    notes: list[str] = []
    dropped: list[str] = []
    new_days: list[DayPlan] = []
    for day in days:
        d = day.model_copy(deep=True)
        for bn in ("morning", "afternoon", "evening"):
            block = getattr(d, bn)
            kept: list = []
            for s in block.stops:
                if seen.contains(s):
                    dropped.append(s.name or "unknown")
                    continue
                seen.add(s)
                kept.append(s)
            setattr(
                d,
                bn,
                TimeBlock(time_of_day=bn, stops=kept, notes=block.notes),
            )
        new_days.append(d)
    if dropped:
        uniq = list(dict.fromkeys(dropped))
        notes.append(
            "Removed duplicate place(s): "
            + ", ".join(uniq[:6])
            + ("…" if len(uniq) > 6 else "")
            + "."
        )
    return new_days, notes
