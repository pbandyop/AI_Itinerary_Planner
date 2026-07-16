"""Phase 3 smoke test: Knowledge RAG retrieval with citations."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from agent.rag.chunking import ensure_chunks
from agent.rag.retrieve import knowledge_search, sources_from_knowledge
from agent.tools.mcp_tools import knowledge_rag_tool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("smoke_rag")


def _safe(text: str) -> str:
    return text.encode("ascii", errors="replace").decode("ascii")


def run_smoke(*, city: str, query: str, topics: list[str], use_tool: bool) -> int:
    print("=== Phase 3 RAG smoke test ===")
    chunks = ensure_chunks(force=False)
    print(f"chunks_available={len(chunks)}")

    if use_tool:
        raw = knowledge_rag_tool.invoke(
            {"city": city, "query": query, "topics": topics, "k": 4}
        )
        payload = json.loads(raw)
        result_notes = payload.get("notes")
        snippets = payload.get("snippets") or []
        missing = payload.get("missing_data")
        sources = []
        for snip in snippets:
            sources.extend(snip.get("citations") or [])
    else:
        result = knowledge_search(city=city, query=query, topics=topics, k=4)
        payload = result.model_dump(mode="json")
        result_notes = result.notes
        snippets = payload.get("snippets") or []
        missing = result.missing_data
        sources = [s.model_dump(mode="json") for s in sources_from_knowledge(result)]

    print(f"city={city} query={query!r} topics={topics}")
    print(f"missing_data={missing} snippets={len(snippets)}")
    if result_notes:
        print(f"notes={_safe(result_notes)}")

    if missing:
        # Explicit missing-data path is a valid Phase 3 behavior for unknown cities.
        if city.lower() in {"atlantis", "narnia"}:
            print("PASS: empty retrieval correctly set missing_data=True")
            return 0
        print("FAIL: expected cited snippets for a corpus city")
        return 1

    if not snippets:
        print("FAIL: no snippets and missing_data=False")
        return 1

    for i, snip in enumerate(snippets, 1):
        citations = snip.get("citations") or []
        if not citations:
            print(f"FAIL: snippet {i} has no citations")
            return 1
        cite = citations[0]
        print(
            f"  [{i}] topic={snip.get('topic')} "
            f"cite={_safe(cite.get('title') or '')} "
            f"id={cite.get('source_id')} "
            f"url={cite.get('url')}"
        )
        print(f"      {_safe((snip.get('text') or '')[:160])}...")

    # Spot-check: every tip traces to a chunk id / URL
    for snip in snippets:
        for cite in snip.get("citations") or []:
            if not cite.get("snippet"):
                print("FAIL: citation missing snippet")
                return 1
            if not (cite.get("source_id") or cite.get("url")):
                print("FAIL: citation missing source_id and url")
                return 1

    print(f"sources_for_ui={len(sources)}")
    print("PASS: RAG tips include citations; sources[] can be filled from retrieval.")
    return 0


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    from agent.rag.paths import repo_root

    load_dotenv(repo_root() / ".env", override=True)
    parser = argparse.ArgumentParser(description="Smoke test Phase 3 Knowledge RAG")
    parser.add_argument("--city", default="Jaipur")
    parser.add_argument(
        "--query",
        default="what if it rains indoor activities food tips",
    )
    parser.add_argument(
        "--topics",
        nargs="*",
        default=["rain", "food"],
    )
    parser.add_argument(
        "--no-tool",
        action="store_true",
        help="Call knowledge_search directly instead of LangChain tool",
    )
    parser.add_argument(
        "--missing-city",
        action="store_true",
        help="Assert missing_data for a city with no corpus",
    )
    args = parser.parse_args(argv)

    if args.missing_city:
        return run_smoke(
            city="Atlantis",
            query="tips",
            topics=["tips"],
            use_tool=not args.no_tool,
        )

    return run_smoke(
        city=args.city,
        query=args.query,
        topics=args.topics,
        use_tool=not args.no_tool,
    )


if __name__ == "__main__":
    sys.exit(main())
