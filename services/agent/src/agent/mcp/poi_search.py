"""POI Search MCP — OpenStreetMap via Overpass API (India cities)."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

from agent.mcp.geo import city_bbox, resolve_city
from agent.schemas.specialists import POICandidate, POISearchResult

logger = logging.getLogger(__name__)

OVERPASS_URL = os.getenv(
    "OVERPASS_API_URL", "https://overpass-api.de/api/interpreter"
)

INTEREST_FILTERS: dict[str, list[str]] = {
    "food": [
        'node["amenity"~"restaurant|cafe|fast_food|food_court"]',
        'way["amenity"~"restaurant|cafe|fast_food|food_court"]',
        'node["shop"="bakery"]',
    ],
    "culture": [
        'node["tourism"~"museum|gallery|attraction|artwork"]',
        'way["tourism"~"attraction|museum|gallery"]',
        'node["historic"]',
        'way["historic"]',
        'relation["historic"]',
    ],
    "heritage": [
        'node["historic"]',
        'way["historic"]',
        'relation["historic"]',
        'node["tourism"="attraction"]',
        'way["tourism"="attraction"]',
        'way["building"="castle"]',
    ],
    "shopping": [
        'node["shop"~"mall|clothes|gift|jewelry"]',
        'way["shop"~"mall|clothes|gift|jewelry"]',
    ],
    "nature": [
        'node["leisure"~"park|garden"]',
        'way["leisure"~"park|garden"]',
        'node["tourism"="viewpoint"]',
    ],
    "temple": [
        'node["amenity"="place_of_worship"]',
        'way["amenity"="place_of_worship"]',
    ],
}

DEFAULT_INTERESTS = ["culture", "heritage", "food"]


def _category_from_tags(tags: dict[str, str]) -> str:
    amenity = tags.get("amenity", "")
    tourism = tags.get("tourism", "")
    historic = tags.get("historic")
    leisure = tags.get("leisure", "")
    shop = tags.get("shop")
    if amenity in {"restaurant", "cafe", "fast_food", "food_court"} or shop == "bakery":
        return "food"
    if amenity == "place_of_worship":
        return "temple"
    if tourism in {"museum", "gallery"}:
        return "museum"
    if tourism == "viewpoint":
        return "viewpoint"
    if leisure in {"park", "garden"}:
        return "park"
    name = (tags.get("name") or "").lower()
    if shop or "bazaar" in name or "market" in name:
        return "market"
    if historic or tourism == "attraction":
        return "heritage"
    if tourism:
        return "attraction"
    return "other"


def build_overpass_query(
    interests: list[str],
    *,
    bbox: tuple[float, float, float, float],
    limit: int = 80,
) -> str:
    keys = [i.lower().strip() for i in interests if i.strip()] or DEFAULT_INTERESTS
    s, w, n, e = bbox
    bbox_clause = f"({s},{w},{n},{e})"
    clauses: list[str] = []
    for key in keys:
        filters = INTEREST_FILTERS.get(key) or [
            'node["tourism"~"attraction|museum"]',
            'way["tourism"~"attraction|museum"]',
            'node["historic"]',
            'way["historic"]',
        ]
        for f in filters:
            clauses.append(f"{f}{bbox_clause};")
    body = "\n  ".join(clauses)
    return f"""[out:json][timeout:45];
