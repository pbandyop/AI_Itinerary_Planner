"""Chunk corpus documents into LangChain Documents with citation metadata."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from agent.rag.corpus import CorpusDoc, load_corpus_docs
from agent.rag.paths import chunking_strategy_path, chunks_path

logger = logging.getLogger(__name__)

CHUNK_SIZE = 900
CHUNK_OVERLAP = 120

STRATEGY_RECURSIVE_900 = "recursive_900_120"
STRATEGY_RECURSIVE_500 = "recursive_500_80"
STRATEGY_RECURSIVE_1400 = "recursive_1400_200"
STRATEGY_SECTION = "section_aware"
STRATEGY_PLACE_ATOMIC = "place_atomic"

ALL_STRATEGIES = (
    STRATEGY_RECURSIVE_900,
    STRATEGY_RECURSIVE_500,
    STRATEGY_RECURSIVE_1400,
    STRATEGY_SECTION,
    STRATEGY_PLACE_ATOMIC,
)


def _chunk_id(city: str, title: str, index: int, text: str) -> str:
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    city_slug = re.sub(r"[^a-z0-9]+", "-", city.lower()).strip("-")
    return f"{city_slug}:{index}:{digest}"


def _meta_for(doc: CorpusDoc, index: int, text: str) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "chunk_id": _chunk_id(doc.city, doc.title, index, text),
        "city": doc.city,
        "title": doc.title,
        "source": doc.source,
        "dataset": doc.dataset,
        "url": doc.url,
        "license": doc.license,
        "chunk_index": index,
        "place_name": doc.place_name or "",
        "atomic": bool(doc.atomic),
    }
    # Chroma rejects empty lists in metadata — store joined string instead.
    if doc.aliases:
        meta["aliases"] = " | ".join(doc.aliases)
    return meta


def _docs_from_pieces(doc: CorpusDoc, pieces: list[str]) -> list[Document]:
    out: list[Document] = []
    for i, piece in enumerate(pieces):
        text = piece.strip()
        if len(text) < 40:
            continue
        out.append(Document(page_content=text, metadata=_meta_for(doc, i, text)))
    return out


def _split_recursive(doc: CorpusDoc, size: int, overlap: int) -> list[Document]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=size,
        chunk_overlap=overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    return _docs_from_pieces(doc, splitter.split_text(doc.text))


def _split_section_aware(doc: CorpusDoc, max_len: int = 1000) -> list[Document]:
    text = doc.text.strip()
    # Split on blank line + Title-like headings common in Wikivoyage/Wikipedia
    parts = re.split(r"\n(?=[A-Z][^\n]{0,60}\n)", text)
    if len(parts) <= 1:
        parts = re.split(r"\n\n+", text)
    pieces: list[str] = []
    buf = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if not buf:
            buf = part
        elif len(buf) + len(part) + 2 <= max_len:
            buf = f"{buf}\n\n{part}"
        else:
            pieces.append(buf)
            buf = part
        while len(buf) > max_len * 2:
            pieces.append(buf[:max_len])
            buf = buf[max_len - 80 :]
    if buf:
        pieces.append(buf)
    if not pieces:
        pieces = [text]
    return _docs_from_pieces(doc, pieces)


def _split_place_atomic(doc: CorpusDoc) -> list[Document]:
    structured = doc.dataset in {
        "openstreetmap",
        "google_places",
        "curated_places",
        "wikipedia",
    } or doc.atomic
    if structured and len(doc.text) < 8000:
        return _docs_from_pieces(doc, [doc.text])
    # Long prose: section-aware
    return _split_section_aware(doc, max_len=900)


def docs_to_chunks(
    corpus: list[CorpusDoc] | None = None,
    *,
    strategy: str | None = None,
    chunk_size: int = CHUNK_SIZE,
    chunk_overlap: int = CHUNK_OVERLAP,
) -> list[Document]:
    corpus = corpus if corpus is not None else load_corpus_docs()
    strategy = strategy or load_chunking_strategy() or STRATEGY_PLACE_ATOMIC

    splitter_fn: Callable[[CorpusDoc], list[Document]]
    if strategy == STRATEGY_RECURSIVE_900:
        splitter_fn = lambda d: _split_recursive(d, 900, 120)
    elif strategy == STRATEGY_RECURSIVE_500:
        splitter_fn = lambda d: _split_recursive(d, 500, 80)
    elif strategy == STRATEGY_RECURSIVE_1400:
        splitter_fn = lambda d: _split_recursive(d, 1400, 200)
    elif strategy == STRATEGY_SECTION:
        splitter_fn = lambda d: _split_section_aware(d, max_len=1000)
    elif strategy == STRATEGY_PLACE_ATOMIC:
        splitter_fn = _split_place_atomic
    else:
        # Legacy kwargs path
        splitter_fn = lambda d: _split_recursive(d, chunk_size, chunk_overlap)

    out: list[Document] = []
    for doc in corpus:
        out.extend(splitter_fn(doc))
    logger.info("Chunked corpus into %d documents (strategy=%s)", len(out), strategy)
    return out


def save_chunks(documents: list[Document], path: Path | None = None) -> Path:
    target = path or chunks_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as fh:
        for doc in documents:
            row: dict[str, Any] = {
                "page_content": doc.page_content,
                "metadata": doc.metadata,
            }
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    logger.info("Wrote %d chunks to %s", len(documents), target)
    return target


def load_chunks(path: Path | None = None) -> list[Document]:
    target = path or chunks_path()
    if not target.is_file():
        return []
    docs: list[Document] = []
    with target.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            docs.append(
                Document(
                    page_content=row["page_content"],
                    metadata=row.get("metadata") or {},
                )
            )
    return docs


def load_chunking_strategy() -> str | None:
    path = chunking_strategy_path()
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    strategy = str(raw.get("strategy") or "").strip()
    return strategy if strategy in ALL_STRATEGIES else None


def save_chunking_strategy(strategy: str, scores: dict[str, Any] | None = None) -> Path:
    path = chunking_strategy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"strategy": strategy, "scores": scores or {}}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def ensure_chunks(*, force: bool = False, strategy: str | None = None) -> list[Document]:
    """Return chunks, building from corpus JSON when missing or forced."""
    if not force and strategy is None:
        existing = load_chunks()
        if existing:
            return existing
    documents = docs_to_chunks(strategy=strategy)
    if documents:
        save_chunks(documents)
    return documents
