"""Build the full Jaipur multi-source RAG corpus."""

from __future__ import annotations

import argparse
import logging
import sys

from agent.rag.fetch_corpus import fetch_city
from agent.rag.fetch_google_places import fetch_google_places
from agent.rag.fetch_osm_facts import fetch_osm_facts
from agent.rag.fetch_tourism import fetch_tourism_pages
from agent.rag.fetch_wikipedia_places import fetch_wikipedia_places
from agent.rag.paths import corpus_dir

logger = logging.getLogger(__name__)


def build_corpus(
    *,
    skip_wikivoyage: bool = False,
    skip_wikipedia: bool = False,
    skip_osm: bool = False,
    skip_tourism: bool = False,
    skip_google: bool = False,
    sleep: float = 1.2,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    out = corpus_dir()
    out.mkdir(parents=True, exist_ok=True)

    if not skip_wikivoyage:
        path = fetch_city("Jaipur", "Jaipur", out_dir=out, sleep_s=sleep)
        counts["wikivoyage"] = 1 if path else 0

    if not skip_wikipedia:
        paths = fetch_wikipedia_places(sleep_s=sleep)
        counts["wikipedia"] = len(paths)

    if not skip_osm:
        path = fetch_osm_facts()
        # count places inside file
        import json

        raw = json.loads(path.read_text(encoding="utf-8"))
        counts["osm"] = len(raw.get("places") or [])

    if not skip_tourism:
        path = fetch_tourism_pages(sleep_s=sleep)
        import json

        raw = json.loads(path.read_text(encoding="utf-8"))
        counts["tourism"] = len(raw.get("places") or [])

    if not skip_google:
        path = fetch_google_places(sleep_s=min(sleep, 0.4))
        import json

        raw = json.loads(path.read_text(encoding="utf-8"))
        counts["google_places"] = len(raw.get("places") or [])

    # curated is committed; always present
    curated = out / "curated" / "jaipur_places.json"
    if curated.is_file():
        import json

        raw = json.loads(curated.read_text(encoding="utf-8"))
        counts["curated_places"] = len(raw.get("places") or [])
    else:
        counts["curated_places"] = 0

    return counts


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Build Jaipur multi-source RAG corpus")
    parser.add_argument("--skip-wikivoyage", action="store_true")
    parser.add_argument("--skip-wikipedia", action="store_true")
    parser.add_argument("--skip-osm", action="store_true")
    parser.add_argument("--skip-tourism", action="store_true")
    parser.add_argument("--skip-google", action="store_true")
    parser.add_argument("--sleep", type=float, default=1.2)
    args = parser.parse_args(argv)

    counts = build_corpus(
        skip_wikivoyage=args.skip_wikivoyage,
        skip_wikipedia=args.skip_wikipedia,
        skip_osm=args.skip_osm,
        skip_tourism=args.skip_tourism,
        skip_google=args.skip_google,
        sleep=args.sleep,
    )
    print("corpus_counts:", counts)
    print("corpus_dir:", corpus_dir())
    return 0


if __name__ == "__main__":
    sys.exit(main())
