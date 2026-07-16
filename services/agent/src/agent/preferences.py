"""Traveler profiles and interest parsing for trip personalization."""

from __future__ import annotations

import re
from typing import Any, Literal

from agent.schemas.itinerary import Pace

TravelerProfile = Literal[
    "kid_friendly",
    "senior_friendly",
    "couple_friendly",
    "friends_friendly",
    "solo",
    "general",
]

PROFILE_LABELS: dict[str, str] = {
    "kid_friendly": "kid-friendly / family",
    "senior_friendly": "senior-friendly",
    "couple_friendly": "couple-friendly",
    "friends_friendly": "friends / group",
    "solo": "solo",
    "general": "general",
}

# Canonical interest tokens used across POI search + itinerary builder.
INTEREST_ALIASES: dict[str, str] = {
    "museums": "museum",
    "musuem": "museum",
    "musuems": "museum",
    "temples": "temple",
    "mandir": "temple",
    "mandirs": "temple",
    "gurudwara": "temple",
    "gurdwara": "temple",
    "mosque": "temple",
    "church": "temple",
    "holy": "temple",
    "spiritual": "temple",
    "pilgrimage": "temple",
    "religious": "temple",
    "parks": "park",
    "garden": "nature",
    "gardens": "nature",
    "zoo": "park",
    "aquarium": "museum",
    "street food": "food",
    "nightlife": "nightlife",
    "night life": "nightlife",
    "night-life": "nightlife",
    "historic": "heritage",
    "historical": "heritage",
    "history": "heritage",
    "fort": "heritage",
    "palace": "heritage",
    "monuments": "heritage",
    "monument": "heritage",
}

INTEREST_CATALOG = [
    "food",
    "heritage",
    "culture",
    "history",
    "temple",
    "museum",
    "market",
    "nature",
    "park",
    "nightlife",
    "shopping",
    "adventure",
    "art",
    "architecture",
]

# Interests with reliable live Overpass coverage in Jaipur — use in user prompts.
PRIMARY_INTERESTS: list[str] = [
    "heritage",
    "temple",
    "food",
    "shopping",
    "museum",
    "park",
]

# Friendlier labels for clarify / confirm copy (values stay canonical tokens).
PRIMARY_INTEREST_LABELS: dict[str, str] = {
    "heritage": "heritage (forts & palaces)",
    "temple": "temples",
    "food": "food",
    "shopping": "shopping & bazaars",
    "museum": "museums",
    "park": "parks",
    "nature": "nature",
    "culture": "culture",
    "market": "markets",
    "nightlife": "nightlife",
    "adventure": "adventure",
    "art": "art",
}

# Category aliases used when ranking POIs against stated interests.
INTEREST_CATEGORY_MAP: dict[str, set[str]] = {
    "food": {"food"},
    "heritage": {"heritage", "attraction"},
    "culture": {"heritage", "museum", "temple", "attraction", "art"},
    "history": {"heritage", "museum"},
    "temple": {"temple"},
    "museum": {"museum"},
    "market": {"market", "shopping"},
    "shopping": {"shopping", "market"},
    "park": {"park", "viewpoint"},
    "nature": {"park", "viewpoint", "nature"},
    "nightlife": {"nightlife"},
    "adventure": {"adventure", "attraction", "viewpoint"},
    "art": {"art", "museum"},
    "architecture": {"heritage", "attraction"},
}

