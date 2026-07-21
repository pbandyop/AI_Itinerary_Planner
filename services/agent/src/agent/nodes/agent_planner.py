"""Orchestrator agent planner — chooses which specialist agents to call.

Plans *waves* of agents: agents in the same wave are independent and may run
in parallel; waves run in order (dependencies respected).

Uses an LLM (Gemini by default) when an API key is set; otherwise a
deterministic heuristic.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Literal

logger = logging.getLogger(__name__)

AgentName = Literal[
    "poi_agent",
    "itinerary_agent",
    "knowledge_agent",
    "weather_agent",
    "travel_time_agent",
]

VALID_AGENTS: set[str] = {
    "poi_agent",
    "itinerary_agent",
    "knowledge_agent",
    "weather_agent",
    "travel_time_agent",
}

# Hard dependencies: agent → must complete in an earlier wave
# Travel estimates among POIs first; Itinerary optimizes using travel + weather.
# RAG (knowledge_agent) is explain/Q&A only — not part of plan generation.
_DEPENDS_ON: dict[str, set[str]] = {
    "travel_time_agent": {"poi_agent"},
    "itinerary_agent": {"poi_agent", "travel_time_agent"},
}


def flatten_waves(waves: list[list[AgentName]]) -> list[AgentName]:
    out: list[AgentName] = []
    seen: set[str] = set()
    for wave in waves:
        for a in wave:
            if a in VALID_AGENTS and a not in seen:
                seen.add(a)
                out.append(a)
    return out


def _dedupe_wave(wave: list[str]) -> list[AgentName]:
    seen: set[str] = set()
    out: list[AgentName] = []
    for a in wave:
        if a in VALID_AGENTS and a not in seen:
            seen.add(a)
            out.append(a)  # type: ignore[arg-type]
    return out


def _agents_satisfied(state: dict[str, Any], completed: set[str]) -> set[str]:
    """Agents already done via state or earlier waves in this plan."""
    done = set(completed)
    if state.get("poi_results"):
        done.add("poi_agent")
    if state.get("travel_time_results"):
        done.add("travel_time_agent")
    if state.get("itinerary_draft"):
        done.add("itinerary_agent")
    if state.get("knowledge_results"):
        done.add("knowledge_agent")
    if state.get("weather_results"):
        done.add("weather_agent")
    return done


def enforce_wave_dependencies(
    waves: list[list[AgentName]],
    state: dict[str, Any],
) -> list[list[AgentName]]:
    """
    Rebuild waves so dependents never run before (or beside) their deps.

    Inserts missing dependency agents when needed. Agents whose deps are
    already satisfied (state or prior wave) are packed into the same wave
    when mutually independent.
    """
    flat = flatten_waves(waves)
    if not flat:
        return []

    # Ensure required deps are in the plan
    needed: list[AgentName] = []
    planned = set(flat)
    for agent in flat:
        for dep in sorted(_DEPENDS_ON.get(agent, set())):
            if dep not in planned and dep not in _agents_satisfied(state, set()):
                needed.append(dep)  # type: ignore[arg-type]
                planned.add(dep)
    ordered = flatten_waves([needed, flat]) if needed else flat

    completed = _agents_satisfied(state, set())
    remaining = list(ordered)
    result: list[list[AgentName]] = []

    while remaining:
        ready: list[AgentName] = []
        blocked: list[AgentName] = []
        for agent in remaining:
            deps = _DEPENDS_ON.get(agent, set())
            if deps <= completed:
                ready.append(agent)
            else:
                blocked.append(agent)
        if not ready:
            # Break deadlock by forcing the first blocked agent alone
            forced = blocked[0]
            ready = [forced]
            blocked = blocked[1:]
        result.append(ready)
        completed.update(ready)
        remaining = blocked

    return result


def sequence_to_waves(
    seq: list[AgentName],
    state: dict[str, Any],
) -> list[list[AgentName]]:
    """Pack a flat sequence into maximal parallel waves under dependencies."""
    if not seq:
        return []
    return enforce_wave_dependencies([[a] for a in seq], state)


def heuristic_agent_waves(
    *,
    intent: str,
    message: str,
    state: dict[str, Any],
) -> list[list[AgentName]]:
    lower = message.lower()
    if intent == "explain":
        # Itinerary-owned Q&A — no knowledge_agent / RAG wave.
        if re.search(
            r"\bwhy (did you |do you )?(pick|choose|include)\b",
            lower,
        ) or re.search(
            r"\b(doable|feasible|too (?:much|packed)|can (?:i|we) (?:do|finish)|"
            r"is (?:this|the|it) (?:plan|itinerary) (?:doable|feasible|realistic))\b",
            lower,
        ):
            return []
        wave: list[AgentName] = ["knowledge_agent"]
        if "rain" in lower or "weather" in lower:
            wave.append("weather_agent")
        return [wave]
    if intent == "edit":
        # Lean edit workflow: Itinerary Agent owns scoped day edits and fetches
        # live POIs itself when add/indoor needs candidates. Avoid poi/travel
        # waves so edits do not hang on Overpass.
        if "indoor" in lower or "rain" in lower:
            return [["weather_agent"], ["itinerary_agent"]]
        return [["itinerary_agent"]]
    # plan: Wave1 POI∥Weather → Wave2 Travel → Wave3 Itinerary
    # knowledge_agent is explain-only (place tips / hours), not plan generation.
    has_poi = bool(state.get("poi_results"))
    has_draft = bool(state.get("itinerary_draft"))
    has_travel = bool(state.get("travel_time_results"))
    waves: list[list[AgentName]] = []
    wave1: list[AgentName] = []
    if not has_poi:
        wave1.append("poi_agent")
    wave1.append("weather_agent")
    if wave1:
        waves.append(wave1)
    if not has_travel:
        waves.append(["travel_time_agent"])
    if not has_draft:
        waves.append(["itinerary_agent"])
    return enforce_wave_dependencies(waves, state) if waves else []


def llm_agent_waves(
    *,
    intent: str,
    message: str,
    state: dict[str, Any],
) -> list[list[AgentName]] | None:
    """Ask an LLM which specialist waves to run for this turn."""
    from agent.nodes.llm_utils import default_chat_model, get_chat_model, llm_api_key

    if not llm_api_key():
        return None
    if os.getenv("ORCHESTRATOR_LLM", "true").lower() in {"0", "false", "no"}:
        return None

    try:
        from langchain_core.messages import HumanMessage, SystemMessage
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM orchestrator unavailable: %s", exc)
        return None

    has_poi = bool(state.get("poi_results"))
    has_draft = bool(state.get("itinerary_draft"))
    has_prev = bool(state.get("previous_itinerary") or state.get("merged_itinerary"))

    system = (
        "You are the Orchestrator of an India city travel-planning multi-agent system. "
        "Produce an explicit execution plan as ordered WAVES. Agents in the same wave "
        "run in PARALLEL. Waves run sequentially. "
        "Valid agents: poi_agent, itinerary_agent, knowledge_agent, weather_agent, "
        "travel_time_agent. "
        "Dependencies: travel_time_agent and itinerary_agent need poi_agent first "
        "(unless POIs exist). itinerary_agent is the OPTIMIZER — it should run AFTER "
        "poi, weather, and travel when possible. "
        "knowledge_agent is ONLY for explain / place tips / hours — NEVER include it "
        "on plan or edit waves. weather_agent can share a wave with poi_agent. "
        "explain → knowledge_agent (+ weather_agent if rain, same wave). "
        "edit → itinerary_agent (+ weather_agent first if rain/indoor). "
        "plan → typically "
        '[["poi_agent","weather_agent"], ["travel_time_agent"], '
        '["itinerary_agent"]]. '
        'Also return success_criteria from: '
        "poi_candidates, travel_times_available, "
        "weather_adjustments, itinerary_complete "
        "(use citations_present only for explain). "
        'Return ONLY JSON: {"waves": [["poi_agent","weather_agent"], '
        '["travel_time_agent"], ["itinerary_agent"]], '
        '"success_criteria": ["poi_candidates","travel_times_available",'
        '"weather_adjustments","itinerary_complete"], '
        '"reason": "..."}'
    )
    human = (
        f"intent={intent}\n"
        f"message={message!r}\n"
        f"has_poi_results={has_poi}\n"
        f"has_itinerary_draft={has_draft}\n"
        f"has_previous_itinerary={has_prev}\n"
    )

    try:
        model = os.getenv("ORCHESTRATOR_MODEL") or default_chat_model()
        llm = get_chat_model(model=model, temperature=0)
        resp = llm.invoke(
            [SystemMessage(content=system), HumanMessage(content=human)]
        )
        content = resp.content
        if isinstance(content, list):
            text = "".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            ).strip()
        else:
            text = str(content).strip()
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        data = json.loads(m.group(0))
        raw_waves = data.get("waves")
        # Backward compat: flat "agents" list
        if not raw_waves and data.get("agents"):
            seq = [str(a) for a in data["agents"] if str(a) in VALID_AGENTS]
            waves = sequence_to_waves(seq, state)  # type: ignore[arg-type]
        else:
            waves = []
            for wave in raw_waves or []:
                if isinstance(wave, list):
                    waves.append(_dedupe_wave([str(a) for a in wave]))
                elif isinstance(wave, str) and wave in VALID_AGENTS:
                    waves.append([wave])  # type: ignore[list-item]
            waves = enforce_wave_dependencies(waves, state)
        if waves:
            logger.info(
                "LLM orchestrator waves=%s reason=%s",
                waves,
                data.get("reason"),
            )
            return waves
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM orchestrator failed, using heuristic: %s", exc)
    return None


def _strip_knowledge_from_plan_waves(
    waves: list[list[AgentName]], *, intent: str
) -> list[list[AgentName]]:
    """RAG is explain-only — never dispatch knowledge_agent on plan/edit."""
    if intent == "explain":
        return waves
    cleaned: list[list[AgentName]] = []
    for wave in waves:
        kept = [a for a in wave if a != "knowledge_agent"]
        if kept:
            cleaned.append(kept)
    return cleaned


def plan_agent_waves(
    *,
    intent: str,
    message: str,
    state: dict[str, Any],
) -> tuple[list[list[AgentName]], str, list[str]]:
    """
    Return (waves, planner_name, success_criteria).

    Default is a deterministic multi-agent *workflow* (stable for voice demos).
    Optional LLM orchestration for plan turns when AGENT_WORKFLOW_MODE=false
    and ORCHESTRATOR_LLM=true.
    """
    from agent.nodes.execution_plan import success_criteria_for_waves

    workflow = os.getenv("AGENT_WORKFLOW_MODE", "true").lower() not in {
        "0",
        "false",
        "no",
    }
    # Edit/explain stay on lean fixed waves. Do NOT expand itinerary→poi/travel
    # deps: previous itinerary already exists, and itinerary_agent fetches POIs
    # inline for add/indoor edits. Expanding deps caused long Overpass hangs → 500.
    if intent in {"edit", "explain"}:
        waves = heuristic_agent_waves(intent=intent, message=message, state=state)
        waves = _strip_knowledge_from_plan_waves(waves, intent=intent)
        return waves, "workflow", success_criteria_for_waves(waves)

    if workflow:
        waves = heuristic_agent_waves(intent=intent, message=message, state=state)
        waves = enforce_wave_dependencies(waves, state)
        waves = _strip_knowledge_from_plan_waves(waves, intent=intent)
        return waves, "workflow", success_criteria_for_waves(waves)

    llm_waves = llm_agent_waves(intent=intent, message=message, state=state)
    if llm_waves:
        llm_waves = _strip_knowledge_from_plan_waves(llm_waves, intent=intent)
        llm_waves = enforce_wave_dependencies(llm_waves, state)
        return llm_waves, "llm", success_criteria_for_waves(llm_waves)
    waves = heuristic_agent_waves(intent=intent, message=message, state=state)
    waves = enforce_wave_dependencies(waves, state)
    waves = _strip_knowledge_from_plan_waves(waves, intent=intent)
    return waves, "heuristic", success_criteria_for_waves(waves)


def waves_for_revision(target: str | None) -> list[list[AgentName]]:
    """Minimal waves to satisfy Reviewer feedback — route to target + dependents."""
    t = target if target in VALID_AGENTS else "itinerary_agent"
    if t == "poi_agent":
        return [
            ["poi_agent"],
            ["travel_time_agent"],
            ["itinerary_agent"],
        ]
    if t == "travel_time_agent":
        return [["travel_time_agent"], ["itinerary_agent"]]
    if t == "itinerary_agent":
        return [["itinerary_agent"]]
    if t == "knowledge_agent":
        # Explain-style revise: re-retrieve tips only (no itinerary rebuild).
        return [["knowledge_agent"]]
    if t == "weather_agent":
        return [["weather_agent"], ["itinerary_agent"]]
    return [["itinerary_agent"]]


# --- Backward-compatible flat API ---


def plan_agent_sequence(
    *,
    intent: str,
    message: str,
    state: dict[str, Any],
) -> tuple[list[AgentName], str]:
    waves, planner, _criteria = plan_agent_waves(
        intent=intent, message=message, state=state
    )
    return flatten_waves(waves), planner
