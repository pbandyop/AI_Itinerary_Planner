"""Phase 4+ LangGraph multi-agent system.

Orchestrator produces an explicit execution plan (waves + success_criteria),
fans out independent specialists via ``Send``, then completes when required
artifacts exist → Synthesis (presentation) → Reviewer.

  START → orchestrator ⇄ specialists (waves)
                      ↓ (artifacts complete)
               synthesis_agent → reviewer → END | orchestrator (revise)
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from agent.nodes.orchestrator import orchestrator_node
from agent.nodes.reviewer import reviewer_node
from agent.nodes.specialists import (
    itinerary_agent_node,
    knowledge_agent_node,
    poi_agent_node,
    travel_time_agent_node,
    weather_agent_node,
)
from agent.nodes.state_utils import as_verdict
from agent.nodes.synthesis import synthesis_node
from agent.schemas.state import GraphState, empty_graph_state

logger = logging.getLogger(__name__)

_SPECIALISTS = frozenset(
    {
        "poi_agent",
        "itinerary_agent",
        "knowledge_agent",
        "weather_agent",
        "travel_time_agent",
    }
)

SpecialistRoute = Literal[
    "poi_agent",
    "itinerary_agent",
    "knowledge_agent",
    "weather_agent",
    "travel_time_agent",
    "synthesis_agent",
    "__end__",
]


def route_after_orchestrator(
    state: GraphState,
) -> SpecialistRoute | list[Send]:
    """Fan out to specialists, or proceed to Synthesis when artifacts are ready."""
    if state.get("safety_status") in {"blocked", "needs_clarify"}:
        return "__end__"
    if state.get("ready_for_synthesis") or state.get("ready_for_merger"):
        return "synthesis_agent"

    agents = [
        a
        for a in (state.get("next_agents") or [])
        if a in _SPECIALISTS
    ]
    if not agents:
        nxt = state.get("next_agent")
        if nxt in _SPECIALISTS:
            agents = [str(nxt)]

    if not agents:
        return "__end__"

    if len(agents) == 1:
        return agents[0]  # type: ignore[return-value]

    logger.info("GRAPH fan-out wave=%s", agents)
    return [Send(a, state) for a in agents]


def route_after_reviewer(state: GraphState) -> Literal["orchestrator", "__end__"]:
    verdict = as_verdict(state.get("reviewer_verdict"))
    revision_count = int(state.get("revision_count") or 0)
    if verdict and verdict.status == "revise" and revision_count < 2:
        return "orchestrator"
    return "__end__"


def build_graph():
    graph = StateGraph(GraphState)

    graph.add_node("orchestrator", orchestrator_node)
    graph.add_node("poi_agent", poi_agent_node)
    graph.add_node("itinerary_agent", itinerary_agent_node)
    graph.add_node("knowledge_agent", knowledge_agent_node)
    graph.add_node("weather_agent", weather_agent_node)
    graph.add_node("travel_time_agent", travel_time_agent_node)
    graph.add_node("synthesis_agent", synthesis_node)
    graph.add_node("reviewer", reviewer_node)

    graph.add_edge(START, "orchestrator")
    graph.add_conditional_edges(
        "orchestrator",
        route_after_orchestrator,
        {
            "poi_agent": "poi_agent",
            "itinerary_agent": "itinerary_agent",
            "knowledge_agent": "knowledge_agent",
            "weather_agent": "weather_agent",
            "travel_time_agent": "travel_time_agent",
            "synthesis_agent": "synthesis_agent",
            "__end__": END,
        },
    )

    for agent in (
        "poi_agent",
        "itinerary_agent",
        "knowledge_agent",
        "weather_agent",
        "travel_time_agent",
    ):
        graph.add_edge(agent, "orchestrator")

    graph.add_edge("synthesis_agent", "reviewer")
    graph.add_conditional_edges(
        "reviewer",
        route_after_reviewer,
        {"orchestrator": "orchestrator", "__end__": END},
    )

    return graph.compile()


app = build_graph()


def invoke_graph(user_message: str, **extra: Any) -> GraphState:
    initial = empty_graph_state(user_message=user_message)
    for key, value in extra.items():
        initial[key] = value  # type: ignore[literal-required]
    return app.invoke(initial, config={"recursion_limit": 50})  # type: ignore[return-value]


def invoke_stub(user_message: str) -> GraphState:
    return invoke_graph(user_message)
