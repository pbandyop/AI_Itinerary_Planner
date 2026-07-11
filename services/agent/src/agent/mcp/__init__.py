"""MCP package — tool backends for specialist agents.

Import submodules directly (e.g. ``agent.mcp.geo``) to avoid circular imports
with ``agent.schemas``.
"""

__all__ = [
    "poi_search",
    "build_itinerary",
    "estimate_travel_times",
    "weather_adjustment",
]


def __getattr__(name: str):
    if name == "poi_search":
        from agent.mcp.poi_search import poi_search

        return poi_search
    if name == "build_itinerary":
        from agent.mcp.itinerary_builder import build_itinerary

        return build_itinerary
    if name == "estimate_travel_times":
        from agent.mcp.travel_time import estimate_travel_times

        return estimate_travel_times
    if name == "weather_adjustment":
        from agent.mcp.weather import weather_adjustment

        return weather_adjustment
    raise AttributeError(name)
