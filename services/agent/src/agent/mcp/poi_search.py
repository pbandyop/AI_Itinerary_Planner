"""POI Search MCP — OpenStreetMap via Overpass API (India cities)."""

from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import httpx

from agent.mcp.geo import city_bbox, resolve_city
from agent.schemas.specialists import POICandidate, POISearchResult

logger = logging.getLogger(__name__)

OVERPASS_URL = os.getenv(
    "OVERPASS_API_URL", "https://overpass.kumi.systems/api/interpreter"
)
# Secondary public Overpass mirrors tried only when the primary URL fails (still live OSM).
_OVERPASS_FALLBACK_URLS = [
    u.strip()
    for u in os.getenv(
        "OVERPASS_FALLBACK_URLS",
        "https://overpass-api.de/api/interpreter,"
        "https://overpass.private.coffee/api/interpreter",
    ).split(",")
    if u.strip() and u.strip() != OVERPASS_URL
]

_SHOPPING_FILTERS = [
    'node["shop"~"mall|clothes|gift|jewelry|marketplace|department_store|fashion|souvenir"]',
    'way["shop"~"mall|clothes|gift|jewelry|marketplace|department_store|fashion|souvenir"]',
    'node["amenity"="marketplace"]',
    'way["amenity"="marketplace"]',
    # Targeted famous bazaar names (cheap) — avoids scanning all pedestrian ways.
    'node["name"~"Johari Bazaar|Bapu Bazaar|Tripolia Bazaar|Nehru Bazaar|Kishanpol Bazaar",i]',
    'way["name"~"Johari Bazaar|Bapu Bazaar|Tripolia Bazaar|Nehru Bazaar|Kishanpol Bazaar",i]',
]

# Parks & gardens share one interest (like shopping & bazaars).
_PARK_GARDEN_FILTERS = [
    # Named tourist parks/gardens first so out-limit cannot drop them for sector parks.
    'node["name"~"Ram Niwas Garden|Sisodia Rani|Sisodiya Rani|Central Park|Statue Circle|Kanak Vrindavan|Vidyadhar Garden|Vidyadhar ka Bagh|Nahargarh Biological|Jawahar Circle|Nehru Park|Smriti Van|Birla Mandir Garden",i]',
    'way["name"~"Ram Niwas Garden|Sisodia Rani|Sisodiya Rani|Central Park|Statue Circle|Kanak Vrindavan|Vidyadhar Garden|Vidyadhar ka Bagh|Nahargarh Biological|Jawahar Circle|Nehru Park|Smriti Van",i]',
    'relation["name"~"Ram Niwas Garden|Sisodia Rani|Central Park|Kanak Vrindavan|Vidyadhar|Nahargarh Biological|Jawahar Circle",i]',
    'node["leisure"~"park|garden"]',
    'way["leisure"~"park|garden"]',
    'relation["leisure"="garden"]',
    'node["leisure"="park"]["garden"="yes"]',
    'way["leisure"="park"]["garden"="yes"]',
    'node["name"~"[Bb]agh"]["leisure"~"park|garden"]',
    'way["name"~"[Bb]agh"]["leisure"~"park|garden"]',
    'node["tourism"="zoo"]',
    'way["tourism"="zoo"]',
]

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
        # Named icons first so the out-limit cannot drop them for random attractions.
        'node["name"~"Amber Fort|Amer Fort|City Palace|Hawa Mahal|Jantar Mantar|Nahargarh Fort|Jaigarh Fort|Albert Hall|Jal Mahal",i]',
        'way["name"~"Amber Fort|Amer Fort|City Palace|Hawa Mahal|Jantar Mantar|Nahargarh Fort|Jaigarh Fort|Albert Hall|Jal Mahal",i]',
        'relation["name"~"Amber Fort|Amer Fort|City Palace|Hawa Mahal|Jantar Mantar|Nahargarh Fort|Jaigarh|Albert Hall",i]',
        'node["historic"~"castle|fort|palace|monument|memorial|ruins|archaeological_site|citywalls|city_gate"]',
        'way["historic"~"castle|fort|palace|monument|memorial|ruins|archaeological_site|citywalls|city_gate"]',
        'relation["historic"~"castle|fort|palace|monument|memorial|ruins|archaeological_site"]',
        'way["building"="castle"]',
        'node["tourism"="attraction"]["historic"]',
        'way["tourism"="attraction"]["historic"]',
    ],
    "history": [
        'node["historic"]',
        'way["historic"]',
        'node["tourism"="museum"]',
        'way["tourism"="museum"]',
    ],
    "shopping": list(_SHOPPING_FILTERS),
    "market": list(_SHOPPING_FILTERS),
    "nature": [
        'node["leisure"~"park|garden"]',
        'way["leisure"~"park|garden"]',
        'node["tourism"="viewpoint"]',
    ],
    "park": list(_PARK_GARDEN_FILTERS),
    "garden": list(_PARK_GARDEN_FILTERS),
    "outdoor": [
        'node["leisure"~"park|garden|playground"]',
        'way["leisure"~"park|garden|playground"]',
        'node["tourism"="viewpoint"]',
    ],
    "museum": [
        'node["name"~"Albert Hall|Anokhi Museum|City Palace Museum|Dolls Museum|Museum of Legacies|Jawahar Kala Kendra",i]',
        'way["name"~"Albert Hall|Anokhi Museum|City Palace Museum|Dolls Museum|Museum of Legacies|Jawahar Kala Kendra",i]',
        'node["tourism"~"museum|gallery"]',
        'way["tourism"~"museum|gallery"]',
    ],
    "temple": [
        'node["name"~"Govind Dev|Govindji|Birla Mandir|Laxmi Narayan|Galta Ji|Galtaji|Digamber Jain|Akshardham|Motiwalas|Shila Devi",i]',
        'way["name"~"Govind Dev|Govindji|Birla Mandir|Laxmi Narayan|Galta Ji|Galtaji|Digamber Jain|Akshardham|Motiwalas|Shila Devi",i]',
        'node["amenity"="place_of_worship"]',
        'way["amenity"="place_of_worship"]',
    ],
    "art": [
        'node["tourism"~"gallery|artwork|museum"]',
        'way["tourism"~"gallery|museum"]',
    ],
    "architecture": [
        'node["historic"]',
        'way["historic"]',
        'node["tourism"="attraction"]',
        'way["building"~"cathedral|chapel|temple"]',
    ],
    "adventure": [
        'node["tourism"~"attraction|theme_park"]',
        'way["tourism"~"attraction|theme_park"]',
        'node["leisure"~"sports_centre|water_park"]',
    ],
    "nightlife": [
        'node["amenity"~"bar|pub|nightclub|biergarten"]',
        'way["amenity"~"bar|pub|nightclub|biergarten"]',
        'node["tourism"="hostel"]',
        'node["leisure"="dance"]',
    ],
}

