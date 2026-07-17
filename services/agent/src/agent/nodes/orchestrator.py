"""Orchestrator agent — safety, slots, and multi-agent routing control loop.

The Orchestrator decides *which* specialist agents to call (LLM or heuristic)
as ordered *waves*. Agents in the same wave are independent and fan out in
parallel via LangGraph Send; waves run sequentially. Specialists return here
so the Orchestrator can dispatch the next wave or finalize → Synthesis.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Any

from agent.mcp.geo import list_india_city_names, resolve_city
from agent.mcp.weather import weather_adjustment
from agent.nodes.agent_planner import flatten_waves, plan_agent_waves, waves_for_revision
from agent.nodes.execution_plan import (
    agents_for_missing_criteria,
    artifacts_complete,
    success_criteria_for_waves,
)
from agent.nodes.state_utils import as_dispatch, as_itinerary, as_trip, as_verdict, dump
from agent.preferences import (
    clear_traveler_profile_request,
    extract_interests,
    is_off_scope_trip_brief,
    primary_interests_prompt,
    profile_label,
    resolve_trip_preferences,
)
from agent.schemas.edits import EditPatch, EditTarget
from agent.schemas.itinerary import Pace, TripConstraints
from agent.schemas.review import normalize_target_agent
from agent.schemas.specialists import DispatchPlan
from agent.schemas.state import GraphState
from agent.trip_limits import (
    MAX_TRIP_DAYS,
    MIN_TRIP_DAYS,
    SCOPED_CITY,
    clamp_trip_days,
    default_city,
    is_city_allowed,
)

logger = logging.getLogger(__name__)

MAX_CLARIFY = 6
MAX_ORCHESTRATOR_STEPS = 12

_BLOCK_PATTERNS = [
    r"\bignore (all |previous )?instructions\b",
    r"\bjailbreak\b",
    r"\bhow to make (a )?bomb\b",
    r"\bkill\b.*\b(people|someone)\b",
    r"\bhack (into|a )\b",
    r"\bchild\s*porn\b",
    r"\bcsam\b",
]

_OFF_SCOPE = [
    r"\bstock tip\b",
    r"\bcrypto signal\b",
    r"\bwrite (my )?essay\b",
    r"\bmedical diagnos",
]


def _safety_check(message: str) -> tuple[str, str | None]:
    lower = message.lower()
    for pat in _BLOCK_PATTERNS:
        if re.search(pat, lower):
            return (
                "blocked",
                "I can't help with that request. I only plan safe travel itineraries "
                "for cities in India.",
            )
    for pat in _OFF_SCOPE:
        if re.search(pat, lower):
            return (
                "blocked",
                "I'm a travel planner for "
                f"**{SCOPED_CITY}** only "
                f"({MIN_TRIP_DAYS}–{MAX_TRIP_DAYS} day trips). "
                "Ask me to plan or edit a Jaipur itinerary instead.",
            )
    return "ok", None


_FOREIGN_DEST_RE = re.compile(
    r"\b("
    r"paris|london|rome|tokyo|dubai|singapore|bangkok|bali|sydney|"
    r"barcelona|amsterdam|berlin|venice|florence|new york|nyc|"
    r"france|italy|spain|europe|germany|greece|portugal|switzerland|"
    r"eiffel(?: tower)?|louvre|colosseum|big ben|times square"
    r")\b",
    re.I,
)


def _foreign_destination(message: str) -> str | None:
    """Named destination outside the India catalog (e.g. Paris)."""
    m = _FOREIGN_DEST_RE.search(message)
    if not m:
        return None
    label = m.group(1).lower()
    lower = message.lower()
    if "paris" in lower and "eiffel" in label:
        return "Paris"
    if "rome" in lower and "colosseum" in label:
        return "Rome"
    if label in {"new york", "nyc"}:
        return "New York"
    if label in {"france", "italy", "spain", "europe", "germany", "greece", "portugal", "switzerland"}:
        return label.title()
    return label.title()


def _find_city(message: str) -> str | None:
    """Resolve city mentions within the current Jaipur-only scope.

    Returns Jaipur when mentioned or when no other city is named.
    Returns a non-allowed city name only so the caller can redirect with a note.
    """
    from agent.mcp.geo import _city_labels

    lower = message.lower()
    mentioned: str | None = None
    for label, canonical in _city_labels():
        if len(label) < 4:
            continue
        if re.search(rf"(?<![a-z]){re.escape(label)}(?![a-z])", lower):
            info = resolve_city(canonical)
            mentioned = info.name if info else canonical
            break
    if mentioned is None:
        foreign = _foreign_destination(message)
        if foreign:
            return foreign
        # Scope is Jaipur-only — default when the user says "plan a trip" without a city.
        if re.search(r"\b(plan|trip|itinerary|visit|weekend)\b", lower):
            return default_city()
        return None
    if is_city_allowed(mentioned):
        return mentioned
    return mentioned  # out-of-scope; orchestrator redirects to Jaipur


def _find_days(message: str) -> int | None:
    """Return requested length if it is within product scope."""
    any_days = _find_any_day_count(message)
    if any_days is None:
        return None
    if MIN_TRIP_DAYS <= any_days <= MAX_TRIP_DAYS:
        return any_days
    return None


_DAY_WORDS: dict[str, int] = {
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "fourteen": 14,
}


def _find_any_day_count(message: str) -> int | None:
    """Any day count mentioned (including out-of-scope like 1 or 30)."""
    lower = message.lower().strip()
    m = re.search(r"\b(\d{1,2})\s*-?\s*days?\b", lower)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(\d{1,2})\s*-?\s*day\b", lower)
    if m:
        return int(m.group(1))
    m = re.search(r"\bfor\s+(\d{1,2})\s+days?\b", lower)
    if m:
        return int(m.group(1))
    # Spoken forms: "three-day trip", "three days"
    word_alt = "|".join(_DAY_WORDS.keys())
    m = re.search(rf"\b({word_alt})\s*-?\s*days?\b", lower)
    if m:
        return _DAY_WORDS[m.group(1)]
    m = re.search(rf"\b({word_alt})\s*-?\s*day\b", lower)
    if m:
        return _DAY_WORDS[m.group(1)]
    # Bare clarify answers: "3", "three", "3." (common voice STT)
    m = re.fullmatch(r"\s*([2-9]|1[0-4]|two|three|four|five|six|seven|eight|nine|ten|fourteen)\s*[.!?]?\s*", lower)
    if m:
        token = m.group(1)
        if token.isdigit():
            return int(token)
        return _DAY_WORDS.get(token)
    return None


def _find_pace(message: str) -> Pace | None:
    lower = message.lower()
    if re.search(r"\b(relax(?:ed)?|chill|slow|leisurely)\b", lower):
        return "relaxed"
    if re.search(r"\b(packed|busy|intense|full[\s-]?day)\b", lower):
        return "packed"
    if re.search(r"\b(moderate|balanced|balance)\b", lower):
        return "moderate"
    return None


def _find_interests(message: str) -> list[str]:
    """Backward-compatible wrapper — prefer resolve_trip_preferences."""
    return extract_interests(message)


def _detect_intent(message: str, state: GraphState) -> str:
    lower = message.lower().strip()
    has_itin = bool(
        as_itinerary(state.get("previous_itinerary") or state.get("merged_itinerary"))
    )

    # Confirm always wins when we are waiting on an unconfirmed trip.
    trip = as_trip(state.get("trip_constraints"))
    if trip and not trip.confirmed and re.search(
        r"\b(yes|confirm|looks good|go ahead|proceed|ok(ay)?|sounds good)\b", lower
    ):
        return "confirm"

    # Standalone weather / tip / rain / why questions (win over day-edit steal).
    if _is_weather_query(message):
        return "explain"
    if re.search(
        r"\b(what if it rains|if it rains|rains? on day|rain on day|"
        r"if(?:\s+if)? it rains)\b",
        lower,
    ) or (
        re.search(r"\brain\b", lower)
        and has_itin
        and not re.search(
            r"\b(add|remove|swap|make day|more (relaxed|packed))\b", lower
        )
    ):
        return "explain"
    if re.search(
        r"\bwhy (did you |do you )?(pick|choose|include|this|that)\b",
        lower,
    ):
        return "explain"
    if _is_knowledge_query(message):
        return "explain"

    # During slot fill / pre-confirm, preference tweaks stay in "plan"
    # (e.g. "remove couple friendly") — never itinerary edit.
    if trip and not trip.confirmed:
        if clear_traveler_profile_request(message):
            return "plan"
        if re.search(
            r"\b(remove|drop|clear|change|update|not|without)\b", lower
        ) and not re.search(
            r"\bday\s*(?:[1-4]|one|two|three|four|first|second|third|fourth)\b",
            lower,
        ):
            return "plan"

    day_mentioned = bool(
        re.search(
            r"\bday\s*(?:[1-4]|one|two|three|four|first|second|third|fourth)\b",
            lower,
        )
    )
    edit_verb = bool(
        re.search(
            r"\b(add|include|change|edit|make|relax|remove|swap|update|replace|"
            r"move|trim|fewer|indoor|outdoor|reduce travel|less travel|"
            r"balance|balanced|mix)\b",
            lower,
        )
    )
    add_foodish = bool(
        re.search(
            r"\b(add|include)\b.{0,50}\b(food|restaurant|cafe|eatery|temple|"
            r"museum|shopping|market|heritage|park)\b",
            lower,
        )
        or re.search(
            r"\badd (a |one |an )?(famous )?(local )?"
            r"(food|restaurant|cafe)\b",
            lower,
        )
    )

    # Day-scoped / add-stop edits MUST win over "plan|itinerary" keywords
    # (e.g. "add food to day two … of the itinerary").
    # Bare "Day 1" alone is NOT an edit — used for rain-day follow-ups.
    edit_phrase = bool(
        re.search(
            r"\b(make day|change day|edit|swap|relax day|more relaxed|"
            r"more packed|less packed|balance|balanced|mix of|"
            r"instead of|reduce travel|less travel|indoor|"
            r"food stops?|something outdoor)\b",
            lower,
        )
    )
    if has_itin and (add_foodish or edit_verb) and (
        day_mentioned or add_foodish or edit_phrase
    ):
        return "edit"

    if re.search(
        r"\b(make day|change day|edit|swap|remove|relax day|more relaxed|"
        r"more packed|less packed|balance|balanced|mix of|"
        r"instead of|reduce travel|less travel|add (a |one |an )?"
        r"(famous )?(local )?(food|restaurant|cafe)|indoor|"
        r"food stops?|something outdoor|add .{0,20}outdoor)\b",
        lower,
    ):
        # Without a generated itinerary, "remove …" is a preference edit, not day edit.
        if not has_itin and trip and not trip.confirmed:
            return "plan"
        if not has_itin:
            return "plan"
        return "edit"
    if has_itin and re.search(
        r"\b(reduce travel|less walking|too much travel|cluster|"
        r"outdoor|food stops?|change day)\b",
        lower,
    ):
        return "edit"
    if has_itin and re.search(
        r"\b(add|include).{0,40}\b(food|restaurant|cafe|eatery|outdoor|park)\b",
        lower,
    ):
        return "edit"

    if re.search(
        r"\b(why (this|that|did you)|explain|what if it rains|tell me (?:more )?about|"
        r"more about|is (this|the) plan doable|is (it|this) doable|feasible|"
        r"is (?:this|the) itinerary|too packed|can (i|we) (?:do|finish)|"
        r"why (pick|choose|choose))\b",
        lower,
    ):
        return "explain"
    if has_itin and re.search(r"\b(doable|feasible|rain|why)\b", lower):
        return "explain"

    # New trip planning — but never steal day-scoped edits that mention "itinerary".
    if re.search(r"\b(plan|trip|itinerary|visit|weekend)\b", lower):
        if has_itin and day_mentioned and edit_verb:
            return "edit"
        return "plan"
    # Bare day mention alone is not an edit (rain/why/tips handled above).
    if has_itin and day_mentioned and edit_verb:
        return "edit"
    return "plan"


def _is_knowledge_query(message: str) -> bool:
    """True for tip / POI / safety / etiquette questions that should use Knowledge RAG."""
    lower = message.lower().strip()
    if _is_weather_query(message):
        return False
    # Doability is itinerary-structure only — never free RAG.
    if re.search(
        r"\b(doable|feasible|too (?:much|packed)|can (?:i|we) (?:do|finish)|"
        r"is (?:this|the|it) plan (?:doable|feasible|realistic))\b",
        lower,
    ):
        return False
    # Opening hours / timings — always RAG (never trip slot clarify).
    if re.search(
        r"\b(opening\s+hours?|opening\s+time|open(?:ing)?\s+times?|"
        r"what(?:'s| is| are)?\s+(the\s+)?hours?|"
        r"hours?\s+for|timings?\s+for|what time|"
        r"when (does|do|is|are).{0,40}\bopen|"
        r"is .+ open)\b",
        lower,
    ):
        return True
    # Don't steal trip-planning utterances
    if re.search(
        r"\b(plan|itinerary|weekend trip|days? (in|to)|relaxed|packed)\b",
        lower,
    ) and not re.search(
        r"\b(crowded|best time|opening|hours|timing|tell me (?:more )?about|"
        r"tips? for|safe|safety|etiquette|scam|customs)\b",
        lower,
    ):
        return False
    if re.search(
        r"\b(how crowded|best time (to )?(visit|see|go)|when (to|should) (visit|go|see)|"
        r"opening hours|is .+ open|open (on|at|in)|tell me (?:more )?about|"
        r"more about|tips? for (visiting|seeing)|what about|worth (visiting|seeing)|"
        r"how long (to spend|should i spend)|what (is|are) .+ (like|famous for)|"
        r"safe(ty)?|scam(s)?|etiquette|customs|dress code|respect|"
        r"neighborhood|area(s)? (to |worth )?visit|which areas?|"
        r"what to (do|avoid|wear)|practical tip|"
        r"where (should|to) (go|visit|stay)|"
        r"why (did you |do you )?(pick|choose|include))\b",
        lower,
    ):
        return True
    # Named place + a question word
    if re.search(
        r"\b(hawa mahal|jantar mantar|jantar manta|amber fort|amer fort|city palace|"
        r"nahargarh|jal mahal|albert hall|birla mandir|johari|bapu bazaar|"
        r"chokhi dhani|patrika gate|pink city|ram niwas|govind\s+dev|janpath)\b",
        lower,
    ) and re.search(
        r"\b(how|what|when|is|are|tell|tips?|about|visit|open|hours?|"
        r"timing|crowded|why|safe|more)\b",
        lower,
    ):
        return True
    return False


def _knowledge_topics_for_message(message: str) -> list[str]:
    lower = message.lower()
    topics = ["tips", "highlights"]
    if re.search(r"\b(safe|safety|scam|theft|caution)\b", lower):
        topics = ["safety", "tips", "culture"]
    elif re.search(r"\b(etiquette|customs|dress|respect|temple rules)\b", lower):
        topics = ["culture", "tips", "safety"]
    elif re.search(r"\b(area|neighborhood|where to (go|visit)|what to do)\b", lower):
        topics = ["highlights", "doable", "tips"]
    elif re.search(r"\b(crowd|crowded|busy|queue)\b", lower):
        topics = ["crowd", "tips", "timing", "highlights"]
    elif re.search(r"\b(best time|morning|evening|hours?|timing|open(?:ing)?)\b", lower):
        topics = ["timing", "tips", "highlights"]
    elif re.search(r"\b(history|built|why|famous|pick|choose)\b", lower):
        topics = ["why", "highlights", "culture", "tips"]
    return topics


def _out_of_scope_tip_place(message: str) -> str | None:
    """Detect non-Jaipur cities/landmarks in tip questions (never invent foreign tips)."""
    catalog = _find_city(message)
    if catalog and not is_city_allowed(catalog):
        return catalog
    return _foreign_destination(message)


def _answer_knowledge_query(
    state: GraphState, message: str, existing_trip: TripConstraints | None
) -> dict[str, Any]:
    """Answer tip / POI questions from Knowledge RAG only — never invent."""
    from agent.rag.retrieve import (
        excerpt_place_from_snippet,
        extract_place_terms,
        is_thin_place_listing,
        knowledge_search,
        sources_from_knowledge,
    )

    oos = _out_of_scope_tip_place(message)
    if oos:
        reply = (
            f"Tips for **{oos}** aren’t available in this demo. "
            f"Ask about places in **{SCOPED_CITY}**, e.g. Hawa Mahal or Jantar Mantar."
        )
        return {
            "safety_status": "ok",
            "intent": "explain",
            "ready_for_merger": False,
            "ready_for_synthesis": False,
            "next_agent": None,
            "orchestration_started": False,
            "trip_constraints": dump(existing_trip) if existing_trip else None,
            "user_reply": reply,
            "sources": [],
            "dispatch_plan": _dispatch_from_waves([]),
            "agent_trace": _trace_append(
                state,
                {
                    "agent": "orchestrator",
                    "action": "knowledge_qa",
                    "missing_data": True,
                    "detail": "out_of_scope_city",
                    "requested_city": oos,
                },
            ),
        }
    city = _find_city(message)
    city = city if city and is_city_allowed(city) else default_city()
    topics = _knowledge_topics_for_message(message)
    rag_query = message
    if re.search(r"\b(hours?|timing|opening|open(?:ing)?\s+time)\b", message.lower()):
        rag_query = f"{message} opening hours timing am pm"
        if "timing" not in topics:
            topics = ["timing", "tips", "highlights"] + [
                t for t in topics if t not in {"timing", "tips", "highlights"}
            ]
    hours_q = bool(
        re.search(
            r"\b(hours?|timing|opening|open(?:ing)?\s+time)\b",
            message.lower(),
        )
    )
    # Hours need place listings; pull a wider set so the clock-time chunk is not truncated out.
    result = knowledge_search(
        city=city, query=rag_query, topics=topics, k=8 if hours_q else 4
    )
    places = extract_place_terms(message, city)
    sources = sources_from_knowledge(result)

    if result.missing_data or not result.snippets:
        note = (result.notes or "").strip()
        reply = (
            f"I don’t have cited guide tips for that in the {city} corpus yet."
            + (f" ({note})" if note else "")
        )
    else:
        hours_q = bool(
            re.search(
                r"\b(hours?|timing|opening|open(?:ing)?\s+time)\b",
                message.lower(),
            )
        )
        _HOUR_CLOCK_RE = re.compile(
            # Guides often write "9AM-5PM" with no space after the digit
            # (\\b does not fire between digit and letter).
            r"\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b"
            r"(?:\s*[-–]\s*\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b)?|"
            r"\b(?:mo|tu|we|th|fr|sa|su|mon|tue|wed|thu|fri|sat|sun)"
            r"[a-z]*\s*[-–]\s*"
            r"(?:mo|tu|we|th|fr|sa|su|mon|tue|wed|thu|fri|sat|sun)"
            r"[a-z]*.{0,40}\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b",
            re.I,
        )
        # Prefer snippets that mention the asked place when possible
        snippets = list(result.snippets)
        place_matched = False
        if places:
            def _snip_matches(s) -> bool:  # noqa: ANN001
                hay = (s.text or "").lower()
                return any(
                    p in hay or excerpt_place_from_snippet(s.text or "", p)
                    for p in places
                )

            preferred = [s for s in snippets if _snip_matches(s)]
            if not preferred:
                # Second chance: re-query place-first without diluting topics
                retry = knowledge_search(
                    city=city,
                    query=" ".join(places[:2]) + " " + message,
                    topics=None,
                    k=8,
                )
                preferred = [s for s in retry.snippets if _snip_matches(s)]
                if preferred:
                    result = retry
                    sources = sources_from_knowledge(result)
            if preferred:
                snippets = preferred
                place_matched = True
            elif hours_q:
                # Hours for a named place with no place-matched chunk — refuse.
                place_label = places[0].title()
                reply = (
                    f"I don’t have cited opening hours for {place_label} "
                    f"in the {city} guide corpus — I won’t invent them or "
                    f"borrow hours from other places."
                )
                return {
                    "safety_status": "ok",
                    "intent": "explain",
                    "ready_for_merger": False,
                    "ready_for_synthesis": False,
                    "next_agent": None,
                    "orchestration_started": False,
                    "trip_constraints": dump(existing_trip) if existing_trip else None,
                    "knowledge_results": dump(result),
                    "user_reply": reply,
                    "sources": [],
                    "dispatch_plan": _dispatch_from_waves([]),
                    "agent_trace": _trace_append(
                        state,
                        {
                            "agent": "orchestrator",
                            "action": "knowledge_qa",
                            "missing_data": True,
                            "detail": "hours_no_place_match",
                            "place": place_label,
                        },
                    ),
                }
            else:
                # Named place with no corpus hit — refuse without inventing from noise.
                place_label = places[0].title()
                reply = (
                    f"I don’t have a cited guide tip about {place_label} "
                    f"in the {city} corpus — I won’t invent details."
                )
                return {
                    "safety_status": "ok",
                    "intent": "explain",
                    "ready_for_merger": False,
                    "ready_for_synthesis": False,
                    "next_agent": None,
                    "orchestration_started": False,
                    "trip_constraints": dump(existing_trip) if existing_trip else None,
                    "knowledge_results": dump(result),
                    "user_reply": reply,
                    "sources": [],
                    "dispatch_plan": _dispatch_from_waves([]),
                    "agent_trace": _trace_append(
                        state,
                        {
                            "agent": "orchestrator",
                            "action": "knowledge_qa",
                            "missing_data": True,
                            "detail": "place_no_match",
                            "place": place_label,
                        },
                    ),
                }
        if hours_q and places:
            # Prefer place+clock listings; incidental mentions without times do not count.
            pool = list(result.snippets)
            timed = [
                s
                for s in pool
                if any(p in (s.text or "").lower() for p in places)
                and _HOUR_CLOCK_RE.search(s.text or "")
            ]
            if not timed:
                place_label = places[0].title()
                reply = (
                    f"The {city} guide mentions {place_label}, but it does not "
                    f"list opening hours for it — I won’t invent hours."
                )
                return {
                    "safety_status": "ok",
                    "intent": "explain",
                    "ready_for_merger": False,
                    "ready_for_synthesis": False,
                    "next_agent": None,
                    "orchestration_started": False,
                    "trip_constraints": dump(existing_trip) if existing_trip else None,
                    "knowledge_results": dump(result),
                    "user_reply": reply,
                    "sources": [dump(s) for s in sources_from_knowledge(result)][:2],
                    "dispatch_plan": _dispatch_from_waves([]),
                    "agent_trace": _trace_append(
                        state,
                        {
                            "agent": "orchestrator",
                            "action": "knowledge_qa",
                            "missing_data": True,
                            "detail": "hours_not_in_snippet",
                            "place": place_label,
                        },
                    ),
                }
            # Prefer numbered listing rows for the place when present.
            listing = []
            for s in timed:
                low = (s.text or "").lower()
                if any(
                    re.search(rf"(?:^|\n)\s*\d+\s+{re.escape(p)}\b", low)
                    for p in places
                ):
                    listing.append(s)
            snippets = listing or timed
            place_matched = True
            # LLM answer: query + retrieved hours snippets → concise hours only
            from agent.nodes.llm_utils import compose_grounded_reply

            rag_inputs: list[dict[str, Any]] = []
            for snip in snippets[:4]:
                if not snip.citations:
                    continue
                c0 = snip.citations[0]
                text = snip.text or ""
                for p in places:
                    excerpt = excerpt_place_from_snippet(text, p)
                    if excerpt and _HOUR_CLOCK_RE.search(excerpt):
                        text = excerpt
                        break
                rag_inputs.append(
                    {"text": text, "title": c0.title, "url": c0.url}
                )
            grounded = compose_grounded_reply(
                user_query=message,
                sources=rag_inputs,
                role_hint="opening_hours",
            )
            if grounded:
                c0 = snippets[0].citations[0] if snippets and snippets[0].citations else None
                from agent.nodes.llm_utils import ensure_source_link, preferred_source_url

                reply = ensure_source_link(
                    grounded,
                    c0,
                    text=snippets[0].text if snippets else None,
                )
                src_out = c0
                if c0:
                    pref = preferred_source_url(
                        c0, text=snippets[0].text if snippets else None
                    )
                    if pref and pref != c0.url:
                        src_out = c0.model_copy(update={"url": pref})
                return {
                    "safety_status": "ok",
                    "intent": "explain",
                    "ready_for_merger": False,
                    "ready_for_synthesis": False,
                    "next_agent": None,
                    "orchestration_started": False,
                    "trip_constraints": dump(existing_trip)
                    if existing_trip
                    else None,
                    "knowledge_results": dump(result),
                    "sources": [dump(src_out)] if src_out else [],
                    "user_reply": reply,
                    "dispatch_plan": _dispatch_from_waves([]),
                    "agent_trace": _trace_append(
                        state,
                        {
                            "agent": "orchestrator",
                            "action": "knowledge_qa",
                            "missing_data": False,
                            "detail": "hours_llm",
                            "place": places[0] if places else None,
                        },
                    ),
                }
            # Deterministic fallback if LLM unavailable: short hours-only extract
            for snip in snippets:
                if not snip.citations:
                    continue
                for p in places:
                    excerpt = excerpt_place_from_snippet(snip.text or "", p)
                    if not excerpt and p in (snip.text or "").lower():
                        excerpt = re.sub(r"\s+", " ", (snip.text or "")).strip()
                    if not excerpt or not _HOUR_CLOCK_RE.search(excerpt):
                        continue
                    # Prefer the "Opening hours: …" clause when present
                    m_hours = re.search(
                        r"Opening hours?\s*:\s*([^.]+(?:\.[^.]+)*?)(?:\.\s*Phone:|\.\s*Website:|\.\s*Rating:|$)",
                        excerpt,
                        flags=re.I,
                    )
                    hours_bit = (
                        m_hours.group(1).strip().rstrip(".")
                        if m_hours
                        else None
                    )
                    place_label = p.title()
                    if hours_bit:
                        reply = f"{place_label} opening hours: {hours_bit}."
                    else:
                        clean = re.sub(r"^\d+\s+", "", excerpt).strip()
                        # Strip address/phone/website noise from Google cards
                        clean = re.split(
                            r"\.\s*(?:Phone|Website|Rating|Types)\s*:",
                            clean,
                            maxsplit=1,
                        )[0].strip()
                        reply = f"{place_label}: {clean.rstrip('.')}."
                    c0 = snip.citations[0]
                    from agent.nodes.llm_utils import ensure_source_link, preferred_source_url

                    reply = ensure_source_link(reply, c0, text=snip.text)
                    pref = preferred_source_url(c0, text=snip.text)
                    src_out = (
                        c0.model_copy(update={"url": pref})
                        if pref and pref != c0.url
                        else c0
                    )
                    return {
                        "safety_status": "ok",
                        "intent": "explain",
                        "ready_for_merger": False,
                        "ready_for_synthesis": False,
                        "next_agent": None,
                        "orchestration_started": False,
                        "trip_constraints": dump(existing_trip)
                        if existing_trip
                        else None,
                        "knowledge_results": dump(result),
                        "sources": [dump(src_out)],
                        "user_reply": reply,
                        "dispatch_plan": _dispatch_from_waves([]),
                        "agent_trace": _trace_append(
                            state,
                            {
                                "agent": "orchestrator",
                                "action": "knowledge_qa",
                                "missing_data": False,
                                "detail": "hours_excerpt_fallback",
                                "place": p,
                            },
                        ),
                    }
        if places and place_matched and not hours_q:
            candidates: list[tuple[int, str, Any, str]] = []
            for snip in snippets:
                if not snip.citations:
                    continue
                for p in places:
                    excerpt = excerpt_place_from_snippet(snip.text or "", p)
                    if not excerpt and any(
                        p in (snip.text or "").lower() for p in [p]
                    ):
                        # Whole atomic card that mentions the place
                        excerpt = re.sub(r"\s+", " ", (snip.text or "")).strip()
                        if len(excerpt) > 420:
                            excerpt = excerpt[:419].rstrip() + "…"
                    if not excerpt:
                        continue
                    ds = str(
                        (snip.citations[0].dataset if snip.citations else "")
                        or ""
                    ).lower()
                    # Prefer wikipedia / curated / osm cards over mixed Wikivoyage blobs
                    rank = 0
                    if ds == "wikipedia":
                        rank += 80
                    elif is_thin_place_listing(excerpt) or len(excerpt) < 280:
                        rank += 35
                    elif ds == "wikivoyage":
                        rank += 45
                    elif ds in {"other"}:
                        rank += 40
                    else:
                        rank += min(len(excerpt), 400) // 20
                    # Prefer OSM hours cards less for open-ended “tell me more”
                    title_l = (snip.citations[0].title or "").lower()
                    if "openstreetmap" in ds or "openstreetmap" in title_l:
                        rank -= 15
                    if "curated" in (snip.citations[0].source_id or "") or "Moon Gate" in (
                        snip.citations[0].title or ""
                    ):
                        rank += 20
                    # Stronger if excerpt starts with the place or a listing number
                    elow = excerpt.lower()
                    if elow.startswith(p) or re.match(r"^\d+\s+", excerpt):
                        rank += 25
                    if p in elow[:80]:
                        rank += 10
                    candidates.append((rank, excerpt, snip, p))
                    break
            if candidates:
                candidates.sort(key=lambda row: row[0], reverse=True)
                _rank, excerpt, snip, p = candidates[0]
                from agent.nodes.llm_utils import compose_grounded_reply

                c0 = snip.citations[0]
                grounded = compose_grounded_reply(
                    user_query=message,
                    sources=[{"text": excerpt, "title": c0.title, "url": c0.url}],
                    role_hint="travel tip",
                )
                from agent.nodes.llm_utils import ensure_source_link, preferred_source_url

                if grounded:
                    reply = ensure_source_link(grounded, c0, text=excerpt)
                else:
                    clean = re.sub(r"^\d+\s+", "", excerpt).strip()
                    # Avoid dumping full Google cards when LLM is offline
                    clean = re.split(
                        r"\.\s*(?:Phone|Website|Rating|Types)\s*:",
                        clean,
                        maxsplit=1,
                    )[0].strip()
                    if len(clean) > 320:
                        clean = clean[:319].rstrip() + "…"
                    reply = ensure_source_link(
                        f"From the {city} guide, {clean.rstrip('.')}.",
                        c0,
                        text=excerpt,
                    )
                pref = preferred_source_url(c0, text=excerpt)
                src_out = (
                    c0.model_copy(update={"url": pref})
                    if pref and pref != c0.url
                    else c0
                )
                return {
                    "safety_status": "ok",
                    "intent": "explain",
                    "ready_for_merger": False,
                    "ready_for_synthesis": False,
                    "next_agent": None,
                    "orchestration_started": False,
                    "trip_constraints": dump(existing_trip)
                    if existing_trip
                    else None,
                    "knowledge_results": dump(result),
                    "sources": [dump(src_out)],
                    "user_reply": reply,
                    "dispatch_plan": _dispatch_from_waves([]),
                    "agent_trace": _trace_append(
                        state,
                        {
                            "agent": "orchestrator",
                            "action": "knowledge_qa",
                            "missing_data": False,
                            "detail": "place_excerpt_llm"
                            if grounded
                            else "place_excerpt",
                            "place": p,
                        },
                    ),
                }

        # Prefer topic-relevant snippets for safety / etiquette / areas
        # (skip when answering about a named place — keep place-matched hits).
        topic_markers = []
        for t in topics:
            if t == "safety":
                topic_markers.extend(["stay safe", "scam", "thieves", "caution", "touts"])
            elif t == "culture":
                topic_markers.extend(
                    ["temple", "dress", "etiquette", "customs", "photos", "bags", "respect"]
                )
            elif t in {"highlights", "doable"} and not hours_q and not place_matched:
                topic_markers.extend(["see", "fort", "palace", "old city", "pink city"])
        if topic_markers and not hours_q and not place_matched:
            topical = [
                s
                for s in snippets
                if any(m in (s.text or "").lower() for m in topic_markers)
            ]
            if topical:
                snippets = topical + [s for s in snippets if s not in topical]
        lines: list[str] = []
        from agent.nodes.llm_utils import format_source_cite

        for snip in snippets[:3]:
            if not snip.citations:
                continue  # Never present an uncited factual tip
            text = re.sub(r"\s+", " ", (snip.text or "")).strip()
            if not text:
                continue
            if places:
                for p in places:
                    excerpt = excerpt_place_from_snippet(text, p)
                    if excerpt:
                        text = excerpt
                        break
            if len(text) > 320:
                text = text[:319].rstrip() + "…"
            c0 = snip.citations[0]
            cite = format_source_cite(c0, text=snip.text)
            lines.append(f"• {text}{cite}")
        place_bit = f" about {places[0].title()}" if places else ""
        grounded = any(
            any(p in (s.text or "").lower() for p in places) for s in snippets
        ) if places else True
        if places and not grounded:
            reply = (
                f"I found {city} guide material, but nothing specific{place_bit} "
                f"in the corpus for that question — I won’t invent details."
            )
        elif not lines:
            reply = (
                f"I don’t have cited guide tips for that in the {city} corpus yet "
                f"— I won’t invent details."
            )
        else:
            from agent.nodes.llm_utils import compose_grounded_reply, ensure_source_link

            rag_inputs = []
            for snip in snippets[:4]:
                if not snip.citations:
                    continue
                c0 = snip.citations[0]
                text = snip.text
                if places:
                    for p in places:
                        excerpt = excerpt_place_from_snippet(snip.text or "", p)
                        if excerpt:
                            text = excerpt
                            break
                rag_inputs.append(
                    {"text": text, "title": c0.title, "url": c0.url}
                )
            grounded = compose_grounded_reply(
                user_query=message,
                sources=rag_inputs,
                role_hint="travel tip",
            )
            if grounded:
                primary = snippets[0].citations[0] if snippets[0].citations else None
                reply = ensure_source_link(
                    grounded,
                    primary,
                    text=snippets[0].text if snippets else None,
                )
            else:
                reply = (
                    f"From the {city} travel guide (RAG, cited — not invented)"
                    f"{place_bit}:\n"
                    + "\n".join(lines)
                )

    return {
        "safety_status": "ok",
        "intent": "explain",
        "ready_for_merger": False,
        "ready_for_synthesis": False,
        "next_agent": None,
        "orchestration_started": False,
        "trip_constraints": dump(existing_trip) if existing_trip else None,
        "knowledge_results": dump(result),
        "sources": [dump(s) for s in sources],
        "user_reply": reply,
        "dispatch_plan": _dispatch_from_waves([]),
        "agent_trace": _trace_append(
            state,
            {
                "agent": "orchestrator",
                "action": "knowledge_qa",
                "tool": "knowledge_rag",
                "city": city,
                "topics": topics,
                "places": places,
                "missing_data": bool(result.missing_data),
                "hit_count": len(result.snippets or []),
            },
        ),
    }


def _is_weather_query(message: str) -> bool:
    """True for forecast/temperature questions, not itinerary 'what if it rains?'."""
    lower = message.lower().strip()
    if re.search(r"\bwhat if it rains\b", lower):
        return False
    if re.search(
        r"\b(weather|forecast|temperature|how hot|how cold|humid|"
        r"will it rain|is it raining|precipitation)\b",
        lower,
    ):
        return True
    if re.search(r"\brain\b", lower) and re.search(
        r"\b(tomorrow|today|tonight|weekend|this week|next week)\b", lower
    ):
        return True
    return False


def _weather_window(message: str) -> tuple[date, int]:
    """Return (start_date, num_days) for a weather Q&A from the utterance."""
    lower = message.lower()
    today = date.today()
    if re.search(r"\btomorrow\b", lower):
        return today + timedelta(days=1), 1
    if re.search(r"\btoday\b", lower) or re.search(r"\btonight\b", lower):
        return today, 1
    if re.search(r"\bweekend\b", lower):
        # Next Sat–Sun (or today if already Sat)
        weekday = today.weekday()  # Mon=0 … Sun=6
        days_until_sat = (5 - weekday) % 7
        start = today + timedelta(days=days_until_sat)
        return start, 2
    if re.search(r"\b(this|next)\s+week\b", lower):
        return today, 7
    # Generic "weather in Jaipur" → next few days, grounded from MCP
    return today, 3


def _format_weather_day(day: Any) -> str:
    """Format one MCP DayWeather row — only stated fields, no invention."""
    bits: list[str] = [str(getattr(day, "calendar_date", "") or "")]
    label = getattr(day, "weather_label", None)
    if label:
        bits.append(str(label))
    tmax = getattr(day, "temp_max_c", None)
    tmin = getattr(day, "temp_min_c", None)
    if tmax is not None and tmin is not None:
        bits.append(f"high {tmax:.0f}°C / low {tmin:.0f}°C")
    elif tmax is not None:
        bits.append(f"high {tmax:.0f}°C")
    elif tmin is not None:
        bits.append(f"low {tmin:.0f}°C")
    prob = getattr(day, "precip_probability_max", None)
    if prob is not None:
        bits.append(f"rain chance {prob:.0f}%")
    mm = getattr(day, "precip_mm_sum", None)
    if mm is not None:
        bits.append(f"precip {mm:.1f} mm")
    risk = getattr(day, "rain_risk", None)
    if risk:
        bits.append(f"rain risk {risk}")
    return ", ".join(b for b in bits if b)


def _strip_weather_place_tail(raw: str) -> str:
    """Drop trailing time-window words from a captured place phrase."""
    s = (raw or "").strip(" .,?!'\"")
    s = re.sub(
        r"\s+(?:today|tomorrow|tonight|this|next|week|weekend|now|please|"
        r"currently|the|in|for|at)\b.*$",
        "",
        s,
        flags=re.IGNORECASE,
    )
    return s.strip(" .,?!'\"")


def _explicit_weather_place(message: str) -> str | None:
    """Pull an explicit place name from a weather utterance (may be non-India)."""
    lower = message.lower().strip()
    patterns = [
        # "weather in Paris", "forecast for Delhi today"
        r"\b(?:weather|forecast|temperature|temps?|humid(?:ity)?|precipitation|"
        r"how hot|how cold|rain(?:ing)?)\b.{0,48}?\b(?:in|for|at)\s+"
        r"([a-z][a-z .'\-]{1,40})",
        # "in Paris weather" / "Paris weather"
        r"\b(?:in|for|at)\s+([a-z][a-z .'\-]{1,40}?)\s+"
        r"(?:weather|forecast|temperature|temps?)\b",
        r"\b([a-z][a-z .'\-]{2,40}?)\s+(?:weather|forecast)\b",
    ]
    skip = {
        "the",
        "a",
        "an",
        "my",
        "our",
        "this",
        "that",
        "there",
        "here",
        "jaipur's",
    }
    for pat in patterns:
        m = re.search(pat, lower)
        if not m:
            continue
        place = _strip_weather_place_tail(m.group(1))
        if not place or place in skip or len(place) < 3:
            continue
        # Ignore pure time windows mistaken as places
        if re.fullmatch(
            r"(today|tomorrow|tonight|weekend|this week|next week)", place
        ):
            continue
        return place
    return None


def _answer_weather_query(
    state: GraphState, message: str, existing_trip: TripConstraints | None
) -> dict[str, Any]:
    """Answer weather questions from Weather MCP only — never invent forecast."""
    catalog_city = _find_city(message)
    explicit = _explicit_weather_place(message)

    oos_name: str | None = None
    if catalog_city and not is_city_allowed(catalog_city):
        oos_name = catalog_city
    elif explicit:
        resolved = resolve_city(explicit)
        if resolved is not None:
            if not is_city_allowed(resolved.name):
                oos_name = resolved.name
        elif not is_city_allowed(explicit):
            # Named place outside the India catalog / Jaipur scope (e.g. Paris)
            oos_name = explicit.title()

    if oos_name:
        reply = (
            f"Weather for **{oos_name}** isn’t available in this demo. "
            f"I can look up **{SCOPED_CITY}** only — ask e.g. "
            f"“What’s the weather in {SCOPED_CITY} today?”"
        )
        return {
            "safety_status": "ok",
            "intent": "explain",
            "ready_for_merger": False,
            "ready_for_synthesis": False,
            "next_agent": None,
            "orchestration_started": False,
            "trip_constraints": dump(existing_trip) if existing_trip else None,
            "user_reply": reply,
            "dispatch_plan": _dispatch_from_waves([]),
            "agent_trace": _trace_append(
                state,
                {
                    "agent": "orchestrator",
                    "action": "weather_qa",
                    "missing_data": True,
                    "detail": "out_of_scope_city",
                    "requested_city": oos_name,
                },
            ),
        }

    city = (
        catalog_city
        if catalog_city and is_city_allowed(catalog_city)
        else default_city()
    )
    start, n_days = _weather_window(message)
    result = weather_adjustment(
        city=city,
        start_date=start.isoformat(),
        num_days=n_days,
        for_trip=False,
    )
    if result.missing_data or not result.days:
        note = (result.notes or "").strip()
        reply = (
            f"Weather data is not available for {city} right now."
            + (f" ({note})" if note else "")
        )
    else:
        lines = [
            _format_weather_day(d) for d in result.days if getattr(d, "calendar_date", None)
        ]
        source = result.source or "Open-Meteo"
        reply = (
            f"Here's the {city} forecast from {source} "
            f"(grounded weather data — not invented):\n"
            + "\n".join(f"• {line}" for line in lines if line)
        )
    return {
        "safety_status": "ok",
        "intent": "explain",
        "ready_for_merger": False,
        "ready_for_synthesis": False,
        "next_agent": None,
        "orchestration_started": False,
        "trip_constraints": dump(existing_trip) if existing_trip else None,
        "weather_results": dump(result),
        "user_reply": reply,
        "dispatch_plan": _dispatch_from_waves([]),
        "agent_trace": _trace_append(
            state,
            {
                "agent": "orchestrator",
                "action": "weather_qa",
                "tool": "weather_adjustment_mcp",
                "city": city,
                "start_date": start.isoformat(),
                "num_days": n_days,
                "missing_data": bool(result.missing_data),
                "days": len(result.days or []),
            },
        ),
    }


_DAY_WORDS = {
    "one": 1,
    "first": 1,
    "1st": 1,
    "two": 2,
    "second": 2,
    "2nd": 2,
    "three": 3,
    "third": 3,
    "3rd": 3,
    "four": 4,
    "fourth": 4,
    "4th": 4,
}

_COUNT_WORDS = {
    **_DAY_WORDS,
    "a": 1,
    "an": 1,
    "single": 1,
    "only": 1,
}


def _parse_count_token(raw: str) -> int | None:
    token = (raw or "").lower().strip()
    token = re.sub(r"^(only|just|a|an)\s+", "", token).strip()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    return _COUNT_WORDS.get(token)


def _parse_target_day(message: str) -> tuple[int | None, int]:
    """Return (matched_day_or_None, day_to_edit). Defaults to Day 1 when unspecified."""
    lower = message.lower()
    day_pat = (
        r"(?:([1-4])|(one|two|three|four|first|second|third|fourth|1st|2nd|3rd|4th))"
    )
    patterns = [
        rf"\b(?:on|for|change|edit|make|update|relax)\s+day\s*{day_pat}\b",
        rf"\bday\s*{day_pat}\b",
    ]
    for pat in patterns:
        m = re.search(pat, lower)
        if not m:
            continue
        if m.group(1):
            day = int(m.group(1))
            return day, day
        word = m.group(2)
        if word in _DAY_WORDS:
            day = _DAY_WORDS[word]
            return day, day
    return None, 1


def _split_edit_clauses(message: str) -> list[str]:
    """Split compound voice edits on 'and' / 'then' while keeping meaningful phrases."""
    text = message.strip()
    if not text:
        return []
    # Prefer splitting on coordinating connectors.
    parts = re.split(
        r"\s+(?:and then|then|,?\s+and)\s+",
        text,
        flags=re.IGNORECASE,
    )
    if len(parts) == 1:
        parts = re.split(r"\s+\band\b\s+", text, flags=re.IGNORECASE)
    out = [p.strip(" ,.") for p in parts if p and p.strip(" ,.")]
    return out or [text]


def _clause_block(clause: str) -> str | None:
    lower = clause.lower()
    if re.search(r"\b(beginning|start|morning)\b", lower):
        return "morning"
    for b in ("afternoon", "evening"):
        if b in lower:
            return b
    return None


_EDIT_CATEGORIES = (
    r"food|restaurant|cafe|eatery|heritage|museum|market|shopping|"
    r"temple|park|outdoor|outdoors|nature|culture|art"
)


def _normalize_edit_category(raw: str) -> str:
    cat = (raw or "").lower().strip()
    cat = re.sub(r"^musu?e?ums?$", "museum", cat)
    if cat in {"restaurant", "cafe", "eatery"}:
        return "food"
    if cat in {"outdoors"}:
        return "outdoor"
    if cat == "shopping":
        return "market"  # shopping ↔ market in itinerary categories
    return cat


def _parse_balance_categories(
    message: str, *, day: int, default_block: str | None
) -> EditPatch | None:
    """Parse 'make day 3 a balance of museum and food' / 'balance museum and food'."""
    lower = message.lower()
    if not re.search(r"\b(balance|balanced|mix)\b", lower):
        return None
    cats = re.findall(
        rf"\b({_EDIT_CATEGORIES}|musu?e?ums?)\b",
        lower,
    )
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in cats:
        cat = _normalize_edit_category(raw)
        if cat in {"outdoor", "outdoors"}:
            cat = "park"
        if cat not in seen:
            seen.add(cat)
            normalized.append(cat)
    if len(normalized) < 2:
        return None
    return EditPatch(
        target=EditTarget(day=day, block=default_block),  # type: ignore[arg-type]
        operation="balance_categories",
        payload={"categories": normalized[:3]},
        user_utterance=message,
    )


def _parse_clause_patch(
    clause: str,
    *,
    day: int,
    full_message: str,
    default_block: str | None = None,
) -> EditPatch | None:
    """Parse one edit clause into a patch (inherits day from the full utterance)."""
    lower = clause.lower().strip()
    if not lower:
        return None
    block = _clause_block(clause) or default_block

    # "remove market from day one" / "drop shopping stops" / "without food"
    remove_m = re.search(
        rf"\b(remove|drop|delete|exclude|cut out|take out|without)\b"
        rf".{{0,50}}\b({_EDIT_CATEGORIES})s?\b",
        lower,
    )
    # Don't steal "remove/cut travel" — that is reduce_travel.
    if remove_m and not re.search(
        r"\b(remove|drop|cut|delete).{0,24}\b(travel|walking)\b", lower
    ):
        category = _normalize_edit_category(remove_m.group(2))
        return EditPatch(
            target=EditTarget(day=day, block=block),  # type: ignore[arg-type]
            operation="trim_category",
            payload={"category": category, "keep": 0},
            user_utterance=full_message,
        )

    # "from 2 food stops to one" / "2 food stops to 1"
    trim_m = re.search(
        r"(?:from\s+)?(\d+|one|two|three|four)\s+"
        r"(food|restaurant|cafe|eatery|heritage|museum|market|temple|park)?\s*"
        r"stops?\s+to\s+(?:only\s+|just\s+)?(\d+|one|two|three|four|a\s+single)\b",
        lower,
    )
    if trim_m:
        keep = _parse_count_token(trim_m.group(3)) or 1
        category = _normalize_edit_category(trim_m.group(2) or "food")
        return EditPatch(
            target=EditTarget(day=day, block=block),  # type: ignore[arg-type]
            operation="trim_category",
            payload={"category": category, "keep": keep},
            user_utterance=full_message,
        )

    trim_m2 = re.search(
        rf"\b(?:only|just|keep)\s+(\d+|one|two|three|four)\s+"
        rf"({_EDIT_CATEGORIES})\s+stops?\b",
        lower,
    )
    if trim_m2:
        keep = _parse_count_token(trim_m2.group(1)) or 1
        category = _normalize_edit_category(trim_m2.group(2))
        return EditPatch(
            target=EditTarget(day=day, block=block),  # type: ignore[arg-type]
            operation="trim_category",
            payload={"category": category, "keep": keep},
            user_utterance=full_message,
        )

    if re.search(
        r"\b(fewer|less|reduce|cut)\s+(food|restaurant|cafe)\s+stops?\b",
        lower,
    ) or re.search(r"\bone\s+food\s+stop\b", lower):
        return EditPatch(
            target=EditTarget(day=day, block=block),  # type: ignore[arg-type]
            operation="trim_category",
            payload={"category": "food", "keep": 1},
            user_utterance=full_message,
        )

    if re.search(
        r"\b(add|include).{0,40}\b(outdoor|outdoors|park|garden|nature|viewpoint)\b",
        lower,
    ) or re.search(r"\bsomething\s+outdoor\b", lower):
        return EditPatch(
            target=EditTarget(day=day, block=block),  # type: ignore[arg-type]
            operation="add_stop",
            payload={"category": "outdoor"},
            user_utterance=full_message,
        )

    if re.search(
        r"\b(add|include).{0,40}\b(food|restaurant|cafe|eatery|local food)\b",
        lower,
    ) or re.search(r"\bfamous local food\b", lower):
        return EditPatch(
            target=EditTarget(day=day, block=block or "morning"),  # type: ignore[arg-type]
            operation="add_stop",
            payload={"category": "food"},
            user_utterance=full_message,
        )

    # "add a shopping stop on day two" / "include a market on day 3"
    add_cat = re.search(
        rf"\b(add|include)\b.{{0,40}}\b({_EDIT_CATEGORIES})s?\b"
        rf"(?:\s+stops?)?",
        lower,
    )
    if add_cat:
        category = _normalize_edit_category(add_cat.group(2))
        return EditPatch(
            target=EditTarget(day=day, block=block),  # type: ignore[arg-type]
            operation="add_stop",
            payload={"category": category},
            user_utterance=full_message,
        )

    if re.search(
        r"\b(reduce travel|less travel|less walking|cut travel|too much travel|"
        r"cluster)\b",
        lower,
    ):
        return EditPatch(
            target=EditTarget(day=day, block=block),  # type: ignore[arg-type]
            operation="reduce_travel",
            payload={},
            user_utterance=full_message,
        )

    if re.search(r"\b(indoor|indoors)\b", lower):
        return EditPatch(
            target=EditTarget(day=day, block=block or "evening"),  # type: ignore[arg-type]
            operation="make_indoor",
            payload={},
            user_utterance=full_message,
        )

    # "more packed" must win over bare "packed"/"less packed" relax cues.
    if re.search(
        r"\b(more packed|pack(?:ed)?er|busier|more busy|more stops|"
        r"fill (the )?day|make .{0,20}packed)\b",
        lower,
    ) or (
        re.search(r"\bpacked\b", lower)
        and not re.search(r"\b(less|more\s+relaxed|relax)\b", lower)
    ):
        return EditPatch(
            target=EditTarget(day=day, block=block),  # type: ignore[arg-type]
            operation="pack_block",
            payload={},
            user_utterance=full_message,
        )

    # "more balanced" / moderate day pacing (not "balance museum and food").
    if re.search(
        r"\b(more balanced|make .{0,24}balanced|balanced(?:\s+pace)?|"
        r"moderate(?:\s+pace)?|less packed but not relax(?:ed)?)\b",
        lower,
    ) or (
        re.search(r"\bbalanced\b", lower)
        and not re.search(
            rf"\b(balance|mix)\b.{{0,40}}\b({_EDIT_CATEGORIES})\b.{{0,20}}"
            rf"\b(and|&)\b.{{0,20}}\b({_EDIT_CATEGORIES})\b",
            lower,
        )
    ):
        return EditPatch(
            target=EditTarget(day=day, block=block),  # type: ignore[arg-type]
            operation="balance_block",
            payload={},
            user_utterance=full_message,
        )

    if re.search(r"\b(relax|more relaxed|less packed|slow(er)?)\b", lower):
        return EditPatch(
            target=EditTarget(day=day, block=block),  # type: ignore[arg-type]
            operation="relax_block",
            payload={},
            user_utterance=full_message,
        )

    return None


def _parse_edits(message: str) -> list[EditPatch]:
    """Parse one or more scoped edits (compound 'and' instructions supported)."""
    day_matched, day = _parse_target_day(message)
    default_block = _clause_block(message)
    # Balance "museum and food" must parse on the whole utterance — clause
    # splitting on 'and' would break the category pair.
    balance = _parse_balance_categories(
        message, day=day, default_block=default_block
    )
    if balance is not None:
        return [balance]

    clauses = _split_edit_clauses(message)
    patches: list[EditPatch] = []
    seen: set[tuple[str, str, str]] = set()

    for clause in clauses:
        # Prefer day mentioned inside this clause when present.
        clause_day_m, clause_day = _parse_target_day(clause)
        use_day = clause_day if clause_day_m is not None else day
        patch = _parse_clause_patch(
            clause,
            day=use_day,
            full_message=message,
            default_block=default_block,
        )
        if patch is None:
            continue
        # No day named → auto-pick heaviest-travel day for reduce-travel edits.
        day_was_named = clause_day_m is not None or day_matched is not None
        if patch.operation == "reduce_travel" and not day_was_named:
            patch = patch.model_copy(
                update={
                    "payload": {**(patch.payload or {}), "auto_day": True},
                }
            )
        key = (
            patch.operation,
            str(patch.target.day),
            str((patch.payload or {}).get("category") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        patches.append(patch)

    # Whole-utterance fallbacks when clause split missed a combined pattern.
    if not patches:
        whole = _parse_clause_patch(
            message, day=day, full_message=message, default_block=default_block
        )
        if whole:
            if whole.operation == "reduce_travel" and day_matched is None:
                whole = whole.model_copy(
                    update={
                        "payload": {**(whole.payload or {}), "auto_day": True},
                    }
                )
            patches.append(whole)
        elif day_matched is not None and re.search(
            r"\b(more packed|pack(?:ed)?er|busier|more busy|packed)\b",
            message.lower(),
        ) and not re.search(r"\b(less packed|relax|more relaxed)\b", message.lower()):
            patches.append(
                EditPatch(
                    target=EditTarget(day=day, block=default_block),  # type: ignore[arg-type]
                    operation="pack_block",
                    payload={},
                    user_utterance=message,
                )
            )
        elif day_matched is not None and re.search(
            r"\b(more balanced|balanced|moderate)\b",
            message.lower(),
        ) and not re.search(
            rf"\b(balance|mix)\b.{{0,40}}\b({_EDIT_CATEGORIES})\b.{{0,20}}"
            rf"\b(and|&)\b.{{0,20}}\b({_EDIT_CATEGORIES})\b",
            message.lower(),
        ):
            patches.append(
                EditPatch(
                    target=EditTarget(day=day, block=default_block),  # type: ignore[arg-type]
                    operation="balance_block",
                    payload={},
                    user_utterance=message,
                )
            )
        elif day_matched is not None and re.search(
            r"\b(relax|more relaxed|less packed|slow(er)?)\b",
            message.lower(),
        ):
            # Only fall back to relax when the user actually asked for it.
            patches.append(
                EditPatch(
                    target=EditTarget(day=day, block=default_block),  # type: ignore[arg-type]
                    operation="relax_block",
                    payload={},
                    user_utterance=message,
                )
            )

    return patches


def _parse_edit(message: str) -> EditPatch | None:
    """Backward-compatible single-patch parse."""
    patches = _parse_edits(message)
    return patches[0] if patches else None


def _merge_trip(
    existing: TripConstraints | None,
    *,
    city: str | None,
    days: int | None,
    pace: Pace | None,
    interests: list[str],
    confirmed: bool | None = None,
    message: str | None = None,
    scope_note: str | None = None,
    days_known: bool = False,
    pace_known: bool = False,
    interests_known: bool = False,
) -> TripConstraints | None:
    base_city = city or (existing.city if existing else None) or default_city()
    if not is_city_allowed(base_city):
        base_city = default_city()
    info = resolve_city(base_city)
    if info is None:
        return None

    # Only reuse slots the user already answered in this clarify thread.
    known_interests = (
        list(existing.interests)
        if existing and existing.interests_known and existing.interests
        else None
    )
    known_pace = (
        existing.pace if existing and existing.pace_known and existing.pace else None
    )
    prefs = resolve_trip_preferences(
        message or "",
        explicit_pace=pace,
        existing_profile=(
            None
            if (message and clear_traveler_profile_request(message))
            else (
                existing.traveler_profile
                if existing and not existing.confirmed
                else None
            )
        ),
        existing_interests=interests if interests_known and interests else known_interests,
        existing_constraints=(
            list(existing.constraints) if existing and not existing.confirmed else None
        ),
        existing_pace=pace or known_pace,
        existing_window=existing.daily_time_window_min if existing else None,
    )

    # Days: only set when user stated or already known
    if days is not None:
        num_days: int | None = clamp_trip_days(days)
        days_known_v = True
    elif existing and existing.days_known and existing.num_days is not None:
        num_days = clamp_trip_days(existing.num_days)
        days_known_v = True
    else:
        num_days = existing.num_days if existing else None
        days_known_v = bool(existing.days_known) if existing else False
    if days_known:
        days_known_v = True

    # Interests: only what was stated (or kept from prior known answers)
    if prefs.get("interests_from_message") or interests_known:
        ints = list(prefs["interests"]) or list(interests)
        interests_known_v = bool(ints)
    elif known_interests:
        ints = list(known_interests)
        interests_known_v = True
    else:
        # Named audience profiles may seed interests; never invent for "general".
        ints = list(prefs["interests"]) if prefs.get("profile_detected") else []
        interests_known_v = bool(ints) and bool(prefs.get("profile_detected"))

    # Pace: never invent relaxed/moderate/packed
    if pace is not None or prefs.get("pace_from_message"):
        pace_v: Pace | None = pace or prefs.get("pace")
        pace_known_v = pace_v is not None
    elif known_pace is not None:
        pace_v = known_pace
        pace_known_v = True
    else:
        pace_v = None
        pace_known_v = False
    if pace_known and pace_v is not None:
        pace_known_v = True

    constraints = list(prefs["constraints"])
    if scope_note:
        note = f"scope: {scope_note}"
        if note not in constraints:
            constraints.insert(0, note)

    if confirmed is None:
        conf = existing.confirmed if existing else False
    else:
        conf = confirmed

    clarify_turns = existing.clarify_turns if existing else 0

    start = _find_start_date(message or "") if message else None
    end = None
    dates_known_v = bool(existing.dates_known) if existing else False
    if start is not None:
        dates_known_v = True
        if num_days:
            end = start + timedelta(days=max(0, int(num_days) - 1))
    elif message and _dates_flexible(message):
        dates_known_v = True
        start = existing.start_date if existing else None
        end = existing.end_date if existing else None
    elif existing:
        start = existing.start_date
        end = existing.end_date

    return TripConstraints(
        city=info.name,
        country="India",
        num_days=num_days,
        start_date=start,
        end_date=end,
        interests=ints,
        pace=pace_v,
        traveler_profile=prefs["traveler_profile"],
        constraints=constraints,
        daily_time_window_min=int(prefs["daily_time_window_min"]),
        confirmed=conf,
        clarify_turns=clarify_turns,
        days_known=days_known_v,
        pace_known=pace_known_v,
        interests_known=interests_known_v,
        dates_known=dates_known_v,
    )


def _trip_audience_phrase(trip: TripConstraints) -> str:
    """Summary of *user-stated* slots only — never invent missing ones in copy."""
    bits: list[str] = []
    if trip.days_known and trip.num_days:
        bits.append(f"{trip.num_days} days in {trip.city}")
    else:
        bits.append(trip.city)
    label = profile_label(trip.traveler_profile)
    if label:
        bits.append(label)
    if trip.pace_known and trip.pace:
        # Surface "balanced" in confirm copy when pace is moderate
        pace_label = "balanced" if trip.pace == "moderate" else trip.pace
        bits.append(pace_label)
    if trip.interests_known and trip.interests:
        bits.append(", ".join(trip.interests))
    return "; ".join(bits)


def _find_start_date(message: str) -> date | None:
    """Parse a trip start date from free text when possible."""
    lower = (message or "").lower().strip()
    if not lower:
        return None
    m = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", lower)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    months = {
        "jan": 1,
        "january": 1,
        "feb": 2,
        "february": 2,
        "mar": 3,
        "march": 3,
        "apr": 4,
        "april": 4,
        "may": 5,
        "jun": 6,
        "june": 6,
        "jul": 7,
        "july": 7,
        "aug": 8,
        "august": 8,
        "sep": 9,
        "sept": 9,
        "september": 9,
        "oct": 10,
        "october": 10,
        "nov": 11,
        "november": 11,
        "dec": 12,
        "december": 12,
    }
    m = re.search(
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
        r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
        r"nov(?:ember)?|dec(?:ember)?)\s+(20\d{2})\b",
        lower,
    )
    if m:
        mon = months.get(m.group(2)) or months.get(m.group(2)[:3])
        if mon:
            try:
                return date(int(m.group(3)), mon, int(m.group(1)))
            except ValueError:
                pass
    m = re.search(
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
        r"nov(?:ember)?|dec(?:ember)?)\s+(\d{1,2})(?:st|nd|rd|th)?[, ]+"
        r"(20\d{2})\b",
        lower,
    )
    if m:
        mon = months.get(m.group(1)) or months.get(m.group(1)[:3])
        if mon:
            try:
                return date(int(m.group(3)), mon, int(m.group(2)))
            except ValueError:
                pass
    return None


def _dates_flexible(message: str) -> bool:
    lower = (message or "").lower()
    return bool(
        re.search(
            r"\b(flexible|no date|skip date|any date|don'?t know|"
            r"not sure|whenever|no set date)\b",
            lower,
        )
    )


def _yes_no(message: str) -> bool | None:
    lower = (message or "").lower().strip()
    if re.search(
        r"\b(yes|yeah|yep|yup|confirm|sure|ok(?:ay)?|go ahead|please do|"
        r"do it|sounds good|absolutely)\b",
        lower,
    ):
        return True
    if re.search(
        r"\b(no|nope|nah|don'?t|do not|skip|leave (it|them)|not now|"
        r"no thanks|cancel)\b",
        lower,
    ):
        return False
    return None


def _monsoon_visit_answer(message: str) -> bool | None:
    """Yes/no for monsoon-month visit; dates in Jun–Sep count as yes."""
    yn = _yes_no(message)
    if yn is not None:
        return yn
    lower = (message or "").lower()
    # Named monsoon months without requiring a full date.
    if re.search(
        r"\b(june|july|august|september|jun|jul|aug|sep|sept)\b", lower
    ):
        return True
    if re.search(
        r"\b(january|february|march|april|may|october|november|december|"
        r"jan|feb|mar|apr|oct|nov|dec)\b",
        lower,
    ):
        return False
    start = _find_start_date(message)
    if start is not None:
        return 6 <= start.month <= 9
    return None


def _outdoor_stops_on_day(itin: Any, day_index: int) -> list[str]:
    from agent.nodes.edit_apply import OUTDOOR_CATEGORIES

    day = next((d for d in (itin.days if itin else []) if d.day_index == day_index), None)
    if day is None:
        return []
    names: list[str] = []
    for s in day.all_stops:
        cat = (s.category or "").lower()
        if cat in OUTDOOR_CATEGORIES or "park" in s.name.lower() or "garden" in s.name.lower():
            names.append(s.name)
    return names


def _missing_slot_question(trip: TripConstraints) -> str | None:
    """Ask exactly one clarifying question for the next missing required slot."""
    if not trip.days_known or trip.num_days is None:
        return (
            f"How many days should I plan in {trip.city} "
            f"({MIN_TRIP_DAYS}–{MAX_TRIP_DAYS})?"
        )
    if not trip.pace_known or trip.pace is None:
        return "Do you prefer a relaxed, balanced, or packed schedule?"
    if not trip.interests_known or not trip.interests:
        return (
            "To personalize your trip, tell me what you enjoy "
            f"(e.g. {primary_interests_prompt()})."
        )
    if not trip.dates_known:
        return (
            "What date do you plan to start your trip? "
            "(e.g. 2026-08-12 or 12 August 2026). "
            "Say “flexible” if dates aren’t set yet."
        )
    return None


def _clarify_count(state: GraphState, trip: TripConstraints | None = None) -> int:
    if trip is not None:
        return int(trip.clarify_turns or 0)
    msgs = state.get("messages") or []
    n = 0
    for m in msgs:
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        if isinstance(content, str) and "?" in content:
            n += 1
    return min(n, MAX_CLARIFY)


def _trace_append(state: GraphState, entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a delta list (concat reducer merges into state.agent_trace)."""
    del state  # prior entries already in graph state via concat_trace
    return [entry]


