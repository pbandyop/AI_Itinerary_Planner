"""Validation helpers for itineraries and grounding rules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from agent.schemas.itinerary import Itinerary


class ValidationResult:
    def __init__(
        self,
        *,
        ok: bool,
        itinerary: Itinerary | None = None,
        errors: list[str] | None = None,
        grounding_errors: list[str] | None = None,
    ) -> None:
        self.ok = ok
        self.itinerary = itinerary
        self.errors = errors or []
        self.grounding_errors = grounding_errors or []

    def raise_if_invalid(self) -> Itinerary:
        if not self.ok or self.itinerary is None:
            parts = self.errors + self.grounding_errors
            raise ValueError("; ".join(parts) or "Invalid itinerary")
        return self.itinerary


def validate_itinerary(data: dict[str, Any] | Itinerary) -> ValidationResult:
    """Parse and structurally validate an itinerary dict or model."""
    try:
        itinerary = (
            data if isinstance(data, Itinerary) else Itinerary.model_validate(data)
        )
    except ValidationError as exc:
        return ValidationResult(
            ok=False,
            errors=[e["msg"] for e in exc.errors()],
        )
    return ValidationResult(ok=True, itinerary=itinerary)


def validate_grounding_rules(itinerary: Itinerary) -> list[str]:
    """Enforce: every stop has OSM id (schema) + citations or explicit uncertainty.

    Tips / reasons that make factual claims should cite RAG sources, or set
    ``uncertainty`` when data is missing. No silent hallucination.
    """
    problems: list[str] = []
    for day in itinerary.days:
        for block_name in ("morning", "afternoon", "evening"):
            block = day.block(block_name)  # type: ignore[arg-type]
            for idx, stop in enumerate(block.stops):
                loc = f"day{day.day_index}.{block_name}.stops[{idx}] ({stop.name})"
                if stop.osm_id <= 0:
                    problems.append(f"{loc}: missing/invalid OSM id")
                if not stop.citations and not stop.uncertainty:
                    problems.append(
                        f"{loc}: must include citations or an explicit uncertainty note"
                    )
    return problems


def load_and_validate_itinerary(
    path: str | Path,
    *,
    enforce_grounding: bool = True,
) -> ValidationResult:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    result = validate_itinerary(data)
    if not result.ok or result.itinerary is None:
        return result
    grounding = (
        validate_grounding_rules(result.itinerary) if enforce_grounding else []
    )
    if grounding:
        return ValidationResult(
            ok=False,
            itinerary=result.itinerary,
            grounding_errors=grounding,
        )
    return result


def itinerary_to_json_schema() -> dict[str, Any]:
    return Itinerary.model_json_schema()
