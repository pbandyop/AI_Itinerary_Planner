"""Fetch Google Places for Jaipur via Places API (New) — no HTML scrape."""

from __future__ import annotations

import argparse
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

DEFAULT_MAX_PLACES = 900
# Places API (New) caps pageSize at 20; paginate to fill the corpus cap.
DEFAULT_PAGE_SIZE = 20
DEFAULT_MAX_PAGES = 5
# Safer default for daily append runs (requests, not unique places).
DEFAULT_MAX_NEW = 150

# Category packs for day-by-day merge builds (dedupe by place_id).
QUERY_PACKS: dict[str, list[str]] = {
    "named": [
        "Hawa Mahal Jaipur",
        "Jantar Mantar Jaipur",
        "City Palace Jaipur",
        "Mubarak Mahal Jaipur",
        "Amber Fort Jaipur",
        "Amer Fort Jaipur",
        "Jaigarh Fort Jaipur",
        "Nahargarh Fort Jaipur",
        "Jal Mahal Jaipur",
        "Albert Hall Museum Jaipur",
        "Anokhi Museum Jaipur",
        "Anokhi Museum of Hand Printing Jaipur",
        "Alice Garg Seashell Museum Jaipur",
        "Akshardham Temple Jaipur",
        "Birla Mandir Jaipur",
        "Galta Ji Temple Jaipur",
        "Patrika Gate Jaipur",
        "Sisodia Rani Garden Jaipur",
        "Central Park Jaipur",
        "Arya College Park Jaipur",
        "Jawahar Circle Jaipur",
        "World Trade Park Jaipur",
        "Johari Bazaar Jaipur",
        "Bapu Bazaar Jaipur",
        "Tripolia Bazaar Jaipur",
        "Chandpole Bazaar Jaipur",
        "MI Road Jaipur",
        "Amarnath Cafe Jaipur",
        "Ganesh Restaurant Jaipur",
        "Caravana Jaipur",
        "Cafe Coffee Day Jaipur",
        "Bella Italia Jaipur",
        "Bella Italia restaurant Jaipur",
        "Bar Palladio Jaipur",
        "Bar Paladio Jaipur",
        "Peacock Rooftop Restaurant Jaipur",
        "1135 AD Amber Fort Jaipur",
        "Suvarna Mahal Jaipur",
        "Spice Court Jaipur",
        "Laxmi Misthan Bhandar Jaipur",
        "Rawat Mishthan Bhandar Jaipur",
        "Niros Jaipur",
        "Handi Restaurant Jaipur",
        "Tapri Central Jaipur",
        "Anokhi Cafe Jaipur",
        "Curious Life Coffee Jaipur",
        "Narain Niwas Palace Jaipur",
    ],
    "museums": [
        "museums in Jaipur",
        "art galleries in Jaipur",
        "Albert Hall Museum Jaipur",
        "Anokhi Museum Jaipur",
        "Alice Garg Seashell Museum Jaipur",
        "museums near City Palace Jaipur",
        "wax museum Jaipur",
        "science museum Jaipur",
    ],
    "temples": [
        "temples in Jaipur",
        "Hindu temples in Jaipur",
        "mandir in Jaipur",
        "Birla Mandir Jaipur",
        "Akshardham Temple Jaipur",
        "Galta Ji Temple Jaipur",
        "Govind Dev Ji Temple Jaipur",
        "Motu Dungri Ganesh Temple Jaipur",
        "Khole Ke Hanuman Ji Temple Jaipur",
        "temples in Amer Jaipur",
        "Jain temples in Jaipur",
        "mosques in Jaipur",
        "gurudwara in Jaipur",
        "churches in Jaipur",
        "place of worship Jaipur",
    ],
    "gardens": [
        "gardens in Jaipur",
        "parks in Jaipur",
        "botanical garden Jaipur",
        "Sisodia Rani Garden Jaipur",
        "Sisodia Rani ka Bagh Jaipur",
        "Central Park Jaipur",
        "Ram Niwas Garden Jaipur",
        "Jawahar Circle Garden Jaipur",
        "Amar Jawan Jyoti Jaipur",
        "Vidyadhar Bagh Jaipur",
        "Kanak Vrindavan Garden Jaipur",
        "Smriti Van Jaipur",
        "Statue Circle Jaipur",
        "public parks in Jaipur",
        "lake gardens Jaipur",
    ],
    "heritage": [
        "tourist attractions in Jaipur",
        "historical places in Jaipur",
        "heritage sites in Jaipur",
        "monuments in Jaipur",
        "forts in Jaipur",
        "palaces in Jaipur",
        "temples in Jaipur",
        "havelis in Jaipur",
        "stepwells in Jaipur",
        "lakes in Jaipur",
        "parks in Jaipur",
        "gardens in Jaipur",
        "things to do in Jaipur",
        "tourist places in Amer Jaipur",
        "tourist places in Sanganer Jaipur",
        "temples in Amer Jaipur",
    ],
    "restaurants": [
        "restaurants in Jaipur",
        "Indian restaurants in Jaipur",
        "Rajasthani restaurants in Jaipur",
        "North Indian restaurants in Jaipur",
        "South Indian restaurants in Jaipur",
        "Mughlai restaurants in Jaipur",
        "vegetarian restaurants in Jaipur",
        "fine dining in Jaipur",
        "rooftop restaurants in Jaipur",
        "Italian restaurants in Jaipur",
        "Chinese restaurants in Jaipur",
        "Mexican restaurants in Jaipur",
        "Thai restaurants in Jaipur",
        "Lebanese restaurants in Jaipur",
        "buffet restaurants in Jaipur",
        "dhaba in Jaipur",
        "thali restaurant Jaipur",
        "breakfast restaurants in Jaipur",
        "brunch in Jaipur",
        "continental restaurants in Jaipur",
        "multi cuisine restaurants in Jaipur",
        "family restaurants in Jaipur",
        "pure veg restaurants in Jaipur",
        "vegan restaurants in Jaipur",
        "fast food in Jaipur",
        "pizza in Jaipur",
        "burger in Jaipur",
        "sushi in Jaipur",
    ],
    "cafes": [
        "cafes in Jaipur",
        "coffee shops in Jaipur",
        "bakeries in Jaipur",
        "sweet shops in Jaipur",
        "dessert cafes in Jaipur",
        "ice cream in Jaipur",
        "street food in Jaipur",
        "chaat in Jaipur",
        "Cafe Coffee Day Jaipur",
        "Anokhi Cafe Jaipur",
    ],
    "nightlife": [
        "bars in Jaipur",
        "pubs in Jaipur",
        "lounge bars in Jaipur",
        "night clubs in Jaipur",
        "Bar Palladio Jaipur",
        "bars in C Scheme Jaipur",
    ],
    "markets": [
        "markets in Jaipur",
        "shopping bazaars in Jaipur",
        "handicraft shops in Jaipur",
        "jewellery shops in Jaipur",
        "bookstores in Jaipur",
        "Johari Bazaar Jaipur",
        "Bapu Bazaar Jaipur",
        "markets near Hawa Mahal Jaipur",
        "shopping in World Trade Park Jaipur",
        "food court World Trade Park Jaipur",
    ],
    "hotels": [
        "hotels in Jaipur",
        "boutique hotels in Jaipur",
        "heritage hotels in Jaipur",
        "spas in Jaipur",
        "hotels in C Scheme Jaipur",
        "hotels near Hawa Mahal Jaipur",
        "hotels near City Palace Jaipur",
        "hotels near Amer Fort Jaipur",
    ],
    "neighborhoods": [
        "restaurants in C Scheme Jaipur",
        "cafes in C Scheme Jaipur",
        "restaurants in Vaishali Nagar Jaipur",
        "restaurants in Mansarovar Jaipur",
        "restaurants in Malviya Nagar Jaipur",
        "restaurants in Bani Park Jaipur",
        "restaurants in Raja Park Jaipur",
        "restaurants in Jagatpura Jaipur",
        "restaurants in Tonk Road Jaipur",
        "restaurants in Amer Jaipur",
        "restaurants in Sodala Jaipur",
        "restaurants in Civil Lines Jaipur",
        "restaurants in Lal Kothi Jaipur",
        "restaurants in Vidhyadhar Nagar Jaipur",
        "restaurants in Ajmer Road Jaipur",
        "restaurants in Pratap Nagar Jaipur",
        "restaurants in Shyam Nagar Jaipur",
        "restaurants in Gandhi Nagar Jaipur",
        "restaurants in Jhotwara Jaipur",
        "cafes in Vaishali Nagar Jaipur",
        "cafes in Mansarovar Jaipur",
        "cafes in Malviya Nagar Jaipur",
        "restaurants near Jal Mahal Jaipur",
        "restaurants near Nahargarh Fort Jaipur",
        "cafes near Patrika Gate Jaipur",
        "restaurants near Railway Station Jaipur",
        "restaurants near Jaipur Airport",
    ],
}

