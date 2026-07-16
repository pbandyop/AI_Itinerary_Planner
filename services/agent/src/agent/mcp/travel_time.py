"""Travel Time Estimator MCP — heuristic legs between stops (Jaipur)."""

from __future__ import annotations

import logging
from typing import Any, Literal

from agent.mcp.geo import estimate_travel_minutes, haversine_km
from agent.schemas.specialists import TravelLeg, TravelTimeResult

logger = logging.getLogger(__name__)

TravelMode = Literal["walk", "city"]
ModeChoice = TravelMode | Literal["auto"]

# Short hops: walk; longer: city-road heuristic (displayed as car — no bus API).
_WALK_MAX_KM = 1.5


def _point_fields(point: dict[str, Any]) -> tuple[str, str | None, float | None, float | None]:
    name = str(point.get("name") or point.get("from_name") or "Unknown")
    osm = point.get("osm_ref")
    if not osm and point.get("osm_type") and point.get("osm_id"):
        osm = f"{point['osm_type']}/{point['osm_id']}"
    lat = point.get("lat")
    lon = point.get("lon")
    return name, osm, lat, lon


def choose_travel_mode(distance_km: float | None) -> TravelMode:
    """Pick walk vs city from distance when MCP has no transit feed."""
    if distance_km is not None and distance_km < _WALK_MAX_KM:
        return "walk"
    return "city"


def display_mode_label(mode: str | None) -> str | None:
    """User-facing mode label; omit bus unless a real MCP mode exists."""
    if mode == "walk":
        return "walk"
    if mode in {"city", "car", "drive", "taxi"}:
        return "car"
    if mode == "bus":
        return "bus"
    return None


def estimate_leg(
    from_point: dict[str, Any],
    to_point: dict[str, Any],
    *,
    mode: ModeChoice = "auto",
) -> TravelLeg | None:
    """Return a travel leg, or None when from/to are the same place (no inventing)."""
    from_name, from_osm, lat1, lon1 = _point_fields(from_point)
    to_name, to_osm, lat2, lon2 = _point_fields(to_point)

    same_osm = (
        from_osm
        and to_osm
        and str(from_osm).lower() == str(to_osm).lower()
    )
    same_name = from_name.strip().lower() == to_name.strip().lower()
    missing_coords = lat1 is None or lon1 is None or lat2 is None or lon2 is None
    distance = None if missing_coords else round(haversine_km(lat1, lon1, lat2, lon2), 2)
    if same_osm or (same_name and (distance is None or distance < 0.05)):
        return None
    if distance is not None and distance < 0.05:
        return None

    chosen: TravelMode = (
        choose_travel_mode(distance) if mode == "auto" else mode  # type: ignore[assignment]
    )
    duration = estimate_travel_minutes(lat1, lon1, lat2, lon2, mode=chosen)
    if duration <= 0:
        return None

    return TravelLeg(
        from_name=from_name,
        to_name=to_name,
        from_osm=from_osm,
        to_osm=to_osm,
        distance_km=distance,
        duration_min=duration,
        mode=chosen,
        method="haversine_heuristic",
    )


def estimate_travel_times(
    *,
    points: list[dict[str, Any]] | None = None,
    legs: list[dict[str, Any]] | None = None,
    mode: ModeChoice = "auto",
) -> TravelTimeResult:
    """MCP: estimate travel minutes between ordered points or explicit pairs.

    Prefer ``points`` (ordered stop list) or ``legs`` as
    ``[{from: {...}, to: {...}}, ...]``.
    Skips zero-distance / same-place hops (no hallucinated travel).
    """
    notes: list[str] = [
        "Travel times are heuristic (haversine + city/walk speed), not live transit."
    ]
    computed: list[TravelLeg] = []
    missing = False

    if legs:
        for item in legs:
            frm = item.get("from") or item.get("from_point") or {}
            to = item.get("to") or item.get("to_point") or {}
            leg = estimate_leg(frm, to, mode=mode)
            if leg is None:
                continue
            if leg.distance_km is None:
                missing = True
            computed.append(leg)
    elif points and len(points) >= 2:
        for i in range(len(points) - 1):
            leg = estimate_leg(points[i], points[i + 1], mode=mode)
            if leg is None:
                # Same place twice in sequence — data issue, not invented travel
                continue
            if leg.distance_km is None:
                missing = True
            computed.append(leg)
    else:
        missing = True
        notes.append("Need at least two points or one explicit leg — data missing.")
        return TravelTimeResult(
            legs=[],
            total_duration_min=0,
            missing_data=True,
            notes="; ".join(notes),
        )

    if missing:
        notes.append("Some coordinates were missing; used default duration for those legs.")

    total = sum(leg.duration_min for leg in computed)
    logger.info("travel_time_mcp: %d legs, total=%dm mode=%s", len(computed), total, mode)
    return TravelTimeResult(
        legs=computed,
        total_duration_min=total,
        missing_data=missing,
        notes="; ".join(notes),
    )


def apply_travel_times_to_days(days: list[dict[str, Any]], *, mode: TravelMode = "city") -> list[dict[str, Any]]:
    """Rewrite travel_to_next_min on stops using the Travel Time Estimator MCP."""
    updated_days: list[dict[str, Any]] = []
    for day in days:
        day_copy = dict(day)
        for block_name in ("morning", "afternoon", "evening"):
            block = dict(day_copy.get(block_name) or {})
            stops = [dict(s) for s in (block.get("stops") or [])]
            if len(stops) >= 2:
                result = estimate_travel_times(points=stops, mode=mode)
                for i, leg in enumerate(result.legs):
                    stops[i]["travel_to_next_min"] = leg.duration_min
                if stops:
                    stops[-1]["travel_to_next_min"] = None
            block["stops"] = stops
            day_copy[block_name] = block
        updated_days.append(day_copy)
    return updated_days
