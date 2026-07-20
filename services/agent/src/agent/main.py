"""CLI + FastAPI entrypoints for the LangGraph agent service."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent.graph import invoke_graph
from agent.mcp.geo import list_india_city_names
from agent.mcp.itinerary_builder import build_itinerary
from agent.mcp.poi_search import poi_search
from agent.mcp.travel_time import estimate_travel_times
from agent.mcp.weather import weather_adjustment
from agent.rag.retrieve import knowledge_search, sources_from_knowledge
from agent.schemas.itinerary import Pace
from agent.schemas.specialists import POICandidate
from agent.schemas.validation import itinerary_to_json_schema
from agent.stt import transcribe_audio
from agent.tools.mcp_tools import get_mcp_tools
from agent.trip_limits import MAX_TRIP_DAYS, MIN_TRIP_DAYS, SCOPED_CITY
# Prefer repo-root .env (monorepo) even when cwd is services/agent
_load_candidates = [
    Path(__file__).resolve().parents[4] / ".env",
    Path(__file__).resolve().parents[5] / ".env",
]
for _env_path in _load_candidates:
    if _env_path.is_file():
        load_dotenv(_env_path)
        break
else:
    load_dotenv()

logging.basicConfig(level=logging.INFO)

api = FastAPI(
    title="AI Itinerary Planner Agent",
    description="LangGraph multi-agent service (Phase 5: voice STT → Orchestrator)",
    version="0.5.0",
)

_cors_origins = [
    o.strip()
    for o in (
        os.getenv("CORS_ORIGINS")
        or "http://localhost:3000,http://127.0.0.1:3000"
    ).split(",")
    if o.strip()
]
api.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    # Local UI + Vercel preview/production hostnames.
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?|https://([\w-]+\.)*vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory multi-turn memory (trip + itinerary + conversation) keyed by session_id.
_SESSIONS: dict[str, dict[str, Any]] = {}


class InvokeRequest(BaseModel):
    user_message: str = Field(..., description="Spoken or typed user input")
    session_id: str | None = Field(
        default=None,
        description="Client session id for conversation memory across turns",
    )
    conversation: list[dict[str, str]] = Field(
        default_factory=list,
        description="Prior turns [{role, content}, ...] for back-and-forth memory",
    )
    previous_itinerary: dict[str, Any] | None = Field(
        default=None,
        description="Last approved itinerary (required for edit / explain turns)",
    )
    merged_itinerary: dict[str, Any] | None = Field(
        default=None,
        description="Optional alias; defaults to previous_itinerary when omitted",
    )
    trip_constraints: dict[str, Any] | None = None


class InvokeResponse(BaseModel):
    user_reply: str
    intent: str | None = None
    safety_status: str | None = None
    revision_count: int = 0
    trip_constraints: dict[str, Any] | None = None
    merged_itinerary: dict[str, Any] | None = None
    travel_time_results: dict[str, Any] | None = None
    weather_results: dict[str, Any] | None = None
    poi_results: dict[str, Any] | None = None
    sources: list[dict[str, Any]] | None = None
    agent_trace: list[dict[str, Any]] = Field(default_factory=list)
    pipeline_log: list[dict[str, Any]] = Field(default_factory=list)
    raw_state: dict[str, Any] | None = None


class POISearchRequest(BaseModel):
    city: str = "Jaipur"
    interests: list[str] = Field(default_factory=lambda: ["food", "culture"])
    constraints: list[str] = Field(default_factory=list)
    limit: int = Field(default=30, ge=5, le=120)
    use_overpass: bool = True


class ItineraryBuilderRequest(BaseModel):
    pois: list[dict[str, Any]]
    num_days: int = Field(default=3, ge=2, le=4)
    pace: Pace = "relaxed"
    daily_time_window_min: int = Field(default=540, ge=180, le=840)
    interests: list[str] = Field(default_factory=list)
    city: str = "Jaipur"


class TravelTimeRequest(BaseModel):
    points: list[dict[str, Any]] = Field(default_factory=list)
    legs: list[dict[str, Any]] = Field(default_factory=list)
    mode: Literal["walk", "city"] = "city"


class WeatherRequest(BaseModel):
    city: str = "Jaipur"
    start_date: str | None = None
    num_days: int = Field(default=3, ge=2, le=4)


class KnowledgeRequest(BaseModel):
    city: str = "Jaipur"
    query: str = ""
    topics: list[str] = Field(default_factory=list)
    k: int = Field(default=4, ge=1, le=10)


@api.get("/health")
def health() -> dict[str, Any]:
    tools = [t.name for t in get_mcp_tools()]
    # Railway injects RAILWAY_GIT_COMMIT_SHA; useful to verify which build is live.
    git_sha = (
        os.getenv("RAILWAY_GIT_COMMIT_SHA")
        or os.getenv("RAILWAY_GIT_COMMIT")
        or os.getenv("SOURCE_COMMIT")
        or ""
    ).strip()
    return {
        "status": "ok",
        "phase": "5",
        "graph": (
            "START→orchestrator⇄[poi|itinerary|knowledge|weather|travel]"
            "→synthesis→reviewer→END"
        ),
        "multi_agent": True,
        "voice": "browser_stt→POST /invoke",
        "schema_version": "1.0",
        "mcp_tools": tools,
        "rag": "knowledge_rag (Wikivoyage + Chroma/BGE)",
        "git_sha": git_sha or None,
        "rag_on_plan": False,
    }


@api.get("/schema/itinerary")
def itinerary_schema() -> dict[str, Any]:
    return itinerary_to_json_schema()


@api.get("/mcp/cities")
def mcp_cities() -> dict[str, Any]:
    names = list_india_city_names()
    return {
        "country": "India",
        "count": len(names),
        "cities": names,
        "notes": "One city per trip. POIs via live Overpass only (no seed fallback).",
    }


@api.get("/mcp/tools")
def list_mcp_tools() -> dict[str, Any]:
    return {
        "tools": [
            {"name": t.name, "description": t.description}
            for t in get_mcp_tools()
        ]
    }


@api.post("/mcp/poi_search")
def mcp_poi_search(body: POISearchRequest) -> dict[str, Any]:
    result = poi_search(
        city=body.city,
        interests=body.interests,
        constraints=body.constraints,
        limit=body.limit,
        # Capstone: live Overpass only — ignore client flag that would disable it.
        use_overpass=True,
    )
    return result.model_dump(mode="json")


@api.post("/mcp/itinerary_builder")
def mcp_itinerary_builder(body: ItineraryBuilderRequest) -> dict[str, Any]:
    pois = [POICandidate.model_validate(p) for p in body.pois]
    draft = build_itinerary(
        candidate_pois=pois,
        num_days=body.num_days,
        pace=body.pace,
        daily_time_window_min=body.daily_time_window_min,
        interests=body.interests,
        city=body.city,
    )
    return draft.model_dump(mode="json")


@api.post("/mcp/travel_time")
def mcp_travel_time(body: TravelTimeRequest) -> dict[str, Any]:
    result = estimate_travel_times(
        points=body.points or None,
        legs=body.legs or None,
        mode=body.mode,
    )
    return result.model_dump(mode="json")


@api.post("/mcp/weather")
def mcp_weather(body: WeatherRequest) -> dict[str, Any]:
    result = weather_adjustment(
        city=body.city,
        start_date=body.start_date,
        num_days=body.num_days,
    )
    return result.model_dump(mode="json")


@api.post("/mcp/knowledge")
def mcp_knowledge(body: KnowledgeRequest) -> dict[str, Any]:
    result = knowledge_search(
        city=body.city,
        query=body.query,
        topics=body.topics,
        k=body.k,
    )
    payload = result.model_dump(mode="json")
    payload["sources"] = [
        s.model_dump(mode="json") for s in sources_from_knowledge(result)
    ]
    return payload


@api.post("/stt")
async def speech_to_text(
    audio: UploadFile = File(..., description="Recorded speech (webm/wav/mp3/ogg)"),
) -> dict[str, Any]:
    """Transcribe browser MediaRecorder audio for the voice UX."""
    data = await audio.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty audio upload.")
    if len(data) > 12_000_000:
        raise HTTPException(status_code=413, detail="Audio too large (max ~12MB).")
    mime = audio.content_type or "audio/webm"
    try:
        transcript = transcribe_audio(data, mime_type=mime)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).exception("STT failed")
        raise HTTPException(
            status_code=500, detail=f"Speech transcription failed: {exc}"
        ) from exc
    return {
        "transcript": transcript,
        "mime_type": mime,
        "bytes": len(data),
        "scope": f"{SCOPED_CITY} · {MIN_TRIP_DAYS}-{MAX_TRIP_DAYS} day trips",
    }


def _run_invoke(body: InvokeRequest) -> InvokeResponse:
    """Run the graph and update session memory (sync; may take several minutes)."""
    sid = (body.session_id or "").strip() or "default"
    sess = _SESSIONS.setdefault(
        sid, {"trip": None, "itinerary": None, "conversation": []}
    )

    extra: dict[str, Any] = {}
    prev = body.previous_itinerary or sess.get("itinerary")
    merged = body.merged_itinerary or prev or sess.get("itinerary")
    # Only reuse an in-progress (unconfirmed) trip for slot filling.
    # Confirmed trips reinject assumed/old preferences into a new "plan" turn.
    trip_in = body.trip_constraints
    if trip_in is None:
        sess_trip = sess.get("trip")
        if isinstance(sess_trip, dict) and not sess_trip.get("confirmed"):
            trip_in = sess_trip
    elif isinstance(trip_in, dict) and trip_in.get("confirmed"):
        # Client may still send previous itinerary.trip — ignore for NEW planning.
        # Do not clear trip context for day-scoped edits that mention "itinerary".
        lower_msg = body.user_message.lower()
        new_plan = bool(
            re.search(r"\b(plan|trip|itinerary|visit|weekend)\b", lower_msg)
        )
        day_edit = bool(
            re.search(
                r"\bday\s*(?:[1-4]|one|two|three|four|first|second|third|fourth)\b",
                lower_msg,
            )
            and re.search(
                r"\b(add|include|change|edit|make|relax|remove|swap|update|"
                r"indoor|outdoor|food)\b",
                lower_msg,
            )
        )
        if new_plan and not day_edit:
            trip_in = None

    if prev is not None:
        extra["previous_itinerary"] = prev
    if merged is not None:
        extra["merged_itinerary"] = merged
    if trip_in is not None:
        extra["trip_constraints"] = trip_in
    elif (
        isinstance(merged, dict)
        and isinstance(merged.get("trip"), dict)
        and not merged["trip"].get("confirmed")
    ):
        extra["trip_constraints"] = merged["trip"]
    if sess.get("pending_dialog") is not None:
        extra["pending_dialog"] = sess.get("pending_dialog")

    user_text = body.user_message.strip()
    from agent.stt_normalize import normalize_stt_message

    user_text = normalize_stt_message(user_text)
    if body.conversation:
        convo = [
            {"role": str(m.get("role", "user")), "content": str(m.get("content", ""))}
            for m in body.conversation
            if isinstance(m, dict) and str(m.get("content", "")).strip()
        ]
    else:
        convo = list(sess.get("conversation") or [])
    if (
        not convo
        or convo[-1].get("role") != "user"
        or (convo[-1].get("content") or "").strip() != user_text
    ):
        convo.append({"role": "user", "content": user_text})
    # Conversation memory for clarify continuity across turns.
    extra["messages"] = [
        {"role": m.get("role", "user"), "content": m.get("content", "")}
        for m in convo[-24:]
        if isinstance(m, dict) and m.get("content")
    ]

    try:
        result = invoke_graph(user_text, **extra)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).exception("invoke_graph failed")
        raise HTTPException(
            status_code=503,
            detail=f"Agent temporarily failed while processing the request: {exc}",
        ) from exc
    trip = result.get("trip_constraints")
    if trip is not None and hasattr(trip, "model_dump"):
        trip = trip.model_dump(mode="json")
    itinerary = _jsonable(result.get("merged_itinerary"))
    # Edits must keep the prior itinerary in session even if a node skipped.
    if not isinstance(itinerary, dict):
        itinerary = sess.get("itinerary") if result.get("intent") == "edit" else None
        itinerary = _jsonable(itinerary) if itinerary else None
    sources = None
    state_sources = _jsonable(result.get("sources"))
    itin_sources = None
    if isinstance(itinerary, dict):
        raw_sources = itinerary.get("sources")
        if isinstance(raw_sources, list) and raw_sources:
            itin_sources = raw_sources
    intent = result.get("intent")
    # Tip / explain turns must expose RAG (or weather) citations — never
    # substitute the plan's OSM/weather References list.
    if intent == "explain":
        if isinstance(state_sources, list) and state_sources:
            sources = state_sources
    elif isinstance(itin_sources, list) and itin_sources:
        sources = itin_sources
    elif isinstance(state_sources, list) and state_sources:
        sources = state_sources
    reply = result.get("user_reply", "") or ""
    convo.append({"role": "assistant", "content": reply})
    sess["conversation"] = convo[-40:]
    if trip is not None:
        sess["trip"] = trip
    elif isinstance(itinerary, dict) and isinstance(itinerary.get("trip"), dict):
        sess["trip"] = itinerary["trip"]
    if isinstance(itinerary, dict):
        sess["itinerary"] = itinerary
    if "pending_dialog" in result:
        # Explicit None clears sticky weather/rain dialogs in the session.
        sess["pending_dialog"] = result.get("pending_dialog")

    trace = _jsonable(result.get("agent_trace") or [])
    if not isinstance(trace, list):
        trace = []
    pipeline = _build_pipeline_log(
        user_message=user_text,
        result=result,
        agent_trace=trace if isinstance(trace, list) else [],
    )
    return InvokeResponse(
        user_reply=reply,
        intent=result.get("intent"),
        safety_status=result.get("safety_status"),
        revision_count=result.get("revision_count", 0),
        trip_constraints=trip if isinstance(trip, dict) else None,
        merged_itinerary=itinerary if isinstance(itinerary, dict) else None,
        travel_time_results=_jsonable(result.get("travel_time_results")),
        weather_results=_jsonable(result.get("weather_results")),
        poi_results=_jsonable(result.get("poi_results")),
        sources=sources,
        agent_trace=trace if isinstance(trace, list) else [],
        pipeline_log=pipeline,
        raw_state=_jsonable(dict(result)),
    )


@api.post("/invoke")
async def invoke(request: Request, body: InvokeRequest):
    """Run the agent.

    Long Overpass/LLM/revise runs can exceed Railway's ~5 minute *silent*
    HTTP limit. We always stream keepalives:

    - ``Accept: application/x-ndjson`` → ping lines + final ``{type:result}``
    - otherwise → whitespace chunks + a single JSON object (works with
      browsers that call ``response.json()``)
    """
    accept = (request.headers.get("accept") or "").lower()
    use_ndjson = "application/x-ndjson" in accept or request.query_params.get(
        "stream"
    ) == "1"
    if request.query_params.get("stream") == "0":
        return _run_invoke(body)

    async def ndjson_stream():
        task = asyncio.create_task(asyncio.to_thread(_run_invoke, body))
        while not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=15.0)
            except asyncio.TimeoutError:
                yield json.dumps({"type": "ping"}) + "\n"
        try:
            payload = task.result()
            yield (
                json.dumps(
                    {
                        "type": "result",
                        "payload": payload.model_dump(mode="json"),
                    }
                )
                + "\n"
            )
        except HTTPException as exc:
            yield (
                json.dumps(
                    {
                        "type": "error",
                        "status": exc.status_code,
                        "message": exc.detail,
                    }
                )
                + "\n"
            )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).exception("streamed invoke failed")
            yield (
                json.dumps(
                    {
                        "type": "error",
                        "status": 500,
                        "message": str(exc),
                    }
                )
                + "\n"
            )

    async def json_keepalive_stream():
        """Whitespace pings + final JSON body (compatible with response.json())."""
        task = asyncio.create_task(asyncio.to_thread(_run_invoke, body))
        while not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=15.0)
            except asyncio.TimeoutError:
                yield "\n"
        try:
            payload = task.result()
            yield json.dumps(payload.model_dump(mode="json"))
        except HTTPException as exc:
            # Emit a JSON error object; status still 200 on stream — clients
            # that need HTTP codes should use ?stream=0.
            yield json.dumps(
                {
                    "user_reply": f"Agent error: {exc.detail}",
                    "intent": None,
                    "safety_status": None,
                    "revision_count": 0,
                    "trip_constraints": None,
                    "merged_itinerary": None,
                    "sources": None,
                    "error": exc.detail,
                    "status": exc.status_code,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).exception("json keepalive invoke failed")
            yield json.dumps(
                {
                    "user_reply": f"Agent error: {exc}",
                    "intent": None,
                    "safety_status": None,
                    "revision_count": 0,
                    "trip_constraints": None,
                    "merged_itinerary": None,
                    "sources": None,
                    "error": str(exc),
                    "status": 500,
                }
            )

    if use_ndjson:
        return StreamingResponse(
            ndjson_stream(),
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    return StreamingResponse(
        json_keepalive_stream(),
        media_type="application/json",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


def _build_pipeline_log(
    *,
    user_message: str,
    result: dict[str, Any],
    agent_trace: list[Any],
) -> list[dict[str, Any]]:
    """Human-readable end-to-end log for the demo UI."""
    steps: list[dict[str, Any]] = [
        {
            "stage": "1 · User input",
            "agent": "user",
            "summary": user_message,
            "detail": {
                "has_previous_itinerary": bool(
                    result.get("previous_itinerary") or result.get("merged_itinerary")
                ),
            },
        }
    ]
    trip = result.get("trip_constraints")
    trip_j = _jsonable(trip) if trip is not None else None
    if isinstance(trip_j, dict):
        steps.append(
            {
                "stage": "2 · Trip constraints",
                "agent": "orchestrator",
                "summary": (
                    f"{trip_j.get('city')} · {trip_j.get('num_days')} days · "
                    f"{trip_j.get('traveler_profile') or 'general'} · "
                    f"{trip_j.get('pace')} · {', '.join(trip_j.get('interests') or [])}"
                ),
                "detail": trip_j,
            }
        )
    steps.append(
        {
            "stage": "3 · Safety / intent",
            "agent": "orchestrator",
            "summary": (
                f"intent={result.get('intent')} · safety={result.get('safety_status')}"
            ),
            "detail": {
                "intent": result.get("intent"),
                "safety_status": result.get("safety_status"),
                "user_reply_preview": (result.get("user_reply") or "")[:240],
            },
        }
    )

    for i, entry in enumerate(agent_trace):
        if not isinstance(entry, dict):
            continue
        agent = str(entry.get("agent") or "node")
        action = entry.get("action") or entry.get("tool") or entry.get("planner")
        tool = entry.get("tool")
        source = entry.get("source")
        bits = [str(action)] if action else []
        if tool:
            bits.append(f"tool={tool}")
        if source:
            bits.append(f"via {source}")
        if entry.get("poi_count") is not None:
            bits.append(f"pois={entry.get('poi_count')}")
        if entry.get("hit_count") is not None:
            bits.append(f"rag_hits={entry.get('hit_count')}")
        if entry.get("output_stops") is not None:
            bits.append(f"stops={entry.get('output_stops')}")
        if entry.get("leg_count") is not None:
            bits.append(f"legs={entry.get('leg_count')}")
        if entry.get("status"):
            bits.append(f"status={entry.get('status')}")
        steps.append(
            {
                "stage": f"graph · {agent}",
                "agent": agent,
                "summary": " · ".join(bits) if bits else str(entry)[:200],
                "detail": entry,
                "index": i,
            }
        )

    itin = _jsonable(result.get("merged_itinerary"))
    if isinstance(itin, dict) and itin.get("days"):
        day_bits = []
        for d in itin.get("days") or []:
            if not isinstance(d, dict):
                continue
            stops = []
            for block in ("morning", "afternoon", "evening"):
                b = d.get(block) or {}
                for s in b.get("stops") or []:
                    if isinstance(s, dict) and s.get("name"):
                        stops.append(s["name"])
            day_bits.append(f"Day {d.get('day_index')}: {', '.join(stops) or '—'}")
        steps.append(
            {
                "stage": "N · Final itinerary",
                "agent": "synthesis",
                "summary": (
                    f"{itin.get('trip', {}).get('city')} · "
                    f"{len(itin.get('days') or [])} days · "
                    f"{sum(len((d.get('morning') or {}).get('stops') or []) + len((d.get('afternoon') or {}).get('stops') or []) + len((d.get('evening') or {}).get('stops') or []) for d in (itin.get('days') or []) if isinstance(d, dict))} stops"
                ),
                "detail": {"days": day_bits, "uncertainty": itin.get("uncertainty_notes")},
            }
        )
    return steps


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def run_once(user_message: str) -> dict[str, Any]:
    return _jsonable(dict(invoke_graph(user_message)))


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Itinerary Planner agent (Phase 5)")
    parser.add_argument(
        "message",
        nargs="?",
        default="Plan a 3-day trip to Jaipur next weekend. I like food and culture.",
        help="User message to send through the LangGraph pipeline",
    )
    parser.add_argument("--serve", action="store_true", help="Run FastAPI server")
    parser.add_argument("--dump-schema", action="store_true")
    parser.add_argument("--smoke-mcp", action="store_true", help="Run MCP smoke test")
    parser.add_argument("--smoke-rag", action="store_true", help="Run RAG smoke test")
    parser.add_argument("--smoke-graph", action="store_true", help="Run Phase 4 graph smoke")
    parser.add_argument("--host", default=os.getenv("AGENT_HOST", "0.0.0.0"))
    # Railway/Render inject PORT; fall back to AGENT_PORT then 8000.
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PORT") or os.getenv("AGENT_PORT") or "8000"),
    )
    args = parser.parse_args()

    if args.dump_schema:
        print(json.dumps(itinerary_to_json_schema(), indent=2))
        return

    if args.smoke_mcp:
        from agent.smoke_mcp import main as smoke_main

        raise SystemExit(smoke_main([]))

    if args.smoke_rag:
        from agent.smoke_rag import main as smoke_rag_main

        raise SystemExit(smoke_rag_main([]))

    if args.smoke_graph:
        from agent.smoke_graph import main as smoke_graph_main

        raise SystemExit(smoke_graph_main([]))

    if args.serve:
        import uvicorn

        uvicorn.run("agent.main:api", host=args.host, port=args.port, reload=False)
        return

    result = run_once(args.message)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
