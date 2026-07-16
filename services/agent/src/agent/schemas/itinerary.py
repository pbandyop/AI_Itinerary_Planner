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
    travel_to_next_km: float | None = Field(
        default=None,
        ge=0,
        description="Estimated distance (km) to the next stop from Travel Time MCP.",
    )
    travel_to_next_mode: Literal["walk", "car", "bus"] | None = Field(
        default=None,
        description="Transport mode for the next leg when available from MCP; omit when unknown.",
    )
    arrive_time: str | None = Field(
        default=None,
        description="Estimated arrival clock time HH:MM (24h), stamped by itinerary builder.",
    )
    depart_time: str | None = Field(
        default=None,
        description="Estimated departure clock time HH:MM (24h) = arrive + duration.",
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

    city: str = Field(
        ...,
        min_length=2,
        description="Indian city from data/india_cities.json (one city per trip).",
    )
    country: Literal["India"] = "India"
    num_days: int | None = Field(
        default=None,
        description="2–4 once the user states it; None while still clarifying.",
    )
    start_date: date | None = None
    end_date: date | None = None
    interests: list[str] = Field(default_factory=list)
    pace: Pace | None = Field(
        default=None,
        description="Required before generation; None until the user states it.",
    )
    traveler_profile: str | None = Field(
        default=None,
        description=(
            "Audience profile: kid_friendly, senior_friendly, couple_friendly, "
            "friends_friendly, solo, or general."
        ),
    )
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
    clarify_turns: int = Field(
        default=0,
        ge=0,
        le=6,
        description="How many clarifying questions have been asked this session.",
    )
    days_known: bool = Field(
        default=False,
        description="True once the user explicitly stated the day count.",
    )
    pace_known: bool = Field(
        default=False,
        description="True once the user explicitly stated the pace.",
    )
    interests_known: bool = Field(
        default=False,
        description="True once the user explicitly stated interests.",
    )
    dates_known: bool = Field(
        default=False,
        description=(
            "True once the user stated a trip start date or said dates are flexible."
        ),
    )
    @field_validator("city")
    @classmethod
    def _normalize_city(cls, value: str) -> str:
        from agent.mcp.geo import resolve_city
        from agent.trip_limits import ALLOWED_CITIES, is_city_allowed

        info = resolve_city(value)
        if info is None:
            raise ValueError(
                f"City {value!r} is not in the India catalog (data/india_cities.json)."
            )
        if not is_city_allowed(info.name):
            raise ValueError(
                f"City {info.name!r} is out of the current demo scope. "
                f"Supported: {', '.join(ALLOWED_CITIES)}."
            )
        return info.name

    @field_validator("interests")
    @classmethod
    def _normalize_interests(cls, values: list[str]) -> list[str]:
        from agent.preferences import normalize_interests

        return normalize_interests(list(values or []))

    @field_validator("traveler_profile")
    @classmethod
    def _normalize_profile(cls, value: str | None) -> str | None:
        if value is None:
            return None
        key = value.strip().lower().replace(" ", "_").replace("-", "_")
        allowed = {
            "kid_friendly",
            "senior_friendly",
            "couple_friendly",
            "friends_friendly",
            "solo",
            "general",
        }
        return key if key in allowed else "general"

    @field_validator("num_days")
    @classmethod
    def _check_days(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 2 or value > 4:
            raise ValueError("num_days must be between 2 and 4")
        return value

    def slots_ready(self) -> bool:
        """True when days, pace, interests, and travel dates are user-known."""
        return bool(
            self.days_known
            and self.num_days is not None
            and self.pace_known
            and self.pace is not None
            and self.interests_known
            and bool(self.interests)
            and self.dates_known
        )


class Itinerary(BaseModel):
    """Full day-wise plan — single source of truth across agents, UI, PDF, evals."""

    schema_version: Literal["1.0"] = "1.0"
    trip: TripConstraints
    days: list[DayPlan] = Field(..., min_length=2, max_length=4)
    sources: list[Source] = Field(default_factory=list)
    summary: str | None = None
    uncertainty_notes: list[str] = Field(default_factory=list)
    reasoning: list[str] = Field(
        default_factory=list,
        description=(
            "Merger synthesis decisions — why the Itinerary Agent moved/skipped/clustered "
            "when resolving weather, travel, and knowledge conflicts."
        ),
    )

    @model_validator(mode="after")
    def _days_match_trip(self) -> Itinerary:
        if self.trip.num_days is None:
            raise ValueError("trip.num_days must be set on a complete itinerary")
        if self.trip.pace is None:
            raise ValueError("trip.pace must be set on a complete itinerary")
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
                    pass
        return self
