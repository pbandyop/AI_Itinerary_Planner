"""Phase 3 — LangChain RAG for city knowledge (Wikivoyage / Wikipedia)."""

from __future__ import annotations


def knowledge_search(*args, **kwargs):
    from agent.rag.retrieve import knowledge_search as _fn

    return _fn(*args, **kwargs)


def sources_from_knowledge(*args, **kwargs):
    from agent.rag.retrieve import sources_from_knowledge as _fn

    return _fn(*args, **kwargs)


__all__ = ["knowledge_search", "sources_from_knowledge"]
