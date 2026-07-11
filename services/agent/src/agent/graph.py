"""Phase 0 LangGraph stub: START → orchestrator → END.

Later phases add specialist nodes (POI, itinerary, knowledge), merger,
reviewer, and conditional revise loops.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph


class GraphState(TypedDict, total=False):
    """Shared graph state — expanded in Phase 1."""

    user_message: str
    intent: str
    safety_status: Literal["ok", "blocked", "needs_clarify"]
    user_reply: str
    revision_count: int


def orchestrator(state: GraphState) -> GraphState:
    """Stub orchestrator: acknowledge input; no specialist dispatch yet."""
    message = (state.get("user_message") or "").strip()
    if not message:
        return {
            **state,
            "safety_status": "needs_clarify",
            "intent": "confirm",
            "user_reply": (
                "Phase 0 stub: I did not receive a trip request yet. "
                "Say something like “Plan a 3-day trip to Jaipur.”"
            ),
            "revision_count": state.get("revision_count", 0),
        }

    return {
        **state,
        "safety_status": "ok",
        "intent": "plan",
        "user_reply": (
            "Phase 0 stub: orchestrator reached. "
            f"Heard: “{message}”. "
            "Specialist agents (POI / itinerary / knowledge) arrive in Phase 4."
        ),
        "revision_count": state.get("revision_count", 0),
    }


def build_graph():
    graph = StateGraph(GraphState)
    graph.add_node("orchestrator", orchestrator)
    graph.add_edge(START, "orchestrator")
    graph.add_edge("orchestrator", END)
    return graph.compile()


# Compiled app used by CLI and HTTP API
app = build_graph()
