"""Shared opening-hours clock detection for RAG grounding checks."""

from __future__ import annotations

import re

# Accept Google-style AM/PM and OSM 24h / weekday ranges (e.g. 11:00-23:00, Mo-Su 10:30-23:00).
HOUR_CLOCK_RE = re.compile(
    r"(?:"
    r"\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b"
    r"(?:\s*[-–]\s*\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b)?"
    r"|"
    r"\b(?:mo|tu|we|th|fr|sa|su|mon|tue|wed|thu|fri|sat|sun)"
    r"[a-z]*\s*[-–]\s*"
    r"(?:mo|tu|we|th|fr|sa|su|mon|tue|wed|thu|fri|sat|sun)"
    r"[a-z]*.{0,40}\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b"
    r"|"
    # OSM 24h ranges: 11:00-23:00 / 11:00–23:00 / 11:00 to 23:00
    r"\b\d{1,2}:\d{2}\s*(?:[-–]|to)\s*\d{1,2}:\d{2}\b"
    r"|"
    # OSM day span + 24h: Mo-Su 10:30-23:00
    r"\b(?:mo|tu|we|th|fr|sa|su|mon|tue|wed|thu|fri|sat|sun)"
    r"[a-z]*\s*[-–]\s*"
    r"(?:mo|tu|we|th|fr|sa|su|mon|tue|wed|thu|fri|sat|sun)"
    r"[a-z]*\s+\d{1,2}:\d{2}\s*(?:[-–]|to)\s*\d{1,2}:\d{2}\b"
    r")",
    re.I,
)


def has_hour_clock(text: str | None) -> bool:
    """True if text contains a parseable opening-hours clock signal."""
    hay = (text or "").lower()
    if "opening hours" in hay and HOUR_CLOCK_RE.search(text or ""):
        return True
    return bool(HOUR_CLOCK_RE.search(text or ""))
