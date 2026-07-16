"""Reviewer agent verdict — approve or request targeted specialist revisions."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

ReviewStatus = Literal["approve", "revise"]

# LangGraph specialist node ids the Reviewer may target
TargetAgent = Literal[
    "poi_agent",
    "itinerary_agent",
    "knowledge_agent",
    "weather_agent",
    "travel_time_agent",
]

_TARGET_ALIASES: dict[str, TargetAgent] = {
    "poi_agent": "poi_agent",
    "poi agent": "poi_agent",
    "poi": "poi_agent",
    "itinerary_agent": "itinerary_agent",
    "itinerary agent": "itinerary_agent",
    "itinerary": "itinerary_agent",
    "knowledge_agent": "knowledge_agent",
    "knowledge agent": "knowledge_agent",
    "knowledge": "knowledge_agent",
    "rag": "knowledge_agent",
    "weather_agent": "weather_agent",
    "weather agent": "weather_agent",
    "weather": "weather_agent",
    "travel_time_agent": "travel_time_agent",
    "travel-time agent": "travel_time_agent",
    "travel time agent": "travel_time_agent",
    "travel_time": "travel_time_agent",
    "travel time": "travel_time_agent",
}


def normalize_target_agent(value: str | None) -> TargetAgent | None:
    if not value:
        return None
    key = value.strip().lower().replace("-", "_")
    key = key.replace("__", "_")
    # Also try spaced form
    spaced = value.strip().lower()
    return _TARGET_ALIASES.get(key) or _TARGET_ALIASES.get(spaced)


class ReviewIssue(BaseModel):
    code: Literal[
        "feasibility_duration",
        "feasibility_travel",
        "feasibility_pace",
        "grounding_osm",
        "grounding_citation",
        "edit_scope",
        "missing_data",
        "other",
    ]
    message: str
    section: str | None = Field(
        default=None, description="e.g. day2.afternoon or trip.pace"
    )


class ReviewerVerdict(BaseModel):
    """Autonomous Reviewer feedback for the Orchestrator.

    On ``revise``, ``target_agent`` + ``constraints`` tell the Orchestrator
    exactly which specialist to re-invoke and what rules to pass through —
    no inference required.
    """

    status: ReviewStatus
    reason: str | None = Field(
        default=None,
        description="Human-readable why approve/revise (primary signal).",
    )
    target_agent: TargetAgent | None = Field(
        default=None,
        description="Specialist node to re-run on revise (e.g. itinerary_agent).",
    )
    constraints: list[str] = Field(
        default_factory=list,
        description="Revision rules for the target specialist, e.g. 'Reduce travel'.",
    )
    issues: list[ReviewIssue] = Field(default_factory=list)
    affected_sections: list[str] = Field(default_factory=list)
    notes: str | None = None

    @field_validator("target_agent", mode="before")
    @classmethod
    def _coerce_target(cls, v: object) -> object:
        if v is None or v == "":
            return None
        if isinstance(v, str):
            return normalize_target_agent(v)
        return v
