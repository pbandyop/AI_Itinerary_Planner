"""Runnable eval entrypoint (Phase 1 stubs; full checks in Phase 7)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = ROOT / "services" / "agent" / "src"
if str(AGENT_SRC) not in sys.path:
    sys.path.insert(0, str(AGENT_SRC))

from evals.runners.edit_correctness import run_edit_correctness_eval
from evals.runners.feasibility import run_feasibility_eval
from evals.runners.grounding import run_grounding_eval
from evals.runners.validate_fixtures import run_fixture_validation

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Itinerary Planner evals")
    parser.add_argument(
        "--suite",
        choices=["all", "fixtures", "feasibility", "edit", "grounding"],
        default="all",
        help="Which eval suite to run",
    )
    args = parser.parse_args(argv)

    results: list[tuple[str, bool, str]] = []

    if args.suite in ("all", "fixtures"):
        results.append(run_fixture_validation(FIXTURES))
    if args.suite in ("all", "feasibility"):
        results.append(run_feasibility_eval(FIXTURES))
    if args.suite in ("all", "edit"):
        results.append(run_edit_correctness_eval(FIXTURES))
    if args.suite in ("all", "grounding"):
        results.append(run_grounding_eval(FIXTURES))

    print("\n=== Eval summary ===")
    failed = 0
    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}: {detail}")
        if not ok:
            failed += 1

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