# Profile → default interests / pace / soft constraints / POI category bias
PROFILE_PRESETS: dict[str, dict[str, Any]] = {
    "kid_friendly": {
        "interests": ["park", "museum", "nature", "culture", "food"],
        "pace": "relaxed",
        "daily_time_window_min": 480,
        "constraints": [
            "kid_friendly",
            "Prefer parks, gardens, museums, and family attractions",
            "Avoid nightlife, bars, pubs, and nightclubs",
            "Keep days shorter with fewer stops",
        ],
        "boost_categories": {"park", "museum", "attraction", "food", "market", "viewpoint"},
        "avoid_categories": {"nightlife"},
    },
    "senior_friendly": {
        "interests": ["temple", "museum", "heritage", "culture", "park"],
        "pace": "relaxed",
        "daily_time_window_min": 420,
        "constraints": [
            "senior_friendly",
            "Prefer temples, museums, heritage sites, and gentle walks",
            "Avoid nightlife, packed hiking, and adventure sports",
            "Fewer stops with more rest time between places",
        ],
        "boost_categories": {"temple", "museum", "heritage", "park", "viewpoint"},
        "avoid_categories": {"nightlife"},
    },
    "couple_friendly": {
        "interests": ["heritage", "culture", "food", "nature", "art"],
        "pace": "moderate",
        "daily_time_window_min": 540,
        "constraints": [
            "couple_friendly",
            "Prefer scenic heritage, cafes, viewpoints, and calmer evenings",
            "Avoid rowdy nightlife unless explicitly requested",
        ],
        "boost_categories": {"heritage", "viewpoint", "food", "museum", "park", "art"},
        "avoid_categories": set(),
    },
    "friends_friendly": {
        "interests": ["food", "nightlife", "market", "shopping", "culture", "adventure"],
        "pace": "packed",
        "daily_time_window_min": 600,
        "constraints": [
            "friends_friendly",
            "Prefer lively food, markets, shared activities, and nightlife",
        ],
        "boost_categories": {"food", "nightlife", "market", "shopping", "attraction"},
        "avoid_categories": set(),
    },
    "solo": {
        "interests": ["culture", "heritage", "food", "museum"],
        "pace": "moderate",
        "daily_time_window_min": 540,
        "constraints": [
            "solo",
            "Prefer flexible daytime sightseeing with cafe and museum stops",
        ],
        "boost_categories": {"museum", "heritage", "food", "culture", "park"},
        "avoid_categories": set(),
    },
    "general": {
        "interests": ["culture", "food"],
        "pace": "relaxed",
        "daily_time_window_min": 540,
        "constraints": [],
        "boost_categories": set(),
        "avoid_categories": set(),
    },
}


def normalize_interest(token: str) -> str:
    t = token.strip().lower()
    if not t:
        return ""
    return INTEREST_ALIASES.get(t, t)