DEFAULT_INTERESTS = ["culture", "heritage", "food"]

# Cap concurrent Overpass HTTP calls (public mirrors rate-limit aggressive clients).
_OVERPASS_MAX_WORKERS = max(
    1, min(3, int(os.getenv("OVERPASS_MAX_WORKERS", "3") or "3"))
)


def fetch_overpass(query: str, *, timeout: float = 50.0) -> list[dict[str, Any]]:
    headers = {
        "User-Agent": (
            "AI-Itinerary-Planner/0.2 "
            "(capstone; contact: github.com/pbandyop/AI_Itinerary_Planner)"
        ),
        "Accept": "application/json",
    }
    urls = [OVERPASS_URL, *_OVERPASS_FALLBACK_URLS]
    last_exc: Exception | None = None
    for url in urls:
        try:
            logger.info("Overpass request (%d chars) -> %s", len(query), url)
            with httpx.Client(timeout=timeout, headers=headers) as client:
                resp = client.post(url, data={"data": query})
                resp.raise_for_status()
                payload = resp.json()
            elements = payload.get("elements") or []
            logger.info("Overpass returned %d elements from %s", len(elements), url)
            return elements
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning("Overpass endpoint failed (%s): %s", url, exc)
    assert last_exc is not None
    raise last_exc


_NOMINATIM_URL = os.getenv(
    "NOMINATIM_API_URL", "https://nominatim.openstreetmap.org/search"
)

# Live Nominatim amenity queries when Overpass is empty/flaky for an interest.
_NOMINATIM_QUERIES: dict[str, list[str]] = {
    "food": ["restaurant", "cafe", "fast_food"],
    "temple": ["place_of_worship"],
    "museum": ["museum"],
    "shopping": ["marketplace", "clothes"],
    "market": ["marketplace"],
    "park": ["park", "garden"],
    "garden": ["garden", "park"],
    "heritage": ["fort", "palace", "historic"],
    "nightlife": ["bar", "pub"],
}

# Famous Jaipur parks — Nominatim name search when generic "park in Jaipur" is junk-heavy.
_JAIPUR_PARK_NAME_QUERIES = (
    "Ram Niwas Garden Jaipur",
    "Sisodia Rani Garden Jaipur",
    "Central Park Jaipur",
    "Statue Circle Jaipur",
    "Kanak Vrindavan Jaipur",
    "Vidyadhar Garden Jaipur",
    "Jawahar Circle Garden Jaipur",
    "Nahargarh Biological Park Jaipur",
    "Nehru Park Jaipur",
)

# Famous Jaipur heritage — pin must-sees when Overpass is thin/flaky.
_JAIPUR_HERITAGE_NAME_QUERIES = (
    "Hawa Mahal Jaipur",
    "City Palace Jaipur",
    "Jantar Mantar Jaipur",
    "Amber Fort Jaipur",
    "Amer Fort Jaipur",
    "Nahargarh Fort Jaipur",
    "Jaigarh Fort Jaipur",
    "Jal Mahal Jaipur",
    "Albert Hall Museum Jaipur",
)


def nominatim_category_search(
    *,
    city: str,
    interest: str,
    limit: int = 20,
    timeout: float = 20.0,
) -> list[POICandidate]:
    """Live OpenStreetMap Nominatim search (secondary live source for edits)."""
    info = resolve_city(city)
    if info is None:
        return []
    amenity_keys = _NOMINATIM_QUERIES.get(interest.lower().strip()) or []
    if not amenity_keys:
        amenity_keys = [interest.lower().strip()]
    headers = {
        "User-Agent": (
            "AI-Itinerary-Planner/0.2 "
            "(capstone; contact: github.com/pbandyop/AI_Itinerary_Planner)"
        ),
        "Accept": "application/json",
    }
    pois: list[POICandidate] = []
    seen: set[str] = set()

    def _append_row(row: dict[str, Any], *, category: str, interest_key: str) -> None:
        nonlocal pois
        if len(pois) >= limit:
            return
        name = normalize_poi_name(
            str(row.get("name") or row.get("display_name") or "").split(",")[0].strip()
        )
        if not name:
            return
        try:
            lat = float(row["lat"])
            lon = float(row["lon"])
        except (KeyError, TypeError, ValueError):
            return
        center = (info.lat, info.lon)
        if abs(lat - center[0]) > 0.45 or abs(lon - center[1]) > 0.45:
            return
        osm_type = str(row.get("osm_type") or "").lower()
        if osm_type == "node":
            ot = "node"
        elif osm_type == "way":
            ot = "way"
        elif osm_type == "relation":
            ot = "relation"
        else:
            return
        try:
            osm_id = int(row.get("osm_id"))
        except (TypeError, ValueError):
            return
        ref = f"{ot}/{osm_id}"
        if ref in seen:
            return
        seen.add(ref)
        score = 7.0
        if _looks_like_food_name(name):
            category = "food"
        if category == "heritage" and _MUST_SEE_NAME_RE.search(name):
            score = 18.0
        elif category == "garden" and _MUST_SEE_NAME_RE.search(name):
            score = 18.0
        if _is_low_signal_poi(name, {"source": "nominatim"}, category):
            return
        pois.append(
            POICandidate(
                name=name,
                osm_type=ot,  # type: ignore[arg-type]
                osm_id=osm_id,
                lat=lat,
                lon=lon,
                category=category,
                tags={"source": "nominatim", "amenity": interest_key},
                rank_score=score,
                matched_interests=[interest.lower()],
            )
        )

    with httpx.Client(timeout=timeout, headers=headers) as client:
        # For parks: query famous names first so tourist gardens beat sector parks.
        if interest.lower().strip() in {"park", "garden"} and info.name.lower() == "jaipur":
            for q in _JAIPUR_PARK_NAME_QUERIES:
                if len(pois) >= limit:
                    break
                params = {
                    "q": q,
                    "format": "jsonv2",
                    "addressdetails": 0,
                    "limit": 3,
                    "countrycodes": "in",
                }
                try:
                    logger.info("Nominatim park name q=%s", q)
                    resp = client.get(_NOMINATIM_URL, params=params)
                    resp.raise_for_status()
                    rows = resp.json()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Nominatim park name failed q=%s: %s", q, exc)
                    continue
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if isinstance(row, dict):
                        _append_row(row, category="garden", interest_key="park")

        # For heritage: pin Jaipur must-sees by name.
        if interest.lower().strip() in {
            "heritage",
            "culture",
            "history",
        } and info.name.lower() == "jaipur":
            for q in _JAIPUR_HERITAGE_NAME_QUERIES:
                if len(pois) >= limit:
                    break
                params = {
                    "q": q,
                    "format": "jsonv2",
                    "addressdetails": 0,
                    "limit": 2,
                    "countrycodes": "in",
                }
                try:
                    logger.info("Nominatim heritage name q=%s", q)
                    resp = client.get(_NOMINATIM_URL, params=params)
                    resp.raise_for_status()
                    rows = resp.json()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Nominatim heritage name failed q=%s: %s", q, exc)
                    continue
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if isinstance(row, dict):
                        _append_row(row, category="heritage", interest_key="heritage")

        for amenity in amenity_keys:
            if len(pois) >= limit:
                break
            params = {
                "q": f"{amenity} in {info.name}, India",
                "format": "jsonv2",
                "addressdetails": 0,
                "limit": min(15, limit),
                "countrycodes": "in",
            }
            try:
                logger.info("Nominatim q=%s", params["q"])
                resp = client.get(_NOMINATIM_URL, params=params)
                resp.raise_for_status()
                rows = resp.json()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Nominatim failed amenity=%s: %s", amenity, exc)
                continue
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                category = {
                    "restaurant": "food",
                    "cafe": "food",
                    "fast_food": "food",
                    "place_of_worship": "temple",
                    "museum": "museum",
                    "marketplace": "market",
                    "clothes": "market",
                    "park": "park",
                    "garden": "garden",
                    "fort": "heritage",
                    "palace": "heritage",
                    "historic": "heritage",
                    "attraction": "attraction",
                    "bar": "nightlife",
                    "pub": "nightlife",
                }.get(amenity, interest.lower())
                if interest.lower() == "heritage" and category == "attraction":
                    category = "heritage"
                _append_row(row, category=category, interest_key=amenity)
    return pois


