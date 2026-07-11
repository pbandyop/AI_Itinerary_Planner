"""Specialist agent result envelopes written into LangGraph state."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from agent.schemas.itinerary import DayPlan, OsmType, Pace, Source


class POICandidate(BaseModel):
    name: str
    osm_type: OsmType
    osm_id: int = Field(..., gt=0)
    lat: float | None = None
    lon: float | None = None
    category: str | None = None
    tags: dict[str, Any] = Field(default_factory=dict)
    rank_score: float | None = None
    matched_interests: list[str] = Field(default_factory=list)


class POISearchResult(BaseModel):
    city: Literal["Jaipur"] = "Jaipur"
    query_interests: list[str] = Field(default_factory=list)
    pois: list[POICandidate] = Field(default_factory=list)
    missing_data: bool = False
    notes: str | None = None


class ItineraryDraftResult(BaseModel):
    """Raw day packing from Itinerary Builder MCP (pre-merge enrichment)."""

    pace: Pace
    days: list[DayPlan] = Field(default_factory=list)
    missing_data: bool = False
    notes: str | None = None


class KnowledgeSnippet(BaseModel):
    topic: str
    text: str
    citations: list[Source] = Field(default_factory=list)
    uncertainty: str | None = None


class KnowledgeResult(BaseModel):
    snippets: list[KnowledgeSnippet] = Field(default_factory=list)
    missing_data: bool = False
    notes: str | None = None


class DispatchPlan(BaseModel):
    """Orchestrator instructions for which specialists to run."""

    run_poi: bool = True
    run_itinerary: bool = True
    run_knowledge: bool = True
    run_weather: bool = False
    run_travel_time: bool = False
    edit_patch: dict[str, Any] | None = Field(
        default=None,
        description="Serialized EditPatch when intent is edit.",
    )
    target_sections: list[str] = Field(
        default_factory=list,
        description="e.g. ['day2', 'day2.evening'] for scoped re-runs.",
    )
