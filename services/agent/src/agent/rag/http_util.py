"""Shared HTTP helpers for RAG corpus fetchers."""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

logger = logging.getLogger(__name__)

USER_AGENT = "AI-Itinerary-Planner-Capstone/0.4 (educational; local-dev)"


def http_get_json(url: str, *, timeout: float = 60.0, headers: dict[str, str] | None = None) -> Any:
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get_text(url: str, *, timeout: float = 60.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        charset = "utf-8"
        ctype = resp.headers.get_content_charset()
        if ctype:
            charset = ctype
        return raw.decode(charset, errors="replace")


def fetch_mediawiki_extract(api_base: str, title: str) -> dict | None:
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
    try:
        data = http_get_json(url)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("Mediawiki fetch failed for %s: %s", title, exc)
        return None
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


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in {"script", "style", "nav", "footer", "header"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "nav", "footer", "header"} and self._skip:
            self._skip -= 1
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "h4"}:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = data.strip()
        if text:
            self._chunks.append(text + " ")

    def text(self) -> str:
        raw = "".join(self._chunks)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def html_to_text(html: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001
        return re.sub(r"<[^>]+>", " ", html)
    return parser.text()


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "place"