_FOOD_NAME_RE = re.compile(
    r"\b("
    r"ice\s*cream|gelato|parlou?r|cafe|café|coffee|restaurant|dhaba|"
    r"misthan|bhandar|bakery|sweet(?:s)?|pizza|burger|kitchen|bistro|"
    r"eatery|canteen|tiffin|snack|juice|lassi|chai"
    r")\b",
    re.I,
)

_TICKET_STUB_RE = re.compile(
    r"\b(tickets?|ticket\s+counter|booking\s+office|entry\s+gate)\b",
    re.I,
)


def _looks_like_food_name(name: str) -> bool:
    return bool(_FOOD_NAME_RE.search(name or ""))


def normalize_poi_name(name: str) -> str:
    """Strip navigation/ticket stubs: 'to Jaigarh Fort' → 'Jaigarh Fort'."""
    n = (name or "").strip()
    if not n:
        return n
    n = re.sub(
        r"^(?:to|towards|toward|near|at|via|for)\s+",
        "",
        n,
        flags=re.I,
    )
    n = re.sub(
        r"\s+(?:tickets?|ticket\s+counter|entry\s+tickets?|booking(?:\s+office)?)\s*$",
        "",
        n,
        flags=re.I,
    )
    n = re.sub(r"\s{2,}", " ", n).strip(" -–—,.")
    return n


def _category_from_tags(tags: dict[str, str]) -> str:
    amenity = tags.get("amenity", "")
    tourism = tags.get("tourism", "")
    historic = tags.get("historic")
    leisure = tags.get("leisure", "")
    shop = tags.get("shop")
    name = (tags.get("name:en") or tags.get("name") or "").lower()
    # Food amenities + food-like names first (e.g. "Jal Mahal Ice Cream Parlour"
    # must not become heritage via the Jal Mahal must-see match).
    if (
        amenity
        in {
            "restaurant",
            "cafe",
            "fast_food",
            "food_court",
            "ice_cream",
        }
        or shop in {"bakery", "pastry", "confectionery", "ice_cream"}
        or _looks_like_food_name(name)
    ):
        return "food"
    if amenity in {"bar", "pub", "nightclub", "biergarten"} or leisure == "dance":
        return "nightlife"
    if amenity == "place_of_worship":
        return "temple"
    if tourism in {"museum", "gallery"}:
        return "museum"
    if tourism == "viewpoint":
        return "viewpoint"
    if tourism == "zoo":
        return "park"
    if leisure == "garden":
        return "garden"
    if leisure in {"park", "playground"}:
        if "garden" in name or "bagh" in name:
            return "garden"
        return "park"
    if leisure and ("garden" in name or "bagh" in name):
        return "garden"
    # Famous bazaars are always market — never heritage via must-see circular match.
    if _MUST_SEE_MARKET_RE.search(name) or (
        re.search(r"\b(bazaar|bazar|market|mela|haat|chaupar)\b", name, re.I)
        and not re.search(r"\b(fort|palace|mahal|qila|garh|museum|mandir|temple)\b", name, re.I)
    ):
        return "market"
    # Heritage must-sees / forts before generic shop tags.
    if not _looks_like_food_name(name) and (
        _MUST_SEE_HERITAGE_RE.search(name)
        or re.search(
            r"\b(fort|palace|mahal|qila|garh)\b", name, re.I
        )
    ):
        if re.search(r"\b(museum|gallery)\b", name, re.I) or tourism in {
            "museum",
            "gallery",
        }:
            return "museum"
        if re.search(r"\b(mandir|temple|masjid|mosque)\b", name, re.I):
            return "temple"
        if re.search(
            r"\b(park|garden|bagh|biological)\b", name, re.I
        ) and not re.search(r"\b(fort|palace|mahal)\b", name, re.I):
            return "garden"
        if _looks_like_food_name(name):
            return "food"
        return "heritage"
    if (
        historic
        in {
            "castle",
            "fort",
            "palace",
            "monument",
            "memorial",
            "ruins",
            "archaeological_site",
            "citywalls",
            "city_gate",
            "tower",
            "manor",
        }
        or tourism in {"castle", "monument"}
    ):
        if not _is_low_signal_heritage_name(name):
            return "heritage"
    if shop or "bazaar" in name or "market" in name:
        # Never classify a named fort/palace as a market.
        if _looks_like_heritage_name(name) and not (
            "bazaar" in name or "market" in name or "bazar" in name
        ):
            return "heritage"
        return "market"
    # Never treat civic/commercial amenities as heritage even if OSM marks historic.
    if amenity in {
        "bank",
        "atm",
        "bureau_de_change",
        "post_office",
        "police",
        "hospital",
        "clinic",
        "school",
        "college",
        "university",
        "fuel",
        "parking",
        "toilets",
        "embassy",
        "townhall",
        "courthouse",
    }:
        return "other"
    # Tourist-grade historic only — not every historic=yes building.
    historic_ok = historic in {
        "castle",
        "fort",
        "palace",
        "monument",
        "memorial",
        "ruins",
        "archaeological_site",
        "citywalls",
        "city_gate",
        "tower",
        "manor",
        "yes",  # kept only if name also looks tourist (checked below)
    }
    if tourism in {"castle", "monument"}:
        return "heritage"
    if historic and historic_ok:
        if historic == "yes" and not _looks_like_heritage_name(name):
            return "other"
        if _is_low_signal_heritage_name(name):
            return "other"
        return "heritage"
    if tourism == "attraction" and _looks_like_heritage_name(name):
        return "heritage"
    if tourism:
        return "attraction"
    return "other"


