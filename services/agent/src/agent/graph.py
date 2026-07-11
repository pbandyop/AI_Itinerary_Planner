"""Phase 1 LangGraph stub using full shared GraphState.

Graph shape remains START → orchestrator → END until Phase 4 adds specialists.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from agent.schemas.state import GraphState, empty_graph_state


def orchestrator(state: GraphState) -> dict:
    """Stub orchestrator: acknowledge input; no specialist dispatch yet."""
    message = (state.get("user_message") or "").strip()
    revision_count = state.get("revision_count", 0)

    if not message:
        return {
            "safety_status": "needs_clarify",
            "intent": "confirm",
            "user_reply": (
                "I did not receive a trip request yet. "
                "Say something like “Plan a 3-day trip to Jaipur.”"
            ),
            "revision_count": revision_count,
        }

    return {
        "safety_status": "ok",
        "intent": "plan",
        "user_reply": (
            "Phase 1: schema + graph state ready. "
            f"Heard: “{message}”. "
            "Specialist agents arrive in Phase 4; MCP tools in Phase 2."
        ),
        "revision_count": revision_count,
        "trip_constraints": {
            "city": "Jaipur",
            "num_days": 3,
            "interests": [],
            "pace": "relaxed",
            "constraints": [],
            "confirmed": False,
        },
    }


def build_graph():
    graph = StateGraph(GraphState)
    graph.add_node("orchestrator", orchestrator)
    graph.add_edge(START, "orchestrator")
    graph.add_edge("orchestrator", END)
    return graph.compile()


app = build_graph()


def invoke_stub(user_message: str) -> GraphState:
    initial = empty_graph_state(user_message=user_message)
    return app.invoke(initial)  # type: ignore[return-value]
