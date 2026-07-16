"""Shared helpers for LangGraph nodes."""

from __future__ import annotations

from typing import Any

from agent.schemas.edits import EditPatch
from agent.schemas.itinerary import Itinerary, TripConstraints
from agent.schemas.review import ReviewerVerdict
from agent.schemas.specialists import (
    DispatchPlan,
    ItineraryDraftResult,
    KnowledgeResult,
    POISearchResult,
    TravelTimeResult,
    WeatherResult,
)


def as_trip(value: Any) -> TripConstraints | None:
    if value is None:
        return None
    if isinstance(value, TripConstraints):
        return value
    try:
        return TripConstraints.model_validate(value)
    except Exception:  # noqa: BLE001
        return None


def as_dispatch(value: Any) -> DispatchPlan:
    if isinstance(value, DispatchPlan):
        return value
    if isinstance(value, dict):
        try:
            return DispatchPlan.model_validate(value)
        except Exception:  # noqa: BLE001
            return DispatchPlan()
    return DispatchPlan()


def as_poi(value: Any) -> POISearchResult | None:
    if value is None:
        return None
    if isinstance(value, POISearchResult):
        return value
    try:
        return POISearchResult.model_validate(value)
    except Exception:  # noqa: BLE001
        return None


def as_draft(value: Any) -> ItineraryDraftResult | None:
    if value is None:
        return None
    if isinstance(value, ItineraryDraftResult):
        return value
    try:
        return ItineraryDraftResult.model_validate(value)
    except Exception:  # noqa: BLE001
        return None


def as_knowledge(value: Any) -> KnowledgeResult | None:
    if value is None:
        return None
    if isinstance(value, KnowledgeResult):
        return value
    try:
        return KnowledgeResult.model_validate(value)
    except Exception:  # noqa: BLE001
        return None


def as_weather(value: Any) -> WeatherResult | None:
    if value is None:
        return None
    if isinstance(value, WeatherResult):
        return value
    try:
        return WeatherResult.model_validate(value)
    except Exception:  # noqa: BLE001
        return None


def as_travel(value: Any) -> TravelTimeResult | None:
    if value is None:
        return None
    if isinstance(value, TravelTimeResult):
        return value
    try:
        return TravelTimeResult.model_validate(value)
    except Exception:  # noqa: BLE001
        return None


def as_itinerary(value: Any) -> Itinerary | None:
    if value is None:
        return None
    if isinstance(value, Itinerary):
        return value
    try:
        return Itinerary.model_validate(value)
    except Exception:  # noqa: BLE001
        return None


def as_verdict(value: Any) -> ReviewerVerdict | None:
    if value is None:
        return None
    if isinstance(value, ReviewerVerdict):
        return value
    try:
        return ReviewerVerdict.model_validate(value)
    except Exception:  # noqa: BLE001
        return None


def as_edit_patch(value: Any) -> EditPatch | None:
    if value is None:
        return None
    if isinstance(value, EditPatch):
        return value
    try:
        return EditPatch.model_validate(value)
    except Exception:  # noqa: BLE001
        return None


def as_edit_patches(value: Any) -> list[EditPatch]:
    """Normalize one patch, a list of patches, or None → list[EditPatch]."""
    if value is None:
        return []
    if isinstance(value, EditPatch):
        return [value]
    if isinstance(value, dict):
        one = as_edit_patch(value)
        return [one] if one else []
    if isinstance(value, list):
        out: list[EditPatch] = []
        for item in value:
            p = as_edit_patch(item)
            if p:
                out.append(p)
        return out
    return []


def dump(model: Any) -> Any:
    if model is None:
        return None
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    return model
