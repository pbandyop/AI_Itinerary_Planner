"""Compare legacy vs hybrid itinerary selection on a fixed POI pool."""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

from agent.mcp.itinerary_builder import build_itinerary
from agent.mcp.poi_shortlist import shortlist_pois
from agent.schemas.specialists import POICandidate

# Attempt A live pool from session (Overpass heritage/shopping + Nominatim food).
# Approximate coords enable geographic day clustering for a fair hybrid demo.
COORDS: dict[str, tuple[float, float]] = {
    "Hawa Mahal": (26.9239, 75.8267),
    "Jantar Mantar": (26.9247, 75.8246),
    "City Palace": (26.9258, 75.8236),
    "Amber Fort": (26.9855, 75.8513),
    "Amer Palace": (26.9855, 75.8513),
    "Jaighar Fort": (26.9124, 75.8150),
    "Bapu Bazaar": (26.9170, 75.8200),
    "Moon Gate": (26.9255, 75.8260),
    "Sun Gate": (26.9260, 75.8240),
    "Jal Mahal": (26.9539, 75.8460),
    "Neota Dam": (26.8200, 75.7200),
    "Amrapali Jewels": (26.9120, 75.7870),
    "Jayanti Market": (26.9125, 75.7880),
    "Annapurna Canteen": (26.9150, 75.8200),
    "Kanha": (26.9120, 75.8100),
    "Saras Parlour": (26.9050, 75.8000),
    "Diwan-i-Am": (26.9265, 75.8230),
    "Diwan-i-Khas": (26.9268, 75.8235),
    "Nahargarh Fort": (26.9373, 75.8155),
}

RAW = [
    ("market", 11.3, "Amrapali Jewels", "node", 3842386472),
    ("heritage", 27.3, "Hawa Mahal", "node", 542886858),
    ("food", 15.0, "Annapurna Canteen", "way", 361514403),
    ("market", 11.3, "Balaji enterprises", "node", 4395594792),
    ("heritage", 27.3, "Jaighar Fort", "node", 542960968),
    ("food", 15.0, "Brown Bites bakes and cafe", "node", 6908274785),
    ("market", 11.3, "Bapu Bazaar", "node", 5240926022),
    ("heritage", 27.3, "Jantar Mantar", "node", 542886857),
    ("food", 15.0, "CHANMAN", "node", 6158494085),
    ("market", 11.3, "Big Bazaar", "node", 4351104822),
    ("heritage", 25.8, "Amber Fort", "node", 2598753117),
    ("food", 15.0, "Cafe Coffee Day", "node", 8084157086),
    ("market", 11.3, "Big Bazar", "node", 3083894535),
    ("heritage", 25.8, "Jaivan Canon", "node", 8384426537),
    ("food", 15.0, "Cafe BAE", "node", 9495760718),
    ("market", 11.3, "CYU Cloth Yourself Urban", "node", 4889780021),
    ("heritage", 25.8, "Neota Dam", "node", 8171738003),
    ("food", 15.0, "Chaudhary Canteen", "node", 8044543830),
    ("market", 11.3, "Chandni Crafts", "node", 3806485613),
    ("heritage", 24.3, "Moon Gate", "node", 5079243358),
    ("food", 15.0, "Ganesh Resturent", "node", 12151385705),
    ("market", 11.3, "Cotton Curio", "node", 2684677444),
    ("heritage", 24.3, "Sun Gate", "node", 5079237889),
    ("food", 15.0, "Kanha", "node", 3476221910),
    ("market", 11.3, "Cottons", "node", 2684743255),
    ("heritage", 22.8, "Amer Palace", "node", 9628392199),
    ("food", 15.0, "Mr Macchiato", "node", 13602800764),
    ("market", 11.3, "Deva collection pashmina", "node", 4781092322),
    ("heritage", 22.8, "Clay Botik - Pottery Studio", "node", 4487887690),
    ("food", 15.0, "Nibs Restaurant Cafe", "node", 6766661285),
    ("market", 11.3, "Gaurav Tower I", "node", 2394723956),
    ("heritage", 22.8, "Diwan-i-Am", "node", 5079249026),
    ("food", 15.0, "R.K Hotel", "node", 13651411902),
    ("market", 11.3, "Gaurav Tower II", "node", 2394724345),
    ("heritage", 22.8, "Diwan-i-Khas", "node", 10747713654),
    ("food", 15.0, "Roseberry", "node", 10917746182),
    ("market", 11.3, "Jayanti Market", "node", 3842381190),
    ("heritage", 22.8, "Elefantastic", "node", 4706056289),
    ("food", 15.0, "Saras Parlour", "node", 3081602535),
    ("market", 11.3, "Jewels Emporium", "node", 3842381185),
]


