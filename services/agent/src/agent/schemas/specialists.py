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
    city: str
    country: Literal["India"] = "India"
    query_interests: list[str] = Field(default_factory=list)
    query_constraints: list[str] = Field(
        default_factory=list,
        description="Soft constraints passed into POI Search MCP (e.g. profile filters).",
    )
    pois: list[POICandidate] = Field(default_factory=list)
    missing_data: bool = False
    notes: str | None = None


class ItineraryDraftResult(BaseModel):
    """Optimized day packing from the Itinerary Builder MCP / Itinerary Agent."""

    pace: Pace
    daily_time_window_min: int | None = Field(
        default=None,
        description="Activity minutes per day used when packing stops.",
    )
    days: list[DayPlan] = Field(default_factory=list)
    missing_data: bool = False
    notes: str | None = None
    optimization_reasoning: list[str] = Field(
        default_factory=list,
        description="Itinerary Agent decisions (move/skip/reorder) with reasons.",
    )


class KnowledgeSnippet(BaseModel):
    topic: str
    text: str
    citations: list[Source] = Field(default_factory=list)
    uncertainty: str | None = None


class KnowledgeResult(BaseModel):
    snippets: list[KnowledgeSnippet] = Field(default_factory=list)
    missing_data: bool = False
    notes: str | None = None


class TravelLeg(BaseModel):
    from_name: str
    to_name: str
    from_osm: str | None = None
    to_osm: str | None = None
    distance_km: float | None = None
    duration_min: int
    mode: Literal["walk", "city"] = "city"
    method: Literal["haversine_heuristic"] = "haversine_heuristic"


class TravelTimeResult(BaseModel):
    legs: list[TravelLeg] = Field(default_factory=list)
    total_duration_min: int = 0
    missing_data: bool = False
    notes: str | None = None


class DayWeather(BaseModel):
    calendar_date: str
    weather_code: int | None = None
    weather_label: str | None = None
    precip_probability_max: float | None = None
    precip_mm_sum: float | None = None
    temp_max_c: float | None = None
    temp_min_c: float | None = None
    rain_risk: Literal["low", "moderate", "high"] = "low"
    recommendation: str | None = None


class WeatherAdjustment(BaseModel):
    section: str = Field(description="e.g. day1.afternoon")
    action: Literal["prefer_indoor", "shorten_outdoor", "keep", "add_buffer"]
    reason: str


class WeatherResult(BaseModel):
    city: str
    country: Literal["India"] = "India"
    latitude: float
    longitude: float
    days: list[DayWeather] = Field(default_factory=list)
    adjustments: list[WeatherAdjustment] = Field(default_factory=list)
    missing_data: bool = False
    notes: str | None = None
    source: str = "Open-Meteo"


class DispatchPlan(BaseModel):
    """Orchestrator execution strategy (explicit plan object)."""

    run_poi: bool = True
    run_itinerary: bool = True
    run_knowledge: bool = True
    run_weather: bool = True
    run_travel_time: bool = True
    agent_sequence: list[
        Literal[
            "poi_agent",
            "itinerary_agent",
            "knowledge_agent",
            "weather_agent",
            "travel_time_agent",
        ]
    ] = Field(
        default_factory=list,
        description="Flattened specialist agents (wave order preserved).",
    )
    agent_waves: list[
        list[
            Literal[
                "poi_agent",
                "itinerary_agent",
                "knowledge_agent",
                "weather_agent",
                "travel_time_agent",
            ]
        ]
    ] = Field(
        default_factory=list,
        description="Parallel waves: agents in one wave are independent.",
    )
    success_criteria: list[str] = Field(
        default_factory=list,
        description=(
            "Artifacts required before Synthesis, e.g. itinerary_complete, "
            "travel_times_available, citations_present, weather_adjustments."
        ),
    )
    plan_reason: str | None = Field(
        default=None,
        description="Why the Orchestrator chose this execution plan.",
    )
    edit_patch: dict[str, Any] | None = Field(
        default=None,
        description="Serialized EditPatch when intent is edit.",
    )
    target_sections: list[str] = Field(
        default_factory=list,
        description="e.g. ['day2', 'day2.evening'] for scoped re-runs.",
    )

    def sync_flags_from_sequence(self) -> DispatchPlan:
        """Derive boolean flags from agent_sequence / agent_waves."""
        seq = set(self.agent_sequence)
        if not seq and self.agent_waves:
            seq = {a for wave in self.agent_waves for a in wave}
        if not seq:
            return self
        return self.model_copy(
            update={
                "run_poi": "poi_agent" in seq,
                "run_itinerary": "itinerary_agent" in seq,
                "run_knowledge": "knowledge_agent" in seq,
                "run_weather": "weather_agent" in seq,
                "run_travel_time": "travel_time_agent" in seq,
            }
        )