# Full default = named first, then remaining packs (deduped order preserved).
_PACK_ORDER = [
    "named",
    "museums",
    "temples",
    "gardens",
    "heritage",
    "restaurants",
    "cafes",
    "nightlife",
    "markets",
    "hotels",
    "neighborhoods",
]


def list_packs() -> list[str]:
    return list(_PACK_ORDER)


def queries_for_pack(pack: str | None) -> list[str]:
    """Return ordered unique queries for one pack or the full default set."""
    if pack and pack != "all":
        key = pack.strip().lower()
        if key == "temple":
            key = "temples"
        if key == "garden":
            key = "gardens"
        if key not in QUERY_PACKS:
            raise ValueError(
                f"Unknown pack {pack!r}. Choose from: {', '.join(list_packs())}, all"
            )
        return list(QUERY_PACKS[key])
    seen: set[str] = set()
    out: list[str] = []
    for name in _PACK_ORDER:
        for q in QUERY_PACKS[name]:
            if q not in seen:
                seen.add(q)
                out.append(q)
    return out


# Back-compat alias used by older call sites / docs.
SEARCH_QUERIES = queries_for_pack("all")


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


def _place_key(card: dict[str, Any]) -> str:
    return str(card.get("place_id") or card.get("place_name") or "").strip()