def make_pool() -> list[POICandidate]:
    out: list[POICandidate] = []
    for cat, score, name, osm_type, osm_id in RAW:
        lat = lon = None
        if name in COORDS:
            lat, lon = COORDS[name]
        out.append(
            POICandidate(
                name=name,
                osm_type=osm_type,  # type: ignore[arg-type]
                osm_id=osm_id,
                category=cat,
                rank_score=score,
                lat=lat,
                lon=lon,
            )
        )
    return out


def summarize(label: str, draft) -> dict:
    days = []
    names = []
    cats: list[str] = []
    for day in draft.days:
        stops = []
        for bn in ("morning", "afternoon", "evening"):
            for s in getattr(day, bn).stops:
                names.append(s.name)
                cats.append((s.category or "?").lower())
                stops.append(
                    {
                        "block": bn,
                        "name": s.name,
                        "category": s.category,
                        "duration_min": s.duration_min,
                    }
                )
        days.append({"day": day.day_index, "theme": day.theme, "stops": stops})
    return {
        "label": label,
        "notes": draft.notes,
        "category_counts": dict(Counter(cats)),
        "names": names,
        "days": days,
    }


def print_summary(payload: dict) -> None:
    print(f"\n===== {payload['label']} =====")
    print("cats:", payload["category_counts"])
    print("names:", payload["names"])
    for day in payload["days"]:
        print(f"--- Day {day['day']} ---")
        for s in day["stops"]:
            print(f"  {s['block']:10} [{s['category']}] {s['name']} ({s['duration_min']}m)")
    print("notes:", (payload["notes"] or "")[:280])


def main() -> None:
    interests = ["shopping", "heritage", "food"]
    pool = make_pool()
    print(f"Pool size={len(pool)} cats={dict(Counter(p.category for p in pool))}")

    # Legacy path (old diversify fill) — same pool, no shortlist stage
    os.environ["ITINERARY_STRATEGY"] = "legacy"
    legacy = build_itinerary(
        city="Jaipur",
        candidate_pois=list(pool),
        num_days=2,
        pace="relaxed",
        interests=interests,
        selection_mode="legacy",
    )
    legacy_sum = summarize("LEGACY (old diversify)", legacy)

    # Hybrid: shortlist then pack
    os.environ["ITINERARY_STRATEGY"] = "hybrid"
    short = shortlist_pois(
        city="Jaipur",
        candidate_pois=list(pool),
        interests=interests,
        num_days=2,
        pace="relaxed",
    )
    print(f"\nShortlist size={len(short.pois)} notes={short.notes}")
    for i, p in enumerate(short.pois, 1):
        print(f"  {i:2}. [{p.category}] {p.name} score_rank={p.rank_score}")

    hybrid = build_itinerary(
        city="Jaipur",
        candidate_pois=list(short.pois),
        num_days=2,
        pace="relaxed",
        interests=interests,
        selection_mode="preselected",
    )
    hybrid_sum = summarize("HYBRID (shortlist+quotas+clusters)", hybrid)

    print_summary(legacy_sum)
    print_summary(hybrid_sum)

    out = Path("data/rag/eval/itinerary_strategy_compare.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "query": "Plan a 2 day trip to Jaipur, Relaxed, interest: shopping, heritage and food",
                "pool_size": len(pool),
                "shortlist": [
                    {"name": p.name, "category": p.category, "rank_score": p.rank_score}
                    for p in short.pois
                ],
                "legacy": legacy_sum,
                "hybrid": hybrid_sum,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
