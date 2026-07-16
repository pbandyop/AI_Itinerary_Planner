"""Fetch Wikivoyage (and optional Wikipedia) extracts into data/rag/corpus/."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from agent.rag.paths import corpus_dir

logger = logging.getLogger(__name__)

DEFAULT_CITIES: list[tuple[str, str]] = [
    # Demo scope is Jaipur-only (expand later as needed).
    ("Jaipur", "Jaipur"),
]

USER_AGENT = "AI-Itinerary-Planner-Capstone/0.3 (educational; local-dev)"


def _fetch_mediawiki_extract(api_base: str, title: str) -> dict | None:
    q = urllib.parse.urlencode(
        {
            "action": "query",
            "prop": "extracts",
            "explaintext": 1,
            "exsectionformat": "plain",
            "titles": title,
            "format": "json",
            "formatversion": 2,
            "redirects": 1,
        }
    )
    url = f"{api_base}?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    page = data["query"]["pages"][0]
    if page.get("missing"):
        return None
    text = (page.get("extract") or "").strip()
    if not text:
        return None
    page_title = page["title"]
    slug = page_title.replace(" ", "_")
    host = "en.wikivoyage.org" if "wikivoyage" in api_base else "en.wikipedia.org"
    return {
        "title": page_title,
        "text": text,
        "url": f"https://{host}/wiki/{urllib.parse.quote(slug)}",
    }


def fetch_city(
    city: str,
    wikivoyage_title: str,
    *,
    out_dir: Path,
    include_wikipedia: bool = False,
    sleep_s: float = 1.5,
) -> Path | None:
    out_dir.mkdir(parents=True, exist_ok=True)
    time.sleep(sleep_s)
    wv = _fetch_mediawiki_extract(
        "https://en.wikivoyage.org/w/api.php", wikivoyage_title
    )
    if not wv:
        logger.warning("No Wikivoyage page for %s (%s)", city, wikivoyage_title)
        return None

    doc = {
        "city": city,
        "title": wv["title"],
        "source": "Wikivoyage",
        "url": wv["url"],
        "license": "CC BY-SA 4.0",
        "text": wv["text"],
    }

    if include_wikipedia:
        time.sleep(sleep_s)
        wp = _fetch_mediawiki_extract(
            "https://en.wikipedia.org/w/api.php", city
        )
        if wp:
            # Append a clearly marked Wikipedia section for extra grounding.
            doc["text"] = (
                wv["text"]
                + "\n\n=== Wikipedia overview ===\n"
                + wp["text"][:12000]
            )
            doc["wikipedia_url"] = wp["url"]

    path = out_dir / f"{city.lower()}.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Wrote %s (%d chars)", path.name, len(doc["text"]))
    return path


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Fetch Wikivoyage corpus for RAG")
    parser.add_argument(
        "--cities",
        nargs="*",
        default=None,
        help="Subset of city names (default: major India set)",
    )
    parser.add_argument(
        "--wikipedia",
        action="store_true",
        help="Also append a short Wikipedia overview",
    )
    parser.add_argument("--sleep", type=float, default=2.0)
    args = parser.parse_args(argv)

    wanted = {c.lower() for c in args.cities} if args.cities else None
    jobs = [
        (city, title)
        for city, title in DEFAULT_CITIES
        if wanted is None or city.lower() in wanted
    ]
    out = corpus_dir()
    ok = 0
    for city, title in jobs:
        try:
            path = fetch_city(
                city,
                title,
                out_dir=out,
                include_wikipedia=args.wikipedia,
                sleep_s=args.sleep,
            )
            if path:
                ok += 1
                print(f"OK {city}")
            else:
                print(f"MISS {city}")
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {city}: {exc}")
            logger.exception("fetch failed for %s", city)
    print(f"done ok={ok}/{len(jobs)} dir={out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