# Neighborhood / sports / campus noise — not tourist parks & gardens.
_LOW_SIGNAL_PARK_RE = re.compile(
    r"\b("
    r"cricket|football|soccer|playground|apartment|apartments|society|colony|"
    r"housing|sector[-\s]?\d|nagar,\s*sector|block\s*[a-z0-9]|plot\s*no|"
    r"enclave|residency|township|college|school|university|campus|institute|"
    r"hospital|hostel|housing\s+board|bike\s*park|bmx|skate\s*park|"
    r"chitrakoot"
    r")\b|^ground$",
    re.I,
)
_TOURIST_PARK_RE = re.compile(
    r"\b("
    r"central\s+park|jawahar(?:\s+circle)?|ram\s*niwas|sisod(?:ia|iya)\s*rani|"
    r"vidyadhar|biological|zoological|\bzoo\b|rose\s+garden|statue\s+circle|"
    r"kanak(?:\s*vrindavan)?|smriti(?:\s*van)?|peace\s+park|nehru(?:\s+park)?|"
    r"gulab\s*bagh|company\s*bagh|\bbagh\b"
    r")\b",
    re.I,
)
_WEDDING_GARDEN_RE = re.compile(
    r"\b(marriage|wedding|farmhouse|banquet|lawn|party\s+plot)\b",
    re.I,
)

_LOW_SIGNAL_HERITAGE_RE = re.compile(
    r"\b("
    r"state\s+bank|bank\s+of|sbi\b|hdfc|icici|axis\s+bank|atm|"
    r"post\s+office|police|hospital|clinic|school|college|university|"
    r"petrol|fuel|parking|toilet|embassy|court|office|warehouse|"
    r"godown|factory|workshop|showroom|branch"
    r")\b",
    re.I,
)

_HERITAGE_NAME_RE = re.compile(
    r"\b("
    r"fort|palace|mahal|haveli|mandir|temple|jantar|observatory|"
    r"gate|pol\b|chabutra|cenotaph|tomb|mosque|masjid|museum|"
    r"qila|garh|bagh|stepwell|baori|baoli"
    r")\b",
    re.I,
)

_LOW_SIGNAL_FOOD_RE = re.compile(
    r"\b("
    r"canteen|mess\b|tiffin|dhaba\s*no|hotel\s*and\s*restaurant|"
    r"cafe\s*coffee\s*day|\bccd\b|coffey\s*day|coffee\s*day|"
    r"domino|mcdonald|kfc\b|subway|"
    r"pizza\s*hut|starbucks|burger\s*king|haldiram'?s?\s*express|"
    r"cheap\s+food|food\s+places?|street\s+food\s+stall|"
    r"food\s+court|unknown\s+restaurant|unnamed\s+restaurant"
    r")\b",
    re.I,
)
_LOW_SIGNAL_MARKET_RE = re.compile(
    r"\b("
    r"jewels?\b|jewelers?|jewellers?|showroom|emporium\s*pvt|"
    r"private\s+limited|\bpvt\b|\bltd\b|wholesale|godown|"
    r"mobile\s+shop|electronics|repair"
    r")\b",
    re.I,
)
_LOW_SIGNAL_TEMPLE_RE = re.compile(
    r"\b("
    r"hospital|clinic|school|college|university|campus|hostel|"
    r"police|office|factory|unknown|unnamed"
    r")\b",
    re.I,
)
_LOW_SIGNAL_MUSEUM_RE = re.compile(
    r"\b("
    r"hospital|school|college|university|private|home\s+museum|"
    r"unknown|unnamed"
    r")\b",
    re.I,
)

_MUST_SEE_HERITAGE_RE = re.compile(
    r"\b("
    r"hawa\s*mahal|city\s*palace|(?:amber|amer)\s*(?:fort|palace)|"
    r"jantar\s*mantar|nahargarh(?:\s+fort)?|jaigarh|jal\s*mahal|albert\s*hall|"
    r"govind\s*dev|govindji|birla\s*mandir|laxmi\s*narayan|"
    r"galta\s*ji|galtaji|digamber\s*jain|shila\s*devi|"
    r"anokhi(?:\s+museum)?|jawahar\s*kala|dolls?\s*museum|museum\s+of\s+legacies|"
    r"sisodia|vidyadhar|ram\s*niwas|kanak\s*vrindavan|"
    r"central\s+park|statue\s+circle|nahargarh\s+biological|"
    r"laxmi\s*misthan|rawat\s*misthan|handi(?:\s+restaurant)?|"
    r"niros?\b|chokhi\s*dhani|tapri\s+central"
    r")\b",
    re.I,
)

# Tourist bazaars only — never treat these as heritage.
_MUST_SEE_MARKET_RE = re.compile(
    r"\b("
    r"johari(?:\s*bazaar|\s*bazar)?|bapu\s*bazaar|bapu\s*bazar|"
    r"tripolia(?:\s*bazaar|\s*bazar)?|nehru\s*bazaar|nehru\s*bazar|"
    r"kishanpol(?:\s*bazaar|\s*bazar)?|"
    r"badi\s*chaupar|chhoti\s*chaupar|choti\s*chaupar"
    r")\b",
    re.I,
)

