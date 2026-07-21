"""E2E: four required voice edits — only the affected day/block changes."""

from __future__ import annotations

import json
import os
import time
from copy import deepcopy

os.environ.setdefault("REVIEWER_LLM", "false")
os.environ.setdefault("ORCHESTRATOR_LLM", "false")

from agent.main import InvokeRequest, invoke
from agent.schemas.itinerary import DayPlan, Itinerary, Stop, TimeBlock, TripConstraints


def mk(
    name: str,
    oid: int,
    cat: str = "heritage",
    travel: int | None = None,
) -> Stop:
    return Stop(
        name=name,
        category=cat,
        duration_min=60,
        osm_type="way",
        osm_id=oid,
        lat=26.91,
        lon=75.79,
        reason="seed",
        uncertainty="test",
        travel_to_next_min=travel,
        travel_to_next_km=(travel or 0) / 10 if travel else None,
        travel_to_next_mode="car" if travel else None,
    )


def fingerprint(itin: dict, day_index: int) -> list[str]:
    day = next(d for d in itin["days"] if d["day_index"] == day_index)
    out: list[str] = []
    for b in ("morning", "afternoon", "evening"):
        for s in (day.get(b) or {}).get("stops") or []:
            out.append(f"{b}:{s['name']}:{s.get('category')}")
    return out


def build_itin() -> dict:
    trip = TripConstraints(
        city="Jaipur",
        country="India",
        num_days=3,
        pace="packed",
        interests=["food", "heritage", "park"],
        confirmed=True,
        days_known=True,
        pace_known=True,
        interests_known=True,
    )
    days = [
        DayPlan(
            day_index=1,
            morning=TimeBlock(
                time_of_day="morning",
                stops=[
                    mk("Hawa Mahal", 101, "heritage", 12),
                    mk("Jantar Mantar", 102, "heritage", 15),
                ],
            ),
            afternoon=TimeBlock(
                time_of_day="afternoon",
                stops=[mk("City Palace", 103, "heritage", 20)],
            ),
            evening=TimeBlock(
                time_of_day="evening",
                stops=[mk("Central Park", 104, "park")],
            ),
        ),
        DayPlan(
            day_index=2,
            morning=TimeBlock(
                time_of_day="morning",
                stops=[
                    mk("Amber Fort", 201, "heritage", 18),
                    mk("Jaigarh Fort", 202, "heritage", 10),
                    mk("Amer Palace", 203, "heritage", 25),
                ],
            ),
            afternoon=TimeBlock(
                time_of_day="afternoon",
                stops=[
                    mk("Jal Mahal", 204, "heritage", 30),
                    mk("Nahargarh", 205, "heritage", 40),
                ],
            ),
            evening=TimeBlock(
                time_of_day="evening",
                stops=[mk("Kanak Vrindavan", 206, "park")],
            ),
        ),
        DayPlan(
            day_index=3,
            morning=TimeBlock(
                time_of_day="morning",
                stops=[mk("Govind Dev Ji", 301, "temple", 12)],
            ),
            afternoon=TimeBlock(
                time_of_day="afternoon",
                stops=[mk("Bapu Bazaar", 302, "market", 50)],
            ),
            evening=TimeBlock(
                time_of_day="evening",
                stops=[mk("Albert Hall", 303, "museum")],
            ),
        ),
    ]
    # Day 3 has the longest hop (50) → undirected reduce-travel should pick Day 3.
    return Itinerary(trip=trip, days=days).model_dump(mode="json")


def _heaviest_travel_day(itin: dict) -> int:
    best_day, best_score = 1, -1
    for day in itin["days"]:
        score = 0
        n = 0
        for b in ("morning", "afternoon", "evening"):
            for s in (day.get(b) or {}).get("stops") or []:
                score += int(s.get("travel_to_next_min") or 0)
                n += 1
        # Prefer more stops when travel totals tie.
        key = score * 100 + n
        if key > best_score:
            best_score = key
            best_day = int(day["day_index"])
    return best_day


CASES = [
    {
        "msg": "Make Day 2 more relaxed.",
        "op": "relax_block",
        "must_change": {2},
        "check": "relaxed",
    },
    {
        "msg": "Make day one relaxed",
        "op": "relax_block",
        "must_change": {1},
        "check": "relaxed",
    },
    {
        "msg": "Make day three relaxed.",
        "op": "relax_block",
        "must_change": {3},
        "check": "relaxed",
    },
    {
        "msg": "Swap the Day 1 evening plan to something indoors.",
        "op": "make_indoor",
        "must_change": {1},
        "check": "indoor",
    },
    {
        "msg": "Reduce travel time.",
        "op": "reduce_travel",
        "must_change": "heaviest",  # auto_day → day with most travel
        "check": "travel",
    },
    {
        "msg": "Add one famous local food place.",
        "op": "add_stop",
        "must_change": {1},
        "check": "food",
    },
]


def run_case(base: dict, case: dict) -> None:
    itin = deepcopy(base)
    before = {d: fingerprint(itin, d) for d in (1, 2, 3)}
    expect = case["must_change"]
    if expect == "heaviest":
        expect = {_heaviest_travel_day(itin)}
    t0 = time.time()
    r = invoke(
        InvokeRequest(
            user_message=case["msg"],
            session_id=f"voice-edit-{case['op']}",
            previous_itinerary=itin,
            merged_itinerary=itin,
        )
    )
    elapsed = time.time() - t0
    out = r.merged_itinerary or {}
    after = {d: fingerprint(out, d) for d in (1, 2, 3)}
    changed = {d for d in (1, 2, 3) if before[d] != after[d]}

    print("=" * 72)
    print(f"MSG: {case['msg']}")
    print(f"  {elapsed:.1f}s intent={r.intent} status={r.safety_status}")
    print(f"  reply: {(r.user_reply or '')[:160].replace(chr(10), ' | ')}")
    print(f"  changed days: {sorted(changed)} (expect {sorted(expect)})")
    for d in (1, 2, 3):
        mark = "CHANGED" if d in changed else "same"
        print(f"  Day {d} [{mark}]: {after[d]}")

    assert r.intent == "edit", f"expected edit, got {r.intent}"
    assert changed == expect, (
        f"scope fail for {case['msg']!r}: changed={changed} expected={expect}"
    )

    if case["check"] == "relaxed":
        assert len(after[2]) < len(before[2]), after[2]
    elif case["check"] == "indoor":
        eve = [x for x in after[1] if x.startswith("evening:")]
        assert eve, after[1]
        assert not any(":park" in x for x in eve), eve
        assert eve != [x for x in before[1] if x.startswith("evening:")], eve
    elif case["check"] == "travel":
        day = next(iter(expect))
        assert len(after[day]) < len(before[day]), (before[day], after[day])
    elif case["check"] == "food":
        assert any(":food" in x for x in after[1]), after[1]

    print("  OK")


def main() -> None:
    base = build_itin()
    print("Base Day fingerprints:")
    for d in (1, 2, 3):
        print(f"  Day {d}: {fingerprint(base, d)}")
    failures: list[str] = []
    for case in CASES:
        try:
            run_case(base, case)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{case['msg']}: {exc}")
            print(f"  FAIL: {exc}")
    if failures:
        print("\nFAILED:")
        for f in failures:
            print(" -", f)
        raise SystemExit(1)
    print("\nAll four voice edits passed.")


if __name__ == "__main__":
    main()
