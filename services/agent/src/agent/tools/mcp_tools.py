"""LangChain tool wrappers around MCP backends (demo-visible tool calls)."""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from agent.mcp.itinerary_builder import build_itinerary
from agent.mcp.poi_search import poi_search
from agent.schemas.itinerary import Pace
from agent.schemas.specialists import POICandidate

logger = logging.getLogger(__name__)


class POISearchInput(BaseModel):
    city: str = Field(default="Jaipur", description="City to search (Jaipur only)")
    interests: list[str] = Field(
        default_factory=lambda: ["food", "culture"],
        description="Traveler interests e.g. food, culture, heritage",
    )
    constraints: list[str] = Field(
        default_factory=list,
        description="Soft constraints e.g. prefer indoor",
    )
    limit: int = Field(default=30, ge=5, le=80)
    use_overpass: bool = Field(
        default=True,
        description="If false, use curated OSM seed only (offline/demo)",
    )


class ItineraryBuilderInput(BaseModel):
    pois_json: str = Field(
        ...,
        description="JSON array of POICandidate objects from poi_search_mcp",
    )
    num_days: int = Field(default=3, ge=2, le=4)
    pace: Pace = Field(default="relaxed")
    daily_time_window_min: int = Field(default=540, ge=180, le=840)
    interests: list[str] = Field(default_factory=list)


def _poi_search_tool(
    city: str = "Jaipur",
    interests: list[str] | None = None,
    constraints: list[str] | None = None,
    limit: int = 30,
    use_overpass: bool = True,
) -> str:
    logger.info(
        "TOOL poi_search_mcp city=%s interests=%s constraints=%s limit=%s overpass=%s",
        city,
        interests,
        constraints,
        limit,
        use_overpass,
    )
    city_arg = "Jaipur" if city.lower() == "jaipur" else city
    result = poi_search(
        city=city_arg,  # type: ignore[arg-type]
        interests=interests,
        constraints=constraints,
        limit=limit,
        use_overpass=use_overpass,
    )
    payload = result.model_dump(mode="json")
    logger.info(
        "TOOL poi_search_mcp → %d pois missing_data=%s",
        len(result.pois),
        result.missing_data,
    )
    return json.dumps(payload, indent=2)


def _itinerary_builder_tool(
    pois_json: str,
    num_days: int = 3,
    pace: Pace = "relaxed",
    daily_time_window_min: int = 540,
    interests: list[str] | None = None,
) -> str:
    logger.info(
        "TOOL itinerary_builder_mcp days=%s pace=%s window=%s",
        num_days,
        pace,
        daily_time_window_min,
    )
    raw: list[dict[str, Any]] = json.loads(pois_json)
    pois = [POICandidate.model_validate(item) for item in raw]
    draft = build_itinerary(
        candidate_pois=pois,
        num_days=num_days,
        pace=pace,
        daily_time_window_min=daily_time_window_min,
        interests=interests or [],
    )
    logger.info(
        "TOOL itinerary_builder_mcp → %d days missing_data=%s",
        len(draft.days),
        draft.missing_data,
    )
    return json.dumps(draft.model_dump(mode="json"), indent=2)


poi_search_tool = StructuredTool.from_function(
    name="poi_search_mcp",
    description=(
        "POI Search MCP: find Jaipur points of interest from OpenStreetMap "
        "(Overpass). Returns ranked POIs with stable osm_type/osm_id."
    ),
    func=_poi_search_tool,
    args_schema=POISearchInput,
)

itinerary_builder_tool = StructuredTool.from_function(
    name="itinerary_builder_mcp",
    description=(
        "Itinerary Builder MCP: pack candidate POIs into a day-wise "
        "morning/afternoon/evening draft itinerary for Jaipur (2–4 days)."
    ),
    func=_itinerary_builder_tool,
    args_schema=ItineraryBuilderInput,
)


def get_mcp_tools() -> list[StructuredTool]:
    return [poi_search_tool, itinerary_builder_tool]