# Combined for ranking / must-see boosts (heritage + markets + food icons).
_MUST_SEE_NAME_RE = re.compile(
    r"\b("
    r"hawa\s*mahal|city\s*palace|(?:amber|amer)\s*(?:fort|palace)|"
    r"jantar\s*mantar|nahargarh(?:\s+fort)?|jaigarh|jal\s*mahal|albert\s*hall|"
    r"govind\s*dev|govindji|birla\s*mandir|laxmi\s*narayan|"
    r"galta\s*ji|galtaji|digamber\s*jain|shila\s*devi|"
    r"anokhi(?:\s+museum)?|jawahar\s*kala|dolls?\s*museum|museum\s+of\s+legacies|"
    r"johari|bapu\s*bazaar|tripolia|nehru\s*bazaar|kishanpol|"
    r"sisodia|vidyadhar|ram\s*niwas|kanak\s*vrindavan|"
    r"central\s+park|statue\s+circle|nahargarh\s+biological|"
    r"laxmi\s*misthan|rawat\s*misthan|handi(?:\s+restaurant)?|"
    r"niros?\b|chokhi\s*dhani|tapri\s+central"
    r")\b",
    re.I,
)

_FAMOUS_BAZAAR_RE = _MUST_SEE_MARKET_RE

_JUNK_MARKET_RE = re.compile(
    r"\b("
    r"big\s*bazaar|world\s*trade\s*park|reliance|dmart|d-?mart|"
    r"colony\s+market|sector\s+[-\w]*\s*market|local\s+market|"
    r"gaurav\s+tower|palika\s+bazaar|sun\s*n\s*moon|sun\s+and\s+moon|"
    r"jayanti\s+market|shopping\s+complex|trade\s+park|"
    r"sindhi\s+colony|apartment\s+market"
    r")\b",
    re.I,
)

# Famous landmarks that are not in Jaipur (or wrong for this planner scope).
_WRONG_CITY_POI_RE = re.compile(
    r"\b("
    r"india\s*gate|gateway\s+of\s+india|red\s+fort|qutub\s*minar|qutb\s*minar|"
    r"taj\s*mahal|lotus\s*temple|charminar|howrah\s+bridge|"
    r"victoria\s+memorial|gateway\s+of\s+india"
    r")\b",
    re.I,
)

_TRANSIT_JUNK_RE = re.compile(
    r"\b("
    r"bus\s*stop|bus\s*stand|bus\s*station|bus\s*depot|"
    r"railway\s*station|train\s*station|metro\s*station|"
    r"auto\s*stand|taxi\s*stand|cab\s*stand|parking\s*(?:lot|area)?|"
    r"petrol\s*pump|fuel\s*station"
    r")\b",
    re.I,
)

_TOURIST_MARKET_RE = re.compile(
    r"\b(bazaar|bazar|market|mela|haat|chaupar|bapu|johari|tripolia|nehru)\b",
    re.I,
)
_TOURIST_FOOD_RE = re.compile(
    r"\b(restaurant|cafe|misthan|bhojanalaya|rasoi|kitchen|dhaba)\b",
    re.I,
)


def _looks_like_heritage_name(name: str) -> bool:
    """True for fort/palace-style names — never bazaars/markets."""
    n = name or ""
    if _MUST_SEE_MARKET_RE.search(n) or re.search(
        r"\b(bazaar|bazar|market|mall|emporium)\b", n, re.I
    ):
        return False
    return bool(_HERITAGE_NAME_RE.search(n) or _MUST_SEE_HERITAGE_RE.search(n))


def _is_low_signal_heritage_name(name: str) -> bool:
    return bool(_LOW_SIGNAL_HERITAGE_RE.search(name or ""))


def _is_low_signal_park(name: str, tags: dict[str, str], category: str) -> bool:
    """Keep tourist-grade parks/gardens; drop numbered/neighborhood/wedding noise."""
    if category not in {"park", "garden"}:
        return False
    n = name or ""
    stripped = n.strip()
    # "park 4", "Park-2", "Park #1" — never tourist attractions.
    if re.search(r"\bpark\s*[-#]?\s*\d+\b", stripped, re.I):
        return True
    if re.fullmatch(r"park\s*[-#]?\s*\d+", stripped, re.I):
        return True
    if _WEDDING_GARDEN_RE.search(n):
        return True
    if _TOURIST_PARK_RE.search(n) or _MUST_SEE_NAME_RE.search(n):
        return False
    if tags.get("wikidata") or tags.get("wikipedia"):
        # Still drop obvious campus/sports parks even with wiki links.
        if _LOW_SIGNAL_PARK_RE.search(n):
            return True
        return False
    if _LOW_SIGNAL_PARK_RE.search(n):
        return True
    # Bare / tiny names ("Ground") without tourist cues.
    if len(stripped) < 5 or stripped.lower() in {"ground", "park", "the park"}:
        return True
    if re.search(r"sector|nagar|colony|apartment|college|school", stripped, re.I):
        return True
    # Default: neighborhood parks (e.g. Deer Park) are not itinerary-worthy.
    return True


_GENERIC_PLACE_NAME_RE = re.compile(
    r"^("
    r"restaurant|cafe|café|coffee|bar|pub|hotel|motel|hostel|"
    r"fort|palace|museum|gallery|temple|mandir|park|garden|zoo|"
    r"market|marketplace|shop|store|mall|attraction|viewpoint|"
    r"monument|memorial|ruins|gate|building|place|unnamed|unknown|"
    r"food|eatery|dhaba|bakery|"
    r"cheap\s+food\s+places?|food\s+places?"
    r")s?$",
    re.I,
)


def _is_generic_place_name(name: str) -> bool:
    """True for OSM stubs like 'Restaurant', 'Fort', 'Park' with no real title."""
    stripped = (name or "").strip()
    if not stripped:
        return True
    if len(stripped) < 3:
        return True
    if _GENERIC_PLACE_NAME_RE.match(stripped):
        return True
    # All-caps type labels: RESTAURANT, FORT, CAFE
    if stripped.isupper() and len(stripped.split()) <= 2 and _GENERIC_PLACE_NAME_RE.match(
        stripped.lower()
    ):
        return True
    return False


