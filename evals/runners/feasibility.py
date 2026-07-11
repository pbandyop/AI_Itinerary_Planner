"""Feasibility eval stub (Phase 1) — loads fixtures; full rules in Phase 7."""

from __future__ import annotations

from pathlib import Path

from agent.schemas.validation import load_and_validate_itinerary


def run_feasibility_eval(fixtures_dir: Path) -> tuple[str, bool, str]:
    """Stub: ensure fixtures load; report daily duration vs window (informational)."""
    notes: list[str] = []
    for path in sorted(fixtures_dir.glob("*.json")):
        result = load_and_validate_itinerary(path)
        if not result.ok or result.itinerary is None:
            return (
                "feasibility",
                False,
                f"Cannot run feasibility stub; invalid fixture {path.name}",
            )
        itinerary = result.itinerary
        window = itinerary.trip.daily_time_window_min
        for day in itinerary.days:
            total = day.total_duration_min
            flag = "ok" if total <= window else "OVER"
            notes.append(f"{path.stem} day{day.day_index}: {total}m/{window}m [{flag}]")

    # Phase 1 stub always passes structurally; Phase 7 will fail on OVER.
    return ("feasibility", True, "Stub OK — " + "; ".join(notes))
