"""Edit-correctness eval — only the intended day/block may change."""

from __future__ import annotations

import json
import json
from pathlib import Path
from typing import Any

from agent.nodes.edit_apply import apply_edit_patches
from agent.schemas.edits import EditPatch
from agent.schemas.itinerary import Itinerary
from agent.schemas.specialists import POICandidate


def _day_fingerprint(itin: Itinerary, day_index: int) -> list[str]:
    day = next(d for d in itin.days if d.day_index == day_index)
    out: list[str] = []
    for bname in ("morning", "afternoon", "evening"):
        block = day.block(bname)  # type: ignore[arg-type]
        for s in block.stops:
            out.append(f"{bname}:{s.name}:{s.category}:{s.osm_id}")
    return out


def _load_pois(raw: list[dict[str, Any]] | None) -> list[POICandidate]:
    if not raw:
        return []
    return [POICandidate.model_validate(p) for p in raw]


def _run_case(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    problems: list[str] = []
    before = Itinerary.model_validate(data["before"])
    patches = [EditPatch.model_validate(p) for p in data["patches"]]
    pois = _load_pois(data.get("candidate_pois"))
    expect_changed = set(int(x) for x in data.get("expect_changed_days") or [])
    if not expect_changed and patches:
        expect_changed = {p.target.day for p in patches}

    after, notes = apply_edit_patches(
        before, patches, candidate_pois=pois or None
    )

    for day in before.days:
        idx = day.day_index
        before_fp = _day_fingerprint(before, idx)
        after_fp = _day_fingerprint(after, idx)
        changed = before_fp != after_fp
        if idx in expect_changed:
            if not changed:
                problems.append(
                    f"{path.name}: expected Day {idx} to change "
                    f"(ops={[p.operation for p in patches]}); notes={notes[:2]}"
                )
        else:
            if changed:
                problems.append(
                    f"{path.name}: Day {idx} changed but was not a target "
                    f"(before={before_fp} after={after_fp})"
                )

    if "expect_target_stop_delta" in data and expect_changed:
        target = next(iter(expect_changed))
        before_n = len(next(d for d in before.days if d.day_index == target).all_stops)
        after_n = len(next(d for d in after.days if d.day_index == target).all_stops)
        delta = after_n - before_n
        want = int(data["expect_target_stop_delta"])
        if want < 0:
            if delta >= 0:
                problems.append(
                    f"{path.name}: Day {target} stop delta {delta}, expected < 0 "
                    f"(≤{want})"
                )
        elif want > 0:
            if delta < want:
                problems.append(
                    f"{path.name}: Day {target} stop delta {delta}, expected ≥{want}"
                )
        elif data.get("strict_delta") and delta != 0:
            problems.append(
                f"{path.name}: Day {target} stop delta {delta}, expected 0"
            )

    return problems


def run_edit_correctness_eval(fixtures_dir: Path) -> tuple[str, bool, str]:
    edits_dir = fixtures_dir / "edits"
    cases = sorted(edits_dir.glob("*.json")) if edits_dir.is_dir() else []
    # Also allow edit_*.json beside golden itineraries.
    cases += sorted(fixtures_dir.glob("edit_*.json"))

    if not cases:
        return (
            "edit_correctness",
            False,
            f"No edit fixtures in {edits_dir} (or edit_*.json)",
        )

    failures: list[str] = []
    for path in cases:
        try:
            failures.extend(_run_case(path))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{path.name}: {exc.__class__.__name__}: {exc}")

    if failures:
        return ("edit_correctness", False, " | ".join(failures))
    return (
        "edit_correctness",
        True,
        f"OK — {len(cases)} edit case(s); only target day(s) changed",
    )
