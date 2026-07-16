"""Human-friendly duration wording for itinerary presentation."""

from __future__ import annotations


def format_spend_duration(minutes: int | None) -> str:
    """Convert visit minutes to nearest-hour copy for easier reading.

    Examples: 103 → "about 2 hours"; 45 → "about 1 hour"; 20 → minutes kept.
    """
    if minutes is None or minutes <= 0:
        return "spend a short time in this place"
    mins = int(minutes)
    hours = int(round(mins / 60.0))
    if hours <= 0:
        return f"spend about {mins} minutes in this place"
    if hours == 1:
        return "spend about 1 hour in this place"
    return f"spend about {hours} hours in this place"
