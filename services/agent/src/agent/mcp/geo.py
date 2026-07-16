"""India geography helpers — city catalog, bbox, and travel heuristics."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class CityInfo:
    name: str
    state: str
    lat: float
    lon: float
    bbox: tuple[float, float, float, float]  # south, west, north, east
    aliases: tuple[str, ...] = ()

    @property
    def slug(self) -> str:
        return (
            self.name.lower()
            .replace(" ", "_")
            .replace(".", "")
            .replace("'", "")
        )


def _repo_data_dir() -> Path:
    # geo.py → mcp → agent → src → services/agent → repo
    return Path(__file__).resolve().parents[5] / "data"


@lru_cache(maxsize=1)
def load_india_cities() -> dict[str, CityInfo]:
    path = _repo_data_dir() / "india_cities.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    cities: dict[str, CityInfo] = {}
    for item in raw.get("cities", []):
        info = CityInfo(
            name=item["name"],
            state=item.get("state", ""),
            lat=float(item["lat"]),
            lon=float(item["lon"]),
            bbox=(
                float(item["bbox"][0]),
                float(item["bbox"][1]),
                float(item["bbox"][2]),
                float(item["bbox"][3]),
            ),
            aliases=tuple(item.get("aliases") or []),
        )
        cities[info.name.lower()] = info
        for alias in info.aliases:
            cities[alias.lower()] = info
    return cities


def list_india_city_names() -> list[str]:
    seen: set[str] = set()
    names: list[str] = []
    for info in load_india_cities().values():
        if info.name not in seen:
            seen.add(info.name)
            names.append(info.name)
    return sorted(names)


def resolve_city(name: str) -> CityInfo | None:
    """Resolve an Indian city by name or alias (case-insensitive).

    Soft matching only allows a query to *contain* a known city/alias
    (e.g. \"New Delhi\" → Delhi). Short tokens like \"to\" / \"a\" must NOT
    match mid-substrings of city names (e.g. \"to\" ⊂ Chittorgarh).
    """
    if not name or not name.strip():
        return None
    key = name.strip().lower()
    cities = load_india_cities()
    if key in cities:
        return cities[key]

    # Soft contains: the query string contains a full city name or alias.
    # Require length ≥ 4 so tiny tokens never soft-match.
    if len(key) < 4:
        return None

    best: CityInfo | None = None
    best_len = 0
    seen: set[str] = set()
    for info in cities.values():
        if info.name in seen:
            continue
        seen.add(info.name)
        candidates = (info.name, *info.aliases)
        for label in candidates:
            label_l = label.lower().strip()
            if len(label_l) < 4:
                continue
            # Query contains the city label (not the other way around).
            if label_l in key and len(label_l) > best_len:
                best = info
                best_len = len(label_l)
    return best


def _city_labels() -> list[tuple[str, str]]:
    """(label_lower, canonical_name) sorted longest-first for message scanning."""
    labels: list[tuple[str, str]] = []
    seen: set[str] = set()
    for info in load_india_cities().values():
        if info.name in seen:
            continue
        seen.add(info.name)
        labels.append((info.name.lower(), info.name))
        for alias in info.aliases:
            labels.append((alias.lower(), info.name))
    labels.sort(key=lambda x: (-len(x[0]), x[0]))
    return labels


def is_supported_indian_city(name: str) -> bool:
    return resolve_city(name) is not None


def city_center(name: str) -> tuple[float, float]:
    info = resolve_city(name)
    if info:
        return info.lat, info.lon
    # Geographic center-ish fallback for India if unknown
    return 22.0, 79.0


def city_bbox(name: str) -> tuple[float, float, float, float]:
    info = resolve_city(name)
    if info:
        return info.bbox
    # Broad India mainland fallback (last resort)
    return (8.0, 68.0, 37.0, 97.5)


# Back-compat aliases used by older modules
JAIPUR_CENTER = (26.9258, 75.8236)
JAIPUR_BBOX = (26.78, 75.70, 27.05, 75.95)


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
    """Heuristic door-to-door minutes (not live transit).

    Same coordinates / sub-50m hops return 0 — never invent a fake 8-minute trip.
    """
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return 20
    km = haversine_km(lat1, lon1, lat2, lon2)
    if km < 0.05:
        return 0
    if mode == "walk":
        return max(5, int(round(km / 4.5 * 60 + 3)))
    return max(8, int(round(km / 18.0 * 60 + 5)))
