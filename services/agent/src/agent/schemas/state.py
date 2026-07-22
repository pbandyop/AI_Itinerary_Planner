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
    TravelTimeResult,
    WeatherResult,
)

Intent = Literal["plan", "edit", "explain", "confirm"]
SafetyStatus = Literal["ok", "blocked", "needs_clarify"]


def _last_value(left: Any, right: Any) -> Any:
    """Reducer for concurrent specialist writes — keep the latest non-null."""
    return right if right is not None else left


def _overwrite_value(left: Any, right: Any) -> Any:
    """Always keep the latest write, including explicit ``None`` clears."""
    del left
    return right


def concat_trace(
    left: list[dict[str, Any]] | None, right: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Append-only reducer so parallel specialists don't overwrite each other."""
    return list(left or []) + list(right or [])


class GraphState(TypedDict, total=False):
    """Shared state flowing through the LangGraph multi-agent pipeline."""

    messages: Annotated[list[Any], add_messages]
    user_message: str
    user_reply: Annotated[str, _last_value]
    intent: Annotated[Intent, _last_value]
    safety_status: Annotated[SafetyStatus, _last_value]

    trip_constraints: Annotated[TripConstraints | dict[str, Any], _last_value]
    dispatch_plan: Annotated[DispatchPlan | dict[str, Any], _last_value]
    edit_patch: Annotated[EditPatch | dict[str, Any] | None, _last_value]
    edit_patches: Annotated[list[EditPatch] | list[dict[str, Any]] | None, _last_value]

    poi_results: Annotated[POISearchResult | dict[str, Any], _last_value]
    # Hybrid strategy: travel + pack operate on this shortlist when present.
    poi_shortlist: Annotated[POISearchResult | dict[str, Any] | None, _last_value]
    itinerary_draft: Annotated[ItineraryDraftResult | dict[str, Any], _last_value]
    knowledge_results: Annotated[KnowledgeResult | dict[str, Any], _last_value]
    travel_time_results: Annotated[TravelTimeResult | dict[str, Any], _last_value]
    weather_results: Annotated[WeatherResult | dict[str, Any], _last_value]

    merged_itinerary: Annotated[Itinerary | dict[str, Any], _last_value]
    previous_itinerary: Annotated[Itinerary | dict[str, Any], _last_value]
    sources: Annotated[list[Source] | list[dict[str, Any]], _last_value]
    # Full selected grounding text(s) for Eval CSV — UI still uses short sources.snippet.
    grounding_documents: Annotated[list[dict[str, Any]], _overwrite_value]
    reviewer_verdict: Annotated[ReviewerVerdict | dict[str, Any] | None, _last_value]
    revision_count: Annotated[int, _last_value]
    # Structured Reviewer → Orchestrator revision packet (survives verdict clear)
    revision_feedback: Annotated[dict[str, Any] | None, _last_value]

    # Multi-agent Orchestrator control loop (waves = parallel batches)
    orchestration_started: Annotated[bool, _last_value]
    ready_for_synthesis: Annotated[bool, _last_value]
    ready_for_merger: Annotated[bool, _last_value]  # compat alias of ready_for_synthesis
    next_agent: Annotated[str | None, _last_value]  # legacy single; prefer next_agents
    next_agents: Annotated[list[str], _last_value]
    pending_agents: Annotated[list[str], _last_value]  # flattened remaining (compat)
    pending_waves: Annotated[list[list[str]], _last_value]
    orchestrator_steps: Annotated[int, _last_value]
    agent_trace: Annotated[list[dict[str, Any]], concat_trace]
    # Multi-turn dialog (rain month ask, indoor-swap offer, weather date ask).
    # Must allow None clears — `_last_value` would leave sticky dialogs forever.
    pending_dialog: Annotated[dict[str, Any] | None, _overwrite_value]


def empty_graph_state(*, user_message: str = "") -> GraphState:
    """Initial state for a new graph invocation."""
    return {
        "messages": [],
        "user_message": user_message,
        "user_reply": "",
        "intent": "plan",
        "safety_status": "ok",
        "revision_count": 0,
        "revision_feedback": None,
        "sources": [],
        "grounding_documents": [],
        "orchestration_started": False,
        "ready_for_synthesis": False,
        "ready_for_merger": False,
        "next_agent": None,
        "next_agents": [],
        "pending_agents": [],
        "pending_waves": [],
        "orchestrator_steps": 0,
        "agent_trace": [],
    }
