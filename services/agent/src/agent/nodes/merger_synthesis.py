"""Compatibility shim — itinerary optimization lives in itinerary_optimize."""

from agent.nodes.itinerary_optimize import (  # noqa: F401
    heuristic_synthesize,
    llm_synthesize,
    optimize_itinerary,
)
from agent.nodes.itinerary_optimize import optimize_itinerary as synthesize_itinerary

__all__ = [
    "heuristic_synthesize",
    "llm_synthesize",
    "optimize_itinerary",
    "synthesize_itinerary",
]
