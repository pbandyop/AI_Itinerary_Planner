"""Fetch official Rajasthan/Jaipur tourism pages into the RAG corpus."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from agent.rag.http_util import html_to_text, http_get_text
from agent.rag.paths import corpus_dir

logger = logging.getLogger(__name__)

# Small allowlist of official / governmental tourism pages (Jaipur-focused).
TOURISM_URLS: list[tuple[str, str]] = [
    (
        "Jaipur — Rajasthan Tourism",
        "https://www.tourism.rajasthan.gov.in/jaipur.html",
    ),
    (
        "Amber Fort — Rajasthan Tourism",
        "https://www.tourism.rajasthan.gov.in/amber-fort.html",
    ),
    (
        "City Palace Jaipur — Rajasthan Tourism",
        "https://www.tourism.rajasthan.gov.in/city-palace-jaipur.html",
    ),
    (
        "Jantar Mantar Jaipur — Rajasthan Tourism",
        "https://www.tourism.rajasthan.gov.in/jantar-mantar-jaipur.html",
    ),
    (
        "Hawa Mahal — Rajasthan Tourism",
        "https://www.tourism.rajasthan.gov.in/hawa-mahal.html",
    ),
]


def fetch_tourism_pages(*, sleep_s: float = 1.5) -> Path:
    places: list[dict] = []
    for title, url in TOURISM_URLS:
        time.sleep(sleep_s)
        try:
            html = http_get_text(url, timeout=45.0)
            text = html_to_text(html)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Tourism fetch failed %s: %s", url, exc)
            continue
        # Keep a usable slice; drop tiny failed shells
        text = text[:12000].strip()
        if len(text) < 200:
            logger.warning("Tourism page too short: %s", url)
            continue
        places.append(
            {
                "city": "Jaipur",
                "title": title,
                "place_name": title.split("—")[0].strip(),
                "source": "Rajasthan Tourism",
                "dataset": "tourism",
                "url": url,
                "license": "All rights reserved (official tourism site; cited extract)",
                "text": f"{title}. {text}",
                "atomic": False,
            }
        )
        logger.info("Tourism OK %s", title)

    out_dir = corpus_dir() / "tourism"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "jaipur_tourism.json"
    path.write_text(
        json.dumps(
            {
                "city": "Jaipur",
                "source": "Rajasthan Tourism",
                "dataset": "tourism",
                "places": places,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path
