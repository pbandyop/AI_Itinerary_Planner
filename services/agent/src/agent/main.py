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

load_dotenv()

api = FastAPI(
    title="AI Itinerary Planner Agent",
    description="LangGraph multi-agent service (Phase 0 stub)",
    version="0.1.0",
)


class InvokeRequest(BaseModel):
    user_message: str = Field(..., min_length=0, description="Spoken or typed user input")


class InvokeResponse(BaseModel):
    user_reply: str
    intent: str | None = None
    safety_status: str | None = None
    revision_count: int = 0
    raw_state: dict[str, Any] | None = None


@api.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "phase": "0", "graph": "START→orchestrator→END"}


@api.post("/invoke", response_model=InvokeResponse)
def invoke(body: InvokeRequest) -> InvokeResponse:
    result = graph_app.invoke(
        {
            "user_message": body.user_message,
            "revision_count": 0,
        }
    )
    return InvokeResponse(
        user_reply=result.get("user_reply", ""),
        intent=result.get("intent"),
        safety_status=result.get("safety_status"),
        revision_count=result.get("revision_count", 0),
        raw_state=dict(result),
    )


def run_once(user_message: str) -> dict[str, Any]:
    return graph_app.invoke(
        {
            "user_message": user_message,
            "revision_count": 0,
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Itinerary Planner agent (Phase 0)")
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
    parser.add_argument("--host", default=os.getenv("AGENT_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("AGENT_PORT", "8000")))
    args = parser.parse_args()

    if args.serve:
        import uvicorn

        uvicorn.run("agent.main:api", host=args.host, port=args.port, reload=False)
        return

    result = run_once(args.message)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