(
  {body}
);
out center tags {limit};
"""


def _element_coords(el: dict[str, Any]) -> tuple[float | None, float | None]:
    if "lat" in el and "lon" in el:
        return float(el["lat"]), float(el["lon"])
    center = el.get("center") or {}
    if "lat" in center and "lon" in center:
        return float(center["lat"]), float(center["lon"])
    return None, None


def _rank_score(tags: dict[str, str], interests: list[str], category: str) -> float:
    score = 1.0
    name = tags.get("name:en") or tags.get("name")
    if not name:
        return 0.0
    score += 2.0
    if tags.get("wikidata") or tags.get("wikipedia"):
        score += 3.0
    if tags.get("tourism") == "attraction":
        score += 1.5
    if tags.get("historic"):
        score += 1.5
    interest_set = {i.lower() for i in interests}
    if category in interest_set:
        score += 2.0
    if "food" in interest_set and category == "food":
        score += 2.0
    if {"culture", "heritage"} & interest_set and category in {
        "heritage",
        "museum",
        "attraction",
    }:
        score += 2.0
    if name.isascii():
        score += 0.3
    return score


def _parse_elements(
    elements: list[dict[str, Any]],
    interests: list[str],
) -> list[POICandidate]:
    seen: set[str] = set()
    pois: list[POICandidate] = []
    for el in elements:
        tags = el.get("tags") or {}
        name = tags.get("name:en") or tags.get("name")
        if not name:
            continue
        osm_type = el.get("type")
        osm_id = el.get("id")
        if osm_type not in {"node", "way", "relation"} or not osm_id:
            continue
        key = f"{osm_type}/{osm_id}"
        if key in seen:
            continue
        seen.add(key)
        lat, lon = _element_coords(el)
        category = _category_from_tags(tags)
        score = _rank_score(tags, interests, category)
        if score <= 0:
            continue
        matched = [
            i
            for i in interests
            if i.lower() in {category, "culture", "heritage", "food"}
            or (
                i.lower() == "culture"
                and category in {"heritage", "museum", "attraction", "temple"}
            )
            or (i.lower() == "heritage" and category in {"heritage", "museum"})
            or (i.lower() == "food" and category == "food")
        ]
        pois.append(
            POICandidate(
                name=name,
                osm_type=osm_type,  # type: ignore[arg-type]
                osm_id=int(osm_id),
                lat=lat,
                lon=lon,
                category=category,
                tags={
                    k: v
                    for k, v in tags.items()
                    if k
                    in {
                        "tourism",
                        "historic",
                        "amenity",
                        "cuisine",
                        "wikipedia",
                        "wikidata",
                    }
                },
                rank_score=round(score, 2),
                matched_interests=matched or [i.lower() for i in interests[:1]],
            )
        )
    pois.sort(key=lambda p: (-(p.rank_score or 0), p.name))
    return pois


def _data_dir() -> Path:
    return Path(__file__).resolve().parents[5] / "data"


def load_seed_pois(city: str, path: Path | None = None) -> list[POICandidate]:
    """Load curated OSM-backed seed for a city slug when available."""
    info = resolve_city(city)
    slug = info.slug if info else city.lower().replace(" ", "_")
    data_dir = _data_dir()
    candidates = [
        path,
        data_dir / "pois" / f"{slug}.json",
        data_dir / "jaipur_pois_seed.json" if slug == "jaipur" else None,
    ]
    seed_path = next((p for p in candidates if p and p.exists()), None)
    if seed_path is None:
        logger.info("No POI seed file for city=%s slug=%s", city, slug)
        return []
    raw = json.loads(seed_path.read_text(encoding="utf-8"))
    return [POICandidate.model_validate(item) for item in raw]


def fetch_overpass(query: str, *, timeout: float = 50.0) -> list[dict[str, Any]]:
    logger.info("Overpass request (%d chars) -> %s", len(query), OVERPASS_URL)
    headers = {
        "User-Agent": "AI-Itinerary-Planner/0.2 (capstone; contact: github.com/pbandyop/AI_Itinerary_Planner)",
        "Accept": "application/json",
    }
    with httpx.Client(timeout=timeout, headers=headers) as client:
        resp = client.post(OVERPASS_URL, data={"data": query})
        resp.raise_for_status()
        payload = resp.json()
    elements = payload.get("elements") or []
    logger.info("Overpass returned %d elements", len(elements))
    return elements


def poi_search(
    *,
    city: str = "Jaipur",
    interests: list[str] | None = None,
    constraints: list[str] | None = None,
    limit: int = 40,
    use_overpass: bool = True,
) -> POISearchResult:
    """MCP: search Indian-city POIs grounded in OpenStreetMap records."""
    info = resolve_city(city)
    if info is None:
        return POISearchResult(
            city=city,
            query_interests=interests or [],
            pois=[],
            missing_data=True,
            notes=(
                f"City {city!r} is not in the India catalog "
                "(data/india_cities.json). Cannot search outside India."
            ),
        )

    canonical = info.name
    interests = [i.strip().lower() for i in (interests or DEFAULT_INTERESTS) if i.strip()]
    constraints = constraints or []
    notes: list[str] = []
    pois: list[POICandidate] = []
    missing = False
    bbox = city_bbox(canonical)

    if use_overpass:
        try:
            query = build_overpass_query(
                interests, bbox=bbox, limit=max(limit * 2, 60)
            )
            elements = fetch_overpass(query)
            pois = _parse_elements(elements, interests)
            if not pois:
                missing = True
                notes.append(
                    f"Overpass returned no named POIs for {canonical} interest filters."
                )
        except Exception as exc:  # noqa: BLE001
            missing = True
            notes.append(f"Overpass unavailable ({exc.__class__.__name__}: {exc}).")
            logger.exception("Overpass POI search failed for %s", canonical)

    if len(pois) < 5:
        seed = load_seed_pois(canonical)
        if seed:
            filtered = [
                p
                for p in seed
                if not interests
                or p.category in interests
                or any(
                    i in {"culture", "heritage"}
                    and p.category in {"heritage", "museum", "attraction", "temple"}
                    for i in interests
                )
                or ("food" in interests and p.category == "food")
            ] or seed
            have = {f"{p.osm_type}/{p.osm_id}" for p in pois}
            for p in filtered:
                ref = f"{p.osm_type}/{p.osm_id}"
                if ref not in have:
                    pois.append(p)
                    have.add(ref)
            notes.append(
                f"Augmented with curated OSM seed for {canonical} "
                f"(data/pois/{info.slug}.json)."
            )
            if missing and pois:
                notes.append(
                    "Live Overpass data was missing/partial; seed used as fallback."
                )
        elif missing:
            notes.append(
                f"No curated seed for {canonical}. Add data/pois/{info.slug}.json "
                "or retry when Overpass is available."
            )

    if any("indoor" in c.lower() for c in constraints):
        for p in pois:
            if p.category in {"museum", "food"}:
                p.rank_score = (p.rank_score or 0) + 1.5
        pois.sort(key=lambda p: (-(p.rank_score or 0), p.name))

    pois = pois[:limit]
    if not pois:
        missing = True
        notes.append(
            f"No POIs available for {canonical}, India. Data is missing — cannot invent places."
        )

    return POISearchResult(
        city=canonical,
        query_interests=interests,
        pois=pois,
        missing_data=missing,
        notes="; ".join(notes) if notes else None,
    )
