"""Shared Pydantic contracts for itineraries, specialists, edits, and review."""

from agent.schemas.edits import EditOperation, EditPatch, EditTarget
from agent.schemas.itinerary import (
    DayPlan,
    Itinerary,
    Pace,
    Source,
    Stop,
    TimeBlock,
    TimeOfDay,
    TripConstraints,
)
from agent.schemas.review import ReviewIssue, ReviewStatus, ReviewerVerdict
from agent.schemas.specialists import (
    DayWeather,
    DispatchPlan,
    ItineraryDraftResult,
    KnowledgeResult,
    KnowledgeSnippet,
    POICandidate,
    POISearchResult,
    TravelLeg,
    TravelTimeResult,
    WeatherAdjustment,
    WeatherResult,
)
from agent.schemas.state import GraphState, Intent, SafetyStatus, empty_graph_state
from agent.schemas.validation import (
    ValidationResult,
    itinerary_to_json_schema,
    load_and_validate_itinerary,
    validate_grounding_rules,
    validate_itinerary,
)

__all__ = [
    "DayPlan",
    "DayWeather",
    "DispatchPlan",
    "EditOperation",
    "EditPatch",
    "EditTarget",
    "GraphState",
    "Intent",
    "Itinerary",
    "ItineraryDraftResult",
    "KnowledgeResult",
    "KnowledgeSnippet",
    "Pace",
    "POICandidate",
    "POISearchResult",
    "ReviewIssue",
    "ReviewStatus",
    "ReviewerVerdict",
    "SafetyStatus",
    "Source",
    "Stop",
    "TimeBlock",
    "TimeOfDay",
    "TravelLeg",
    "TravelTimeResult",
    "TripConstraints",
    "ValidationResult",
    "WeatherAdjustment",
    "WeatherResult",
    "empty_graph_state",
    "itinerary_to_json_schema",
    "load_and_validate_itinerary",
    "validate_grounding_rules",
    "validate_itinerary",
]
