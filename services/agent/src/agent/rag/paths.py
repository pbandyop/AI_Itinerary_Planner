"""Filesystem paths for RAG corpus and vector index."""

from __future__ import annotations

from pathlib import Path


def repo_root() -> Path:
    # services/agent/src/agent/rag/paths.py → repo root is 5 parents up
    return Path(__file__).resolve().parents[5]


def rag_dir() -> Path:
    return repo_root() / "data" / "rag"


def corpus_dir() -> Path:
    return rag_dir() / "corpus"


def chunks_path() -> Path:
    return rag_dir() / "chunks.jsonl"


def chroma_dir() -> Path:
    """Persistent Chroma vector store directory."""
    return rag_dir() / "chroma"


def index_dir() -> Path:
    """Deprecated alias — previously FAISS; now points at Chroma."""
    return chroma_dir()


def chunking_strategy_path() -> Path:
    return rag_dir() / "chunking_strategy.json"


def retrieval_strategy_path() -> Path:
    return rag_dir() / "retrieval_strategy.json"


def eval_dir() -> Path:
    return rag_dir() / "eval"
