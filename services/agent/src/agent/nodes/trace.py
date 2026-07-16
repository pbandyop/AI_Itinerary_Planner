"""Shared agent_trace helpers — append-only deltas for LangGraph reducers."""

from __future__ import annotations

from typing import Any

from agent.schemas.state import concat_trace

__all__ = ["trace_delta", "concat_trace"]


def trace_delta(*entries: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a list of new trace entries (concatenated by GraphState reducer)."""
    return [e for e in entries if e]
