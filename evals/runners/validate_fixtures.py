"""Validate golden itinerary fixtures against the Phase 1 schema."""

from __future__ import annotations

from pathlib import Path

from agent.schemas.validation import load_and_validate_itinerary


def run_fixture_validation(fixtures_dir: Path) -> tuple[str, bool, str]:
    files = sorted(fixtures_dir.glob("*.json"))
    if not files:
        return ("fixtures", False, f"No JSON fixtures in {fixtures_dir}")

    errors: list[str] = []
    for path in files:
        result = load_and_validate_itinerary(path, enforce_grounding=True)
        if not result.ok:
            msg = "; ".join(result.errors + result.grounding_errors)
            errors.append(f"{path.name}: {msg}")

    if errors:
        return ("fixtures", False, " | ".join(errors))
    return ("fixtures", True, f"Validated {len(files)} golden itinerary(ies)")