def _load_existing(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read existing Places corpus: %s", exc)
        return []
    places = raw.get("places") if isinstance(raw, dict) else None
    if not isinstance(places, list):
        return []
    return [p for p in places if isinstance(p, dict)]


def _search_text(
    key: str,
    query: str,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
    sleep_s: float = 0.25,
) -> tuple[list[dict[str, Any]], bool]:
    """Return (places, quota_hit). quota_hit True on HTTP 429."""
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
            "nextPageToken",
        ]
    )
    headers = {
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": field_mask,
        "Content-Type": "application/json",
        "User-Agent": "AI-Itinerary-Planner-Capstone/0.4",
    }
    places: list[dict[str, Any]] = []
    page_token: str | None = None
    for page_idx in range(max(1, max_pages)):
        body: dict[str, Any] = {
            "textQuery": query,
            "locationBias": {
                "circle": {
                    "center": {"latitude": JAIPUR_LAT, "longitude": JAIPUR_LON},
                    "radius": 25000.0,
                }
            },
            "pageSize": min(20, max(1, page_size)),
            "languageCode": "en",
        }
        if page_token:
            body["pageToken"] = page_token
        try:
            data = _http_json(SEARCH_URL, method="POST", headers=headers, body=body)
        except urllib.error.HTTPError as exc:
            err = exc.read().decode("utf-8", errors="replace")
            logger.warning("Places searchText failed (%s): %s", exc.code, err[:300])
            return places, exc.code == 429
        except Exception as exc:  # noqa: BLE001
            logger.warning("Places searchText failed: %s", exc)
            break
        batch = list(data.get("places") or [])
        places.extend(batch)
        page_token = data.get("nextPageToken") or None
        if not page_token or not batch:
            break
        if page_idx + 1 < max_pages:
            time.sleep(sleep_s)
    return places, False


