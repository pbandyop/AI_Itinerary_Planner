"""Grounding & hallucination eval — OSM ids, citations, explicit uncertainty."""

from __future__ import annotations

import json
from pathlib import Path

from agent.schemas.validation import (
    load_and_validate_itinerary,
    validate_grounding_rules,
)


def _check_tip_fixture(path: Path) -> list[str]:
    """Tip/Q&A fixtures: reply must cite source or declare missing data."""
    data = json.loads(path.read_text(encoding="utf-8"))
    problems: list[str] = []
    reply = str(data.get("user_reply") or "")
    missing = bool(data.get("missing_data"))
    sources = data.get("sources") or []

    if missing:
        lower = reply.lower()
        if not any(
            token in lower
            for token in (
                "won't invent",
                "will not invent",
                "don’t invent",
                "don't invent",
                "missing",
                "no cited",
                "does not list",
                "not list",
            )
        ):
            problems.append(
                f"{path.name}: missing_data=true but reply does not refuse invention"
            )
        return problems

    if not sources:
        problems.append(f"{path.name}: tip reply has no sources[]")
    if "source:" not in reply.lower() and not any(
        (s.get("url") or s.get("title")) for s in sources if isinstance(s, dict)
    ):
        problems.append(f"{path.name}: tip has neither (Source: …) nor source metadata")
    for i, s in enumerate(sources):
        if not isinstance(s, dict):
            problems.append(f"{path.name}: sources[{i}] not an object")
            continue
        if not (s.get("title") or "").strip():
            problems.append(f"{path.name}: sources[{i}] missing title")
    return problems


def run_grounding_eval(fixtures_dir: Path) -> tuple[str, bool, str]:
    failures: list[str] = []
    checked_itin = 0
    checked_tips = 0

    for path in sorted(fixtures_dir.glob("*.json")):
        result = load_and_validate_itinerary(path, enforce_grounding=False)
        if not result.ok or result.itinerary is None:
            # May be a tip fixture, not a full itinerary.
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                failures.append(f"{path.name}: invalid JSON")
                continue
            if "user_reply" in data and "days" not in data:
                tip_problems = _check_tip_fixture(path)
                checked_tips += 1
                failures.extend(tip_problems)
                continue
            failures.append(f"{path.name}: schema invalid")
            continue

        problems = validate_grounding_rules(result.itinerary)
        checked_itin += 1
        if problems:
            failures.append(f"{path.name}: " + "; ".join(problems))

        # Every stop OSM id must be a positive int (schema already enforces gt=0).
        for day in result.itinerary.days:
            for stop in day.all_stops:
                if stop.osm_type not in {"node", "way", "relation"}:
                    failures.append(
                        f"{path.name}: {stop.name} has invalid osm_type={stop.osm_type}"
                    )
                if stop.osm_id <= 0:
                    failures.append(f"{path.name}: {stop.name} missing OSM id")

    tips_dir = fixtures_dir / "tips"
    if tips_dir.is_dir():
        for path in sorted(tips_dir.glob("*.json")):
            tip_problems = _check_tip_fixture(path)
            checked_tips += 1
            failures.extend(tip_problems)

    if failures:
        return ("grounding", False, " | ".join(failures))
    if checked_itin == 0 and checked_tips == 0:
        return ("grounding", False, "No fixtures checked")
    return (
        "grounding",
        True,
        f"OK — OSM + citation/uncertainty on {checked_itin} itinerary(ies); "
        f"{checked_tips} tip fixture(s)",
    )
