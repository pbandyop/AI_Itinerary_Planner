"""CLI + FastAPI entrypoints for the LangGraph agent service."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel, Field

from agent.graph import app as graph_app
from agent.schemas.state import empty_graph_state
from agent.schemas.validation import itinerary_to_json_schema

load_dotenv()

api = FastAPI(
    title="AI Itinerary Planner Agent",
    description="LangGraph multi-agent service (Phase 1: schema + state)",
    version="0.1.1",
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


@api.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "phase": "1",
        "graph": "START→orchestrator→END",
        "schema_version": "1.0",
    }


@api.get("/schema/itinerary")
def itinerary_schema() -> dict[str, Any]:
    """JSON Schema for the shared itinerary contract."""
    return itinerary_to_json_schema()


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
    parser = argparse.ArgumentParser(description="AI Itinerary Planner agent (Phase 1)")
    parser.add_argument(
        "message",
        nargs="?",
        default="Plan a 3-day trip to Jaipur next weekend. I like food and culture.",
        help="User message to send through the stub graph",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Run FastAPI server instead of a one-shot invoke",
    )
    parser.add_argument(
        "--dump-schema",
        action="store_true",
        help="Print itinerary JSON Schema and exit",
    )
    parser.add_argument("--host", default=os.getenv("AGENT_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("AGENT_PORT", "8000")))
    args = parser.parse_args()

    if args.dump_schema:
        print(json.dumps(itinerary_to_json_schema(), indent=2))
        return

    if args.serve:
        import uvicorn

        uvicorn.run("agent.main:api", host=args.host, port=args.port, reload=False)
        return

    result = run_once(args.message)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
