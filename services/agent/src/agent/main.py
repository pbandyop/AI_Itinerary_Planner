"""CLI + FastAPI entrypoints for the LangGraph agent service."""

from __future__ import annotations

import argparse
import json
import logging
import os
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel, Field

from agent.graph import app as graph_app
from agent.mcp.itinerary_builder import build_itinerary
from agent.mcp.poi_search import poi_search
from agent.schemas.itinerary import Pace
from agent.schemas.specialists import POICandidate
from agent.schemas.state import empty_graph_state
from agent.schemas.validation import itinerary_to_json_schema
from agent.tools.mcp_tools import get_mcp_tools

load_dotenv()
logging.basicConfig(level=logging.INFO)

api = FastAPI(
    title="AI Itinerary Planner Agent",
    description="LangGraph multi-agent service (Phase 2: MCP tools)",
    version="0.2.0",
)


class InvokeRequest(BaseModel):
    user_message: str = Field(..., description="Spoken or typed user input")


class InvokeResponse(BaseModel):
    user_reply: str
    intent: str | None = None
    safety_status: str | None = None
    revision_count: int = 0
    trip_constraints: dict[str, Any] | None = None
    raw_state: dict[str, Any] | None = None


class POISearchRequest(BaseModel):
    city: Literal["Jaipur"] = "Jaipur"
    interests: list[str] = Field(default_factory=lambda: ["food", "culture"])
    constraints: list[str] = Field(default_factory=list)
    limit: int = Field(default=30, ge=5, le=80)
    use_overpass: bool = True


class ItineraryBuilderRequest(BaseModel):
    pois: list[dict[str, Any]]
    num_days: int = Field(default=3, ge=2, le=4)
    pace: Pace = "relaxed"
    daily_time_window_min: int = Field(default=540, ge=180, le=840)
    interests: list[str] = Field(default_factory=list)


@api.get("/health")
def health() -> dict[str, Any]:
    tools = [t.name for t in get_mcp_tools()]
    return {
        "status": "ok",
        "phase": "2",
        "graph": "START→orchestrator→END",
        "schema_version": "1.0",
        "mcp_tools": tools,
    }


@api.get("/schema/itinerary")
def itinerary_schema() -> dict[str, Any]:
    return itinerary_to_json_schema()


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
        use_overpass=body.use_overpass,
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
    )
    return draft.model_dump(mode="json")


@api.post("/invoke", response_model=InvokeResponse)
def invoke(body: InvokeRequest) -> InvokeResponse:
    result = graph_app.invoke(empty_graph_state(user_message=body.user_message))
    trip = result.get("trip_constraints")
    if trip is not None and hasattr(trip, "model_dump"):
        trip = trip.model_dump(mode="json")
    return InvokeResponse(
        user_reply=result.get("user_reply", ""),
        intent=result.get("intent"),
        safety_status=result.get("safety_status"),
        revision_count=result.get("revision_count", 0),
        trip_constraints=trip if isinstance(trip, dict) else trip,
        raw_state=_jsonable(dict(result)),
    )


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def run_once(user_message: str) -> dict[str, Any]:
    return _jsonable(dict(graph_app.invoke(empty_graph_state(user_message=user_message))))


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Itinerary Planner agent (Phase 2)")
    parser.add_argument(
        "message",
        nargs="?",
        default="Plan a 3-day trip to Jaipur next weekend. I like food and culture.",
        help="User message to send through the stub graph",
    )
    parser.add_argument("--serve", action="store_true", help="Run FastAPI server")
    parser.add_argument("--dump-schema", action="store_true")
    parser.add_argument("--smoke-mcp", action="store_true", help="Run MCP smoke test")
    parser.add_argument("--host", default=os.getenv("AGENT_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("AGENT_PORT", "8000")))
    args = parser.parse_args()

    if args.dump_schema:
        print(json.dumps(itinerary_to_json_schema(), indent=2))
        return

    if args.smoke_mcp:
        from agent.smoke_mcp import main as smoke_main

        raise SystemExit(smoke_main([]))

    if args.serve:
        import uvicorn

        uvicorn.run("agent.main:api", host=args.host, port=args.port, reload=False)
        return

    result = run_once(args.message)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