def fetch_google_places(
    *,
    sleep_s: float = 0.25,
    max_places: int = DEFAULT_MAX_PLACES,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_new: int | None = None,
    pack: str | None = None,
    merge: bool = False,
) -> Path:
    """
    Fetch Places cards into ``data/rag/corpus/google/jaipur_places.json``.

    ``merge=True`` appends unique ``place_id``s onto the existing file (safe for
    day-by-day category packs). On HTTP 429, stops early and keeps prior cards
    when merging (never truncates the corpus to a partial overwrite).
    """
    out_dir = corpus_dir() / "google"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "jaipur_places.json"
    key = _api_key()
    queries = queries_for_pack(pack)
    new_budget = max_new if max_new is not None else (
        DEFAULT_MAX_NEW if merge or (pack and pack != "all") else max_places
    )

    if not key:
        logger.warning(
            "No GOOGLE_PLACES_API_KEY — writing empty Google Places corpus stub"
        )
        if merge and path.is_file():
            return path
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

    existing = _load_existing(path) if merge else []
    prior_on_disk = _load_existing(path)
    places_out: list[dict[str, Any]] = list(existing)
    seen: set[str] = {_place_key(p) for p in places_out if _place_key(p)}
    added = 0
    quota_hit = False
    pack_label = pack or "all"

    logger.info(
        "Places fetch pack=%s merge=%s existing=%d max_new=%d max_places=%d queries=%d",
        pack_label,
        merge,
        len(existing),
        new_budget,
        max_places,
        len(queries),
    )

    for q in queries:
        if added >= new_budget or len(places_out) >= max_places:
            break
        rows, hit_429 = _search_text(key, q, max_pages=max_pages, sleep_s=sleep_s)
        if hit_429:
            quota_hit = True
            logger.warning(
                "Quota hit — stopping Places fetch (kept %d cards, added %d this run)",
                len(places_out) if merge else added,
                added,
            )
            break
        for place in rows:
            card = _place_card(place)
            if not card:
                continue
            pid = _place_key(card)
            if not pid or pid in seen:
                continue
            seen.add(pid)
            places_out.append(card)
            added += 1
            if added >= new_budget or len(places_out) >= max_places:
                break
        time.sleep(sleep_s)

    # Never wipe a healthy corpus with an empty/failed overwrite run.
    if not merge and added == 0 and prior_on_disk:
        logger.warning(
            "No new Places cards fetched; leaving existing corpus (%d) untouched",
            len(prior_on_disk),
        )
        return path

    if max_places > 0:
        places_out = places_out[:max_places]

    payload = {
        "city": "Jaipur",
        "source": "Google Places",
        "dataset": "google_places",
        "api": "places.googleapis.com (New)",
        "max_places": max_places,
        "pack": pack_label,
        "merge": merge,
        "added_this_run": added,
        "quota_hit": quota_hit,
        "places": places_out,
    }

    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(
        "Google Places wrote %d cards (added=%d pack=%s merge=%s quota_hit=%s)",
        len(places_out),
        added,
        pack_label,
        merge,
        quota_hit,
    )
    return path


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        from dotenv import load_dotenv

        from agent.rag.paths import repo_root

        load_dotenv(repo_root() / ".env", override=True)
    except Exception:  # noqa: BLE001
        pass

    parser = argparse.ArgumentParser(
        description=(
            "Fetch Jaipur Google Places RAG cards. "
            "Use --merge --pack <category> for day-by-day append builds."
        )
    )
    parser.add_argument(
        "--pack",
        choices=[*list_packs(), "temple", "garden", "all"],
        default="all",
        help="Query category pack (default: all). Prefer one pack/day with --merge.",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help="Append unique place_ids onto existing jaipur_places.json (do not wipe).",
    )
    parser.add_argument(
        "--max-places",
        type=int,
        default=None,
        help=(
            f"Hard cap on total cards in the file "
            f"(default {DEFAULT_MAX_PLACES}; with --merge default 5000)"
        ),
    )
    parser.add_argument(
        "--max-new",
        type=int,
        default=None,
        help=(
            f"Max NEW unique places this run "
            f"(default {DEFAULT_MAX_NEW} with --merge/--pack, else --max-places)"
        ),
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
        help=f"Max Text Search pages per query (default {DEFAULT_MAX_PAGES})",
    )
    parser.add_argument("--sleep", type=float, default=0.25, help="Delay between requests")
    parser.add_argument(
        "--list-packs",
        action="store_true",
        help="Print available category packs and exit",
    )
    args = parser.parse_args(argv)
    if args.list_packs:
        for name in list_packs():
            print(f"{name}: {len(QUERY_PACKS[name])} queries")
        print("all: union of packs (named first)")
        return 0

    max_places = args.max_places
    if max_places is None:
        max_places = 5000 if args.merge else DEFAULT_MAX_PLACES

    path = fetch_google_places(
        sleep_s=args.sleep,
        max_places=max_places,
        max_pages=args.max_pages,
        max_new=args.max_new,
        pack=args.pack,
        merge=args.merge,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
