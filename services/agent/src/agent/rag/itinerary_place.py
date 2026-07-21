"""Match user knowledge questions to stops on the active itinerary."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from agent.rag.retrieve import extract_place_terms
from agent.schemas.itinerary import Itinerary, Stop, TimeOfDay


@dataclass(frozen=True)
class ItineraryPlaceMatch:
    stop_name: str
    day_index: int | None
    time_of_day: TimeOfDay | None
    score: float
    needs_confirm: bool


def _normalize(text: str) -> str:
    from agent.rag.retrieve import normalize_match_text

    return normalize_match_text(text)


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", _normalize(text)) if len(t) > 1}


def score_place_match(query: str, stop_name: str) -> float:
    """0–1 similarity between a user place phrase and an itinerary stop name."""
    q = _normalize(query)
    name = _normalize(stop_name)
    if not q or not name:
        return 0.0
    if q == name:
        return 1.0
    if q in name or name in q:
        return 0.93
    seq = SequenceMatcher(None, q, name).ratio()
    q_tok = _tokens(q)
    n_tok = _tokens(name)
    if not q_tok:
        return seq
    overlap = len(q_tok & n_tok) / len(q_tok)
    return max(seq, overlap * 0.96)


def _needs_confirm(top: ItineraryPlaceMatch, second_score: float) -> bool:
    if top.score >= 0.92:
        return False
    if top.score >= 0.82 and top.score - second_score >= 0.14:
        return False
    if second_score >= top.score - 0.08 and second_score >= 0.62:
        return True
    if top.score < 0.80:
        return True
    return top.score - second_score < 0.12


def iter_itinerary_stops(
    itinerary: Itinerary,
) -> list[tuple[Stop, int, TimeOfDay]]:
    rows: list[tuple[Stop, int, TimeOfDay]] = []
    for day in itinerary.days or []:
        for tod in ("morning", "afternoon", "evening"):
            block = day.block(tod)
            for stop in block.stops or []:
                if (stop.name or "").strip():
                    rows.append((stop, day.day_index, tod))
    return rows


def match_itinerary_place(
    message: str,
    itinerary: Itinerary | None,
    *,
    city: str = "Jaipur",
) -> ItineraryPlaceMatch | None:
    """
    Pick the best itinerary stop for a knowledge/hours/why-pick question.

    High-confidence matches proceed without confirmation; fuzzy or ambiguous
    matches set ``needs_confirm=True``.
    """
    if itinerary is None or not itinerary.days:
        return None

    query_terms = list(extract_place_terms(message, city))
    # Fallback: strip question chrome and treat the remainder as a place phrase
    # (covers typos like "elephantastic" vs "Elefantastic").
    stripped = _normalize(message)
    stripped = re.sub(
        r"\b("
        r"why|did|do|you|pick|choose|include|selected|recommend(?:ed)?|"
        r"tell|me|more|about|opening|hours?|for|of|the|a|an|please|"
        r"what|when|where|is|are|was|were"
        r")\b",
        " ",
        stripped,
        flags=re.I,
    )
    stripped = re.sub(r"\s+", " ", stripped).strip(" .,?!")
    if stripped and len(stripped) >= 4 and stripped not in query_terms:
        query_terms.append(stripped)

    if not query_terms:
        # Last resort: score every stop against the full message.
        query_terms = [_normalize(message)]

    best_by_name: dict[str, ItineraryPlaceMatch] = {}
    for stop, day_index, tod in iter_itinerary_stops(itinerary):
        score = max(score_place_match(term, stop.name) for term in query_terms)
        if score < 0.58:
            continue
        key = _normalize(stop.name)
        prev = best_by_name.get(key)
        if prev is None or score > prev.score:
            best_by_name[key] = ItineraryPlaceMatch(
                stop_name=stop.name,
                day_index=day_index,
                time_of_day=tod,
                score=score,
                needs_confirm=False,
            )

    if not best_by_name:
        return None

    ranked = sorted(best_by_name.values(), key=lambda m: m.score, reverse=True)
    top = ranked[0]
    second = ranked[1].score if len(ranked) > 1 else 0.0
    confirm = _needs_confirm(top, second)
    return ItineraryPlaceMatch(
        stop_name=top.stop_name,
        day_index=top.day_index,
        time_of_day=top.time_of_day,
        score=top.score,
        needs_confirm=confirm,
    )


def itinerary_place_context(match: ItineraryPlaceMatch | None) -> str:
    if match is None:
        return ""
    bits = [match.stop_name]
    if match.day_index is not None:
        bits.append(f"Day {match.day_index}")
    if match.time_of_day:
        bits.append(match.time_of_day)
    return " · ".join(bits)
