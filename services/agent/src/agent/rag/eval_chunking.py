"""Evaluate chunking strategies on a Jaipur RAG gold query set."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from typing import Any

from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document

from agent.rag.chunking import (
    ALL_STRATEGIES,
    docs_to_chunks,
    save_chunking_strategy,
)
from agent.rag.corpus import load_corpus_docs
from agent.rag.paths import eval_dir

logger = logging.getLogger(__name__)

_HOUR_RE = re.compile(
    r"\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b|"
    r"opening hours\s*:",
    re.I,
)

GOLD_QUERIES: list[dict[str, Any]] = [
    {
        "id": "nevta",
        "query": "Tell me more about Nevta Dam",
        "must_any": ["nevta", "neota", "niota"],
        "expect_hit": True,
    },
    {
        "id": "niota_alias",
        "query": "Tell me more about Niota Dam",
        "must_any": ["nevta", "neota", "niota"],
        "expect_hit": True,
    },
    {
        "id": "akshardham",
        "query": "Tell me more about Akshardham Temple",
        "must_any": ["akshardham"],
        "expect_hit": True,
    },
    {
        "id": "jantar_hours",
        "query": "What are the opening hours for Jantar Mantar?",
        "must_any": ["jantar mantar"],
        "expect_hours": True,
        "expect_hit": True,
    },
    {
        "id": "city_palace_hours",
        "query": "What are the opening hours for City Palace?",
        "must_any": ["city palace"],
        "expect_hours": True,
        "expect_hit": True,
    },
    {
        "id": "hawa_hours",
        "query": "What are the opening hours for Hawa Mahal?",
        "must_any": ["hawa mahal"],
        "expect_hours": True,
        "expect_hit": True,
    },
    {
        "id": "moon_gate",
        "query": "Tell me about Moon Gate",
        "must_any": ["moon gate"],
        "expect_hit": True,
    },
    {
        "id": "hawa",
        "query": "Tell me more about Hawa Mahal",
        "must_any": ["hawa mahal"],
        "expect_hit": True,
    },
    {
        "id": "birla",
        "query": "Tell me more about Birla Mandir",
        "must_any": ["birla"],
        "expect_hit": True,
    },
    {
        "id": "city_palace",
        "query": "Tell me more about City Palace",
        "must_any": ["city palace"],
        "expect_hit": True,
    },
    {
        "id": "amarnath",
        "query": "What is the opening hour of Amarnath Cafe?",
        "must_any": ["amarnath"],
        "expect_hit": True,
        "expect_hours": False,  # may lack clock times; still should retrieve card
    },
    {
        "id": "amber",
        "query": "Tell me more about Amber Fort",
        "must_any": ["amber", "amer"],
        "expect_hit": True,
    },
    {
        "id": "jal_mahal",
        "query": "Tell me more about Jal Mahal",
        "must_any": ["jal mahal"],
        "expect_hit": True,
    },
    {
        "id": "badrinath_miss",
        "query": "Tell me more about Badrinath Temple",
        "must_any": ["badrinath"],
        "expect_hit": False,
    },
]


def _hit(docs: list[Document], must_any: list[str]) -> bool:
    blob = " ".join((d.page_content or "").lower() for d in docs)
    meta = " ".join(
        str((d.metadata or {}).get("place_name") or "").lower()
        + " "
        + str((d.metadata or {}).get("title") or "").lower()
        for d in docs
    )
    hay = blob + " " + meta
    return any(tok in hay for tok in must_any)


def _rank(docs: list[Document], must_any: list[str]) -> int | None:
    for i, d in enumerate(docs, start=1):
        hay = (
            (d.page_content or "").lower()
            + " "
            + str((d.metadata or {}).get("place_name") or "").lower()
            + " "
            + str((d.metadata or {}).get("title") or "").lower()
        )
        if any(tok in hay for tok in must_any):
            return i
    return None


def _hours_ok(docs: list[Document]) -> bool:
    return any(_HOUR_RE.search(d.page_content or "") for d in docs)


def evaluate_strategy(strategy: str, *, k: int = 3) -> dict[str, Any]:
    from agent.rag.retrieve import extract_place_terms

    corpus = load_corpus_docs()
    chunks = docs_to_chunks(corpus, strategy=strategy)
    if not chunks:
        return {
            "strategy": strategy,
            "num_chunks": 0,
            "hit_at_3": 0.0,
            "mrr": 0.0,
            "hours_acc": 0.0,
            "per_query": [],
        }

    city_chunks = [
        d
        for d in chunks
        if str((d.metadata or {}).get("city") or "").lower() == "jaipur"
    ] or chunks
    retriever = BM25Retriever.from_documents(city_chunks)
    retriever.k = max(k * 8, 16)

    hits = 0
    eligible = 0
    rr_sum = 0.0
    rr_n = 0
    hours_ok = 0
    hours_n = 0
    per_query: list[dict[str, Any]] = []

    for g in GOLD_QUERIES:
        places = extract_place_terms(g["query"], "Jaipur")
        raw_docs = list(retriever.invoke(g["query"]))
        # Place-aware rerank (mirrors production knowledge_search boost)
        ranked = sorted(
            raw_docs,
            key=lambda d: sum(
                10
                for p in places
                if p
                in (
                    (d.page_content or "")
                    + " "
                    + str((d.metadata or {}).get("place_name") or "")
                    + " "
                    + str((d.metadata or {}).get("title") or "")
                ).lower()
            ),
            reverse=True,
        )
        docs = ranked[:k]
        must = list(g["must_any"])
        expect = bool(g.get("expect_hit", True))
        is_hit = _hit(docs, must)
        rank = _rank(docs, must)
        if expect:
            eligible += 1
            if is_hit:
                hits += 1
            if rank:
                rr_sum += 1.0 / rank
            rr_n += 1
        else:
            eligible += 1
            if not is_hit:
                hits += 1
            rr_n += 1
            rr_sum += 1.0 if not is_hit else 0.0

        hours_pass = None
        if g.get("expect_hours"):
            hours_n += 1
            hours_pass = bool(is_hit and _hours_ok(docs))
            if hours_pass:
                hours_ok += 1

        per_query.append(
            {
                "id": g["id"],
                "hit": is_hit,
                "rank": rank,
                "expect_hit": expect,
                "hours_pass": hours_pass,
            }
        )

    return {
        "strategy": strategy,
        "num_chunks": len(chunks),
        "hit_at_3": hits / eligible if eligible else 0.0,
        "mrr": rr_sum / rr_n if rr_n else 0.0,
        "hours_acc": hours_ok / hours_n if hours_n else 1.0,
        "per_query": per_query,
    }


def pick_winner(results: list[dict[str, Any]]) -> dict[str, Any]:
    # Prefer place_atomic on ties — best for mixed structured + prose corpora.
    return sorted(
        results,
        key=lambda r: (
            r["hit_at_3"],
            r["mrr"],
            r["hours_acc"],
            1 if r["strategy"] == "place_atomic" else 0,
            -r["num_chunks"],
        ),
        reverse=True,
    )[0]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Evaluate RAG chunking strategies")
    parser.add_argument(
        "--strategies",
        nargs="*",
        default=list(ALL_STRATEGIES),
    )
    args = parser.parse_args(argv)

    results = [evaluate_strategy(s) for s in args.strategies]
    winner = pick_winner(results)
    out_dir = eval_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "chunking_results.json"
    payload = {"results": results, "winner": winner["strategy"]}
    results_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    save_chunking_strategy(
        winner["strategy"],
        scores={
            "hit_at_3": winner["hit_at_3"],
            "mrr": winner["mrr"],
            "hours_acc": winner["hours_acc"],
            "num_chunks": winner["num_chunks"],
        },
    )
    print(json.dumps(payload, indent=2))
    print(f"winner={winner['strategy']} hit@3={winner['hit_at_3']:.3f} mrr={winner['mrr']:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
