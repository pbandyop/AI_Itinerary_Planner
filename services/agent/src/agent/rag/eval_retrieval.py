"""Evaluate retrieval + rerank strategies for Jaipur multi-source RAG."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document

from agent.rag.chunking import (
    ALL_STRATEGIES,
    docs_to_chunks,
    load_chunking_strategy,
    save_chunking_strategy,
)
from agent.rag.corpus import load_corpus_docs
from agent.rag.eval_chunking import GOLD_QUERIES, _hit, _hours_ok, _rank
from agent.rag.paths import eval_dir, rag_dir
from agent.rag.retrieve import extract_place_terms

logger = logging.getLogger(__name__)

RetrieverFn = Callable[[str, list[Document], int], list[Document]]
RerankFn = Callable[[str, list[Document], int], list[Document]]


def _doc_hay(d: Document) -> str:
    meta = d.metadata or {}
    alias = meta.get("aliases") or ""
    if isinstance(alias, list):
        alias = " ".join(str(a) for a in alias)
    return " ".join(
        [
            d.page_content or "",
            str(meta.get("place_name") or ""),
            str(meta.get("title") or ""),
            str(alias),
            str(meta.get("dataset") or ""),
            str(meta.get("source") or ""),
        ]
    ).lower()


def retrieve_bm25(query: str, chunks: list[Document], fetch_k: int) -> list[Document]:
    r = BM25Retriever.from_documents(chunks)
    r.k = fetch_k
    return list(r.invoke(query))


def retrieve_chroma(query: str, chunks: list[Document], fetch_k: int) -> list[Document]:
    # Fall back to BM25 if Chroma unavailable; uses live index when present.
    try:
        from agent.rag.embeddings import format_retrieval_query
        from agent.rag.ingest import load_chroma_index

        store = load_chroma_index()
        if store is None:
            return retrieve_bm25(query, chunks, fetch_k)
        hits = store.similarity_search(format_retrieval_query(query), k=fetch_k)
        # Prefer in-memory chunk texts when ids match ( fresher eval )
        return hits or retrieve_bm25(query, chunks, fetch_k)
    except Exception:  # noqa: BLE001
        return retrieve_bm25(query, chunks, fetch_k)


def retrieve_hybrid(query: str, chunks: list[Document], fetch_k: int) -> list[Document]:
    a = retrieve_bm25(query, chunks, fetch_k)
    b = retrieve_chroma(query, chunks, fetch_k)
    return _rrf_fuse([a, b], k=fetch_k)


def _rrf_fuse(lists: list[list[Document]], k: int, rrf_k: int = 60) -> list[Document]:
    scores: dict[str, float] = defaultdict(float)
    docs: dict[str, Document] = {}
    for lst in lists:
        for rank, d in enumerate(lst, start=1):
            key = str((d.metadata or {}).get("chunk_id") or "") or (d.page_content or "")[:100]
            scores[key] += 1.0 / (rrf_k + rank)
            docs[key] = d
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [docs[key] for key, _ in ordered[:k]]


def rerank_none(query: str, docs: list[Document], k: int) -> list[Document]:
    return docs[:k]


def _hours_signals(hay: str) -> bool:
    return bool(
        "opening hours" in hay
        or re.search(r"\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?|am|pm)", hay)
    )


def rerank_place_boost(query: str, docs: list[Document], k: int) -> list[Document]:
    places = extract_place_terms(query, "Jaipur")
    hours_q = bool(re.search(r"\b(hours?|timing|opening)\b", query.lower()))

    def score(d: Document) -> float:
        hay = _doc_hay(d)
        s = 0.0
        for p in places:
            if p in hay:
                s += 10.0
                if hay.startswith(p) or re.search(rf"(?:^|\n)\s*\d+\s+{re.escape(p)}", hay):
                    s += 8.0
        ds = str((d.metadata or {}).get("dataset") or "").lower()
        if hours_q:
            if _hours_signals(hay):
                s += 12.0
            # Prefer structured Google/OSM hour cards over prose chapter dumps.
            if ds == "google_places" and _hours_signals(hay):
                s += 45.0
            elif ds == "openstreetmap" and _hours_signals(hay):
                s += 22.0
            elif ds == "curated_places" and _hours_signals(hay):
                s += 14.0
            elif ds == "wikivoyage":
                s -= 8.0
        else:
            if ds == "wikipedia":
                s += 8.0
            if ds == "google_places":
                s += 4.0
            if ds == "curated_places":
                s += 5.0
        q_tokens = [t for t in re.split(r"\W+", query.lower()) if len(t) > 3]
        s += sum(1.5 for t in q_tokens if t in hay)
        return s

    ranked = sorted(enumerate(docs), key=lambda pair: (score(pair[1]), -pair[0]), reverse=True)
    return [d for _, d in ranked[:k]]


def rerank_dataset_aware(query: str, docs: list[Document], k: int) -> list[Document]:
    """Stronger dataset priors + place boost (best production candidate)."""
    places = extract_place_terms(query, "Jaipur")
    hours_q = bool(re.search(r"\b(hours?|timing|opening)\b", query.lower()))
    about_q = bool(re.search(r"\b(tell me|about|what is|describe)\b", query.lower()))

    def score(d: Document) -> float:
        hay = _doc_hay(d)
        meta = d.metadata or {}
        ds = str(meta.get("dataset") or "").lower()
        s = 0.0
        matched = False
        for p in places:
            if p in hay:
                matched = True
                s += 15.0
                if p in str(meta.get("place_name") or "").lower():
                    s += 12.0
                if p in str(meta.get("title") or "").lower():
                    s += 8.0
        if places and not matched:
            s -= 20.0
        if hours_q:
            if "opening hours" in hay:
                s += 20.0
            if re.search(r"\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?|am|pm)", hay):
                s += 15.0
            if ds == "google_places" and _hours_signals(hay):
                s += 50.0
            elif ds == "openstreetmap" and _hours_signals(hay):
                s += 22.0
            elif ds == "curated_places" and _hours_signals(hay):
                s += 12.0
            elif ds == "wikivoyage":
                s -= 10.0
            elif ds == "wikipedia" and not _hours_signals(hay):
                s -= 15.0
        elif about_q:
            if ds == "wikipedia":
                s += 16.0
            elif ds == "wikivoyage":
                s += 10.0
            elif ds == "google_places":
                s += 9.0
            elif ds == "tourism":
                s += 8.0
            elif ds == "curated_places":
                s += 11.0
            elif ds == "openstreetmap":
                s += 6.0
        q_tokens = [t for t in re.split(r"\W+", query.lower()) if len(t) > 3]
        s += sum(1.2 for t in q_tokens if t in hay)
        return s

    ranked = sorted(enumerate(docs), key=lambda pair: (score(pair[1]), -pair[0]), reverse=True)
    out = [d for _, d in ranked[:k]]
    # If place query and no place match in top-k, force lexical place scan from input pool
    if places and not any(any(p in _doc_hay(d) for p in places) for d in out):
        forced = [d for d in docs if any(p in _doc_hay(d) for p in places)]
        if forced:
            return rerank_place_boost(query, forced + out, k)
    return out


def _place_scan_inject(
    query: str, pool: list[Document], retrieved: list[Document], k: int
) -> list[Document]:
    """Mirror production: inject lexical place matches from the full chunk pool."""
    places = extract_place_terms(query, "Jaipur")
    if not places:
        return retrieved
    lexical = [d for d in pool if any(p in _doc_hay(d) for p in places)]
    if not lexical:
        return retrieved
    seen: set[str] = set()
    merged: list[Document] = []
    for d in lexical + list(retrieved):
        key = str((d.metadata or {}).get("chunk_id") or "") or (d.page_content or "")[:80]
        if key in seen:
            continue
        seen.add(key)
        merged.append(d)
        if len(merged) >= max(k * 4, 16):
            break
    return merged


RETRIEVERS: dict[str, RetrieverFn] = {
    "bm25": retrieve_bm25,
    "chroma": retrieve_chroma,
    "hybrid_rrf": retrieve_hybrid,
}

RERANKERS: dict[str, RerankFn] = {
    "none": rerank_none,
    "place_boost": rerank_place_boost,
    "dataset_aware": rerank_dataset_aware,
}


def evaluate_combo(
    *,
    strategy: str,
    retriever_name: str,
    reranker_name: str,
    chunks: list[Document],
    k: int = 3,
    fetch_k: int = 16,
) -> dict[str, Any]:
    retrieve = RETRIEVERS[retriever_name]
    rerank = RERANKERS[reranker_name]
    hits = 0
    eligible = 0
    rr_sum = 0.0
    rr_n = 0
    hours_ok = 0
    hours_n = 0
    google_pref = 0
    google_n = 0
    per_query: list[dict[str, Any]] = []

    for g in GOLD_QUERIES:
        raw = retrieve(g["query"], chunks, fetch_k)
        raw = _place_scan_inject(g["query"], chunks, raw, k)
        docs = rerank(g["query"], raw, k)
        must = list(g["must_any"])
        expect = bool(g.get("expect_hit", True))
        is_hit = _hit(docs, must)
        rank = _rank(docs, must)
        eligible += 1
        if expect:
            if is_hit:
                hits += 1
            if rank:
                rr_sum += 1.0 / rank
            rr_n += 1
        else:
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
            # Prefer Google Places on hours when present in top hit
            google_n += 1
            top_ds = str((docs[0].metadata or {}).get("dataset") or "") if docs else ""
            if top_ds == "google_places" and hours_pass:
                google_pref += 1

        per_query.append(
            {
                "id": g["id"],
                "hit": is_hit,
                "rank": rank,
                "expect_hit": expect,
                "hours_pass": hours_pass,
                "top_dataset": str((docs[0].metadata or {}).get("dataset") or "")
                if docs
                else None,
                "top_title": str((docs[0].metadata or {}).get("title") or "")[:60]
                if docs
                else None,
            }
        )

    return {
        "chunk_strategy": strategy,
        "retriever": retriever_name,
        "reranker": reranker_name,
        "num_chunks": len(chunks),
        "hit_at_3": hits / eligible if eligible else 0.0,
        "mrr": rr_sum / rr_n if rr_n else 0.0,
        "hours_acc": hours_ok / hours_n if hours_n else 1.0,
        "google_hours_pref": google_pref / google_n if google_n else 0.0,
        "per_query": per_query,
    }


def pick_winner(results: list[dict[str, Any]]) -> dict[str, Any]:
    return sorted(
        results,
        key=lambda r: (
            r["hit_at_3"],
            r["mrr"],
            r["hours_acc"],
            r.get("google_hours_pref", 0.0),
            1 if r["reranker"] == "dataset_aware" else 0,
            1 if r["retriever"] == "hybrid_rrf" else 0,
            1 if r["chunk_strategy"] == "place_atomic" else 0,
        ),
        reverse=True,
    )[0]


def save_retrieval_strategy(payload: dict[str, Any]) -> Path:
    path = rag_dir() / "retrieval_strategy.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Evaluate RAG retrieval/rerank combos")
    parser.add_argument(
        "--chunk-strategies",
        nargs="*",
        default=["place_atomic", "section_aware", "recursive_900_120"],
    )
    parser.add_argument(
        "--retrievers",
        nargs="*",
        default=list(RETRIEVERS.keys()),
    )
    parser.add_argument(
        "--rerankers",
        nargs="*",
        default=list(RERANKERS.keys()),
    )
    args = parser.parse_args(argv)

    corpus = load_corpus_docs()
    results: list[dict[str, Any]] = []
    for cs in args.chunk_strategies:
        chunks = docs_to_chunks(corpus, strategy=cs)
        city_chunks = [
            d
            for d in chunks
            if str((d.metadata or {}).get("city") or "").lower() == "jaipur"
        ] or chunks
        for ret in args.retrievers:
            for rr in args.rerankers:
                logger.info("eval chunk=%s retriever=%s rerank=%s", cs, ret, rr)
                results.append(
                    evaluate_combo(
                        strategy=cs,
                        retriever_name=ret,
                        reranker_name=rr,
                        chunks=city_chunks,
                    )
                )

    winner = pick_winner(results)
    out_dir = eval_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {"results": results, "winner": winner}
    (out_dir / "retrieval_results.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    save_chunking_strategy(
        winner["chunk_strategy"],
        scores={
            "hit_at_3": winner["hit_at_3"],
            "mrr": winner["mrr"],
            "hours_acc": winner["hours_acc"],
            "num_chunks": winner["num_chunks"],
        },
    )
    save_retrieval_strategy(
        {
            "retriever": winner["retriever"],
            "reranker": winner["reranker"],
            "chunk_strategy": winner["chunk_strategy"],
            "scores": {
                "hit_at_3": winner["hit_at_3"],
                "mrr": winner["mrr"],
                "hours_acc": winner["hours_acc"],
                "google_hours_pref": winner.get("google_hours_pref"),
            },
        }
    )
    print(
        json.dumps(
            {
                "winner": {
                    "chunk_strategy": winner["chunk_strategy"],
                    "retriever": winner["retriever"],
                    "reranker": winner["reranker"],
                    "hit_at_3": winner["hit_at_3"],
                    "mrr": winner["mrr"],
                    "hours_acc": winner["hours_acc"],
                    "google_hours_pref": winner.get("google_hours_pref"),
                },
                "num_combos": len(results),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