def _dispatch_from_waves(
    waves: list[list[str]],
    *,
    success_criteria: list[str] | None = None,
    plan_reason: str | None = None,
    **extra: Any,
) -> dict:
    flat = flatten_waves(waves)  # type: ignore[arg-type]
    criteria = success_criteria or success_criteria_for_waves(waves)
    plan = DispatchPlan(
        agent_sequence=flat,  # type: ignore[arg-type]
        agent_waves=waves,  # type: ignore[arg-type]
        success_criteria=list(criteria),
        plan_reason=plan_reason,
        **extra,
    ).sync_flags_from_sequence()
    return dump(plan)


def _flatten_remaining(waves: list[list[str]]) -> list[str]:
    return [a for wave in waves for a in wave]


def _start_agent_loop(
    state: GraphState,
    *,
    waves: list[list[str]],
    planner: str,
    intent: str,
    trip: TripConstraints | None,
    user_reply: str,
    edit_patch: EditPatch | None = None,
    edit_patches: list[EditPatch] | None = None,
    success_criteria: list[str] | None = None,
    plan_reason: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    criteria = success_criteria or success_criteria_for_waves(waves)
    patches = list(edit_patches or [])
    if edit_patch is not None and not patches:
        patches = [edit_patch]
    primary = patches[0] if patches else edit_patch
    if not waves or not any(waves):
        return {
            "safety_status": "ok",
            "intent": intent,
            "ready_for_synthesis": True,
            "ready_for_merger": True,  # compat alias
            "next_agent": None,
            "next_agents": [],
            "pending_agents": [],
            "pending_waves": [],
            "orchestration_started": True,
            "user_reply": user_reply,
            "trip_constraints": dump(trip) if trip else state.get("trip_constraints"),
            "dispatch_plan": _dispatch_from_waves(
                [], success_criteria=criteria, plan_reason=plan_reason
            ),
            "agent_trace": _trace_append(
                state,
                {
                    "agent": "orchestrator",
                    "planner": planner,
                    "waves": [],
                    "success_criteria": criteria,
                    "action": "finalize",
                },
            ),
        }

    first = list(waves[0])
    rest = [list(w) for w in waves[1:]]
    # Belt-and-suspenders: strip knowledge_agent from plan/edit dispatch.
    if intent != "explain":
        first = [a for a in first if a != "knowledge_agent"]
        rest = [[a for a in w if a != "knowledge_agent"] for w in rest]
        rest = [w for w in rest if w]
        if not first and rest:
            first = list(rest[0])
            rest = rest[1:]
        criteria = [c for c in criteria if c != "citations_present"]
    if not first:
        return {
            "safety_status": "ok",
            "intent": intent,
            "ready_for_synthesis": True,
            "ready_for_merger": True,
            "next_agent": None,
            "next_agents": [],
            "pending_agents": [],
            "pending_waves": [],
            "orchestration_started": True,
            "user_reply": user_reply,
            "trip_constraints": dump(trip) if trip else state.get("trip_constraints"),
            "dispatch_plan": _dispatch_from_waves(
                [], success_criteria=criteria, plan_reason=plan_reason
            ),
            "knowledge_results": None if intent != "explain" else state.get("knowledge_results"),
            "agent_trace": _trace_append(
                state,
                {
                    "agent": "orchestrator",
                    "planner": planner,
                    "waves": [],
                    "success_criteria": criteria,
                    "action": "finalize",
                },
            ),
        }
    plan_kwargs: dict[str, Any] = {}
    if primary is not None:
        plan_kwargs["edit_patch"] = dump(primary)
    cleaned_waves = [first, *rest] if first else []
    out: dict[str, Any] = {
        "safety_status": "ok",
        "intent": intent,
        "orchestration_started": True,
        "ready_for_synthesis": False,
        "ready_for_merger": False,
        "next_agent": first[0] if len(first) == 1 else None,
        "next_agents": first,
        "pending_agents": _flatten_remaining(rest),
        "pending_waves": rest,
        "orchestrator_steps": int(state.get("orchestrator_steps") or 0) + 1,
        "trip_constraints": dump(trip) if trip else state.get("trip_constraints"),
        "dispatch_plan": _dispatch_from_waves(
            cleaned_waves,
            success_criteria=criteria,
            plan_reason=plan_reason or f"planner={planner}",
            **plan_kwargs,
        ),
        "user_reply": user_reply,
        "agent_trace": _trace_append(
            state,
            {
                "agent": "orchestrator",
                "planner": planner,
                "waves": cleaned_waves,
                "success_criteria": criteria,
                "action": f"dispatch_wave:{','.join(first)}",
                "execution_plan": {
                    "wave1": cleaned_waves[0] if cleaned_waves else [],
                    **{f"wave{i+1}": w for i, w in enumerate(cleaned_waves)},
                    "success_criteria": criteria,
                },
            },
        ),
        "reviewer_verdict": None,
    }
    if primary is not None:
        out["edit_patch"] = dump(primary)
    if patches:
        out["edit_patches"] = [dump(p) for p in patches]
    # Plan/edit: clear stale RAG so Synthesis References stay POI/weather/travel only.
    if intent in {"plan", "edit"}:
        out["knowledge_results"] = None
    if extra:
        out.update(extra)
    else:
        out["revision_feedback"] = None
    if extra is not None and "revision_feedback" not in extra:
        out["revision_feedback"] = None
    logger.info(
        "NODE orchestrator START plan planner=%s waves=%s criteria=%s next_wave=%s",
        planner,
        waves,
        criteria,
        first,
    )
    return out


def _finalize_to_synthesis(state: GraphState, steps: int, *, reason: str) -> dict[str, Any]:
    logger.info("NODE orchestrator PROCEED → synthesis (%s)", reason)
    return {
        "ready_for_synthesis": True,
        "ready_for_merger": True,
        "next_agent": None,
        "next_agents": [],
        "pending_agents": [],
        "pending_waves": [],
        "orchestrator_steps": steps,
        "agent_trace": _trace_append(
            state,
            {
                "agent": "orchestrator",
                "action": "artifacts_complete",
                "reason": reason,
            },
        ),
    }


def _continue_agent_loop(state: GraphState) -> dict[str, Any]:
    steps = int(state.get("orchestrator_steps") or 0) + 1
    if steps > MAX_ORCHESTRATOR_STEPS:
        logger.warning("NODE orchestrator step cap reached → synthesis")
        return _finalize_to_synthesis(state, steps, reason="step_cap")

    pending_waves: list[list[str]] = [
        list(w) for w in (state.get("pending_waves") or [])
    ]
    if not pending_waves and state.get("pending_agents"):
        pending_waves = [[a] for a in (state.get("pending_agents") or [])]

    prev_wave = list(state.get("next_agents") or [])
    if not prev_wave and state.get("next_agent"):
        prev_wave = [str(state.get("next_agent"))]

    plan = as_dispatch(state.get("dispatch_plan"))
    criteria = list(plan.success_criteria or []) or success_criteria_for_waves(
        [prev_wave, *pending_waves] if prev_wave else pending_waves
    )

    trace = _trace_append(
        state,
        {
            "agent": "orchestrator",
            "action": "wave_returned",
            "from": prev_wave,
            "pending_waves": [list(w) for w in pending_waves],
        },
    )

    # Adaptive: after POI, ensure itinerary queued for plan intent
    if (
        "poi_agent" in prev_wave
        and state.get("poi_results")
        and not state.get("itinerary_draft")
        and (state.get("intent") or "plan") == "plan"
    ):
        flat_pending = _flatten_remaining(pending_waves)
        if "itinerary_agent" not in flat_pending:
            if pending_waves:
                # Keep travel before itinerary if present
                inserted = False
                for i, wave in enumerate(pending_waves):
                    if "travel_time_agent" in wave:
                        pending_waves.insert(i + 1, ["itinerary_agent"])
                        inserted = True
                        break
                if not inserted:
                    pending_waves.append(["itinerary_agent"])
            else:
                pending_waves = [["itinerary_agent"]]

    if pending_waves:
        nxt = list(pending_waves[0])
        rest = [list(w) for w in pending_waves[1:]]
        intent_now = state.get("intent") or "plan"
        if intent_now != "explain":
            nxt = [a for a in nxt if a != "knowledge_agent"]
            rest = [[a for a in w if a != "knowledge_agent"] for w in rest]
            rest = [w for w in rest if w]
            while not nxt and rest:
                nxt = list(rest[0])
                rest = rest[1:]
            if not nxt:
                # No more non-RAG waves — fall through to completion check.
                pending_waves = []
            else:
                pending_waves = [nxt, *rest]
        if nxt:
            logger.info(
                "NODE orchestrator NEXT wave=%s remaining_waves=%s", nxt, rest
            )
            return {
                "ready_for_synthesis": False,
                "ready_for_merger": False,
                "next_agent": nxt[0] if len(nxt) == 1 else None,
                "next_agents": nxt,
                "pending_agents": _flatten_remaining(rest),
                "pending_waves": rest,
                "orchestrator_steps": steps,
                "agent_trace": trace
                + [
                    {
                        "agent": "orchestrator",
                        "action": f"dispatch_wave:{','.join(nxt)}",
                    }
                ],
            }

    # Waves done — completion check (not a bare flag)
    ok, missing = artifacts_complete(dict(state), criteria)
    if not ok:
        intent_now = state.get("intent") or "plan"
        refill = agents_for_missing_criteria(missing)
        if intent_now != "explain":
            refill = [a for a in refill if a != "knowledge_agent"]
            missing = [m for m in missing if m != "citations_present"]
            ok = len(missing) == 0
        if not ok:
            logger.info(
                "NODE orchestrator artifacts incomplete missing=%s → redispatch %s",
                missing,
                refill,
            )
            if refill and steps < MAX_ORCHESTRATOR_STEPS:
                nxt = [refill[0]]
                rest = [[a] for a in refill[1:]]
                return {
                    "ready_for_synthesis": False,
                    "ready_for_merger": False,
                    "next_agent": nxt[0],
                    "next_agents": nxt,
                    "pending_agents": _flatten_remaining(rest),
                    "pending_waves": rest,
                    "orchestrator_steps": steps,
                    "agent_trace": trace
                    + [
                        {
                            "agent": "orchestrator",
                            "action": "fill_missing_artifacts",
                            "missing": missing,
                            "dispatch": refill,
                        }
                    ],
                }
            # Cap / cannot fill — proceed anyway and let Reviewer catch issues
            return {
                **_finalize_to_synthesis(
                    state, steps, reason=f"incomplete:{','.join(missing)}"
                ),
                "agent_trace": trace
                + [
                    {
                        "agent": "orchestrator",
                        "action": "artifacts_incomplete_proceed",
                        "missing": missing,
                    }
                ],
            }

    return {
        **_finalize_to_synthesis(state, steps, reason="all_success_criteria_met"),
        "agent_trace": trace
        + [
            {
                "agent": "orchestrator",
                "action": "artifacts_complete",
                "success_criteria": criteria,
            }
        ],
    }


def _rain_outdoor_followup(
    *,
    state: GraphState,
    existing_trip: TripConstraints | None,
    prev: Any,
    rain_day: int,
) -> dict[str, Any]:
    """After a rain day is chosen: check outdoor stops and offer an indoor swap."""
    trip = existing_trip or (prev.trip if prev else None)

    def _base(**extra: Any) -> dict[str, Any]:
        out: dict[str, Any] = {
            "safety_status": "ok",
            "intent": "explain",
            "ready_for_merger": False,
            "next_agent": None,
            "orchestration_started": False,
            "dispatch_plan": _dispatch_from_waves([]),
            "trip_constraints": dump(trip) if trip else None,
            "previous_itinerary": dump(prev) if prev else None,
            "merged_itinerary": dump(prev) if prev else None,
            "sources": [],
        }
        out.update(extra)
        return out

    weather_offer = (
        " Would you like me to look up the Weather MCP forecast for a "
        "particular date? If yes, tell me the date (YYYY-MM-DD)."
    )
    outdoors = _outdoor_stops_on_day(prev, rain_day) if prev else []
    if not outdoors:
        return _base(
            pending_dialog={"type": "weather_date_ask", "day": rain_day},
            user_reply=(
                f"Day {rain_day} looks indoor-friendly already "
                "(no park/outdoor stops), so rain shouldn’t disrupt that day."
                + weather_offer
            ),
            agent_trace=_trace_append(
                state,
                {
                    "agent": "orchestrator",
                    "action": "rain_indoor_ok",
                    "day": rain_day,
                },
            ),
        )
    return _base(
        safety_status="needs_clarify",
        pending_dialog={
            "type": "rain_swap_offer",
            "day": rain_day,
            "outdoor_stops": outdoors,
        },
        user_reply=(
            f"Your Day {rain_day} itinerary has outdoor place(s): "
            f"{', '.join(outdoors)}. "
            "I can change those outdoor stops to indoor places "
            "(museums/cafes) and keep everything else the same — should I?"
        ),
        agent_trace=_trace_append(
            state,
            {
                "agent": "orchestrator",
                "action": "rain_swap_offer",
                "day": rain_day,
                "outdoor_stops": outdoors,
            },
        ),
    )


def _begin_rain_monsoon_dialog(
    state: GraphState,
    message: str,
    existing_trip: TripConstraints | None,
    prev: Any,
) -> dict[str, Any] | None:
    """Start rain check: ask for day, then outdoor swap offer (no monsoon Q)."""
    if prev is None:
        return {
            "safety_status": "needs_clarify",
            "intent": "explain",
            "ready_for_merger": False,
            "next_agent": None,
            "orchestration_started": False,
            "pending_dialog": None,
            "user_reply": (
                "I can check rain risk once we have an itinerary. "
                f"Let’s plan a {SCOPED_CITY} trip first."
            ),
            "dispatch_plan": _dispatch_from_waves([]),
        }
    day_m, rain_day = _parse_target_day(message)
    if day_m is None:
        return {
            "safety_status": "needs_clarify",
            "intent": "explain",
            "ready_for_merger": False,
            "next_agent": None,
            "orchestration_started": False,
            "trip_constraints": dump(existing_trip or prev.trip),
            "previous_itinerary": dump(prev),
            "merged_itinerary": dump(prev),
            "pending_dialog": {"type": "rain_day_ask"},
            "user_reply": (
                "Please say which day for the rain check "
                "(e.g. Day 1, Day 2)."
            ),
            "dispatch_plan": _dispatch_from_waves([]),
        }
    if not any(d.day_index == rain_day for d in prev.days):
        avail = ", ".join(f"Day {d.day_index}" for d in prev.days)
        return {
            "safety_status": "needs_clarify",
            "intent": "explain",
            "ready_for_merger": False,
            "next_agent": None,
            "orchestration_started": False,
            "trip_constraints": dump(existing_trip or prev.trip),
            "previous_itinerary": dump(prev),
            "merged_itinerary": dump(prev),
            "pending_dialog": {"type": "rain_day_ask"},
            "user_reply": (
                f"Day {rain_day} isn’t in this plan ({avail}). "
                "Which day should I check for rain?"
            ),
            "dispatch_plan": _dispatch_from_waves([]),
        }

    return _rain_outdoor_followup(
        state=state,
        existing_trip=existing_trip,
        prev=prev,
        rain_day=rain_day,
    )


def _is_pending_dialog_escape(message: str, kind: str) -> bool:
    """True when the user asked a new request instead of answering the dialog."""
    lower = (message or "").lower().strip()
    if not lower:
        return False
    # Keep yes/no / dated / day answers inside the current dialog.
    if _yes_no(message) is not None:
        return False
    if kind in {"rain_day_ask", "rain_monsoon_ask", "rain_swap_offer"}:
        day_m, _ = _parse_target_day(message)
        if day_m is not None and kind == "rain_day_ask":
            return False
        # Bare "Day N" while answering monsoon/swap is also not escape.
        if day_m is not None and re.fullmatch(
            r"(?:day\s*)?(?:[1-4]|one|two|three|four|first|second|third|fourth)",
            lower,
        ):
            return False
        # Month/date answers belong to the monsoon yes/no step.
        if kind == "rain_monsoon_ask" and _monsoon_visit_answer(message) is not None:
            return False
    if kind == "weather_date_ask":
        if _find_start_date(message) is not None:
            return False
        if re.search(r"\b(skip|later|not now)\b", lower):
            return False
    # New work: doability, edits, hours/tips, explicit weather Q.
    if _is_knowledge_query(message) or _is_weather_query(message):
        return True
    if re.search(
        r"\b("
        r"doable|feasible|too (?:much|packed)|can (?:i|we) (?:do|finish)|"
        r"swap|add|remove|include|drop|replace|change|edit|"
        r"make .{0,20}(?:packed|relax|relaxed|balanced)|"
        r"pack(?:ed)?|relax(?:ed)?|outdoor|indoor|food|museum|shopping|"
        r"opening hours?|hours? for|why (?:did|do) you|tell me (?:more )?about"
        r")\b",
        lower,
    ):
        return True
    return False


def _continue_rain_after_day(
    state: GraphState,
    message: str,
    existing_trip: TripConstraints | None,
    prev: Any,
    rain_day: int,
) -> dict[str, Any]:
    """Continue rain check once the day is known — outdoor swap, no monsoon Q."""
    del message
    return _rain_outdoor_followup(
        state=state,
        existing_trip=existing_trip,
        prev=prev,
        rain_day=rain_day,
    )


def _continue_pending_dialog(
    state: GraphState,
    message: str,
    existing_trip: TripConstraints | None,
    prev: Any,
    pending: dict[str, Any],
) -> dict[str, Any] | None:
    """Handle yes/no follow-ups for rain / weather dialogs."""
    kind = str(pending.get("type") or "")
    rain_day = int(pending.get("day") or 1)
    trip = existing_trip or (prev.trip if prev else None)

    def _base(**extra: Any) -> dict[str, Any]:
        out: dict[str, Any] = {
            "safety_status": "ok",
            "intent": "explain",
            "ready_for_merger": False,
            "next_agent": None,
            "orchestration_started": False,
            "dispatch_plan": _dispatch_from_waves([]),
            "trip_constraints": dump(trip) if trip else None,
            "previous_itinerary": dump(prev) if prev else state.get("previous_itinerary"),
            "merged_itinerary": dump(prev) if prev else state.get("merged_itinerary"),
        }
        out.update(extra)
        return out

    weather_offer = (
        " Would you like me to look up the Weather MCP forecast for a "
        "particular date? If yes, tell me the date (YYYY-MM-DD)."
    )

    if kind == "rain_day_ask":
        day_m, parsed = _parse_target_day(message)
        if day_m is None:
            return _base(
                safety_status="needs_clarify",
                pending_dialog={"type": "rain_day_ask"},
                user_reply=(
                    "Please say which day for the rain check "
                    "(e.g. Day 1, Day 2)."
                ),
            )
        if prev is None:
            return _base(
                pending_dialog=None,
                user_reply=(
                    "I can check rain risk once we have an itinerary. "
                    f"Let’s plan a {SCOPED_CITY} trip first."
                ),
            )
        if not any(d.day_index == parsed for d in prev.days):
            avail = ", ".join(f"Day {d.day_index}" for d in prev.days)
            return _base(
                safety_status="needs_clarify",
                pending_dialog={"type": "rain_day_ask"},
                user_reply=(
                    f"Day {parsed} isn’t in this plan ({avail}). "
                    "Which day should I check for rain?"
                ),
            )
        return _continue_rain_after_day(
            state, message, existing_trip, prev, parsed
        )

    if kind == "rain_monsoon_ask":
        # Legacy sticky state from older builds — skip monsoon Q, jump to outdoor check.
        return _rain_outdoor_followup(
            state=state,
            existing_trip=existing_trip,
            prev=prev,
            rain_day=rain_day,
        )

    if kind == "rain_swap_offer":
        yn = _yes_no(message)
        if yn is None:
            return _base(
                safety_status="needs_clarify",
                pending_dialog=pending,
                user_reply=(
                    "Should I swap the outdoor stops on "
                    f"Day {rain_day} for indoor places? Please say yes or no."
                ),
            )
        if yn is False:
            return _base(
                pending_dialog={"type": "weather_date_ask", "day": rain_day},
                user_reply=(
                    f"Okay — I left Day {rain_day} unchanged."
                    + weather_offer
                ),
            )
        if prev is None or trip is None:
            return _base(
                pending_dialog=None,
                user_reply="I don’t have an itinerary to edit right now.",
            )
        rain_patch = EditPatch(
            target=EditTarget(day=rain_day, block=None),
            operation="make_indoor",
            payload={"rain_adjust": True},
            user_utterance=message,
        )
        waves, planner, criteria = plan_agent_waves(
            intent="edit",
            message=f"indoor rain day {rain_day}",
            state=dict(state),
        )
        return _start_agent_loop(
            state,
            waves=[list(w) for w in waves],
            planner=planner,
            intent="edit",
            trip=trip,
            success_criteria=criteria,
            user_reply=(
                f"Updating Day {rain_day} — swapping outdoor stops for indoor ones."
            ),
            edit_patch=rain_patch,
            edit_patches=[rain_patch],
            extra={
                "previous_itinerary": dump(prev),
                "merged_itinerary": dump(prev),
                "pending_dialog": {"type": "weather_date_ask", "day": rain_day},
                "rain_day_index": rain_day,
            },
        )

    if kind == "weather_date_ask":
        lower = message.lower().strip()
        if _yes_no(message) is False or re.search(
            r"\b(no|skip|later|not now)\b", lower
        ):
            return _base(
                pending_dialog=None,
                user_reply="Alright — ask anytime if you want a dated forecast.",
            )
        start = _find_start_date(message)
        if start is None and _yes_no(message) is True:
            return _base(
                safety_status="needs_clarify",
                pending_dialog=pending,
                user_reply=(
                    "Sure — which date should I check? "
                    "(e.g. 2026-08-12 or 12 August 2026)"
                ),
            )
        if start is None:
            return _base(
                safety_status="needs_clarify",
                pending_dialog=pending,
                user_reply=(
                    "Please share a date to look up (YYYY-MM-DD), "
                    "or say “no” to skip."
                ),
            )
        # Dated weather MCP lookup.
        city = (trip.city if trip else None) or SCOPED_CITY
        days = int(trip.num_days or 1) if trip else 1
        result = weather_adjustment(city=city, start_date=start.isoformat(), num_days=1)
        if result.missing_data or not result.days:
            return _base(
                pending_dialog=None,
                weather_results=dump(result),
                user_reply=(
                    f"I couldn’t get Weather MCP data for {start.isoformat()} "
                    f"right now. ({result.notes or 'Try another date.'})"
                ),
            )
        d0 = result.days[0]
        bits = [
            f"Forecast for {d0.calendar_date}",
            f"rain risk {d0.rain_risk}",
        ]
        if d0.precip_probability_max is not None:
            bits.append(f"rain chance about {d0.precip_probability_max:.0f}%")
        if d0.temp_max_c is not None and d0.temp_min_c is not None:
            bits.append(f"temps {d0.temp_min_c:.0f}–{d0.temp_max_c:.0f}°C")
        if d0.weather_label:
            bits.append(d0.weather_label)
        rec = f" {d0.recommendation}" if d0.recommendation else ""
        return _base(
            pending_dialog=None,
            weather_results=dump(result),
            sources=[
                {
                    "title": "Open-Meteo forecast",
                    "dataset": "open-meteo",
                    "snippet": f"Weather for {d0.calendar_date}",
                }
            ],
            user_reply="; ".join(bits) + "." + rec,
        )

    return None


def orchestrator_node(state: GraphState) -> dict[str, Any]:
    message = (state.get("user_message") or "").strip()
    revision_count = int(state.get("revision_count") or 0)
    existing_trip = as_trip(state.get("trip_constraints"))

    ready = bool(state.get("ready_for_synthesis") or state.get("ready_for_merger"))

    # Mid multi-agent loop (specialist just returned). Do this *before* revise
    # handling: clearing reviewer_verdict to None is a no-op under _last_value,
    # so a stale revise verdict must not restart the revise plan every step.
    if state.get("orchestration_started") and not ready:
        return _continue_agent_loop(state)

    # --- Reviewer-driven revise: route structured feedback to target specialist ---
    v = as_verdict(state.get("reviewer_verdict"))
    if v and v.status == "revise" and revision_count < 2:
        trip = existing_trip
        feedback = state.get("revision_feedback") or {
            "status": "revise",
            "reason": v.reason,
            "target_agent": v.target_agent,
            "constraints": list(v.constraints or []),
            "affected_sections": list(v.affected_sections or []),
        }
        target = normalize_target_agent(
            str(feedback.get("target_agent") or v.target_agent or "")
        ) or "itinerary_agent"
        # Plan/edit must never re-enter knowledge_agent (RAG is explain-only).
        if target == "knowledge_agent":
            logger.info(
                "NODE orchestrator REVISE remapped knowledge_agent → itinerary_agent"
            )
            target = "itinerary_agent"
        constraints = [
            str(c)
            for c in (feedback.get("constraints") or v.constraints or [])
            if str(c).strip()
        ]
        reason = str(feedback.get("reason") or v.reason or "feasibility").strip()

        # Soft trip tweak only when Reviewer targeted itinerary for pace/duration
        if trip and target == "itinerary_agent" and any(
            i.code in {"feasibility_duration", "feasibility_pace"} for i in v.issues
        ):
            trip = trip.model_copy(update={"pace": "relaxed", "confirmed": True})

        waves = waves_for_revision(target)
        criteria = success_criteria_for_waves(waves)
        logger.info(
            "NODE orchestrator REVISE route target=%s reason=%s constraints=%s waves=%s",
            target,
            reason,
            constraints,
            waves,
        )
        # Scoped voice edits must stay scoped across Reviewer revise loops.
        from agent.nodes.state_utils import as_edit_patches

        prior_patches = as_edit_patches(state.get("edit_patches"))
        if not prior_patches:
            prior_patches = as_edit_patches(state.get("edit_patch"))

        prev_itin = as_itinerary(
            state.get("previous_itinerary") or state.get("merged_itinerary")
        )
        revise_intent = "edit" if prior_patches and prev_itin else "plan"
        # Never expand scoped edits into poi/travel Overpass waves on revise —
        # that has caused long hangs / proxy 500s. Re-apply edit via itinerary only.
        if revise_intent == "edit":
            target = "itinerary_agent"
            waves = [["itinerary_agent"]]
            criteria = success_criteria_for_waves(waves)
        extra_rev: dict[str, Any] = {
            "revision_feedback": {
                "status": "revise",
                "reason": reason,
                "target_agent": target,
                "constraints": list(constraints),
                "affected_sections": list(
                    feedback.get("affected_sections")
                    or v.affected_sections
                    or []
                ),
            },
        }
        if revise_intent == "edit" and prev_itin and prior_patches:
            touched = {int(p.target.day) for p in prior_patches}
            for d in prev_itin.days:
                if d.day_index not in touched:
                    tag = f"Preserve Day {d.day_index}"
                    if tag not in constraints:
                        constraints.append(tag)
            extra_rev["revision_feedback"]["constraints"] = list(constraints)
            extra_rev["previous_itinerary"] = dump(prev_itin)
            extra_rev["merged_itinerary"] = dump(prev_itin)
            extra_rev["edit_patches"] = [dump(p) for p in prior_patches]
            extra_rev["edit_patch"] = dump(prior_patches[0])

        return _start_agent_loop(
            state,
            waves=[list(w) for w in waves],
            planner="reviewer_feedback",
            intent=revise_intent,
            trip=trip,
            success_criteria=criteria,
            plan_reason=f"Reviewer → {target}: {reason}",
            user_reply=(
                f"Reviewer asked {target} to revise: {reason}. "
                + (f"Constraints: {'; '.join(constraints)}." if constraints else "")
            ),
            edit_patch=prior_patches[0] if prior_patches else None,
            edit_patches=prior_patches or None,
            extra=extra_rev,
        )

    if not message:
        return {
            "safety_status": "needs_clarify",
            "intent": "confirm",
            "ready_for_merger": False,
            "next_agent": None,
            "orchestration_started": False,
            "user_reply": (
                f"I plan **{SCOPED_CITY}** trips ({MIN_TRIP_DAYS}–{MAX_TRIP_DAYS} days). "
                f"For example: “Plan a 3-day trip to {SCOPED_CITY}, food and culture, relaxed.”"
            ),
            "dispatch_plan": _dispatch_from_waves([]),
        }

    status, refusal = _safety_check(message)
    if status == "blocked":
        logger.info("NODE orchestrator SAFETY blocked")
        return {
            "safety_status": "blocked",
            "intent": "confirm",
            "ready_for_merger": False,
            "next_agent": None,
            "orchestration_started": False,
            "user_reply": refusal,
            "dispatch_plan": _dispatch_from_waves([]),
            "agent_trace": _trace_append(
                state, {"agent": "orchestrator", "action": "blocked"}
            ),
        }

    intent = _detect_intent(message, state)
    prev_itin = as_itinerary(
        state.get("previous_itinerary") or state.get("merged_itinerary")
    )

    # Resume rain / weather dialogs before other routing.
    # If the user starts a new task instead of answering, clear the sticky dialog
    # and continue normal routing (pending_dialog must overwrite with None).
    pending = state.get("pending_dialog")
    if isinstance(pending, dict) and pending.get("type"):
        kind = str(pending.get("type") or "")
        if _is_pending_dialog_escape(message, kind):
            cleared = dict(state)
            cleared["pending_dialog"] = None
            out = orchestrator_node(cleared)  # type: ignore[arg-type]
            return {**out, "pending_dialog": None}
        handled = _continue_pending_dialog(
            state, message, existing_trip, prev_itin, pending
        )
        if handled is not None:
            return handled

    # Weather Q&A → Weather MCP only (never invent; never start trip clarify).
    if _is_weather_query(message):
        return _answer_weather_query(state, message, existing_trip)

    # Tips / POI / hours Q&A → Knowledge RAG (no trip required).
    # Never steal confirm / slot-fill / plan turns — those must build the itinerary.
    # Itinerary "why did you pick X" keeps the explain→synthesis path for
    # place-matched justifications + citations in the UI.
    if _is_knowledge_query(message):
        confirmish = bool(
            intent == "confirm"
            or (
                existing_trip
                and not existing_trip.confirmed
                and _yes_no(message) is True
            )
            or re.search(
                r"transcription task|i will transcribe|audio contains the word",
                message.lower(),
            )
        )
        has_itin = bool(prev_itin)
        itinerary_why = bool(
            re.search(
                r"\bwhy (did you |do you )?(pick|choose|include|this|that)\b",
                message.lower(),
            )
        )
        if confirmish:
            logger.info(
                "NODE orchestrator skip knowledge_qa (confirm/plan turn) intent=%s",
                intent,
            )
        elif not (has_itin and itinerary_why):
            return _answer_knowledge_query(state, message, existing_trip)

    # Rain hypothetical → monsoon-month clarifying dialog (not instant swap).
    if intent == "explain" and re.search(
        r"\b(what if it rains|if(?:\s+if)? it rains|rains? on day|rain on day|\brain\b)",
        message.lower(),
    ):
        rain_start = _begin_rain_monsoon_dialog(
            state, message, existing_trip, prev_itin
        )
        if rain_start is not None:
            return rain_start

    # While clarifying an unconfirmed trip, treat short answers as slot updates.
    if (
        existing_trip
        and not existing_trip.confirmed
        and intent not in {"confirm", "edit", "explain"}
    ):
        intent = "plan"

    # Off-topic Europe-style briefs must not overwrite Jaipur slot answers.
    if (
        intent == "plan"
        and existing_trip
        and not existing_trip.confirmed
        and is_off_scope_trip_brief(message)
    ):
        bumped = existing_trip.model_copy(
            update={
                "clarify_turns": min(
                    MAX_CLARIFY, _clarify_count(state, existing_trip) + 1
                )
            }
        )
        known_bits = []
        if bumped.days_known and bumped.num_days:
            known_bits.append(f"{bumped.num_days} days")
        if bumped.pace_known and bumped.pace:
            known_bits.append(
                "balanced" if bumped.pace == "moderate" else bumped.pace
            )
        if bumped.interests_known and bumped.interests:
            known_bits.append(", ".join(bumped.interests))
        known = f" Got it so far: {'; '.join(known_bits)}." if known_bits else ""
        ask = _missing_slot_question(bumped) or (
            f"What do you enjoy in {SCOPED_CITY} "
            f"(e.g. {primary_interests_prompt()})?"
        )
        return {
            "safety_status": "needs_clarify",
            "intent": "plan",
            "ready_for_merger": False,
            "next_agent": None,
            "orchestration_started": False,
            "trip_constraints": dump(bumped),
            "user_reply": (
                f"That sounds like a trip outside this demo’s scope — "
                f"I only plan **{SCOPED_CITY}** ({MIN_TRIP_DAYS}–{MAX_TRIP_DAYS} days). "
                f"{ask}{known}"
            ),
            "dispatch_plan": _dispatch_from_waves([]),
            "agent_trace": _trace_append(
                state,
                {
                    "agent": "orchestrator",
                    "action": "reject_off_scope_brief",
                    "detail": "europe_or_multi_country_brief",
                },
            ),
        }

    # New "plan a trip" must NOT reuse pace/interests from a prior finished itinerary.
    if (
        intent == "plan"
        and existing_trip
        and existing_trip.confirmed
        and not state.get("orchestration_started")
    ):
        existing_trip = None

    city = _find_city(message)
    days = _find_days(message)
    any_days = _find_any_day_count(message)
    # Bare day answers during slot fill (also covered by _find_any_day_count fullmatch)
    if (
        days is None
        and any_days is None
        and existing_trip
        and not existing_trip.days_known
    ):
        bare = re.fullmatch(
            r"\s*([2-4]|two|three|four)\s*[.!?]?\s*",
            message.lower().strip(),
        )
        if bare:
            token = bare.group(1)
            any_days = int(token) if token.isdigit() else _DAY_WORDS[token]
            days = any_days
    pace = _find_pace(message)
    prefs = resolve_trip_preferences(
        message,
        explicit_pace=pace,
        existing_profile=(
            None
            if clear_traveler_profile_request(message)
            else (
                existing_trip.traveler_profile
                if existing_trip and not existing_trip.confirmed
                else None
            )
        ),
        existing_interests=(
            list(existing_trip.interests)
            if existing_trip
            and existing_trip.interests_known
            and existing_trip.interests
            else None
        ),
        existing_constraints=(
            list(existing_trip.constraints)
            if existing_trip and not existing_trip.confirmed
            else None
        ),
        existing_pace=(
            existing_trip.pace
            if existing_trip and existing_trip.pace_known
            else None
        ),
        existing_window=(
            existing_trip.daily_time_window_min if existing_trip else None
        ),
    )
    interests = list(prefs["interests"])
    clarify_n = _clarify_count(state, existing_trip)

    # Product scope is MIN–MAX days. Out-of-range requests: clamp and tell the user.
    scope_note: str | None = None
    if any_days is not None and days is None:
        if any_days > MAX_TRIP_DAYS:
            days = MAX_TRIP_DAYS
            scope_note = (
                f"I plan {MIN_TRIP_DAYS}–{MAX_TRIP_DAYS} day {SCOPED_CITY} trips "
                f"(not {any_days} days), so I'll build a **{MAX_TRIP_DAYS}-day** itinerary."
            )
        elif any_days < MIN_TRIP_DAYS:
            days = MIN_TRIP_DAYS
            scope_note = (
                f"I plan at least {MIN_TRIP_DAYS} days per trip "
                f"(you asked for {any_days}), "
                f"so I'll build a **{MIN_TRIP_DAYS}-day** itinerary."
            )
        else:
            days = any_days

    # City scope: Jaipur only for this milestone
    if city and not is_city_allowed(city):
        city = default_city()
        city_note = f"Right now I only plan for {SCOPED_CITY} city in India."
        scope_note = f"{city_note} {scope_note}" if scope_note else city_note
    elif not city:
        city = default_city() if (existing_trip or intent == "plan") else None

    if intent == "explain":
        prev = as_itinerary(
            state.get("merged_itinerary") or state.get("previous_itinerary")
        )
        trip = existing_trip or (prev.trip if prev else None)
        if trip is None:
            return {
                "safety_status": "needs_clarify",
                "intent": "explain",
                "ready_for_merger": False,
                "next_agent": None,
                "orchestration_started": False,
                "user_reply": (
                    "I don’t have a plan to explain yet. "
                    f"Let’s plan a {SCOPED_CITY} trip first."
                ),
                "dispatch_plan": _dispatch_from_waves([]),
            }

        waves, planner, criteria = plan_agent_waves(
            intent="explain", message=message, state=dict(state)
        )
        explain_kickoff = "Looking up cited tips for your question…"
        if re.search(
            r"\b(doable|feasible|too (?:much|packed)|can (?:i|we) (?:do|finish))\b",
            message.lower(),
        ):
            explain_kickoff = "Checking how doable this itinerary looks…"
        return _start_agent_loop(
            state,
            waves=[list(w) for w in waves],
            planner=planner,
            intent="explain",
            trip=trip,
            success_criteria=criteria,
            user_reply=explain_kickoff,
            extra={
                "previous_itinerary": dump(prev) if prev else state.get("previous_itinerary"),
                "merged_itinerary": dump(prev) if prev else state.get("merged_itinerary"),
            },
        )

    if intent == "edit":
        prev = as_itinerary(
            state.get("previous_itinerary") or state.get("merged_itinerary")
        )
        patches = _parse_edits(message)
        patch = patches[0] if patches else None
        if prev is None:
            return {
                "safety_status": "needs_clarify",
                "intent": "edit",
                "ready_for_merger": False,
                "next_agent": None,
                "orchestration_started": False,
                "trip_constraints": dump(existing_trip) if existing_trip else None,
                "user_reply": (
                    "I don't have an itinerary to edit yet. "
                    "Plan and confirm a trip first, then say e.g. "
                    "“Make Day 2 more relaxed.”"
                ),
                "dispatch_plan": _dispatch_from_waves([]),
            }
        if patch is None:
            return {
                "safety_status": "needs_clarify",
                "intent": "edit",
                "ready_for_merger": False,
                "next_agent": None,
                "orchestration_started": False,
                "trip_constraints": dump(prev.trip),
                "previous_itinerary": dump(prev),
                "merged_itinerary": dump(prev),
                "user_reply": (
                    "Which day should I change (Day 1–4), and what should change? "
                    "Example: “Make Day 2 more relaxed.” "
                    "“Make Day 3 more packed.” "
                    "“Make Day 2 more balanced.” "
                    "or “Remove market from Day 1.”"
                ),
                "dispatch_plan": _dispatch_from_waves([]),
            }
        waves, planner, criteria = plan_agent_waves(
            intent="edit", message=message, state=dict(state)
        )
        days = sorted({p.target.day for p in patches})
        section = (
            f"day{days[0]}"
            if len(days) == 1
            else "days " + ", ".join(str(d) for d in days)
        )
        if len(patches) == 1 and patch.target.block:
            section = f"day{patch.target.day}.{patch.target.block}"
        ops = " and ".join(p.operation.replace("_", " ") for p in patches)
        return _start_agent_loop(
            state,
            waves=[list(w) for w in waves],
            planner=planner,
            success_criteria=criteria,
            intent="edit",
            trip=prev.trip,
            user_reply=(
                f"Updating {section} only ({ops}) — leaving other days unchanged."
            ),
            edit_patch=patch,
            edit_patches=patches,
            extra={
                "previous_itinerary": dump(prev),
                "merged_itinerary": dump(prev),
            },
        )

    if intent == "confirm" and existing_trip:
        if not existing_trip.slots_ready():
            q = _missing_slot_question(existing_trip)
            bumped = existing_trip.model_copy(
                update={"clarify_turns": min(MAX_CLARIFY, clarify_n + 1)}
            )
            return {
                "safety_status": "needs_clarify",
                "intent": "confirm",
                "ready_for_merger": False,
                "next_agent": None,
                "orchestration_started": False,
                "trip_constraints": dump(bumped),
                "user_reply": (
                    "I still need a few details before I can generate the plan. "
                    + (q or "Please share days, pace, and interests.")
                ),
                "dispatch_plan": _dispatch_from_waves([]),
            }
        # Apply profile implied pace only if user confirmed without pace? No — require pace.
        trip = existing_trip.model_copy(update={"confirmed": True})
        waves, planner, criteria = plan_agent_waves(
            intent="plan", message=message, state=dict(state)
        )
        return _start_agent_loop(
            state,
            waves=[list(w) for w in waves],
            planner=planner,
            success_criteria=criteria,
            intent="plan",
            trip=trip,
            user_reply=(
                f"Confirmed — planning {trip.num_days} days in {trip.city} "
                f"({_trip_audience_phrase(trip)})."
            ),
        )

    trip = _merge_trip(
        existing_trip
        if not (
            city
            and existing_trip
            and resolve_city(city)
            and resolve_city(city).name != existing_trip.city  # type: ignore[union-attr]
        )
        else None,
        city=city,
        days=days,
        pace=pace,
        interests=interests,
        confirmed=False,
        message=message,
        scope_note=scope_note,
        days_known=days is not None,
        pace_known=pace is not None,
        interests_known=bool(prefs.get("interests_from_message"))
        or bool(prefs.get("profile_detected") and interests),
    )

    if trip is None:
        reply = (
            f"I currently plan **{SCOPED_CITY}** only "
            f"({MIN_TRIP_DAYS}–{MAX_TRIP_DAYS} days). "
            f"Try: “Plan a trip to {SCOPED_CITY}.”"
        )
        return {
            "safety_status": "needs_clarify",
            "intent": "confirm",
            "ready_for_merger": False,
            "next_agent": None,
            "orchestration_started": False,
            "user_reply": reply,
            "dispatch_plan": _dispatch_from_waves([]),
        }

    # Ask clarifying questions (max 6) — never invent pace / interests / days.
    missing_q = _missing_slot_question(trip)
    if missing_q:
        if clarify_n >= MAX_CLARIFY:
            return {
                "safety_status": "needs_clarify",
                "intent": "confirm",
                "ready_for_merger": False,
                "next_agent": None,
                "orchestration_started": False,
                "trip_constraints": dump(trip),
                "user_reply": (
                    f"I’ve asked several clarifying questions already. "
                    f"Please answer with days ({MIN_TRIP_DAYS}–{MAX_TRIP_DAYS}), "
                    "schedule (relaxed / balanced / packed), and what you enjoy "
                    f"(e.g. {primary_interests_prompt()}), "
                    "then say “confirm”."
                ),
                "dispatch_plan": _dispatch_from_waves([]),
            }
        bumped = trip.model_copy(
            update={"clarify_turns": min(MAX_CLARIFY, clarify_n + 1)}
        )
        preface = ""
        if scope_note:
            preface = f"{scope_note} "
        known_bits = []
        if bumped.days_known and bumped.num_days:
            known_bits.append(f"{bumped.num_days} days")
        if bumped.pace_known and bumped.pace:
            known_bits.append(
                "balanced" if bumped.pace == "moderate" else bumped.pace
            )
        if bumped.interests_known and bumped.interests:
            known_bits.append(", ".join(bumped.interests))
        known = f" Got it so far: {'; '.join(known_bits)}." if known_bits else ""
        return {
            "safety_status": "needs_clarify",
            "intent": "confirm",
            "ready_for_merger": False,
            "next_agent": None,
            "orchestration_started": False,
            "trip_constraints": dump(bumped),
            "user_reply": f"{preface}{missing_q}{known}",
            "dispatch_plan": _dispatch_from_waves([]),
            "agent_trace": _trace_append(
                state,
                {
                    "agent": "orchestrator",
                    "action": "clarify",
                    "clarify_turns": bumped.clarify_turns,
                    "missing": missing_q,
                    "trip": dump(bumped),
                },
            ),
        }

    # All slots known → confirm before generating.
    if not trip.confirmed:
        confirm_bits = [_trip_audience_phrase(trip)]
        if scope_note:
            confirm_bits.insert(0, scope_note)
        return {
            "safety_status": "needs_clarify",
            "intent": "confirm",
            "ready_for_merger": False,
            "next_agent": None,
            "orchestration_started": False,
            "trip_constraints": dump(trip),
            "user_reply": (
                f"Please confirm: {'; '.join(confirm_bits)}. "
                'Say “yes” or “confirm” to generate the itinerary '
                "(or tell me what to change)."
            ),
            "dispatch_plan": _dispatch_from_waves([]),
            "agent_trace": _trace_append(
                state,
                {
                    "agent": "orchestrator",
                    "action": "await_confirm",
                    "trip": dump(trip),
                },
            ),
        }

    waves, planner, criteria = plan_agent_waves(
        intent="plan", message=message, state=dict(state)
    )
    flat = flatten_waves(waves)
    wave_desc = " → ".join("∥".join(w) for w in waves)
    plan_reply = (
        f"Planning {trip.num_days} days in {trip.city} "
        f"({_trip_audience_phrase(trip)}). "
        f"Dispatching: {wave_desc or ', '.join(flat)}."
    )
    if scope_note:
        plan_reply = f"{scope_note} {plan_reply}"
    return _start_agent_loop(
        state,
        waves=[list(w) for w in waves],
        planner=planner,
        success_criteria=criteria,
        intent="plan",
        trip=trip,
        user_reply=plan_reply,
    )
