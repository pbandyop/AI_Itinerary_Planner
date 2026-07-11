"""Grounding & hallucination eval stub (Phase 1)."""

from __future__ import annotations

from pathlib import Path

from agent.schemas.validation import (
    load_and_validate_itinerary,
    validate_grounding_rules,
)


def run_grounding_eval(fixtures_dir: Path) -> tuple[str, bool, str]:
    failures: list[str] = []
    checked = 0
    for path in sorted(fixtures_dir.glob("*.json")):
        result = load_and_validate_itinerary(path, enforce_grounding=False)
        if not result.ok or result.itinerary is None:
            failures.append(f"{path.name}: schema invalid")
            continue
        problems = validate_grounding_rules(result.itinerary)
        checked += 1
        if problems:
            failures.append(f"{path.name}: " + "; ".join(problems))

    if failures:
        return ("grounding", False, " | ".join(failures))
    return (
        "grounding",
        True,
        f"Stub OK — OSM id + citation/uncertainty rules passed on {checked} fixture(s)",
    )
