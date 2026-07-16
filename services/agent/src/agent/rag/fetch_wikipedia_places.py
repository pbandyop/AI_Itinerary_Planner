"""Fetch Wikipedia place pages for Jaipur into data/rag/corpus/wikipedia/."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from agent.rag.http_util import fetch_mediawiki_extract, slugify
from agent.rag.paths import corpus_dir, repo_root

logger = logging.getLogger(__name__)

EXTRA_TITLES = [
    "Hawa Mahal",
    "City Palace, Jaipur",
    "Jantar Mantar, Jaipur",
    "Amber Fort",
    "Jal Mahal",
    "Albert Hall Museum",
    "Nahargarh Fort",
    "Jaigarh Fort",
    "Birla Mandir, Jaipur",
    "Patrika Gate",
    "Govind Dev Ji Temple",
    "Chokhi Dhani",
]


def _titles_from_poi_seed() -> list[str]:
    path = repo_root() / "data" / "pois" / "jaipur.json"
    titles: list[str] = []
    if not path.is_file():
        return titles
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return titles
    for item in raw if isinstance(raw, list) else []:
        tags = (item or {}).get("tags") or {}
        wp = str(tags.get("wikipedia") or "").strip()
        if wp.lower().startswith("en:"):
            titles.append(wp.split(":", 1)[1].strip())
        elif wp:
            titles.append(wp)
        name = str((item or {}).get("name") or "").strip()
        if name and name not in titles:
            # Try common "Name, Jaipur" form later as fallback only via EXTRA
            pass
    return titles


def fetch_wikipedia_places(*, sleep_s: float = 1.2) -> list[Path]:
    out_dir = corpus_dir() / "wikipedia"
    out_dir.mkdir(parents=True, exist_ok=True)
    titles = list(dict.fromkeys([*_titles_from_poi_seed(), *EXTRA_TITLES]))
    written: list[Path] = []
    for title in titles:
        time.sleep(sleep_s)
        page = fetch_mediawiki_extract("https://en.wikipedia.org/w/api.php", title)
        if not page:
            # Retry with ", Jaipur" suffix when needed
            if ", Jaipur" not in title and "Jaipur" not in title:
                time.sleep(sleep_s)
                page = fetch_mediawiki_extract(
                    "https://en.wikipedia.org/w/api.php", f"{title}, Jaipur"
                )
        if not page:
            logger.info("Wikipedia miss: %s", title)
            continue
        place = page["title"]
        doc = {
            "city": "Jaipur",
            "title": place,
            "place_name": place,
            "source": "Wikipedia",
            "dataset": "wikipedia",
            "url": page["url"],
            "license": "CC BY-SA 4.0",
            "text": page["text"][:20000],
            "atomic": True,
        }
        path = out_dir / f"{slugify(place)}.json"
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(path)
        logger.info("Wikipedia OK %s", place)
    return written
