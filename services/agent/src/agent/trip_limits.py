"""Product scope bounds — shared across schema, orchestrator, and MCP tools.

Current demo scope: **Jaipur only**, **2–4 day** trips.
Expand ALLOWED_CITIES later; keep MAX_TRIP_DAYS at 4.
"""

from __future__ import annotations

MIN_TRIP_DAYS = 2
MAX_TRIP_DAYS = 4

# Locked city list for the current milestone (expand when ready).
SCOPED_CITY = "Jaipur"
ALLOWED_CITIES: tuple[str, ...] = (SCOPED_CITY,)


def clamp_trip_days(days: int) -> int:
    return max(MIN_TRIP_DAYS, min(MAX_TRIP_DAYS, int(days)))


def clamp_forecast_days(days: int) -> int:
    """Standalone weather Q&A can request 1–7 forecast days (not trip bounds)."""
    return max(1, min(7, int(days)))


def is_city_allowed(name: str | None) -> bool:
    if not name or not str(name).strip():
        return False
    key = str(name).strip().lower()
    return any(key == c.lower() for c in ALLOWED_CITIES)


def default_city() -> str:
    return SCOPED_CITY
