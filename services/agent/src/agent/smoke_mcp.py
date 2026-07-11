"""Phase 2 smoke test: POI Search MCP → Itinerary Builder MCP via LangChain tools."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from agent.mcp.itinerary_builder import build_itinerary
from agent.mcp.poi_search import poi_search
from agent.schemas.itinerary import Itinerary, TripConstraints
from agent.schemas.validation import validate_grounding_rules, validate_itinerary
from agent.tools.mcp_tools import itinerary_builder_tool, poi_search_tool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("smoke_mcp")


def _safe(text: str) -> str:
    """Avoid Windows console UnicodeEncodeError on non-ASCII OSM names."""
    return text.encode("ascii", errors="replace").decode("ascii")


def run_smoke(
    *,
    interests: list[str],
    num_days: int,
    pace: str,
    use_tools: bool,
    use_overpass: bool,
) -> int:
    print("=== Phase 2 MCP smoke test ===")
    print(f"interests={interests} days={num_days} pace={pace} tools={use_tools}")

    if use_tools:
        poi_raw = poi_search_tool.invoke(
            {
                "city": "Jaipur",
                "interests": interests,
                "constraints": [],
                "limit": 30,
                "use_overpass": use_overpass,
            }
        )
        poi_payload = json.loads(poi_raw)
        print(f"\n[poi_search_mcp] pois={len(poi_payload.get('pois', []))} "
              f"missing_data={poi_payload.get('missing_data')} "
              f"notes={poi_payload.get('notes')}")
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
            }
        )
        draft_payload = json.loads(draft_raw)
    else:
        poi_result = poi_search(
            interests=interests,
            limit=30,
            use_overpass=use_overpass,
        )
        print(
            f"\n[poi_search] pois={len(poi_result.pois)} "
            f"missing_data={poi_result.missing_data} notes={poi_result.notes}"
        )
        for p in poi_result.pois[:5]:
            print(
                f"  - {_safe(p.name)} ({p.osm_type}/{p.osm_id}) score={p.rank_score}"
            )
        draft = build_itinerary(
            candidate_pois=poi_result.pois,
            num_days=num_days,
            pace=pace,  # type: ignore[arg-type]
            interests=interests,
        )
        draft_payload = draft.model_dump(mode="json")
        poi_payload = poi_result.model_dump(mode="json")

    print(
        f"\n[itinerary_builder] days={len(draft_payload.get('days', []))} "
        f"missing_data={draft_payload.get('missing_data')} "
        f"notes={draft_payload.get('notes')}"
    )

    # Assemble a full Itinerary for schema validation
    trip = TripConstraints(
        city="Jaipur",
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
            }
        ],
        summary="Phase 2 smoke-test draft itinerary",
        uncertainty_notes=[
            n for n in [poi_payload.get("notes"), draft_payload.get("notes")] if n
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

    print("\nPASS: MCP pipeline produced a schema-valid, OSM-grounded draft.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test POI + Itinerary MCPs")
    parser.add_argument("--interests", nargs="*", default=["food", "culture"])
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--pace", default="relaxed", choices=["relaxed", "moderate", "packed"])
    parser.add_argument("--no-tools", action="store_true", help="Call MCP functions directly")
    parser.add_argument("--no-overpass", action="store_true", help="Skip live Overpass (seed only)")
    args = parser.parse_args(argv)
    return run_smoke(
        interests=args.interests,
        num_days=args.days,
        pace=args.pace,
        use_tools=not args.no_tools,
        use_overpass=not args.no_overpass,
    )


if __name__ == "__main__":
    sys.exit(main())
