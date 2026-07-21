"""Feasibility eval — daily duration, travel legs, and pace stop caps."""

from __future__ import annotations

from pathlib import Path

from agent.mcp.itinerary_builder import STOPS_PER_DAY
from agent.schemas.validation import load_and_validate_itinerary

# Heuristic legs should stay within a same-city day (not intercity hops).
MAX_LEG_TRAVEL_MIN = 90
DAY_HARD_END_MIN = 21 * 60


def _parse_clock(value: str | None) -> int | None:
    if not value:
        return None
    try:
        hh, mm = str(value).split(":")[:2]
        return int(hh) * 60 + int(mm)
    except (TypeError, ValueError):
        return None


def run_feasibility_eval(fixtures_dir: Path) -> tuple[str, bool, str]:
    """Fail when a golden plan exceeds time window, pace stop cap, or travel sanity."""
    failures: list[str] = []
    notes: list[str] = []
    checked = 0

    for path in sorted(fixtures_dir.glob("*.json")):
        # Skip edit-case fixtures (before/after pairs live under edits/).
        if path.parent.name == "edits" or path.name.startswith("edit_"):
            continue
        result = load_and_validate_itinerary(path, enforce_grounding=False)
        if not result.ok or result.itinerary is None:
            failures.append(f"{path.name}: invalid schema")
            continue

        itinerary = result.itinerary
        window = itinerary.trip.daily_time_window_min
        pace = itinerary.trip.pace or "moderate"
        stop_cap = STOPS_PER_DAY.get(pace, 6)  # type: ignore[arg-type]
        checked += 1

        for day in itinerary.days:
            total = day.total_duration_min
            n_stops = len(day.all_stops)
            label = f"{path.stem} day{day.day_index}"

            if total > window:
                failures.append(f"{label}: duration {total}m > window {window}m")
            else:
                notes.append(f"{label}: {total}m/{window}m ok")

            if n_stops > stop_cap:
                failures.append(
                    f"{label}: {n_stops} stops > pace={pace} cap {stop_cap}"
                )
            else:
                notes.append(f"{label}: {n_stops}≤{stop_cap} stops ({pace})")

            for stop in day.all_stops:
                leg = stop.travel_to_next_min
                if leg is None:
                    continue
                if leg < 0:
                    failures.append(f"{label}: negative travel on {stop.name}")
                elif leg > MAX_LEG_TRAVEL_MIN:
                    failures.append(
                        f"{label}: travel {leg}m after {stop.name} "
                        f"> {MAX_LEG_TRAVEL_MIN}m (unreasonable for same-city day)"
                    )

                depart = _parse_clock(getattr(stop, "depart_time", None))
                arrive = _parse_clock(getattr(stop, "arrive_time", None))
                if depart is not None and arrive is not None and depart < arrive:
                    failures.append(
                        f"{label}: {stop.name} past midnight "
                        f"({stop.arrive_time}→{stop.depart_time})"
                    )
                elif depart is not None and depart > DAY_HARD_END_MIN:
                    failures.append(
                        f"{label}: {stop.name} departs {stop.depart_time} > 21:00"
                    )

    if not checked:
        return ("feasibility", False, "No itinerary fixtures found")
    if failures:
        return ("feasibility", False, " | ".join(failures))
    return (
        "feasibility",
        True,
        f"OK — {checked} fixture(s); duration≤window, stops≤pace cap, "
        f"legs≤{MAX_LEG_TRAVEL_MIN}m, day≤21:00",
    )
