"""Specialist LangGraph nodes — MCP / RAG; Itinerary owns optimization.

Honor ``revision_feedback`` from the Reviewer when re-invoked in a revise loop.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from agent.mcp.itinerary_builder import build_itinerary
from agent.mcp.poi_search import poi_search
from agent.mcp.poi_shortlist import active_itinerary_strategy, shortlist_pois
from agent.mcp.travel_time import estimate_travel_times
from agent.mcp.weather import weather_adjustment
from agent.nodes.itinerary_optimize import optimize_itinerary
from agent.nodes.state_utils import (
    as_draft,
    as_itinerary,
    as_knowledge,
    as_poi,
    as_travel,
    as_trip,
    as_weather,
    dump,
)
from agent.nodes.trace import trace_delta
from agent.rag.retrieve import knowledge_search
from agent.schemas.itinerary import DayPlan, Itinerary
from agent.schemas.specialists import ItineraryDraftResult, POISearchResult
from agent.schemas.state import GraphState

logger = logging.getLogger(__name__)


def _revision_constraints(state: GraphState) -> list[str]:
    fb = state.get("revision_feedback")
    if isinstance(fb, dict):
        return [str(c) for c in (fb.get("constraints") or []) if str(c).strip()]
    return []


def _revision_reason(state: GraphState) -> str | None:
    fb = state.get("revision_feedback")
    if isinstance(fb, dict) and fb.get("reason"):
        return str(fb["reason"])
    return None


def poi_agent_node(state: GraphState) -> dict[str, Any]:
    trip = as_trip(state.get("trip_constraints"))
    if trip is None:
        return {
            "poi_results": dump(
                POISearchResult(
                    city="Unknown",
                    pois=[],
                    missing_data=True,
                    notes="No trip constraints for POI search.",
                )
            ),
            "agent_trace": trace_delta(
                {
                    "agent": "poi_agent",
                    "tool": "poi_search_mcp",
                    "action": "skip",
                    "detail": "No trip constraints — cannot search POIs.",
                }
            ),
        }
    constraints = list(trip.constraints) + _revision_constraints(state)
    logger.info(
        "NODE poi_agent city=%s interests=%s revision=%s",
        trip.city,
        trip.interests,
        _revision_reason(state),
    )
    day_n = int(trip.num_days or 3)
    result = poi_search(
        city=trip.city,
        interests=trip.interests,
        constraints=constraints,
        limit=max(40, min(120, day_n * 8)),
        use_overpass=True,
    )
    if _revision_constraints(state):
        result = result.model_copy(
            update={
                "notes": (
                    (result.notes or "")
                    + f" Reviewer revision: {_revision_reason(state) or 'constraints applied'}."
                ).strip()
            }
        )
    logger.info(
        "NODE poi_agent → %d pois missing=%s", len(result.pois), result.missing_data
    )
    sample = [p.name for p in result.pois[:8]]
    out: dict[str, Any] = {
        "poi_results": dump(result),
        "agent_trace": trace_delta(
            {
                "agent": "poi_agent",
                "tool": "poi_search_mcp",
                "source": "OpenStreetMap Overpass (live only)",
                "action": "search",
                "city": trip.city,
                "interests": list(trip.interests),
                "use_overpass": True,
                "poi_count": len(result.pois),
                "missing_data": result.missing_data,
                "sample_pois": sample,
                "notes": result.notes,
                "revision": _revision_reason(state),
            }
        ),
    }
    if active_itinerary_strategy() == "hybrid" and result.pois:
        short = shortlist_pois(
            city=trip.city or "Jaipur",
            candidate_pois=list(result.pois),
            interests=list(trip.interests),
            num_days=int(trip.num_days or 3),
            pace=trip.pace or "relaxed",
        )
        out["poi_shortlist"] = dump(short)
        out["agent_trace"] = list(out["agent_trace"]) + trace_delta(
            {
                "agent": "poi_agent",
                "tool": "poi_shortlist_mcp",
                "action": "shortlist",
                "strategy": "hybrid",
                "shortlist_count": len(short.pois),
                "sample_pois": [p.name for p in short.pois[:10]],
                "notes": short.notes,
            }
        )
    else:
        out["poi_shortlist"] = None
    return out


def itinerary_agent_node(state: GraphState) -> dict[str, Any]:
    """Build + optimize itinerary (move/skip/reorder). Synthesis must not change it."""
    from agent.nodes.edit_apply import apply_edit_patches, resolve_edit_patches
    from agent.nodes.state_utils import as_edit_patches

    trip = as_trip(state.get("trip_constraints"))
    poi = as_poi(state.get("poi_results"))
    shortlist = as_poi(state.get("poi_shortlist"))
    if trip is None:
        return {
            "agent_trace": trace_delta(
                {
                    "agent": "itinerary_agent",
                    "action": "skip",
                    "detail": "No trip constraints.",
                }
            )
        }
    strategy = active_itinerary_strategy()
    if strategy == "hybrid" and shortlist and shortlist.pois:
        pois = list(shortlist.pois)
        selection_mode = "preselected"
    else:
        pois = poi.pois if poi else []
        selection_mode = "legacy" if strategy == "legacy" else "hybrid"
    rev = _revision_constraints(state)
    patches = as_edit_patches(state.get("edit_patches"))
    if not patches:
        patches = as_edit_patches(state.get("edit_patch"))
    prev = as_itinerary(
        state.get("previous_itinerary") or state.get("merged_itinerary")
    )

    # --- Voice edit path: mutate only the target day/block(s) ---
    # Prefer scoped patches whenever present — even if Reviewer revise briefly
    # flipped intent to "plan", never rebuild unrelated days.
    if patches and prev:
        need_poi_ops = {
            "make_indoor",
            "add_stop",
            "pack_block",
            "balance_block",
            "balance_categories",
        }
        need_poi = any(p.operation in need_poi_ops for p in patches)

        def _interests_for_patches() -> list[str]:
            for p in patches:
                if p.operation == "add_stop":
                    cat = str((p.payload or {}).get("category") or "food").lower()
                    if cat in {"outdoor", "outdoors", "park", "nature"}:
                        return ["park", "nature"]
                    if cat in {"shopping", "market"}:
                        return ["shopping", "market"]
                    return [cat]
                if p.operation == "make_indoor":
                    return ["museum", "food"]
                if p.operation == "pack_block":
                    return list(trip.interests[:3]) or ["heritage", "food"]
                if p.operation == "balance_block":
                    # Use trip interests only — do not invent shopping.
                    return list(trip.interests[:3]) or ["museum", "heritage"]
                if p.operation == "balance_categories":
                    cats = [
                        str(c).lower()
                        for c in ((p.payload or {}).get("categories") or [])
                        if str(c).strip()
                    ]
                    return cats or list(trip.interests[:3]) or ["museum", "food"]
            return list(trip.interests[:2]) or ["food"]

        def _missing_edit_pois() -> bool:
            if not need_poi:
                return False
            if not pois:
                return True
            cats_have = {(p.category or "").lower() for p in pois}
            for p in patches:
                if p.operation == "add_stop":
                    cat = str((p.payload or {}).get("category") or "food").lower()
                    wanted = {
                        "outdoor": {"park", "viewpoint", "nature", "adventure"},
                        "outdoors": {"park", "viewpoint", "nature", "adventure"},
                        "park": {"park", "nature", "viewpoint"},
                        "food": {"food"},
                        "shopping": {"shopping", "market"},
                        "market": {"market", "shopping"},
                    }.get(cat, {cat})
                    if cats_have.isdisjoint(wanted):
                        return True
                if p.operation == "make_indoor" and cats_have.isdisjoint(
                    {"museum", "food", "market", "temple", "heritage", "art"}
                ):
                    return True
                if p.operation == "pack_block" and len(pois) < 3:
                    return True
                if p.operation == "balance_block":
                    # Need unused candidates beyond what's already on the plan.
                    used = {
                        (s.osm_type, s.osm_id)
                        for d in prev.days
                        for s in d.all_stops
                    }
                    unused = [
                        x
                        for x in pois
                        if (x.osm_type, x.osm_id) not in used
                    ]
                    if len(unused) < 3:
                        return True
                if p.operation == "balance_categories":
                    wanted_cats = {
                        str(c).lower()
                        for c in ((p.payload or {}).get("categories") or [])
                    }
                    if wanted_cats and cats_have.isdisjoint(wanted_cats):
                        return True
            return False

        if _missing_edit_pois():
            # Always fetch the requested category (food/shopping/…) live.
            cat_interests = _interests_for_patches()
            fetched = poi_search(
                city=trip.city,
                interests=cat_interests,
                constraints=list(trip.constraints),
                limit=25,
                use_overpass=True,
            )
            pois = list(fetched.pois) + list(pois)
            poi = fetched
            logger.info(
                "NODE itinerary_agent EDIT poi fetch interests=%s count=%d notes=%s",
                cat_interests,
                len(fetched.pois),
                fetched.notes,
            )

        patches = resolve_edit_patches(prev, patches)
        updated, edit_notes = apply_edit_patches(
            prev, patches, candidate_pois=pois
        )
        # Prefer pace written by relax/balance/pack edits on the itinerary trip.
        edit_pace = (
            (updated.trip.pace if updated.trip else None)
            or trip.pace
            or (prev.trip.pace if prev and prev.trip else None)
            or "moderate"
        )
        touched = {p.target.day for p in patches}
        draft = ItineraryDraftResult(
            pace=edit_pace,
            days=list(updated.days),
            missing_data=False,
            notes="; ".join(edit_notes) if edit_notes else "Scoped voice edit applied.",
            optimization_reasoning=edit_notes,
        )
        logger.info(
            "NODE itinerary_agent EDIT ops=%s days=%s notes=%s",
            [p.operation for p in patches],
            sorted(touched),
            edit_notes,
        )
        return {
            "itinerary_draft": dump(draft),
            "merged_itinerary": dump(updated),
            "edit_patches": [dump(p) for p in patches],
            "edit_patch": dump(patches[0]) if patches else None,
            "agent_trace": trace_delta(
                {
                    "agent": "itinerary_agent",
                    "action": "apply_edit",
                    "operations": [p.operation for p in patches],
                    "days": sorted(touched),
                    "notes": edit_notes,
                    "preserved_days": [
                        d.day_index
                        for d in updated.days
                        if d.day_index not in touched
                    ],
                }
            ),
        }

    # Preserve prior days named by Reviewer ("Preserve Day N").
    preserve: dict[int, DayPlan] = {}
    if prev and rev:
        for c in rev:
            lower = c.lower()
            if "preserve day" in lower:
                try:
                    day_n = int(
                        "".join(ch for ch in lower.split("day", 1)[1] if ch.isdigit())
                        or "0"
                    )
                except ValueError:
                    day_n = 0
                for d in prev.days:
                    if d.day_index == day_n:
                        preserve[day_n] = d

    logger.info(
        "NODE itinerary_agent city=%s days=%s pois=%d constraints=%s",
        trip.city,
        trip.num_days,
        len(pois),
        rev,
    )
    if trip.num_days is None or trip.pace is None:
        return {
            "agent_trace": trace_delta(
                {
                    "agent": "itinerary_agent",
                    "action": "skip",
                    "detail": "Trip days/pace not confirmed yet.",
                }
            )
        }
    draft = build_itinerary(
        candidate_pois=pois,
        num_days=trip.num_days,
        pace=trip.pace,
        daily_time_window_min=trip.daily_time_window_min,
        interests=trip.interests,
        city=trip.city,
        revision_constraints=(list(trip.constraints) + rev) or None,
        preserve_days=preserve or None,
        selection_mode=selection_mode,
    )

    # --- Optimization ownership (Itinerary Agent) ---
    skeleton = Itinerary(
        trip=trip.model_copy(update={"confirmed": True}),
        days=list(draft.days),
        sources=[],
        reasoning=[],
    )
    optimized, reasoning, mode, _ = optimize_itinerary(
        skeleton,
        weather=as_weather(state.get("weather_results")),
        knowledge=as_knowledge(state.get("knowledge_results")),
        travel=as_travel(state.get("travel_time_results")),
        poi=poi,
    )
    # After optimize, restore any stated interest that disappeared (e.g. park).
    from agent.mcp.itinerary_builder import ensure_interest_coverage

    cov_days, cov_notes = ensure_interest_coverage(
        list(optimized.days),
        interests=list(trip.interests or []),
        candidate_pois=pois,
        pace=trip.pace or "relaxed",
    )
    if cov_notes:
        reasoning = list(dict.fromkeys([*(reasoning or []), *cov_notes]))
        optimized = optimized.model_copy(
            update={"days": cov_days, "reasoning": reasoning}
        )
    draft = draft.model_copy(
        update={
            "days": optimized.days,
            "pace": optimized.trip.pace if optimized.trip else draft.pace,
            "optimization_reasoning": reasoning,
            "notes": (
                ((draft.notes or "") + " | " if draft.notes else "")
                + f"Optimized ({mode}): "
                + "; ".join(reasoning[:3])
            ).strip(" |"),
        }
    )
    stops = sum(len(d.all_stops) for d in draft.days)
    day_summary = [
        {
            "day": d.day_index,
            "stops": len(d.all_stops),
            "names": [s.name for s in d.all_stops],
        }
        for d in draft.days
    ]
    logger.info(
        "NODE itinerary_agent → %d days missing=%s mode=%s reasoning=%s",
        len(draft.days),
        draft.missing_data,
        mode,
        reasoning,
    )
    return {
        "itinerary_draft": dump(draft),
        "agent_trace": trace_delta(
            {
                "agent": "itinerary_agent",
                "tool": "itinerary_builder_mcp + optimizer",
                "action": "build_and_optimize",
                "city": trip.city,
                "num_days": trip.num_days,
                "pace": trip.pace,
                "input_pois": len(pois),
                "output_stops": stops,
                "missing_data": draft.missing_data,
                "optimize_mode": mode,
                "reasoning": reasoning[:6],
                "days": day_summary,
                "notes": draft.notes,
                "constraints": rev,
            }
        ),
    }


def _is_confirmish_utterance(message: str) -> bool:
    """True for short confirm/ack turns that must never trigger RAG."""
    lower = (message or "").lower().strip()
    if not lower:
        return False
    # Pure confirm / yes (allow trailing punctuation).
    if re.fullmatch(
        r"(yes|yeah|yep|yup|ok(?:ay)?|confirm|sure|go ahead|sounds good)"
        r"[\s.!?]*",
        lower,
    ):
        return True
    # Gemini STT sometimes wraps “yes” in meta-commentary.
    if re.search(
        r"transcription task|i will transcribe|audio contains the word",
        lower,
    ) and re.search(r"\b(yes|yeah|yep|yup|ok(?:ay)?|confirm)\b", lower):
        return True
    return False


def knowledge_agent_node(state: GraphState) -> dict[str, Any]:
    intent = state.get("intent") or "plan"
    msg = (state.get("user_message") or "").strip()
    # Hard gate: RAG is explain / place-Q&A only — never during itinerary build.
    if intent != "explain" or _is_confirmish_utterance(msg):
        logger.info(
            "NODE knowledge_agent SKIP intent=%s confirmish=%s (RAG is explain-only)",
            intent,
            _is_confirmish_utterance(msg),
        )
        return {
            "agent_trace": trace_delta(
                {
                    "agent": "knowledge_agent",
                    "action": "skip",
                    "detail": "RAG skipped during plan/edit/confirm — explain-only.",
                    "intent": intent,
                }
            ),
        }

    trip = as_trip(state.get("trip_constraints"))
    city = trip.city if trip else "Jaipur"
    topics = ["tips", "doable"]
    lower = msg.lower()
    if "rain" in lower or "weather" in lower:
        topics = ["rain", "weather", "indoor"]
    elif re.search(r"\b(safe|safety|scam|theft|caution)\b", lower):
        topics = ["safety", "tips", "culture"]
    elif re.search(r"\b(etiquette|customs|dress|respect)\b", lower):
        topics = ["culture", "tips", "safety"]
    elif re.search(r"\b(crowd|crowded|busy|queue)\b", lower):
        topics = ["crowd", "tips", "timing", "highlights"]
    elif re.search(r"\b(best time|morning|evening|hours|open)\b", lower):
        topics = ["timing", "tips", "highlights"]
    elif "why" in lower or "pick" in lower or "choose" in lower:
        topics = ["why", "highlights", "culture", "tips"]
    elif "doable" in lower or "feasible" in lower or "packed" in lower:
        topics = ["doable", "tips", "pace"]
    elif re.search(r"\b(area|neighborhood|what to do)\b", lower):
        topics = ["highlights", "doable", "tips"]
    if trip and trip.interests:
        topics = list(trip.interests[:2]) + topics
    if trip and trip.traveler_profile and trip.traveler_profile != "general":
        topics = [trip.traveler_profile.replace("_", " "), *topics]
    rev = _revision_constraints(state)
    query = msg or "travel tips highlights food culture"
    if trip and trip.traveler_profile and trip.traveler_profile != "general":
        query = f"{query} {trip.traveler_profile.replace('_', ' ')}"
    if rev:
        query = f"{query} {' '.join(rev)}"
    logger.info(
        "NODE knowledge_agent city=%s topics=%s revision=%s",
        city,
        topics,
        _revision_reason(state),
    )
    result = knowledge_search(city=city, query=query, topics=topics, k=4)
    logger.info(
        "NODE knowledge_agent → snippets=%d missing=%s",
        len(result.snippets),
        result.missing_data,
    )
    snippets = [
        {
            "text": (s.text or "")[:180],
            "source": (s.citations[0].title if s.citations else None),
        }
        for s in result.snippets[:4]
    ]
    return {
        "knowledge_results": dump(result),
        "agent_trace": trace_delta(
            {
                "agent": "knowledge_agent",
                "tool": "knowledge_rag",
                "source": "Wikivoyage corpus via Chroma/BGE or BM25",
                "action": "retrieve",
                "city": city,
                "query": query[:200],
                "topics": topics,
                "hit_count": len(result.snippets),
                "missing_data": result.missing_data,
                "snippets": snippets,
                "notes": result.notes,
            }
        ),
    }


def weather_agent_node(state: GraphState) -> dict[str, Any]:
    trip = as_trip(state.get("trip_constraints"))
    if trip is None:
        return {
            "agent_trace": trace_delta(
                {
                    "agent": "weather_agent",
                    "action": "skip",
                    "detail": "No trip constraints.",
                }
            )
        }
    start = trip.start_date.isoformat() if trip.start_date else None
    logger.info(
        "NODE weather_agent city=%s days=%s revision=%s",
        trip.city,
        trip.num_days,
        _revision_reason(state),
    )
    result = weather_adjustment(
        city=trip.city,
        start_date=start,
        num_days=int(trip.num_days or 3),
    )
    logger.info(
        "NODE weather_agent → days=%d missing=%s",
        len(result.days),
        result.missing_data,
    )
    day_risks = [
        {
            "day": i + 1,
            "rain_risk": getattr(d, "rain_risk", None),
            "recommendation": getattr(d, "recommendation", None),
        }
        for i, d in enumerate(result.days[:4])
    ]
    return {
        "weather_results": dump(result),
        "agent_trace": trace_delta(
            {
                "agent": "weather_agent",
                "tool": "weather_adjustment_mcp",
                "source": "Open-Meteo forecast API",
                "action": "forecast",
                "city": trip.city,
                "num_days": trip.num_days,
                "missing_data": result.missing_data,
                "days": day_risks,
                "notes": result.notes,
            }
        ),
    }


def travel_time_agent_node(state: GraphState) -> dict[str, Any]:
    """Estimate travel among POI candidates (or draft stops if present)."""
    draft = as_draft(state.get("itinerary_draft"))
    points: list[dict[str, Any]] = []
    if draft and draft.days:
        for stop in draft.days[0].all_stops:
            points.append(stop.model_dump(mode="json"))
    else:
        # Prefer hybrid shortlist so travel edges match candidates that can be packed.
        shortlist = as_poi(state.get("poi_shortlist"))
        poi = as_poi(state.get("poi_results"))
        pool = (
            shortlist.pois
            if shortlist and shortlist.pois
            else (poi.pois if poi else [])
        )
        if pool:
            # Estimate among the full shortlist (or first 12 of legacy pool).
            limit = len(pool) if shortlist and shortlist.pois else 8
            for p in pool[: max(limit, 8)]:
                points.append(p.model_dump(mode="json"))
        else:
            itin = as_itinerary(
                state.get("merged_itinerary") or state.get("previous_itinerary")
            )
            if itin and itin.days:
                for stop in itin.days[0].all_stops:
                    points.append(stop.model_dump(mode="json"))

    logger.info(
        "NODE travel_time_agent points=%d revision=%s",
        len(points),
        _revision_reason(state),
    )
    result = estimate_travel_times(points=points, mode="city")
    logger.info(
        "NODE travel_time_agent → legs=%d missing=%s",
        len(result.legs),
        result.missing_data,
    )
    legs = [
        {
            "from": getattr(leg, "from_name", None),
            "to": getattr(leg, "to_name", None),
            "duration_min": getattr(leg, "duration_min", None),
        }
        for leg in result.legs[:8]
    ]
    return {
        "travel_time_results": dump(result),
        "agent_trace": trace_delta(
            {
                "agent": "travel_time_agent",
                "tool": "travel_time_estimator_mcp",
                "source": "Haversine city-travel heuristic (not live transit)",
                "action": "estimate",
                "point_count": len(points),
                "leg_count": len(result.legs),
                "missing_data": result.missing_data,
                "legs": legs,
                "notes": result.notes,
            }
        ),
    }