def _is_low_signal_poi(name: str, tags: dict[str, str], category: str) -> bool:
    cat = (category or "").lower()
    n = name or ""
    if _is_generic_place_name(n):
        return True
    # Bare ticket counters / "Hawa Mahal tickets" stubs without a real place title.
    if _TICKET_STUB_RE.fullmatch((n or "").strip()) or re.fullmatch(
        r"tickets?", (n or "").strip(), re.I
    ):
        return True
    if _WRONG_CITY_POI_RE.search(n):
        return True
    if _TRANSIT_JUNK_RE.search(n):
        return True
    if _is_low_signal_park(n, tags, cat):
        return True
    # Heritage must not be ice-cream shops / cafés that merely share a landmark name.
    if cat in {"heritage", "museum", "temple", "attraction"} and _looks_like_food_name(n):
        return True
    # Bazaars mis-tagged as heritage: keep famous ones (reclassified later);
    # drop junk "X Market" stubs from the heritage pool.
    if cat in {"heritage", "attraction"} and re.search(
        r"\b(bazaar|bazar|market)\b", n, re.I
    ):
        if _FAMOUS_BAZAAR_RE.search(n):
            return False
        return True
    if cat == "heritage" and (
        _is_low_signal_heritage_name(n)
        or (
            not _looks_like_heritage_name(n)
            and not (tags.get("wikidata") or tags.get("wikipedia"))
            and (tags.get("historic") or "") == "yes"
        )
    ):
        return True
    if cat == "temple":
        if _LOW_SIGNAL_TEMPLE_RE.search(n):
            return True
        if len(n.strip()) < 4:
            return True
    if cat == "museum":
        if _LOW_SIGNAL_MUSEUM_RE.search(n):
            return True
        if len(n.strip()) < 4:
            return True
    # Junk food stubs (any category — OSM often mis-tags cafés).
    if _LOW_SIGNAL_FOOD_RE.search(n):
        return True
    if cat in {"food", "cafe", "restaurant"} and len(n.strip()) < 4:
        return True
    if cat in {"market", "shopping"}:
        # Prefer famous tourist bazaars; drop malls / colony markets / chains.
        if _MUST_SEE_HERITAGE_RE.search(n) and re.search(
            r"\b(fort|palace|mahal|qila|garh|museum)\b", n, re.I
        ):
            return True  # drop — wrong category; heritage copy should win
        if _JUNK_MARKET_RE.search(n):
            return True
        if _FAMOUS_BAZAAR_RE.search(n):
            return False
        if _LOW_SIGNAL_MARKET_RE.search(n):
            return True
        # Keep wiki-backed marketplace/mall; drop generic shops & colony markets.
        if tags.get("wikidata") or tags.get("wikipedia"):
            if re.search(r"\b(bazaar|bazar|market|mall|marketplace)\b", n, re.I):
                return False
        if tags.get("shop") in {"mall", "marketplace", "department_store"}:
            # Malls are not Jaipur tourist bazaars for culture+shopping mixes.
            return True
        # Default: no famous-bazaar cue → drop.
        return True
    return False


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


def _coalesce_overpass_interest_keys(interests: list[str]) -> list[str]:
    """One query per unique filter set (e.g. shopping/market, park/garden)."""
    seen: set[tuple[str, ...]] = set()
    keys: list[str] = []
    for raw in interests:
        key = raw.lower().strip()
        if not key:
            continue
        filters = INTEREST_FILTERS.get(key)
        sig: tuple[str, ...] = (
            tuple(filters) if filters is not None else (f"__fallback__:{key}",)
        )
        if sig in seen:
            continue
        seen.add(sig)
        keys.append(key)
    return keys


