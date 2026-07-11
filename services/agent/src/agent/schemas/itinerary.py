"""Core itinerary JSON contract used by Merger, Reviewer, UI, PDF, and evals."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Pace = Literal["relaxed", "moderate", "packed"]
TimeOfDay = Literal["morning", "afternoon", "evening"]
OsmType = Literal["node", "way", "relation"]


class Source(BaseModel):
    """Citation or dataset attribution shown in the UI Sources panel."""

    title: str
    url: str | None = None
    dataset: Literal["openstreetmap", "wikivoyage", "wikipedia", "open-meteo", "other"] = (
        "other"
    )
    snippet: str | None = None
    source_id: str | None = Field(
        default=None,
        description="Stable id within the dataset (e.g. OSM node id, wiki page id).",
    )


class Stop(BaseModel):
    """A single place visit within a time block."""

    name: str
    osm_type: OsmType
    osm_id: int = Field(..., gt=0, description="OpenStreetMap element id — required.")
    lat: float | None = None
    lon: float | None = None
    category: str | None = Field(
        default=None, description="e.g. heritage, food, market, park"
    )
    duration_min: int = Field(..., ge=15, le=480)
    travel_to_next_min: int | None = Field(
        default=None,
        ge=0,
        description="Estimated travel minutes to the next stop; null if last in block/day.",
    )
    reason: str = Field(..., min_length=1, description="Why this stop was chosen.")
    citations: list[Source] = Field(default_factory=list)
    uncertainty: str | None = Field(
        default=None,
        description="Explicit uncertainty when data is missing (required if no citations for factual tips).",
    )

    @property
    def osm_ref(self) -> str:
        return f"{self.osm_type}/{self.osm_id}"


class TimeBlock(BaseModel):
    time_of_day: TimeOfDay
    stops: list[Stop] = Field(default_factory=list)
    notes: str | None = None

    @property
    def total_duration_min(self) -> int:
        stop_time = sum(s.duration_min for s in self.stops)
        travel = sum(s.travel_to_next_min or 0 for s in self.stops)
        return stop_time + travel


class DayPlan(BaseModel):
    day_index: int = Field(..., ge=1, le=4)
    calendar_date: date | None = None
    theme: str | None = None
    morning: TimeBlock = Field(default_factory=lambda: TimeBlock(time_of_day="morning"))
    afternoon: TimeBlock = Field(
        default_factory=lambda: TimeBlock(time_of_day="afternoon")
    )
    evening: TimeBlock = Field(default_factory=lambda: TimeBlock(time_of_day="evening"))

    @model_validator(mode="after")
    def _align_block_labels(self) -> DayPlan:
        self.morning.time_of_day = "morning"
        self.afternoon.time_of_day = "afternoon"
        self.evening.time_of_day = "evening"
        return self

    def block(self, time_of_day: TimeOfDay) -> TimeBlock:
        return getattr(self, time_of_day)

    @property
    def all_stops(self) -> list[Stop]:
        return [*self.morning.stops, *self.afternoon.stops, *self.evening.stops]

    @property
    def total_duration_min(self) -> int:
        return (
            self.morning.total_duration_min
            + self.afternoon.total_duration_min
            + self.evening.total_duration_min
        )


class TripConstraints(BaseModel):
    """User preferences collected / confirmed by the Orchestrator."""

    city: Literal["Jaipur"] = "Jaipur"
    num_days: int = Field(..., ge=2, le=4)
    start_date: date | None = None
    end_date: date | None = None
    interests: list[str] = Field(default_factory=list)
    pace: Pace = "relaxed"
    constraints: list[str] = Field(
        default_factory=list,
        description="Free-form constraints e.g. 'prefer indoor evenings', 'vegetarian'.",
    )
    daily_time_window_min: int = Field(
        default=540,
        ge=180,
        le=840,
        description="Available activity minutes per day (default 9h).",
    )
    confirmed: bool = False

    @field_validator("interests")
    @classmethod
    def _normalize_interests(cls, values: list[str]) -> list[str]:
        return [v.strip().lower() for v in values if v and v.strip()]


class Itinerary(BaseModel):
    """Full day-wise plan — single source of truth across agents, UI, PDF, evals."""

    schema_version: Literal["1.0"] = "1.0"
    trip: TripConstraints
    days: list[DayPlan] = Field(..., min_length=2, max_length=4)
    sources: list[Source] = Field(default_factory=list)
    summary: str | None = None
    uncertainty_notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _days_match_trip(self) -> Itinerary:
        if len(self.days) != self.trip.num_days:
            raise ValueError(
                f"trip.num_days={self.trip.num_days} but days has {len(self.days)} entries"
            )
        indexes = [d.day_index for d in self.days]
        if indexes != list(range(1, len(self.days) + 1)):
            raise ValueError(f"day_index values must be 1..N in order; got {indexes}")
        for day in self.days:
            for stop in day.all_stops:
                if not stop.citations and not stop.uncertainty:
                    # Soft rule enforced strictly in validate_grounding_rules;
                    # keep model parseable for drafts mid-pipeline.
                    pass
        return self
