"""Orchestrator execution plan — explicit waves + success criteria.

Makes planning autonomy tangible: the Orchestrator produces a strategy object,
then dispatches waves and checks whether required artifacts exist before
proceeding to the Synthesis Agent.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

AgentName = Literal[
    "poi_agent",
    "itinerary_agent",
    "knowledge_agent",
    "weather_agent",
    "travel_time_agent",
]

SuccessCriterion = Literal[
    "poi_candidates",
    "itinerary_complete",
    "travel_times_available",
    "citations_present",
    "weather_adjustments",
]


class ExecutionPlan(BaseModel):
    """Explicit multi-agent execution strategy from the Orchestrator."""

    waves: list[list[AgentName]] = Field(
        default_factory=list,
        description="Ordered parallel waves of specialist agents.",
    )
    success_criteria: list[SuccessCriterion] = Field(
        default_factory=list,
        description="Artifacts that must exist before Synthesis may run.",
    )
    reason: str | None = Field(
        default=None,
        description="Why this plan was chosen (heuristic or LLM).",
    )
    planner: str = Field(
        default="heuristic",
        description="llm | heuristic | reviewer_feedback",
    )

    def flattened(self) -> list[AgentName]:
        seen: set[str] = set()
        out: list[AgentName] = []
        for wave in self.waves:
            for a in wave:
                if a not in seen:
                    seen.add(a)
                    out.append(a)
        return out


def success_criteria_for_waves(
    waves: list[list[str]],
) -> list[SuccessCriterion]:
    """Derive completion checks from which agents are planned."""
    flat = {a for wave in waves for a in wave}
    criteria: list[SuccessCriterion] = []
    if "poi_agent" in flat:
        criteria.append("poi_candidates")
    if "travel_time_agent" in flat:
        criteria.append("travel_times_available")
    if "knowledge_agent" in flat:
        criteria.append("citations_present")
    if "weather_agent" in flat:
        criteria.append("weather_adjustments")
    if "itinerary_agent" in flat:
        criteria.append("itinerary_complete")
    return criteria


def artifacts_complete(
    state: dict[str, Any],
    criteria: list[str] | None,
) -> tuple[bool, list[str]]:
    """
    Return (ok, missing_criteria).

    Orchestrator uses this instead of a bare ready_for_merger flag.
    """
    missing: list[str] = []
    for c in criteria or []:
        if c == "poi_candidates":
            poi = state.get("poi_results")
            ok = bool(poi) and (
                (isinstance(poi, dict) and poi.get("pois"))
                or (not isinstance(poi, dict) and getattr(poi, "pois", None))
            )
            if not ok:
                missing.append(c)
        elif c == "itinerary_complete":
            draft = state.get("itinerary_draft")
            ok = bool(draft) and (
                (isinstance(draft, dict) and draft.get("days"))
                or (not isinstance(draft, dict) and getattr(draft, "days", None))
            )
            if not ok:
                missing.append(c)
        elif c == "travel_times_available":
            travel = state.get("travel_time_results")
            ok = bool(travel) and (
                (isinstance(travel, dict) and travel.get("legs") is not None)
                or (not isinstance(travel, dict) and hasattr(travel, "legs"))
            )
            if not ok:
                missing.append(c)
        elif c == "citations_present":
            knowledge = state.get("knowledge_results")
            if not knowledge:
                missing.append(c)
        elif c == "weather_adjustments":
            weather = state.get("weather_results")
            if not weather:
                missing.append(c)
    return (len(missing) == 0), missing


def agents_for_missing_criteria(missing: list[str]) -> list[AgentName]:
    """Map unmet success criteria back to specialists to re-dispatch."""
    mapping: dict[str, AgentName] = {
        "poi_candidates": "poi_agent",
        "itinerary_complete": "itinerary_agent",
        "travel_times_available": "travel_time_agent",
        "citations_present": "knowledge_agent",
        "weather_adjustments": "weather_agent",
    }
    out: list[AgentName] = []
    for c in missing:
        a = mapping.get(c)
        if a and a not in out:
            out.append(a)
    return out
