"""Geo helpers for heuristic travel-time estimates."""

from __future__ import annotations

import math


# Approx Jaipur Old City center (City Palace area)
JAIPUR_CENTER = (26.9258, 75.8236)

# Rough Jaipur bounding box for Overpass
JAIPUR_BBOX = (26.78, 75.70, 27.05, 75.95)  # south, west, north, east


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def estimate_travel_minutes(
    lat1: float | None,
    lon1: float | None,
    lat2: float | None,
    lon2: float | None,
    *,
    mode: str = "city",
) -> int:
    """Heuristic door-to-door minutes (not live transit)."""
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return 20
    km = haversine_km(lat1, lon1, lat2, lon2)
    if mode == "walk":
        # ~4.5 km/h + 3 min overhead
        return max(5, int(round(km / 4.5 * 60 + 3)))
    # Mixed city traffic heuristic ~18 km/h effective + overhead
    return max(8, int(round(km / 18.0 * 60 + 5)))
