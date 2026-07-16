"""Phase 2 smoke test: all four MCP tools via LangChain wrappers."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from agent.mcp.itinerary_builder import build_itinerary
from agent.mcp.poi_search import poi_search
from agent.mcp.travel_time import estimate_travel_times
from agent.mcp.weather import weather_adjustment
from agent.schemas.itinerary import Itinerary, TripConstraints
from agent.schemas.validation import validate_grounding_rules, validate_itinerary
from agent.tools.mcp_tools import (
    itinerary_builder_tool,
    poi_search_tool,
    travel_time_tool,
    weather_tool,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("smoke_mcp")


def _safe(text: str) -> str:
    """Avoid Windows console UnicodeEncodeError on non-ASCII OSM names."""
    return text.encode("ascii", errors="replace").decode("ascii")


def _first_day_points(draft_payload: dict) -> list[dict]:
    points: list[dict] = []
    days = draft_payload.get("days") or []
    if not days:
        return points
    day = days[0]
    for block_name in ("morning", "afternoon", "evening"):
        for stop in (day.get(block_name) or {}).get("stops") or []:
            points.append(stop)
    return points


def run_smoke(
    *,
    city: str,
    interests: list[str],
    num_days: int,
    pace: str,
    use_tools: bool,
    use_overpass: bool,
) -> int:
    print("=== Phase 2 MCP smoke test (4 tools, India) ===")
    print(
        f"city={city} interests={interests} days={num_days} pace={pace} tools={use_tools}"
    )

    if use_tools:
        poi_raw = poi_search_tool.invoke(
            {
                "city": city,
                "interests": interests,
                "constraints": [],
                "limit": 30,
                "use_overpass": use_overpass,
            }
        )
        poi_payload = json.loads(poi_raw)
        print(
            f"\n[poi_search_mcp] city={poi_payload.get('city')} "
            f"pois={len(poi_payload.get('pois', []))} "
            f"missing_data={poi_payload.get('missing_data')}"
        )
        for p in poi_payload.get("pois", [])[:5]:
            print(
                f"  - {_safe(p['name'])} ({p['osm_type']}/{p['osm_id']}) "
                f"score={p.get('rank_score')}"
            )

        draft_raw = itinerary_builder_tool.invoke(
            {
                "pois_json": json.dumps(poi_payload.get("pois", [])),
                "num_days": num_days,
                "pace": pace,
                "daily_time_window_min": 540,
                "interests": interests,
                "city": city,
            }
        )
        draft_payload = json.loads(draft_raw)

        points = _first_day_points(draft_payload)
        travel_raw = travel_time_tool.invoke(
            {"points_json": json.dumps(points), "mode": "city"}
        )
        travel_payload = json.loads(travel_raw)

        weather_raw = weather_tool.invoke(
            {"city": city, "start_date": None, "num_days": num_days}
        )
        weather_payload = json.loads(weather_raw)
    else:
        poi_result = poi_search(
            city=city,
            interests=interests,
            limit=30,
            use_overpass=use_overpass,
        )
        print(
            f"\n[poi_search] city={poi_result.city} pois={len(poi_result.pois)} "
            f"missing_data={poi_result.missing_data}"
        )
        draft = build_itinerary(
            candidate_pois=poi_result.pois,
            num_days=num_days,
            pace=pace,  # type: ignore[arg-type]
            interests=interests,
            city=city,
        )
        draft_payload = draft.model_dump(mode="json")
        poi_payload = poi_result.model_dump(mode="json")
        points = _first_day_points(draft_payload)
        travel_payload = estimate_travel_times(points=points, mode="city").model_dump(
            mode="json"
        )
        weather_payload = weather_adjustment(
            city=city, num_days=num_days
        ).model_dump(mode="json")

    print(
        f"\n[itinerary_builder_mcp] days={len(draft_payload.get('days', []))} "
        f"missing_data={draft_payload.get('missing_data')}"
    )
    print(
        f"[travel_time_estimator_mcp] legs={len(travel_payload.get('legs', []))} "
        f"total={travel_payload.get('total_duration_min')}m "
        f"missing_data={travel_payload.get('missing_data')}"
    )
    for leg in (travel_payload.get("legs") or [])[:3]:
        print(
            f"  - {_safe(leg['from_name'])} -> {_safe(leg['to_name'])}: "
            f"{leg['duration_min']}m ({leg.get('distance_km')} km)"
        )

    print(
        f"[weather_adjustment_mcp] days={len(weather_payload.get('days', []))} "
        f"missing_data={weather_payload.get('missing_data')} "
        f"adjustments={len(weather_payload.get('adjustments', []))}"
    )
    for day in weather_payload.get("days") or []:
        print(
            f"  - {day.get('calendar_date')}: {day.get('weather_label')} "
            f"risk={day.get('rain_risk')} precip%={day.get('precip_probability_max')}"
        )

    trip = TripConstraints(
        city=city,
        country="India",
        num_days=num_days,
        interests=interests,
        pace=pace,  # type: ignore[arg-type]
        confirmed=False,
    )
    itinerary = Itinerary(
        trip=trip,
        days=draft_payload["days"],
        sources=[
            {
                "title": "OpenStreetMap via Overpass",
                "url": "https://www.openstreetmap.org",
                "dataset": "openstreetmap",
                "snippet": "POIs resolved to osm_type/osm_id records.",
            },
            {
                "title": "Open-Meteo Forecast",
                "url": "https://open-meteo.com/",
                "dataset": "open-meteo",
                "snippet": "Daily weather used for rain-risk adjustments.",
            },
        ],
        summary="Phase 2 smoke-test draft itinerary",
        uncertainty_notes=[
            n
            for n in [
                poi_payload.get("notes"),
                draft_payload.get("notes"),
                travel_payload.get("notes"),
                weather_payload.get("notes"),
            ]
            if n
        ],
    )
    result = validate_itinerary(itinerary)
    if not result.ok or result.itinerary is None:
        print("FAIL schema:", result.errors)
        return 1

    grounding = validate_grounding_rules(result.itinerary)
    if grounding:
        print("FAIL grounding:", grounding)
        return 1

    if weather_payload.get("missing_data"):
        print("WARN: weather missing_data=True (stated honestly).")

    for day in result.itinerary.days:
        print(
            f"  Day {day.day_index}: {day.total_duration_min}m | "
            f"stops={len(day.all_stops)} | theme={_safe(day.theme or '')}"
        )
        for stop in day.all_stops:
            assert stop.osm_id > 0
            print(
                f"    * {_safe(stop.name)} [{stop.osm_type}/{stop.osm_id}] "
                f"{stop.duration_min}m"
            )

    print(
        "\nPASS: All 4 MCP tools ran; itinerary is schema-valid and OSM-grounded."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test Phase 2 MCP tools")
    parser.add_argument("--city", default="Jaipur", help="Indian city from catalog")
    parser.add_argument("--interests", nargs="*", default=["food", "culture"])
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument(
        "--pace", default="relaxed", choices=["relaxed", "moderate", "packed"]
    )
    parser.add_argument(
        "--no-tools", action="store_true", help="Call MCP functions directly"
    )
    parser.add_argument(
        "--no-overpass",
        action="store_true",
        help="(Disabled) Capstone requires live Overpass; flag exits with error",
    )
    args = parser.parse_args(argv)
    if args.no_overpass:
        print(
            "ERROR: --no-overpass is disabled. Capstone POI search is live Overpass only.",
            file=sys.stderr,
        )
        return 2
    return run_smoke(
        city=args.city,
        interests=args.interests,
        num_days=args.days,
        pace=args.pace,
        use_tools=not args.no_tools,
        use_overpass=True,
    )


if __name__ == "__main__":
    sys.exit(main())
