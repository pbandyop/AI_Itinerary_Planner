"""Edit-correctness eval stub (Phase 1) — fixtures only until Phase 7."""

from __future__ import annotations

from pathlib import Path


def run_edit_correctness_eval(fixtures_dir: Path) -> tuple[str, bool, str]:
    _ = fixtures_dir
    return (
        "edit_correctness",
        True,
        "Stub OK — before/after edit fixtures will be added in Phase 7",
    )
