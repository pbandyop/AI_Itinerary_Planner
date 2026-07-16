"""Verify add-food Day-1 edit succeeds with live Nominatim backup."""

from __future__ import annotations

import time

from agent.main import InvokeRequest, invoke
from agent.mcp.poi_search import nominatim_category_search
from agent.schemas.itinerary import DayPlan, Itinerary, Stop, TimeBlock, TripConstraints


def mk(name: str, oid: int, cat: str = "museum") -> Stop:
    return Stop(
        name=name,
        category=cat,
        duration_min=60,
        osm_type="way",
        osm_id=oid,
        lat=26.9,
        lon=75.8,
        reason="t",
        uncertainty="u",
    )


def day(i: int, names: list[str], cat: str = "museum") -> DayPlan:
    stops = [mk(n, i * 100 + k + 1, cat) for k, n in enumerate(names)]
    return DayPlan(
        day_index=i,
        morning=TimeBlock(time_of_day="morning", stops=stops[:1]),
        afternoon=TimeBlock(
            time_of_day="afternoon", stops=stops[1:2] if len(stops) > 1 else []
        ),
        evening=TimeBlock(time_of_day="evening"),
    )


def main() -> None:
    foods = nominatim_category_search(city="Jaipur", interest="food", limit=5)
    print("Nominatim food:", [(p.name, p.category) for p in foods])

    trip = TripConstraints(
        city="Jaipur",
        country="India",
        num_days=3,
        pace="packed",
        interests=["food", "museum", "shopping"],
        confirmed=True,
        days_known=True,
        pace_known=True,
        interests_known=True,
    )
    itin = Itinerary(
        trip=trip,
        days=[
            day(1, ["Amrapali Museum", "Albert Hall"]),
            day(2, ["Pink city", "Mubarak"]),
            day(3, ["Bapu Bazaar", "Big Bazaar"], "market"),
        ],
    ).model_dump(mode="json")

    t0 = time.time()
    r = invoke(
        InvokeRequest(
            user_message="add food to day one",
            session_id="add-food-fix",
            previous_itinerary=itin,
            merged_itinerary=itin,
        )
    )
    print(f"{time.time() - t0:.1f}s intent={r.intent}")
    print((r.user_reply or "")[:220].replace("\n", " | "))
    days = (r.merged_itinerary or {}).get("days") or []
    d1 = next((d for d in days if d.get("day_index") == 1), None)
    names: list[str] = []
    if d1:
        for b in ("morning", "afternoon", "evening"):
            for s in (d1.get(b) or {}).get("stops") or []:
                names.append(f"{s['name']}:{s.get('category')}")
    print("day1", names)
    assert any(n.endswith(":food") for n in names), names
    print("OK food added")


if __name__ == "__main__":
    main()
