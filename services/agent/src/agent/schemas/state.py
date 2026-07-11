"""LangGraph shared state schema (Phase 1)."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph.message import add_messages

from agent.schemas.edits import EditPatch
from agent.schemas.itinerary import Itinerary, Source, TripConstraints
from agent.schemas.review import ReviewerVerdict
from agent.schemas.specialists import (
    DispatchPlan,
    ItineraryDraftResult,
    KnowledgeResult,
    POISearchResult,
)

Intent = Literal["plan", "edit", "explain", "confirm"]
SafetyStatus = Literal["ok", "blocked", "needs_clarify"]


class GraphState(TypedDict, total=False):
    """Shared state flowing through the LangGraph multi-agent pipeline."""

    # Conversation
    messages: Annotated[list[Any], add_messages]
    user_message: str
    user_reply: str
    intent: Intent
    safety_status: SafetyStatus

    # Planning inputs / routing
    trip_constraints: TripConstraints | dict[str, Any]
    dispatch_plan: DispatchPlan | dict[str, Any]
    edit_patch: EditPatch | dict[str, Any]

    # Specialist outputs
    poi_results: POISearchResult | dict[str, Any]
    itinerary_draft: ItineraryDraftResult | dict[str, Any]
    knowledge_results: KnowledgeResult | dict[str, Any]

    # Merger / Reviewer
    merged_itinerary: Itinerary | dict[str, Any]
    previous_itinerary: Itinerary | dict[str, Any]
    sources: list[Source] | list[dict[str, Any]]
    reviewer_verdict: ReviewerVerdict | dict[str, Any]
    revision_count: int


def empty_graph_state(*, user_message: str = "") -> GraphState:
    """Initial state for a new graph invocation."""
    return {
        "messages": [],
        "user_message": user_message,
        "user_reply": "",
        "intent": "plan",
        "safety_status": "ok",
        "revision_count": 0,
        "sources": [],
    }
