"""Embedding backends for Chroma (OpenAI or local HuggingFace / BGE)."""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Default local model: free, strong retrieval quality, deployable on Railway/Render.
DEFAULT_HF_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"

# BGE asymmetric retrieval instruction (queries only; documents stay plain text).
BGE_QUERY_INSTRUCTION = (
    "Represent this sentence for searching relevant passages: "
)


def active_hf_model_name() -> str:
    return (
        os.getenv("RAG_HF_EMBEDDING_MODEL") or DEFAULT_HF_EMBEDDING_MODEL
    ).strip()


def format_retrieval_query(query: str) -> str:
    """Apply model-specific query prefixes (BGE) before vector search."""
    text = query.strip()
    if not text:
        return text
    model = active_hf_model_name().lower()
    if "bge" in model and not text.startswith(BGE_QUERY_INSTRUCTION):
        return f"{BGE_QUERY_INSTRUCTION}{text}"
    return text


def resolve_embeddings() -> tuple[Any | None, str]:
    """
    Return (embeddings, backend_name).

    Order:
    1. OpenAI when OPENAI_API_KEY is set (RAG_EMBEDDINGS=auto|openai)
    2. HuggingFace / BGE when RAG_EMBEDDINGS=huggingface|hf|auto (auto falls back here)
    3. None → BM25-only retrieval
    """
    preference = (os.getenv("RAG_EMBEDDINGS") or "auto").strip().lower()
    has_openai = bool(os.getenv("OPENAI_API_KEY"))

    if preference in {"bm25", "none", "off"}:
        return None, "bm25"

    if preference in {"openai", "auto"} and has_openai:
        try:
            from langchain_openai import OpenAIEmbeddings

            model = os.getenv("RAG_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
            emb = OpenAIEmbeddings(model=model)
            logger.info("RAG embeddings: OpenAI (%s)", model)
            return emb, "openai"
        except Exception as exc:  # noqa: BLE001
            logger.warning("OpenAI embeddings unavailable: %s", exc)
            if preference == "openai":
                return None, "bm25"

    if preference in {"huggingface", "hf", "auto"}:
        try:
            from langchain_community.embeddings import HuggingFaceEmbeddings

            model = active_hf_model_name()
            emb = HuggingFaceEmbeddings(
                model_name=model,
                encode_kwargs={"normalize_embeddings": True},
            )
            logger.info("RAG embeddings: HuggingFace (%s)", model)
            return emb, "huggingface"
        except Exception as exc:  # noqa: BLE001
            logger.warning("HuggingFace embeddings unavailable: %s", exc)
            return None, "bm25"

    return None, "bm25"
