"""Build and persist LangChain Chroma vector store for Knowledge RAG."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

from langchain_core.documents import Document

from agent.rag.chunking import ensure_chunks
from agent.rag.embeddings import resolve_embeddings
from agent.rag.paths import chroma_dir

logger = logging.getLogger(__name__)

COLLECTION_NAME = "india_travel_knowledge"


def build_chroma_index(
    documents: list[Document] | None = None,
    *,
    force: bool = False,
) -> tuple[Path | None, str]:
    """
    Embed chunks into a persistent Chroma store under data/rag/chroma/.

    Returns (persist_path, backend). backend is openai|huggingface|bm25.
    """
    docs = documents if documents is not None else ensure_chunks(force=force)
    if not docs:
        logger.error("No RAG chunks available — run corpus fetch first.")
        return None, "bm25"

    embeddings, backend = resolve_embeddings()
    if embeddings is None:
        logger.info(
            "No embedding backend available; Chroma skipped (BM25 retrieval still works)."
        )
        return None, "bm25"

    from langchain_community.vectorstores import Chroma

    out = chroma_dir()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    logger.info("Building Chroma store from %d chunks (%s)...", len(docs), backend)
    Chroma.from_documents(
        documents=docs,
        embedding=embeddings,
        persist_directory=str(out),
        collection_name=COLLECTION_NAME,
    )
    meta = {
        "backend": backend,
        "vector_store": "chroma",
        "collection": COLLECTION_NAME,
        "num_chunks": len(docs),
        "cities": sorted(
            {str(d.metadata.get("city")) for d in docs if d.metadata.get("city")}
        ),
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    logger.info("Chroma store saved to %s", out)
    return out, backend


def load_chroma_index():
    """Load persisted Chroma store, or None if missing / embeddings unavailable."""
    out = chroma_dir()
    if not out.is_dir():
        return None
    # Fresh empty dir or wipe mid-build
    has_data = any(out.iterdir()) and (
        (out / "chroma.sqlite3").is_file()
        or any(out.rglob("*.sqlite3"))
        or (out / "meta.json").is_file()
    )
    if not has_data:
        return None

    embeddings, backend = resolve_embeddings()
    if embeddings is None:
        return None

    from langchain_community.vectorstores import Chroma

    try:
        store = Chroma(
            persist_directory=str(out),
            embedding_function=embeddings,
            collection_name=COLLECTION_NAME,
        )
        logger.info("Loaded Chroma store from %s (backend=%s)", out, backend)
        return store
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load Chroma store: %s", exc)
        return None


# Back-compat aliases (older call sites / notebooks)
build_faiss_index = build_chroma_index
load_faiss_index = load_chroma_index


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    from agent.rag.paths import repo_root

    load_dotenv(repo_root() / ".env", override=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Ingest RAG corpus into Chroma")
    parser.add_argument(
        "--force-chunks",
        action="store_true",
        help="Rebuild chunks.jsonl from corpus JSON",
    )
    args = parser.parse_args(argv)

    docs = ensure_chunks(force=args.force_chunks)
    try:
        from agent.rag.retrieve import clear_retriever_cache

        clear_retriever_cache()
    except Exception:  # noqa: BLE001
        pass
    print(f"chunks={len(docs)}")
    path, backend = build_chroma_index(docs, force=False)
    if path is None and backend == "bm25":
        print(
            "Chroma not built (no embeddings). BM25 retrieval will use chunks.jsonl."
        )
        return 0 if docs else 1
    print(f"chroma={path} backend={backend}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
