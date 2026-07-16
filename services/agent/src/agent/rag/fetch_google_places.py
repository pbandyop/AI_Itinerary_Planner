"""Fetch Google Places for Jaipur via Places API (New) — no HTML scrape."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agent.rag.paths import corpus_dir

logger = logging.getLogger(__name__)

JAIPUR_LAT = 26.9124
JAIPUR_LON = 75.7873
SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"


def _api_key() -> str | None:
    for name in ("GOOGLE_PLACES_API_KEY", "GOOGLE_MAPS_API_KEY"):
        raw = os.getenv(name)
        if raw and raw.strip():
            return raw.strip()
    return None


def _http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
    timeout: float = 45.0,
) -> dict[str, Any]:
    data = None
    hdrs = dict(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _weekday_hours(place: dict[str, Any]) -> str:
    hours = place.get("regularOpeningHours") or place.get("currentOpeningHours") or {}
    weekday = hours.get("weekdayDescriptions") or []
    if weekday:
        return "; ".join(str(x) for x in weekday)
    periods = hours.get("periods") or []
    if periods:
        return f"{len(periods)} opening period(s) listed"
    return ""


def _place_card(place: dict[str, Any]) -> dict[str, Any] | None:
    name = str(place.get("displayName", {}).get("text") or place.get("name") or "").strip()
    # New API: name is resource name places/XXX; displayName.text is the title
    if isinstance(place.get("displayName"), dict):
        name = str(place["displayName"].get("text") or "").strip()
    if not name:
        return None
    resource = str(place.get("name") or "")  # places/ChIJ...
    place_id = resource.split("/", 1)[-1] if resource.startswith("places/") else resource
    addr = place.get("formattedAddress") or ""
    phone = place.get("nationalPhoneNumber") or place.get("internationalPhoneNumber")
    website = place.get("websiteUri")
    hours_line = _weekday_hours(place)
    types = place.get("types") or []
    rating = place.get("rating")
    maps_uri = place.get("googleMapsUri") or (
        f"https://www.google.com/maps/place/?q=place_id:{place_id}" if place_id else ""
    )
    bits = [f"{name} (Google Places)."]
    if addr:
        bits.append(f"Address: {addr}.")
    if hours_line:
        bits.append(f"Opening hours: {hours_line}.")
    if phone:
        bits.append(f"Phone: {phone}.")
    if website:
        bits.append(f"Website: {website}.")
    if rating is not None:
        bits.append(f"Rating: {rating}.")
    if types:
        bits.append("Types: " + ", ".join(str(t) for t in types[:8]) + ".")
    return {
        "city": "Jaipur",
        "title": name,
        "place_name": name,
        "source": "Google Places",
        "dataset": "google_places",
        "url": maps_uri,
        "license": "Google Places API terms (cited extract for demo)",
        "text": " ".join(bits),
        "atomic": True,
        "place_id": place_id,
    }


def _search_text(key: str, query: str) -> list[dict[str, Any]]:
    field_mask = ",".join(
        [
            "places.id",
            "places.name",
            "places.displayName",
            "places.formattedAddress",
            "places.types",
            "places.rating",
            "places.nationalPhoneNumber",
            "places.internationalPhoneNumber",
            "places.websiteUri",
            "places.googleMapsUri",
            "places.regularOpeningHours",
            "places.currentOpeningHours",
        ]
    )
    headers = {
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": field_mask,
        "Content-Type": "application/json",
        "User-Agent": "AI-Itinerary-Planner-Capstone/0.4",
    }
    body = {
        "textQuery": query,
        "locationBias": {
            "circle": {
                "center": {"latitude": JAIPUR_LAT, "longitude": JAIPUR_LON},
                "radius": 25000.0,
            }
        },
        "pageSize": 10,
        "languageCode": "en",
    }
    try:
        data = _http_json(SEARCH_URL, method="POST", headers=headers, body=body)
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")
        logger.warning("Places searchText failed (%s): %s", exc.code, err[:300])
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Places searchText failed: %s", exc)
        return []
    return list(data.get("places") or [])


def fetch_google_places(*, sleep_s: float = 0.25, max_places: int = 80) -> Path:
    out_dir = corpus_dir() / "google"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "jaipur_places.json"
    key = _api_key()
    places_out: list[dict[str, Any]] = []

    if not key:
        logger.warning(
            "No GOOGLE_PLACES_API_KEY — writing empty Google Places corpus stub"
        )
        path.write_text(
            json.dumps(
                {
                    "city": "Jaipur",
                    "source": "Google Places",
                    "dataset": "google_places",
                    "places": [],
                    "notes": "Set GOOGLE_PLACES_API_KEY to populate.",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return path

    queries = [
        "tourist attractions in Jaipur",
        "cafes in Jaipur",
        "restaurants in Jaipur",
        "temples in Jaipur",
        "forts in Jaipur",
        "museums in Jaipur",
        "Hawa Mahal Jaipur",
        "Jantar Mantar Jaipur",
        "City Palace Jaipur",
        "Amber Fort Jaipur",
        "Akshardham Temple Jaipur",
        "Birla Mandir Jaipur",
        "Patrika Gate Jaipur",
        "Nahargarh Fort Jaipur",
        "Albert Hall Museum Jaipur",
        "Amarnath Cafe Jaipur",
        "Jal Mahal Jaipur",
        "Ganesh Restaurant Jaipur",
        "Caravana Jaipur",
    ]

    seen: set[str] = set()
    for q in queries:
        rows = _search_text(key, q)
        for place in rows:
            card = _place_card(place)
            if not card:
                continue
            pid = card.get("place_id") or card["place_name"]
            if pid in seen:
                continue
            seen.add(str(pid))
            places_out.append(card)
            if len(places_out) >= max_places:
                break
        time.sleep(sleep_s)
        if len(places_out) >= max_places:
            break

    path.write_text(
        json.dumps(
            {
                "city": "Jaipur",
                "source": "Google Places",
                "dataset": "google_places",
                "api": "places.googleapis.com (New)",
                "places": places_out,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info("Google Places (New) wrote %d cards", len(places_out))
    return path
