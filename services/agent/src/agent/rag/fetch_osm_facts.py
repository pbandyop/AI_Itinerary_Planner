"""Fetch OSM description / opening_hours fact cards for Jaipur."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from agent.mcp.poi_search import fetch_overpass
from agent.rag.paths import corpus_dir

logger = logging.getLogger(__name__)

# Approx Jaipur metro bbox
JAIPUR_BBOX = (26.75, 75.65, 27.10, 76.00)  # south, west, north, east


def _osm_url(osm_type: str, osm_id: int) -> str:
    return f"https://www.openstreetmap.org/{osm_type}/{osm_id}"


def _element_center(el: dict[str, Any]) -> tuple[float | None, float | None]:
    if "lat" in el and "lon" in el:
        return float(el["lat"]), float(el["lon"])
    center = el.get("center") or {}
    if "lat" in center and "lon" in center:
        return float(center["lat"]), float(center["lon"])
    return None, None


def _build_fact_text(name: str, tags: dict[str, Any], osm_type: str, osm_id: int) -> str:
    lines = [f"{name} (OpenStreetMap {osm_type}/{osm_id})."]
    category_bits = []
    for key in ("tourism", "historic", "amenity", "leisure", "shop", "natural"):
        if tags.get(key):
            category_bits.append(f"{key}={tags[key]}")
    if category_bits:
        lines.append("Category: " + ", ".join(category_bits) + ".")
    if tags.get("description"):
        lines.append(str(tags["description"]).strip())
    if tags.get("opening_hours"):
        lines.append(f"Opening hours: {tags['opening_hours']}.")
    phone = tags.get("phone") or tags.get("contact:phone")
    if phone:
        lines.append(f"Phone: {phone}.")
    website = tags.get("website") or tags.get("contact:website")
    if website:
        lines.append(f"Website: {website}.")
    addr_parts = [
        tags.get(k)
        for k in (
            "addr:housenumber",
            "addr:street",
            "addr:suburb",
            "addr:city",
        )
        if tags.get(k)
    ]
    if addr_parts:
        lines.append("Address: " + ", ".join(str(p) for p in addr_parts) + ".")
    if tags.get("wikipedia"):
        lines.append(f"Wikipedia: {tags['wikipedia']}.")
    return " ".join(lines)


def fetch_osm_facts(*, limit: int = 250) -> Path:
    s, w, n, e = JAIPUR_BBOX
    # Prefer elements that have useful tip/hours fields; also keep notable named tourism.
    query = f"""
    [out:json][timeout:90];
    (
      node["name"]["opening_hours"]({s},{w},{n},{e});
      way["name"]["opening_hours"]({s},{w},{n},{e});
      node["name"]["description"]({s},{w},{n},{e});
      way["name"]["description"]({s},{w},{n},{e});
      node["name"]["tourism"~"attraction|museum|viewpoint|theme_park|zoo|hotel"]({s},{w},{n},{e});
      way["name"]["tourism"~"attraction|museum|viewpoint|theme_park|zoo|hotel"]({s},{w},{n},{e});
      node["name"]["historic"]({s},{w},{n},{e});
      way["name"]["historic"]({s},{w},{n},{e});
      node["name"]["amenity"~"cafe|restaurant|place_of_worship"]({s},{w},{n},{e});
      way["name"]["amenity"~"cafe|restaurant|place_of_worship"]({s},{w},{n},{e});
    );
    out center tags;
    """
    elements = fetch_overpass(query, timeout=100.0)
    places: list[dict[str, Any]] = []
    seen: set[str] = set()
    for el in elements:
        tags = el.get("tags") or {}
        name = str(tags.get("name") or "").strip()
        if not name:
            continue
        osm_type = str(el.get("type") or "node")
        osm_id = int(el.get("id") or 0)
        if osm_id <= 0:
            continue
        key = f"{osm_type}/{osm_id}"
        if key in seen:
            continue
        # Require at least one useful field OR tourism/historic
        useful = any(
            tags.get(k)
            for k in (
                "description",
                "opening_hours",
                "tourism",
                "historic",
                "wikipedia",
            )
        )
        if not useful:
            continue
        seen.add(key)
        lat, lon = _element_center(el)
        text = _build_fact_text(name, tags, osm_type, osm_id)
        places.append(
            {
                "city": "Jaipur",
                "title": name,
                "place_name": name,
                "source": "OpenStreetMap",
                "dataset": "openstreetmap",
                "url": _osm_url(osm_type, osm_id),
                "license": "ODbL 1.0",
                "text": text,
                "atomic": True,
                "osm_type": osm_type,
                "osm_id": osm_id,
                "lat": lat,
                "lon": lon,
                "aliases": [],
            }
        )
        if len(places) >= limit:
            break

    out_dir = corpus_dir() / "osm"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "jaipur_osm_facts.json"
    payload = {
        "city": "Jaipur",
        "source": "OpenStreetMap",
        "dataset": "openstreetmap",
        "license": "ODbL 1.0",
        "places": places,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("OSM facts wrote %d places → %s", len(places), path)
    return path