def normalize_interests(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        n = normalize_interest(raw)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def interest_match_score(category: str | None, interests: list[str]) -> float:
    """How strongly a POI category matches the traveler's stated interests."""
    cat = (category or "").lower().strip()
    if not cat or not interests:
        return 0.0
    score = 0.0
    for interest in interests:
        key = normalize_interest(interest)
        if not key:
            continue
        if cat == key:
            score += 10.0
            continue
        related = INTEREST_CATEGORY_MAP.get(key, set())
        if cat in related:
            score += 6.0
    return score


def categories_for_interests(interests: list[str]) -> set[str]:
    cats: set[str] = set()
    for interest in interests:
        key = normalize_interest(interest)
        if not key:
            continue
        cats.add(key)
        cats |= INTEREST_CATEGORY_MAP.get(key, set())
    return cats


def categories_for_interest(interest: str) -> set[str]:
    """POI categories that satisfy a single stated interest."""
    key = normalize_interest(interest)
    if not key:
        return set()
    return {key} | INTEREST_CATEGORY_MAP.get(key, set())


def primary_interests_prompt() -> str:
    return ", ".join(
        PRIMARY_INTEREST_LABELS.get(i, i) for i in PRIMARY_INTERESTS
    )


def detect_traveler_profile(message: str) -> TravelerProfile | None:
    """Return the strongest matching traveler profile, or None if unspecified."""
    lower = message.lower()
    if clear_traveler_profile_request(message):
        return None

    kid = re.search(
        r"\b(kid[-\s]?friendly|kids?|children|child|toddler|family\s+with\s+"
        r"kids|with\s+kids|baby|babies)\b",
        lower,
    )
    senior = re.search(
        r"\b(senior[-\s]?citizen|senior[-\s]?friendly|seniors?|elderly|"
        r"older\s+adults?|retired|retiree)\b",
        lower,
    )
    couple = re.search(
        r"\b(couple[-\s]?friendly|couples?|honeymoon|romantic|anniversary|"
        r"with\s+my\s+(husband|wife|partner|spouse)|date\s+night)\b",
        lower,
    )
    friends = re.search(
        r"\b(friends?[-\s]?friendly|with\s+friends|group\s+of\s+friends|"
        r"buddies|buddy\s+trip|bachelor|bachelorette|girls?\s+trip|"
        r"boys?\s+trip)\b",
        lower,
    )
    solo = re.search(r"\b(solo|alone|by\s+myself|travelling\s+alone|traveling\s+alone)\b", lower)

    # Prefer the most specific audience cue when several appear.
    if kid:
        return "kid_friendly"
    if senior:
        return "senior_friendly"
    if couple:
        return "couple_friendly"
    if friends:
        return "friends_friendly"
    if solo:
        return "solo"
    return None


def clear_traveler_profile_request(message: str) -> bool:
    """True when the user asks to drop an audience profile (e.g. couple-friendly)."""
    lower = message.lower().strip()
    if re.search(
        r"\b(remove|drop|clear|cancel|without|exclude)\b.{0,40}\b("
        r"couple[-\s]?friendly|couple|romantic|kid[-\s]?friendly|kids?|"
        r"senior[-\s]?friendly|seniors?|friends?[-\s]?friendly|friends|"
        r"solo|profile|audience"
        r")\b",
        lower,
    ):
        return True
    if re.search(
        r"\b(not|no|don'?t want|do not want)\b.{0,20}\b("
        r"couple[-\s]?friendly|couple|romantic|kid[-\s]?friendly|"
        r"senior[-\s]?friendly|friends?[-\s]?friendly|solo"
        r")\b",
        lower,
    ):
        return True
    return False


def is_off_scope_trip_brief(message: str) -> bool:
    """True for multi-country/Europe-style briefs that aren't a Jaipur slot answer."""
    lower = message.lower()
    foreign = re.search(
        r"\b(europe|italy|france|spain|paris|rome|barcelona|venice|florence|"
        r"london|greece|portugal|switzerland|germany|two weeks|2 weeks|"
        r"fortnight)\b",
        lower,
    )
    if not foreign:
        return False
    if "jaipur" in lower or "rajasthan" in lower:
        return False
    # Short interest lists shouldn't trigger even if they somehow mention france cheese…
    if len(message.strip()) < 80 and not re.search(
        r"\b(europe|italy|france|spain|two weeks|2 weeks)\b", lower
    ):
        return False
    return True


def extract_interests(message: str) -> list[str]:
    """Parse explicit interest keywords (including typos / holy-place phrasing)."""
    lower = message.lower()
    found: list[str] = []

    for term in INTEREST_CATALOG:
        if term in lower:
            found.append(normalize_interest(term))

    # Multi-word / alias phrases not covered by simple substring catalog matches.
    alias_patterns: list[tuple[str, str]] = [
        (r"\bmusu?e?ums?\b", "museum"),
        (r"\btemples?\b", "temple"),
        (r"\bmandirs?\b", "temple"),
        (r"\b(holy\s+places?|places?\s+of\s+worship|pilgrimage|spiritual)\b", "temple"),
        (r"\b(street\s+foods?)\b", "food"),
        (r"\b(night[\s-]?life)\b", "nightlife"),
        (r"\b(forts?|palaces?|monuments?)\b", "heritage"),
        (r"\b(zoos?|aquariums?)\b", "park"),
        (r"\b(gardens?)\b", "nature"),
    ]
    for pat, interest in alias_patterns:
        if re.search(pat, lower):
            found.append(interest)

    return normalize_interests(found)


_REMOVE_CLAUSE = re.compile(
    r"^(?:remove|drop|exclude|without|no more|don'?t want|do not want)\b",
    re.I,
)
_ADD_CLAUSE = re.compile(
    r"^(?:add|include|also(?:\s+add)?|plus)\b",
    re.I,
)
_REPLACE_INTEREST = re.compile(
    r"\b(?:replace|swap|change)\s+(.+?)\s+(?:with|to|for)\s+(.+)$",
    re.I,
)


def parse_interest_updates(
    message: str,
    *,
    existing_interests: list[str] | None = None,
) -> tuple[list[str], bool]:
    """
    Apply absolute interest lists or add/remove deltas.

    Examples:
      "Temples museum shopping" → replace with [temple, museum, shopping]
      "Remove shopping and add food" (existing temple, museum, shopping)
        → [temple, museum, food]
    """
    text = (message or "").strip()
    if not text:
        return normalize_interests(list(existing_interests or [])), False

    lower = text.lower()
    existing = normalize_interests(list(existing_interests or []))

    # "replace shopping with food" / "change shopping to food"
    replace_m = _REPLACE_INTEREST.search(lower)
    if replace_m:
        remove = extract_interests(replace_m.group(1))
        add = extract_interests(replace_m.group(2))
        out = [i for i in existing if i not in set(remove)]
        for a in add:
            if a not in out:
                out.append(a)
        return out, True

    clauses = re.split(
        r"\s+(?:and then|then|,?\s+and)\s+",
        lower,
        flags=re.IGNORECASE,
    )
    if len(clauses) == 1:
        clauses = re.split(r"\s+\band\b\s+", lower, flags=re.IGNORECASE)
    clauses = [c.strip(" ,.") for c in clauses if c and c.strip(" ,.")]

    remove: list[str] = []
    add: list[str] = []
    absolute: list[str] = []
    saw_op = False

    for clause in clauses:
        if _REMOVE_CLAUSE.search(clause):
            saw_op = True
            remove.extend(extract_interests(clause))
        elif _ADD_CLAUSE.search(clause):
            saw_op = True
            add.extend(extract_interests(clause))
        else:
            absolute.extend(extract_interests(clause))

    # Single-clause forms: "remove shopping", "add food"
    if not saw_op and len(clauses) == 1:
        if _REMOVE_CLAUSE.search(lower):
            saw_op = True
            remove = extract_interests(lower)
        elif _ADD_CLAUSE.search(lower):
            saw_op = True
            add = extract_interests(lower)

    remove = normalize_interests(remove)
    add = normalize_interests(add)
    absolute = normalize_interests(absolute)

    if saw_op and (remove or add):
        out = [i for i in existing if i not in set(remove)]
        for a in add:
            if a not in out:
                out.append(a)
        return out, True

    if absolute:
        return absolute, True

    # Fallback: raw interest keywords with no op verbs → absolute replace.
    spoken = extract_interests(text)
    if spoken:
        return spoken, True
    return existing, False


def resolve_trip_preferences(
    message: str,
    *,
    explicit_pace: Pace | None = None,
    existing_profile: str | None = None,
    existing_interests: list[str] | None = None,
    existing_constraints: list[str] | None = None,
    existing_pace: Pace | None = None,
    existing_window: int | None = None,
) -> dict[str, Any]:
    """
    Merge user message cues with profile presets.

    Explicit interests and pace in the utterance win over profile defaults;
    profile still adds soft constraints and fills missing interests.
    Add/remove phrasing patches the existing interest list instead of replacing it.
    """
    clear_profile = clear_traveler_profile_request(message)
    detected = None if clear_profile else detect_traveler_profile(message)
    # Clearing wins over a sticky existing audience (e.g. "remove couple friendly").
    if clear_profile:
        profile = "general"
        existing_profile = None
        existing_constraints = [
            c
            for c in (existing_constraints or [])
            if c.strip().lower()
            not in {
                "kid_friendly",
                "senior_friendly",
                "couple_friendly",
                "friends_friendly",
                "solo",
            }
            and "couple" not in c.lower()
            and "romantic" not in c.lower()
        ]
    else:
        profile = detected or existing_profile or "general"
    preset = PROFILE_PRESETS.get(profile, PROFILE_PRESETS["general"])

    interests, interests_from_message = parse_interest_updates(
        message, existing_interests=existing_interests
    )
    # Never invent interests/pace for a "general" visitor — only reuse what was
    # already stated, or profile presets when the audience was explicitly named.
    if interests_from_message:
        if detected:
            for extra in preset["interests"]:
                if extra not in interests:
                    interests.append(extra)
    elif existing_interests:
        interests = normalize_interests(list(existing_interests))
    elif detected:
        interests = normalize_interests(list(preset["interests"]))
    else:
        interests = []

    pace: Pace | None = explicit_pace or existing_pace
    if pace is None and detected:
        # Profile named (e.g. kid-friendly) may imply a pace — still mark as known
        # only when the orchestrator treats profile detection as an answer.
        pace = None

    constraints: list[str] = []
    seen_c: set[str] = set()
    preset_constraints = list(preset.get("constraints") or []) if detected else []
    for c in preset_constraints + list(existing_constraints or []):
        key = c.strip().lower()
        if not key or key in seen_c:
            continue
        if key in {
            "kid_friendly",
            "senior_friendly",
            "couple_friendly",
            "friends_friendly",
            "solo",
        } and key != profile:
            continue
        seen_c.add(key)
        constraints.append(c.strip())

    window = int(existing_window or preset.get("daily_time_window_min") or 540)
    if detected:
        window = int(preset.get("daily_time_window_min") or window)

    return {
        "traveler_profile": (
            "general"
            if clear_profile
            else (profile if detected or existing_profile else "general")
        ),
        "interests": interests,
        "pace": pace,
        "constraints": constraints,
        "daily_time_window_min": window,
        "boost_categories": set(preset.get("boost_categories") or set()) if detected else set(),
        "avoid_categories": set(preset.get("avoid_categories") or set()) if detected else set(),
        "profile_label": PROFILE_LABELS.get(
            "general" if clear_profile else profile,
            profile,
        ),
        "interests_from_message": interests_from_message,
        "pace_from_message": bool(explicit_pace),
        "profile_detected": bool(detected),
        "profile_cleared": clear_profile,
    }


def profile_label(profile: str | None) -> str | None:
    if not profile or profile == "general":
        return None
    return PROFILE_LABELS.get(profile, profile.replace("_", " "))


def constraint_mentions(constraints: list[str], *needles: str) -> bool:
    blob = " ".join(constraints).lower()
    return any(n.lower() in blob for n in needles)