def _fetch_overpass_for_interests(
    interests: list[str],
    *,
    bbox: tuple[float, float, float, float],
    per_limit: int,
    timeout: float = 25.0,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch Overpass in parallel (coalesced interests). Returns (elements, notes)."""
    keys = _coalesce_overpass_interest_keys(interests) or list(DEFAULT_INTERESTS)
    notes: list[str] = []
    seen_ids: set[str] = set()
    elements: list[dict[str, Any]] = []

    def _one(interest: str) -> tuple[str, list[dict[str, Any]] | None, str | None]:
        query = build_overpass_query([interest], bbox=bbox, limit=per_limit)
        try:
            return interest, fetch_overpass(query, timeout=timeout), None
        except Exception as exc:  # noqa: BLE001
            return interest, None, f"{exc.__class__.__name__}: {exc}"

    workers = min(_OVERPASS_MAX_WORKERS, max(1, len(keys)))
    logger.info(
        "Overpass parallel interests=%s workers=%d",
        keys,
        workers,
    )
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, key) for key in keys]
        for fut in as_completed(futures):
            interest, chunk, err = fut.result()
            if err is not None or chunk is None:
                notes.append(
                    f"Overpass partial failure for interest={interest!r} ({err})."
                )
                logger.warning(
                    "Overpass failed for interest=%s: %s", interest, err
                )
                continue
            for el in chunk:
                osm_type = el.get("type")
                osm_id = el.get("id")
                if osm_type and osm_id is not None:
                    key = f"{osm_type}/{osm_id}"
                    if key in seen_ids:
                        continue
                    seen_ids.add(key)
                elements.append(el)
    return elements, notes


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
        score += 2.5
    if _MUST_SEE_NAME_RE.search(name):
        score += 25.0
    interest_set = {i.lower() for i in interests}
    if category in interest_set:
        score += 6.0
    # Culture categories get a standing boost whenever requested alongside soft leisure.
    if category in {"heritage", "temple", "museum"} and (
        {"heritage", "temple", "museum", "culture", "history"} & interest_set
    ):
        score += 4.0
    if "food" in interest_set and category == "food":
        score += 3.0
    if {"culture", "heritage", "history", "architecture"} & interest_set and category in {
        "heritage",
        "museum",
        "temple",
    }:
        score += 5.0
    if {"temple"} & interest_set and category == "temple":
        score += 5.0
    if {"museum"} & interest_set and category == "museum":
        score += 5.0
    if {"park", "nature", "outdoor"} & interest_set and category in {
        "park",
        "garden",
        "viewpoint",
    }:
        score += 2.5
    if category == "garden" and {"park", "garden", "nature", "outdoor"} & interest_set:
        score += 2.0
    if {"shopping", "market"} & interest_set and category in {"shopping", "market"}:
        score += 2.5
    if {"nightlife"} & interest_set and category == "nightlife":
        score += 3.0
    if {"adventure"} & interest_set and category in {
        "adventure",
        "attraction",
        "viewpoint",
    }:
        score += 3.0
    if name.isascii():
        score += 0.3
    return score


def _boost_by_stated_interests(
    pois: list[POICandidate], interests: list[str]
) -> list[POICandidate]:
    """Re-rank so places matching user interests stay at the top."""
    interest_set = {i.lower() for i in interests if i}
    if not interest_set or not pois:
        return pois
    related = {
        "history": {"heritage", "museum", "temple"},
        "heritage": {"heritage", "museum", "temple"},
        "culture": {"heritage", "museum", "temple", "art"},
        "food": {"food", "market"},
        "shopping": {"shopping", "market"},
        "market": {"market", "shopping"},
        "nightlife": {"nightlife", "food"},
        "adventure": {"adventure", "attraction", "viewpoint"},
        "nature": {"park", "garden", "viewpoint", "nature"},
        "park": {"park", "garden"},
        "garden": {"garden", "park"},
        "outdoor": {"park", "garden", "viewpoint"},
        "temple": {"temple"},
        "museum": {"museum"},
    }
    preferred: set[str] = set()
    for i in interest_set:
        preferred.add(i)
        preferred |= related.get(i, set())

    for p in pois:
        cat = (p.category or "other").lower()
        score = float(p.rank_score or 0)
        if cat in interest_set:
            score += 8.0
        elif cat in preferred:
            score += 4.0
        # Soft: attractions only help adventure/culture, not heritage quotas.
        if cat == "attraction" and "heritage" in interest_set:
            score -= 2.0
        p.rank_score = score
    return sorted(pois, key=lambda x: (-(x.rank_score or 0), x.name))


def _balance_by_interests(
    pois: list[POICandidate],
    interests: list[str],
    limit: int,
) -> list[POICandidate]:
    """Keep a fair live-MCP quota per stated interest before applying limit."""
    if limit <= 0 or not pois:
        return []
    if not interests or len(pois) <= limit:
        return pois[:limit]

    from agent.preferences import (
        CULTURE_TIER_INTERESTS,
        SOFT_TIER_INTERESTS,
        categories_for_interest,
        culture_soft_mix_active,
        normalize_interest,
        order_interests_by_priority,
    )

    keys = order_interests_by_priority(
        list(dict.fromkeys(normalize_interest(i) or i for i in interests if i.strip()))
    )
    buckets: dict[str, list[POICandidate]] = {k: [] for k in keys}
    other: list[POICandidate] = []
    claimed: set[str] = set()

    for p in pois:
        ref = f"{p.osm_type}/{p.osm_id}"
        if ref in claimed:
            continue
        cat = (p.category or "").lower()
        matched_key: str | None = None
        for key in keys:
            if cat in categories_for_interest(key):
                matched_key = key
                break
        claimed.add(ref)
        if matched_key is not None:
            buckets[matched_key].append(p)
        else:
            other.append(p)

    out: list[POICandidate] = []
    used: set[str] = set()
    mixed = culture_soft_mix_active(keys)

    def _take(key: str) -> bool:
        bucket = buckets.get(key) or []
        while bucket:
            p = bucket.pop(0)
            ref = f"{p.osm_type}/{p.osm_id}"
            if ref in used:
                continue
            out.append(p)
            used.add(ref)
            return True
        return False

    # Guarantee ≥1 per stated interest before weighted fill (stops food starving park).
    for key in keys:
        if len(out) >= limit:
            break
        _take(key)

    soft_rr = 0
    while len(out) < limit:
        took = False
        if mixed:
            culture_keys = [k for k in keys if k in CULTURE_TIER_INTERESTS]
            soft_keys = [k for k in keys if k in SOFT_TIER_INTERESTS]
            other_keys = [
                k
                for k in keys
                if k not in CULTURE_TIER_INTERESTS and k not in SOFT_TIER_INTERESTS
            ]
            for _ in range(2):
                if len(out) >= limit:
                    break
                for key in culture_keys:
                    if _take(key):
                        took = True
                        break
            if len(out) < limit and soft_keys:
                for i in range(len(soft_keys)):
                    key = soft_keys[(soft_rr + i) % len(soft_keys)]
                    if _take(key):
                        soft_rr = (soft_rr + i + 1) % len(soft_keys)
                        took = True
                        break
            if len(out) < limit:
                for key in other_keys:
                    if _take(key):
                        took = True
                        break
        else:
            for key in keys:
                if len(out) >= limit:
                    break
                if _take(key):
                    took = True
                    break
        if not took:
            break

    for p in other:
        if len(out) >= limit:
            break
        ref = f"{p.osm_type}/{p.osm_id}"
        if ref in used:
            continue
        out.append(p)
        used.add(ref)
    return out


def _apply_audience_bias(
    pois: list[POICandidate],
    constraints: list[str],
) -> list[POICandidate]:
    """Boost / demote / filter POIs based on traveler-profile constraints."""
    from agent.preferences import PROFILE_PRESETS, constraint_mentions

    profile_key = None
    for key in (
        "kid_friendly",
        "senior_friendly",
        "couple_friendly",
        "friends_friendly",
        "solo",
    ):
        if constraint_mentions(constraints, key):
            profile_key = key
            break
    if not profile_key:
        return pois

    preset = PROFILE_PRESETS.get(profile_key) or {}
    boost = set(preset.get("boost_categories") or set())
    avoid = set(preset.get("avoid_categories") or set())

    kept: list[POICandidate] = []
    for p in pois:
        cat = (p.category or "other").lower()
        if cat in avoid:
            continue
        score = float(p.rank_score or 0)
        if cat in boost:
            score += 2.5
        if profile_key == "kid_friendly" and cat in {"park", "museum"}:
            score += 1.5
        if profile_key == "senior_friendly" and cat in {"temple", "museum", "heritage"}:
            score += 1.5
        if profile_key == "couple_friendly" and cat in {"viewpoint", "heritage", "food"}:
            score += 1.0
        if profile_key == "friends_friendly" and cat in {"nightlife", "food", "market"}:
            score += 1.5
        p.rank_score = score
        kept.append(p)
    kept.sort(key=lambda x: (-(x.rank_score or 0), x.name))
    return kept


def _parse_elements(
    elements: list[dict[str, Any]],
    interests: list[str],
) -> list[POICandidate]:
    seen: set[str] = set()
    pois: list[POICandidate] = []
    for el in elements:
        tags = el.get("tags") or {}
        raw_name = tags.get("name:en") or tags.get("name")
        if not raw_name:
            continue
        name = normalize_poi_name(str(raw_name))
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
        if _is_low_signal_poi(name, tags, category):
            continue
        score = _rank_score(tags, interests, category)
        if score <= 0:
            continue
        matched = [
            i
            for i in interests
            if i.lower() == category
            or (
                i.lower() == "culture"
                and category in {"heritage", "museum", "temple", "art"}
            )
            or (i.lower() == "heritage" and category in {"heritage", "museum"})
            or (i.lower() == "food" and category == "food")
            or (i.lower() == "temple" and category == "temple")
            or (i.lower() == "museum" and category == "museum")
            or (
                i.lower() in {"park", "garden", "outdoor", "nature"}
                and category in {"park", "garden", "viewpoint"}
            )
            or (
                i.lower() in {"shopping", "market"}
                and category in {"shopping", "market"}
            )
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
                        "leisure",
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


def poi_search(
    *,
    city: str = "Jaipur",
    interests: list[str] | None = None,
    constraints: list[str] | None = None,
    limit: int = 40,
    use_overpass: bool = True,
) -> POISearchResult:
    """MCP: search Indian-city POIs from live OpenStreetMap Overpass only.

    Capstone policy: no local seed / offline POI fallback. If Overpass fails or
    returns nothing, report ``missing_data`` rather than inventing or seeding stops.
    """
    info = resolve_city(city)
    if info is None:
        return POISearchResult(
            city=city,
            query_interests=interests or [],
            query_constraints=[],
            pois=[],
            missing_data=True,
            notes=(
                f"City {city!r} is not in the India catalog "
                "(data/india_cities.json). Cannot search outside India."
            ),
        )

    canonical = info.name
    # Prefer empty interests over catalog defaults during planning — callers must
    # pass stated interests. Keep DEFAULT_INTERESTS only for standalone MCP demos.
    interests = [i.strip().lower() for i in (interests or []) if i.strip()]
    used_defaults = False
    if not interests:
        interests = list(DEFAULT_INTERESTS)
        used_defaults = True
    constraints = constraints or []
    notes: list[str] = []
    if used_defaults:
        notes.append(
            "No traveler interests provided — using broad catalog defaults for this search."
        )
    notes.append("POI source: live OpenStreetMap (Overpass; Nominatim backup).")
    pois: list[POICandidate] = []
    missing = False
    bbox = city_bbox(canonical)

    if not use_overpass:
        return POISearchResult(
            city=canonical,
            query_interests=interests,
            query_constraints=list(constraints),
            pois=[],
            missing_data=True,
            notes=(
                "; ".join(notes)
                + "; Live Overpass is required for this capstone — "
                "local seed fallback is disabled."
            ),
        )

    try:
        # Parallel per coalesced interest so one heavy filter cannot stall the rest.
        per_limit = max(40, limit)
        elements, partial_notes = _fetch_overpass_for_interests(
            interests, bbox=bbox, per_limit=per_limit, timeout=25.0
        )
        notes.extend(partial_notes)
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

    # Live Nominatim backup per interest that Overpass missed (still OSM ids).
    from agent.preferences import INTEREST_CATEGORY_MAP, normalize_interest

    for interest in interests:
        key = normalize_interest(interest) or interest.lower().strip()
        # Require a *primary* category hit — e.g. heritage needs historic sites,
        # not a leftover tourism=attraction from the park/zoo query.
        primary = {key} | INTEREST_CATEGORY_MAP.get(key, set())
        quality_hits = sum(
            1
            for p in pois
            if (p.category or "").lower() in primary
            and not _is_low_signal_poi(
                p.name or "", p.tags or {}, (p.category or "").lower()
            )
        )
        # Parks/heritage are junk-heavy or flaky — top up until icons exist.
        min_needed = 3 if key in {"park", "garden", "heritage", "culture", "history"} else 1
        if quality_hits >= min_needed:
            continue
        try:
            extra = nominatim_category_search(
                city=canonical, interest=interest, limit=max(8, limit // 2)
            )
        except Exception as exc:  # noqa: BLE001
            notes.append(
                f"Nominatim backup failed for {interest!r} ({exc.__class__.__name__})."
            )
            continue
        if not extra:
            continue
        notes.append(
            f"Live Nominatim backup supplied {len(extra)} POIs for interest={interest!r}."
        )
        existing = {f"{p.osm_type}/{p.osm_id}" for p in pois}
        added = 0
        for p in extra:
            ref = f"{p.osm_type}/{p.osm_id}"
            if ref in existing:
                continue
            if _is_low_signal_poi(
                p.name or "", p.tags or {}, (p.category or "").lower()
            ):
                continue
            # Named park/heritage queries get a must-see boost.
            if key in {"park", "garden", "heritage", "culture", "history"} and (
                _MUST_SEE_NAME_RE.search(p.name or "")
            ):
                p.rank_score = max(float(p.rank_score or 0), 18.0)
            pois.append(p)
            existing.add(ref)
            added += 1
        if added:
            missing = False
            notes.append(
                f"Kept {added} quality Nominatim POIs after low-signal filter "
                f"for interest={interest!r}."
            )

    pois.sort(key=lambda p: (-(p.rank_score or 0), p.name))
    pois = _apply_audience_bias(pois, constraints)
    pois = _boost_by_stated_interests(pois, interests)

    if any("indoor" in c.lower() for c in constraints):
        for p in pois:
            if p.category in {"museum", "food"}:
                p.rank_score = (p.rank_score or 0) + 1.5
        pois.sort(key=lambda p: (-(p.rank_score or 0), p.name))

    before = len(pois)
    pois = _balance_by_interests(pois, interests, limit)
    if before > limit:
        notes.append(
            f"Balanced live MCP candidates across interests "
            f"{', '.join(interests)} (kept {len(pois)}/{before})."
        )
    if not pois:
        missing = True
        notes.append(
            f"No live Overpass POIs available for {canonical}, India. "
            "Cannot invent places or use local seed fallback."
        )

    if any(
        x in " ".join(c.lower() for c in constraints)
        for x in (
            "kid_friendly",
            "senior_friendly",
            "couple_friendly",
            "friends_friendly",
        )
    ):
        notes.append(
            "POI ranking biased by traveler profile constraints "
            f"({', '.join(c for c in constraints if '_' in c or 'Prefer' in c)[:120]})."
        )

    return POISearchResult(
        city=canonical,
        query_interests=interests,
        query_constraints=list(constraints),
        pois=pois,
        missing_data=missing,
        notes="; ".join(notes) if notes else None,
    )
