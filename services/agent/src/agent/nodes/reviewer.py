"""Reviewer Agent — fully autonomous structured revision feedback.

Produces approve/revise with ``reason``, ``target_agent``, and ``constraints``
so the Orchestrator can route directly to the right specialist without inferring.
Heuristic hard checks always run; optional LLM enriches the feedback packet.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agent.nodes.llm_utils import chat_json, compact_itinerary, llm_enabled
from agent.nodes.state_utils import (
    as_edit_patch,
    as_edit_patches,
    as_itinerary,
    as_trip,
    dump,
)
from agent.schemas.review import (
    ReviewIssue,
    ReviewerVerdict,
    TargetAgent,
    normalize_target_agent,
)
from agent.schemas.state import GraphState
from agent.schemas.validation import validate_grounding_rules

logger = logging.getLogger(__name__)

_HARD_CODES = {
    "feasibility_duration",
    "feasibility_pace",
    "feasibility_travel",
    "missing_data",
    "edit_scope",
    "grounding_osm",
}

_VALID_CODES = {
    "feasibility_duration",
    "feasibility_travel",
    "feasibility_pace",
    "grounding_osm",
    "grounding_citation",
    "edit_scope",
    "missing_data",
    "other",
}

# Default specialist routing for each issue code
_CODE_TARGET: dict[str, TargetAgent] = {
    "feasibility_duration": "itinerary_agent",
    "feasibility_pace": "itinerary_agent",
    "feasibility_travel": "travel_time_agent",
    "grounding_osm": "poi_agent",
    "grounding_citation": "itinerary_agent",
    "missing_data": "poi_agent",
    "edit_scope": "itinerary_agent",
    "other": "itinerary_agent",
}


def _trace_append(state: GraphState, entry: dict[str, Any]) -> list[dict[str, Any]]:
    del state
    return [entry]


def _heuristic_issues(state: GraphState) -> tuple[list[ReviewIssue], list[str]]:
    intent = state.get("intent") or "plan"
    itin = as_itinerary(state.get("merged_itinerary"))
    trip = as_trip(state.get("trip_constraints"))
    prev = as_itinerary(state.get("previous_itinerary"))
    patch = as_edit_patch(state.get("edit_patch"))

    issues: list[ReviewIssue] = []
    affected: list[str] = []

    if itin is None:
        return (
            [
                ReviewIssue(
                    code="missing_data",
                    message="No merged itinerary present for review.",
                )
            ],
            [],
        )

    window = (trip.daily_time_window_min if trip else 540) + 60
    for day in itin.days:
        if day.total_duration_min > window:
            issues.append(
                ReviewIssue(
                    code="feasibility_duration",
                    message=(
                        f"Day {day.day_index} total {day.total_duration_min}m "
                        f"exceeds window ~{window}m."
                    ),
                    section=f"day{day.day_index}",
                )
            )
            affected.append(f"day{day.day_index}")
        if trip and trip.pace == "relaxed" and len(day.all_stops) > 3:
            issues.append(
                ReviewIssue(
                    code="feasibility_pace",
                    message=(
                        f"Day {day.day_index} has {len(day.all_stops)} stops "
                        "for relaxed pace (target ~1 morning + 1 afternoon + "
                        "optional evening food/park/market)."
                    ),
                    section=f"day{day.day_index}",
                )
            )
            affected.append(f"day{day.day_index}")
        # Hard day-end: any stop departing after 21:00 (or past midnight).
        for s in day.all_stops:
            dep = getattr(s, "depart_time", None)
            arr = getattr(s, "arrive_time", None)
            if not dep:
                continue
            try:
                dh, dm = str(dep).split(":")[:2]
                dep_min = int(dh) * 60 + int(dm)
            except (TypeError, ValueError):
                continue
            past_midnight = False
            if arr:
                try:
                    ah, am = str(arr).split(":")[:2]
                    arr_min = int(ah) * 60 + int(am)
                    past_midnight = dep_min < arr_min
                except (TypeError, ValueError):
                    past_midnight = False
            if past_midnight or dep_min > 21 * 60:
                issues.append(
                    ReviewIssue(
                        code="feasibility_duration",
                        message=(
                            f"Day {day.day_index} stop '{s.name}' ends after "
                            "21:00 hard day end."
                        ),
                        section=f"day{day.day_index}",
                    )
                )
                affected.append(f"day{day.day_index}")
                break
        if not day.all_stops:
            issues.append(
                ReviewIssue(
                    code="missing_data",
                    message=f"Day {day.day_index} has no stops.",
                    section=f"day{day.day_index}",
                )
            )
            affected.append(f"day{day.day_index}")

    for g in validate_grounding_rules(itin):
        code = "grounding_osm" if "osm" in g.lower() else "grounding_citation"
        issues.append(ReviewIssue(code=code, message=g, section=None))  # type: ignore[arg-type]

    if intent == "edit" and prev and patch:
        target_day = patch.target.day
        for day, old in zip(itin.days, prev.days, strict=False):
            if day.day_index == target_day:
                continue
            if dump(day) != dump(old):
                issues.append(
                    ReviewIssue(
                        code="edit_scope",
                        message=(
                            f"Day {day.day_index} changed but edit targeted "
                            f"Day {target_day}."
                        ),
                        section=f"day{day.day_index}",
                    )
                )
                affected.append(f"day{day.day_index}")

    return issues, affected


def _constraints_from_issues(
    issues: list[ReviewIssue],
    affected: list[str],
    *,
    trip,
) -> list[str]:
    constraints: list[str] = []
    codes = {i.code for i in issues}

    if "feasibility_duration" in codes or "feasibility_pace" in codes:
        constraints.append("Trim day under window")
        constraints.append("Reduce travel")
        # Only ask for relaxed packing when the trip is already relaxed.
        if trip and trip.pace == "relaxed":
            constraints.append("Respect relaxed pace")
            constraints.append("Reduce stops")
        elif trip and trip.pace == "packed":
            constraints.append("Respect packed pace")
        else:
            constraints.append("Reduce stops")
    if "feasibility_travel" in codes:
        constraints.append("Reduce travel")
        constraints.append("Cluster nearby stops")
    if "grounding_citation" in codes:
        constraints.append("Attach citations or mark uncertainty")
    if "grounding_osm" in codes:
        constraints.append("Only OSM-grounded POIs")
    if "missing_data" in codes:
        constraints.append("Fill empty days with grounded POIs")
    if "edit_scope" in codes:
        constraints.append("Only change the edited day/block")

    # Preserve days that were NOT flagged
    for section in affected:
        # e.g. day2 → preserve other days via complementary constraints
        pass
    flagged_days = set()
    for a in affected:
        if a.startswith("day") and a[3:].isdigit():
            flagged_days.add(int(a[3:]))
    if trip and flagged_days:
        for d in range(1, (trip.num_days or 3) + 1):
            if d not in flagged_days:
                constraints.append(f"Preserve Day {d}")

    # Prefer keeping museums/heritage when trimming
    if trip and trip.interests:
        for interest in trip.interests[:2]:
            if interest.lower() in {"museum", "heritage", "culture", "food"}:
                constraints.append(f"Keep {interest}")

    # Dedupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for c in constraints:
        key = c.lower()
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def _feedback_from_hard(
    issues: list[ReviewIssue],
    affected: list[str],
    *,
    trip,
    llm: ReviewerVerdict | None,
) -> ReviewerVerdict:
    """Build autonomous revise packet from hard issues (+ optional LLM enrich)."""
    primary = issues[0]
    target = _CODE_TARGET.get(primary.code, "itinerary_agent")
    reason = primary.message
    constraints = _constraints_from_issues(issues, affected, trip=trip)

    if llm:
        if llm.target_agent:
            target = llm.target_agent
        if llm.reason:
            reason = llm.reason
        if llm.constraints:
            # LLM constraints first, then heuristic fills gaps
            merged = list(llm.constraints)
            for c in constraints:
                if c.lower() not in {x.lower() for x in merged}:
                    merged.append(c)
            constraints = merged
        if llm.issues:
            seen = {(i.code, i.message) for i in issues}
            for i in llm.issues:
                if (i.code, i.message) not in seen:
                    issues.append(i)

    return ReviewerVerdict(
        status="revise",
        reason=reason[:400],
        target_agent=target,
        constraints=constraints,
        issues=issues,
        affected_sections=sorted(set(affected + (llm.affected_sections if llm else []))),
        notes=llm.notes if llm else "Revision requested (hard checks).",
    )


def _llm_review(
    *,
    intent: str,
    itinerary,
    trip,
    patch,
    heuristic_issues: list[ReviewIssue],
    revision_count: int,
) -> ReviewerVerdict | None:
    if not llm_enabled("REVIEWER_LLM"):
        return None

    system = (
        "You are the fully autonomous Reviewer Agent for an India city travel "
        "multi-agent system. You do NOT invent POIs or call tools. "
        "Decide approve or revise. On revise you MUST name the specialist to fix "
        "the problem and give concrete constraints the Orchestrator will pass through. "
        "Valid target_agent values: poi_agent, itinerary_agent, "
        "weather_agent, travel_time_agent. "
        "Do NOT target knowledge_agent — RAG tips are explain-only and are not "
        "part of itinerary generation. Citation gaps alone must APPROVE. "
        "Examples: duration/pace → itinerary_agent with constraints like "
        '["Reduce travel", "Keep museum", "Preserve Day 1"]; '
        "missing real POIs → poi_agent; "
        "leg times only → travel_time_agent. "
        "If heuristic_issues list hard problems, status must be revise. "
        "Soft notes alone may approve — including placeholder/demo OSM IDs, "
        "minor clustering preferences, and citation gaps. Do NOT revise solely "
        "for OSM ID format, geographic clustering polish, or missing Wikivoyage tips. "
        "Return ONLY JSON: "
        '{"status":"approve"|"revise","reason":"...",'
        '"target_agent":"itinerary_agent"|null,'
        '"constraints":["Reduce travel","Keep museum"],'
        '"issues":[{"code":"...","message":"...","section":"day2"}],'
        '"affected_sections":["day2"],"notes":"..."}'
    )
    human = json.dumps(
        {
            "intent": intent,
            "revision_count": revision_count,
            "trip": dump(trip) if trip else None,
            "edit_patch": dump(patch) if patch else None,
            "itinerary": compact_itinerary(itinerary),
            "heuristic_issues": [i.model_dump(mode="json") for i in heuristic_issues],
        },
        ensure_ascii=False,
    )
    data = chat_json(
        system=system,
        human=human,
        model_env="REVIEWER_MODEL",
    )
    if not data:
        return None

    status = data.get("status")
    if status not in {"approve", "revise"}:
        return None

    parsed: list[ReviewIssue] = []
    for raw in data.get("issues") or []:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "other")
        if code not in _VALID_CODES:
            code = "other"
        msg = str(raw.get("message") or "").strip()
        if not msg:
            continue
        parsed.append(
            ReviewIssue(
                code=code,  # type: ignore[arg-type]
                message=msg[:400],
                section=raw.get("section"),
            )
        )

    affected = [
        str(s)
        for s in (data.get("affected_sections") or [])
        if isinstance(s, (str, int))
    ]
    constraints = [
        str(c).strip()[:120]
        for c in (data.get("constraints") or [])
        if str(c).strip()
    ]
    target = normalize_target_agent(
        str(data.get("target_agent") or "") if data.get("target_agent") else None
    )
    reason = str(data.get("reason") or data.get("notes") or "").strip()[:400] or None

    logger.info(
        "LLM reviewer status=%s target=%s reason=%s",
        status,
        target,
        reason,
    )
    return ReviewerVerdict(
        status=status,
        reason=reason,
        target_agent=target if status == "revise" else None,
        constraints=constraints if status == "revise" else [],
        issues=parsed,
        affected_sections=affected,
        notes=(str(data.get("notes") or "")[:400] or None),
    )


def _finalize_verdict(
    *,
    heuristic: list[ReviewIssue],
    affected: list[str],
    llm: ReviewerVerdict | None,
    revision_count: int,
    trip,
    intent: str = "plan",
) -> tuple[ReviewerVerdict, int, str]:
    hard = [i for i in heuristic if i.code in _HARD_CODES]
    soft = [i for i in heuristic if i.code not in _HARD_CODES]
    mode = "llm" if llm else "heuristic"

    # Allow at most one revise round-trip. A second revise would bump
    # revision_count to 2 and end the graph with status=revise (bad UX).
    can_revise = revision_count < 1

    if hard and can_revise:
        verdict = _feedback_from_hard(hard, affected, trip=trip, llm=llm)
        # Never send plan/edit back to knowledge_agent (RAG is explain-only).
        if intent in {"plan", "edit"} and verdict.target_agent == "knowledge_agent":
            verdict = verdict.model_copy(update={"target_agent": "itinerary_agent"})
        return verdict, revision_count + 1, mode if llm else "heuristic"

    if llm and llm.status == "revise" and can_revise:
        target = llm.target_agent or "itinerary_agent"
        # Citation/RAG revises during plan/edit → approve with notes instead.
        if intent in {"plan", "edit"} and target == "knowledge_agent":
            reason = llm.reason or "Approved (RAG not used during itinerary generation)."
            return (
                ReviewerVerdict(
                    status="approve",
                    reason=reason[:400],
                    target_agent=None,
                    constraints=[],
                    issues=llm.issues or soft,
                    affected_sections=[],
                    notes=llm.notes or reason,
                ),
                revision_count,
                "llm",
            )
        constraints = list(llm.constraints) or _constraints_from_issues(
            llm.issues or soft, llm.affected_sections or affected, trip=trip
        )
        reason = llm.reason or (
            llm.issues[0].message if llm.issues else "Revision requested by Reviewer."
        )
        return (
            ReviewerVerdict(
                status="revise",
                reason=reason[:400],
                target_agent=target,
                constraints=constraints,
                issues=llm.issues or soft,
                affected_sections=sorted(set(llm.affected_sections or affected)),
                notes=llm.notes,
            ),
            revision_count + 1,
            "llm",
        )

    notes_issues = soft
    if llm and llm.issues:
        notes_issues = [i for i in llm.issues if i.code not in _HARD_CODES] or soft
    if hard and not can_revise:
        notes_issues = hard + notes_issues
    reason = (llm.reason if llm else None) or (
        "Approved with notes." if notes_issues else "Approved."
    )
    if llm and llm.status == "revise" and not can_revise:
        reason = (
            f"Approved after revision cap. Remaining notes: {reason}"
        )[:400]
    return (
        ReviewerVerdict(
            status="approve",
            reason=reason[:400],
            target_agent=None,
            constraints=[],
            issues=notes_issues if notes_issues else [],
            affected_sections=[],
            notes=(llm.notes if llm else None) or reason,
        ),
        revision_count,
        mode,
    )


def reviewer_node(state: GraphState) -> dict[str, Any]:
    intent = state.get("intent") or "plan"
    revision_count = int(state.get("revision_count") or 0)
    itin = as_itinerary(state.get("merged_itinerary"))
    trip = as_trip(state.get("trip_constraints"))
    patch = as_edit_patch(state.get("edit_patch"))

    if intent == "explain":
        verdict = ReviewerVerdict(
            status="approve",
            reason="Explain path — no itinerary rebuild to review.",
            notes="Explain path — no itinerary rebuild to review.",
        )
        logger.info("NODE reviewer approve (explain)")
        return {
            "reviewer_verdict": dump(verdict),
            "agent_trace": _trace_append(
                state,
                {"agent": "reviewer", "mode": "heuristic", "action": "approve_explain"},
            ),
        }

    # Scoped day edits: approve when other days stayed intact. Skip LLM revise
    # loops that re-fetch Overpass POIs and time out as agent 500s.
    patches = as_edit_patches(state.get("edit_patches"))
    if not patches and patch:
        patches = [patch]
    if intent == "edit" and patches and itin is not None:
        heuristic, affected = _heuristic_issues(state)
        scope_issues = [i for i in heuristic if i.code == "edit_scope"]
        if not scope_issues:
            ops = ", ".join(p.operation for p in patches)
            days = ", ".join(str(p.target.day) for p in patches)
            verdict = ReviewerVerdict(
                status="approve",
                reason=f"Scoped edit applied ({ops} on day {days}); other days unchanged.",
                notes=f"Scoped edit applied ({ops} on day {days}); other days unchanged.",
            )
            logger.info("NODE reviewer approve (scoped edit)")
            return {
                "reviewer_verdict": dump(verdict),
                "revision_feedback": None,
                "user_reply": state.get("user_reply") or "",
                "agent_trace": _trace_append(
                    state,
                    {
                        "agent": "reviewer",
                        "mode": "heuristic",
                        "action": "approve_scoped_edit",
                        "reason": verdict.reason,
                    },
                ),
            }

    heuristic, affected = _heuristic_issues(state)
    llm_verdict = None
    if itin is not None:
        llm_verdict = _llm_review(
            intent=intent,
            itinerary=itin,
            trip=trip,
            patch=patch,
            heuristic_issues=heuristic,
            revision_count=revision_count,
        )

    verdict, new_rev, mode = _finalize_verdict(
        heuristic=heuristic,
        affected=affected,
        llm=llm_verdict,
        revision_count=revision_count,
        trip=trip,
        intent=intent,
    )

    logger.info(
        "NODE reviewer status=%s target=%s reason=%s constraints=%s mode=%s",
        verdict.status,
        verdict.target_agent,
        verdict.reason,
        verdict.constraints,
        mode,
    )

    reply = state.get("user_reply") or ""
    if verdict.status == "approve" and itin:
        reply = reply or (
            f"Approved itinerary for {itin.trip.city}: "
            f"{itin.trip.num_days} days, "
            f"{sum(len(d.all_stops) for d in itin.days)} stops."
        )

    out: dict[str, Any] = {
        "reviewer_verdict": dump(verdict),
        "revision_count": new_rev,
        "user_reply": reply,
        "agent_trace": _trace_append(
            state,
            {
                "agent": "reviewer",
                "mode": mode,
                "action": verdict.status,
                "reason": verdict.reason,
                "target_agent": verdict.target_agent,
                "constraints": verdict.constraints,
                "issues": len(verdict.issues),
            },
        ),
    }
    if verdict.status == "revise":
        out["revision_feedback"] = {
            "status": "revise",
            "reason": verdict.reason,
            "target_agent": verdict.target_agent,
            "constraints": list(verdict.constraints),
            "affected_sections": list(verdict.affected_sections),
        }
    else:
        out["revision_feedback"] = None
    return out
