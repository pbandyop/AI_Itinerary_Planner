"""Knowledge Agent RAG retriever — cited tips from Wikivoyage/Wikipedia chunks."""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from functools import lru_cache
from typing import Any

from langchain_core.documents import Document

from agent.rag.chunking import ensure_chunks
from agent.schemas.itinerary import Source
from agent.schemas.specialists import KnowledgeResult, KnowledgeSnippet

logger = logging.getLogger(__name__)

TOPIC_QUERY_HINTS: dict[str, str] = {
    "rain": "rain monsoon weather indoor outdoor what to do when it rains",
    "weather": "climate seasons monsoon heat best time to visit",
    "food": "food cuisine restaurants street food eat drink",
    "safety": "stay safe safety scams gem scam caution touts thieves",
    "transport": "get around transport metro bus taxi auto rickshaw",
    "doable": "see do itinerary day trip highlights attractions",
    "why": "understand history culture why visit overview",
    "tips": "respect practical tips money costs hours opening",
    "culture": "culture customs festivals religion etiquette dress code temple",
    "highlights": "see do attractions forts palaces temples museums old city",
    "crowd": "busy crowded queues morning evening avoid peak",
    "timing": "hours opening best time morning evening visit",
}

# Lexical markers used to prefer on-topic chunks (anti-noise rerank).
TOPIC_TEXT_MARKERS: dict[str, tuple[str, ...]] = {
    "safety": (
        "stay safe",
        "scam",
        "gem scam",
        "thieves",
        "hustlers",
        "touts",
        "pickpocket",
        "caution",
    ),
    "culture": (
        "etiquette",
        "dress",
        "conservatively",
        "customs",
        "respect",
        "temple",
        "shoes",
        "modest",
        "no bags",
        "no photos",
        "religious",
    ),
    "crowd": ("crowded", "busy", "queue", "peak", "avoid"),
    "timing": ("opening", "hours", "am-", "pm", "best time", "morning", "evening"),
    "highlights": ("see", "fort", "palace", "temple", "museum", "old city", "pink city"),
}


def _normalize_city(city: str) -> str:
    return re.sub(r"\s+", " ", city.strip()).lower()


def normalize_match_text(text: str | None) -> str:
    """Lowercase + fold accents (Café → cafe) for place substring matching."""
    s = (text or "").lower().strip()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", s).strip()


def place_mentioned(haystack: str | None, place: str | None) -> bool:
    """True if place appears in haystack, ignoring accents (cafe ≡ café)."""
    h = normalize_match_text(haystack)
    p = normalize_match_text(place)
    if not h or not p:
        return False
    if p in h:
        return True
    tokens = [t for t in re.split(r"\s+", p) if t and t not in {"the", "a", "an"}]
    return bool(tokens) and all(t in h for t in tokens)


def places_mentioned(haystack: str | None, places: list[str] | None) -> bool:
    return any(place_mentioned(haystack, p) for p in (places or []) if p)


def _load_place_names(city: str) -> list[str]:
    """Known POI names for place-aware boost (Jaipur seed + common labels)."""
    from pathlib import Path

    names: list[str] = [
        "hawa mahal",
        "jantar mantar",
        "city palace",
        "amber fort",
        "amer fort",
        "nahargarh fort",
        "jaigarh fort",
        "jal mahal",
        "albert hall",
        "birla mandir",
        "birla temple",
        "akshardham temple",
        "akshardham",
        "govind devji temple",
        "govind dev ji temple",
        "govind devji",
        "galwar bagh",
        "monkey temple",
        "moti dungri",
        "johari bazaar",
        "bapu bazaar",
        "chokhi dhani",
        "patrika gate",
        "moon gate",
        "nevta dam",
        "neota dam",
        "niota dam",
        "amarnath cafe",
        "central park",
        "anokhi cafe",
        "anokhi museum",
        "anokhi museum of hand printing",
        "ganesh restaurant",
        "ganesh resturent",
        "caravana",
    ]
    try:
        # retrieve.py → rag → agent → src → services/agent → repo
        root = Path(__file__).resolve().parents[5] / "data" / "pois"
        slug = re.sub(r"[^a-z0-9]+", "_", city.strip().lower()).strip("_")
        path = root / f"{slug}.json"
        if path.exists():
            import json

            raw = json.loads(path.read_text(encoding="utf-8"))
            items = raw if isinstance(raw, list) else raw.get("pois") or raw.get("places") or []
            for item in items:
                n = str((item or {}).get("name") or "").strip().lower()
                if n and n not in names:
                    names.append(n)
    except Exception:  # noqa: BLE001
        pass
    # Longest first for matching
    names.sort(key=len, reverse=True)
    return names


_PLACE_QUERY_SKIP = frozenset(
    {
        "jaipur",
        "the city",
        "this place",
        "opening hours",
        "opening hour",
        "the hours",
        "more",
        "it",
        "that",
        "this",
    }
)


def extract_place_terms(query: str, city: str = "Jaipur") -> list[str]:
    """Place names mentioned in the user query (for boost + grounding checks)."""
    from agent.rag.corpus import load_place_aliases

    lower = (query or "").lower()
    fold_q = normalize_match_text(query)
    found: list[str] = []
    extras = ("pink city", "old city", "ram niwas", "albert hall")
    for name in list(_load_place_names(city)) + list(extras):
        if (
            name in lower or normalize_match_text(name) in fold_q
        ) and name not in found:
            found.append(name)
    # Free-form: "tell me more about X", "opening hours for X",
    # "why did you pick X", "why choose X"
    for m in re.finditer(
        r"(?:about|for|of|near|pick|choose|include|selected|recommend(?:ed)?)\s+(?:the\s+)?"
        r"([a-z0-9][\w'’.-]*(?:\s+[a-z0-9][\w'’.-]*){0,5})"
        r"(?:\s*[?.!,]|$)",
        lower,
    ):
        cand = re.sub(r"\s+", " ", m.group(1)).strip(" .,?!")
        if not cand or cand in _PLACE_QUERY_SKIP or len(cand) < 4:
            continue
        # Drop trailing filler after pick/choose ("pick that place" → skip).
        if cand in {"that place", "this place", "this stop", "that stop"}:
            continue
        if cand not in found:
            found.append(cand)

    # Expand aliases (Niota → Nevta Dam, etc.)
    try:
        alias_map = load_place_aliases()
    except Exception:  # noqa: BLE001
        alias_map = {}
    expanded: list[str] = []
    for term in found:
        if term not in expanded:
            expanded.append(term)
        for canon in alias_map.get(term, []):
            c = canon.lower().strip()
            if c and c not in expanded:
                expanded.append(c)
        # Exact alias-key equality only (avoid substring false positives).
        if term in alias_map:
            for canon in alias_map[term]:
                c = canon.lower().strip()
                if c and c not in expanded:
                    expanded.append(c)
    return expanded


def _doc_matches_places(doc: Document, places: list[str]) -> bool:
    if not places:
        return False
    meta = doc.metadata or {}
    hay = " ".join(
        [
            (doc.page_content or ""),
            str(meta.get("place_name") or ""),
            str(meta.get("title") or ""),
            " ".join(str(a) for a in (meta.get("aliases") or [])),
        ]
    )
    # aliases may be stored as joined string for Chroma
    alias_field = meta.get("aliases")
    if isinstance(alias_field, str) and alias_field:
        hay += " " + alias_field
    return places_mentioned(hay, places)


def _place_boost_score(text: str, places: list[str], doc: Document | None = None) -> int:
    if not places:
        return 0
    low = normalize_match_text(text)
    if doc is not None:
        meta = doc.metadata or {}
        low = normalize_match_text(
            low
            + " "
            + str(meta.get("place_name") or "")
            + " "
            + str(meta.get("title") or "")
            + " "
            + " ".join(str(a) for a in (meta.get("aliases") or []))
        )
        alias_field = meta.get("aliases")
        if isinstance(alias_field, str) and alias_field:
            low = normalize_match_text(low + " " + alias_field)
    score = 0
    matched = False
    for p in places:
        pf = normalize_match_text(p)
        if pf not in low and not place_mentioned(low, p):
            continue
        matched = True
        score += 10
        # Prefer the guide listing entry for that place
        if re.search(rf"(?:^|\n)\s*\d+\s+{re.escape(pf)}\b", low):
            score += 20
        if "am-" in low or "am–" in low or re.search(r"\d+\s*am\s*[-–]\s*\d+\s*pm", low):
            score += 3
        if re.search(r"\b\d{1,2}:\d{2}\s*(?:[-–]|to)\s*\d{1,2}:\d{2}\b", low):
            score += 3
        if "opening hours" in low:
            score += 5
    if matched and doc is not None:
        ds = str((doc.metadata or {}).get("dataset") or "").lower()
        if ds in {"openstreetmap", "google_places"} and "opening hours" in low:
            score += 8
        if ds in {"curated_places", "wikipedia", "openstreetmap", "google_places"}:
            score += 2
    return score


def excerpt_place_from_snippet(text: str, place: str) -> str | None:
    """Pull the Wikivoyage listing/paragraph for one place from a mixed chunk."""
    if not text or not place:
        return None
    if not place_mentioned(text, place):
        return None
    raw = text.strip()
    low = raw.lower()
    fold_low = normalize_match_text(raw)
    fold_p = normalize_match_text(place)
    tokens = [t for t in re.split(r"\s+", fold_p) if t and t not in {"the", "a", "an"}]
    # Prefer exact folded phrase; else first token (e.g. anokhi).
    if fold_p in fold_low:
        needle = tokens[0] if tokens else fold_p
    else:
        needle = next((t for t in tokens if t in fold_low), "")
    if not needle:
        # Single-place Google chunks: return whole snippet.
        excerpt = re.sub(r"\s+", " ", raw).strip()
        return (excerpt[:419].rstrip() + "…") if len(excerpt) > 420 else excerpt

    # Locate needle in original text (ASCII token usually present even when café ≠ cafe).
    idx = low.find(needle)
    if idx < 0:
        excerpt = re.sub(r"\s+", " ", raw).strip()
        return (excerpt[:419].rstrip() + "…") if len(excerpt) > 420 else excerpt
    start = raw.rfind("\n", 0, idx) + 1
    window = raw[start:]
    m = re.match(
        r"(.+?)(?=\n\s*\d+\s+[A-Za-z]|\Z)",
        window,
        flags=re.S,
    )
    excerpt = (m.group(1) if m else window).strip()
    excerpt = re.sub(r"\s+", " ", excerpt).strip()
    if not excerpt or not place_mentioned(excerpt, place):
        return None
    if len(excerpt) > 420:
        excerpt = excerpt[:419].rstrip() + "…"
    return excerpt


def is_thin_place_listing(text: str) -> bool:
    """True when the guide only has a stub listing (name/area/phone)."""
    t = re.sub(r"^\d+\s+", "", (text or "").strip())
    if len(t) < 120:
        return True
    has_contact = bool(re.search(r"[☏+]|phone|nagar|road|marg|near", t, re.I))
    return len(t) < 200 and has_contact


def _topic_boost_score(text: str, topics: list[str] | None) -> int:
    if not topics:
        return 0
    low = (text or "").lower()
    score = 0
    for idx, topic in enumerate(topics):
        # Primary topic (first listed) outweighs secondary tags.
        weight = 3 if idx == 0 else (2 if idx == 1 else 1)
        for marker in TOPIC_TEXT_MARKERS.get(topic, ()):
            if marker in low:
                score += (12 if " " in marker else 4) * weight
    return score


def _merge_topic_lexical(
    docs: list[Document], *, city: str, topics: list[str] | None, k: int
) -> list[Document]:
    """Pull in corpus chunks that match topic markers (e.g. Stay safe) when vector noise wins."""
    if not topics:
        return docs
    markers: list[str] = []
    for topic in topics:
        markers.extend(TOPIC_TEXT_MARKERS.get(topic, ()))
    if not markers:
        return docs
    try:
        city_docs = _filter_by_city(list(_bm25_all_docs()), city)
    except Exception:  # noqa: BLE001
        return docs
    lexical = [
        d
        for d in city_docs
        if _topic_boost_score(d.page_content or "", topics) > 0
    ]
    lexical.sort(
        key=lambda d: _topic_boost_score(d.page_content or "", topics),
        reverse=True,
    )
    if not lexical:
        return docs
    out: list[Document] = []
    seen: set[str] = set()
    for d in lexical[: max(k, 3)] + list(docs):
        key = str((d.metadata or {}).get("chunk_id") or "") or (d.page_content or "")[:80]
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
        if len(out) >= max(k * 3, 8):
            break
    return out


def _rerank_docs(
    docs: list[Document],
    *,
    query: str,
    city: str,
    k: int,
    topics: list[str] | None = None,
) -> list[Document]:
    places = extract_place_terms(query, city)
    hours_q = bool(
        re.search(r"\b(hours?|timing|opening|open(?:ing)?\s+time)\b", (query or "").lower())
    )
    if not docs:
        return []

    def score(pair: tuple[int, Document]) -> tuple[int, int]:
        idx, d = pair
        s = _place_boost_score(d.page_content or "", places, d) + _topic_boost_score(
            d.page_content or "", topics
        )
        if hours_q:
            low = (d.page_content or "").lower()
            ds = str((d.metadata or {}).get("dataset") or "").lower()
            has_hours = bool(
                "opening hours" in low
                or re.search(r"\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)", low)
            )
            if has_hours:
                s += 15
            if ds == "google_places" and has_hours:
                s += 50
            elif ds == "openstreetmap" and has_hours:
                s += 22
            elif ds == "curated_places" and has_hours:
                s += 12
            elif ds == "wikivoyage":
                s -= 10
            elif ds == "wikipedia" and not has_hours:
                s -= 15
        return (s, -idx)

    ranked = sorted(enumerate(docs), key=score, reverse=True)
    preferred = [
        d
        for _, d in ranked
        if _place_boost_score(d.page_content or "", places, d) > 0
        or _topic_boost_score(d.page_content or "", topics) > 0
    ]
    pool = preferred if preferred else [d for _, d in ranked]
    out: list[Document] = []
    seen: set[str] = set()
    for d in pool + [d for _, d in ranked]:
        key = str((d.metadata or {}).get("chunk_id") or "") or (d.page_content or "")[:80]
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
        if len(out) >= k:
            break
    return out


def _city_aliases(city: str) -> set[str]:
    """Match corpus city labels (e.g. Bengaluru ↔ Bangalore)."""
    c = _normalize_city(city)
    aliases = {c}
    mapping = {
        "bengaluru": {"bangalore", "bengaluru"},
        "bangalore": {"bangalore", "bengaluru"},
        "mumbai": {"mumbai", "bombay"},
        "bombay": {"mumbai", "bombay"},
        "kolkata": {"kolkata", "calcutta"},
        "calcutta": {"kolkata", "calcutta"},
        "kochi": {"kochi", "cochin"},
        "cochin": {"kochi", "cochin"},
        "varanasi": {"varanasi", "banaras", "benaras"},
        "delhi": {"delhi", "new delhi", "ncr"},
        "new delhi": {"delhi", "new delhi"},
    }
    aliases |= mapping.get(c, set())
    return aliases


def _filter_by_city(docs: list[Document], city: str) -> list[Document]:
    aliases = _city_aliases(city)
    matched = [
        d
        for d in docs
        if _normalize_city(str(d.metadata.get("city") or "")) in aliases
        or any(
            a in _normalize_city(str(d.metadata.get("title") or "")) for a in aliases
        )
    ]
    return matched


def _snippet_text(text: str, limit: int = 280) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _doc_to_source(doc: Document) -> Source:
    from agent.rag.corpus import source_dataset_from_corpus

    meta = doc.metadata or {}
    dataset = source_dataset_from_corpus(str(meta.get("dataset") or "other"))
    return Source(
        title=str(meta.get("title") or "Travel guide"),
        url=str(meta.get("url") or None) or None,
        dataset=dataset,  # type: ignore[arg-type]
        snippet=_snippet_text(doc.page_content),
        source_id=str(meta.get("chunk_id") or None) or None,
    )


def _build_query(query: str, topics: list[str] | None) -> str:
    parts = [query.strip()] if query.strip() else []
    for topic in topics or []:
        key = topic.strip().lower()
        hint = TOPIC_QUERY_HINTS.get(key, key)
        parts.append(hint)
    if not parts:
        parts.append("travel tips highlights see do")
    return " ".join(parts)


@lru_cache(maxsize=1)
def _bm25_all_docs() -> tuple[Document, ...]:
    return tuple(ensure_chunks(force=False))


def _retrieve_bm25(
    *,
    city: str,
    query: str,
    k: int,
) -> list[Document]:
    from langchain_community.retrievers import BM25Retriever

    all_docs = list(_bm25_all_docs())
    city_docs = _filter_by_city(all_docs, city)
    if not city_docs:
        return []
    retriever = BM25Retriever.from_documents(city_docs)
    retriever.k = min(max(k * 6, k), len(city_docs))
    return list(retriever.invoke(query))


@lru_cache(maxsize=1)
def _cached_chroma_store():
    from agent.rag.ingest import load_chroma_index

    return load_chroma_index()


def _retrieve_chroma(
    *,
    city: str,
    query: str,
    k: int,
) -> list[Document] | None:
    from agent.rag.embeddings import format_retrieval_query

    store = _cached_chroma_store()
    if store is None:
        return None
    search_text = format_retrieval_query(query)
    # Over-fetch then filter by city so multi-city index + aliases still work.
    try:
        candidates = store.similarity_search(search_text, k=max(k * 8, 16))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Chroma search failed: %s", exc)
        return None
    city_hits = _filter_by_city(candidates, city)
    if city_hits:
        return city_hits[:k]
    try:
        prefixed = store.similarity_search(
            format_retrieval_query(f"{city} {query}"),
            k=max(k * 8, 16),
        )
    except Exception:  # noqa: BLE001
        return []
    return _filter_by_city(prefixed, city)[:k]


def load_retrieval_strategy() -> dict[str, str]:
    from agent.rag.paths import retrieval_strategy_path

    defaults = {
        "retriever": "hybrid_rrf",
        "reranker": "dataset_aware",
    }
    path = retrieval_strategy_path()
    if not path.is_file():
        return defaults
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults
    return {
        "retriever": str(raw.get("retriever") or defaults["retriever"]),
        "reranker": str(raw.get("reranker") or defaults["reranker"]),
    }


def _rrf_merge(lists: list[list[Document]], k: int, rrf_k: int = 60) -> list[Document]:
    scores: dict[str, float] = {}
    docs: dict[str, Document] = {}
    for lst in lists:
        for rank, d in enumerate(lst, start=1):
            key = str((d.metadata or {}).get("chunk_id") or "") or (d.page_content or "")[:100]
            scores[key] = scores.get(key, 0.0) + 1.0 / (rrf_k + rank)
            docs[key] = d
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [docs[key] for key, _ in ordered[:k]]


def _dataset_aware_rerank(
    docs: list[Document],
    *,
    query: str,
    places: list[str],
    k: int,
) -> list[Document]:
    hours_q = bool(re.search(r"\b(hours?|timing|opening)\b", (query or "").lower()))
    about_q = bool(re.search(r"\b(tell me|about|what is|describe)\b", (query or "").lower()))

    def hay(d: Document) -> str:
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
            ]
        ).lower()

    def score(d: Document) -> float:
        h = hay(d)
        ds = str((d.metadata or {}).get("dataset") or "").lower()
        s = 0.0
        matched = False
        for p in places:
            if place_mentioned(h, p):
                matched = True
                s += 15.0
                if place_mentioned(str((d.metadata or {}).get("place_name") or ""), p):
                    s += 12.0
        if places and not matched:
            s -= 25.0
        if hours_q:
            from agent.rag.hours import has_hour_clock

            has_hours = has_hour_clock(h)
            if "opening hours" in h:
                s += 20.0
            if has_hours:
                s += 15.0
            if ds == "google_places" and has_hours:
                s += 50.0
            elif ds == "openstreetmap" and has_hours:
                s += 22.0
            elif ds == "curated_places" and has_hours:
                s += 12.0
            elif ds == "wikivoyage":
                s -= 10.0
            elif ds == "wikipedia" and not has_hours:
                s -= 15.0
        elif about_q:
            weights = {
                "wikipedia": 16.0,
                "wikivoyage": 10.0,
                "google_places": 10.0,
                "tourism": 8.0,
                "curated_places": 11.0,
                "openstreetmap": 6.0,
            }
            s += weights.get(ds, 0.0)
        q_tokens = [t for t in re.split(r"\W+", (query or "").lower()) if len(t) > 3]
        s += sum(1.2 for t in q_tokens if t in h)
        return s

    ranked = sorted(enumerate(docs), key=lambda pair: (score(pair[1]), -pair[0]), reverse=True)
    return [d for _, d in ranked[:k]]


def knowledge_search(
    city: str,
    query: str = "",
    topics: list[str] | None = None,
    k: int = 4,
) -> KnowledgeResult:
    """
    Retrieve cited city guidance for the Knowledge Agent.

    Empty retrieval → missing_data=True (no hallucinated tips).
    """
    city = city.strip()
    if not city:
        return KnowledgeResult(
            snippets=[],
            missing_data=True,
            notes="City is required for knowledge retrieval.",
        )

    search_query = _build_query(query, topics)
    k = max(1, min(int(k), 10))
    places = extract_place_terms(query, city)
    # Place-first queries: lead with the place name so topic hints don't drown it.
    if places:
        lead = " ".join(places[:3])
        search_query = f"{lead} {query.strip()} {search_query}".strip()

    strat = load_retrieval_strategy()
    fetch_k = max(k * 6, 16) if places else max(k * 4, 8)
    method = strat.get("retriever") or "hybrid_rrf"

    bm25_docs = _retrieve_bm25(city=city, query=search_query, k=fetch_k)
    chroma_docs = _retrieve_chroma(city=city, query=search_query, k=fetch_k) or []

    if method == "bm25":
        docs = bm25_docs
    elif method == "chroma" and chroma_docs:
        docs = chroma_docs
        method = "chroma"
    else:
        # Default / hybrid_rrf
        docs = _rrf_merge([bm25_docs, chroma_docs], k=fetch_k) if chroma_docs else bm25_docs
        method = "hybrid_rrf" if chroma_docs else "bm25"

    if not places:
        docs = _merge_topic_lexical(docs, city=city, topics=topics, k=k)

    rerank_name = strat.get("reranker") or "dataset_aware"
    if rerank_name == "dataset_aware":
        docs = _dataset_aware_rerank(
            docs, query=query or search_query, places=places, k=max(k, 6) if places else k
        )
        method = f"{method}+dataset_aware"
    else:
        docs = _rerank_docs(
            docs,
            query=query or search_query,
            city=city,
            k=max(k, 6) if places else k,
            topics=topics,
        )
        method = f"{method}+place_boost"

    if places:
        city_docs = _filter_by_city(list(_bm25_all_docs()), city)
        lexical = [
            d
            for d in city_docs
            if _place_boost_score(d.page_content or "", places, d) > 0
            or _doc_matches_places(d, places)
        ]
        lexical.sort(
            key=lambda d: _place_boost_score(d.page_content or "", places, d),
            reverse=True,
        )
        if lexical:
            # Prefer Google Places for hours queries when available among lexical hits
            hours_q = bool(re.search(r"\b(hours?|timing|opening)\b", (query or "").lower()))
            if hours_q:
                lexical.sort(
                    key=lambda d: (
                        1
                        if str((d.metadata or {}).get("dataset") or "") == "google_places"
                        and "opening hours" in (d.page_content or "").lower()
                        else 0,
                        _place_boost_score(d.page_content or "", places, d),
                    ),
                    reverse=True,
                )
            seen: set[str] = set()
            merged: list[Document] = []
            for d in lexical[: max(k, 4)] + list(docs):
                key = str((d.metadata or {}).get("chunk_id") or "") or (d.page_content or "")[:80]
                if key in seen:
                    continue
                seen.add(key)
                merged.append(d)
                if len(merged) >= k:
                    break
            docs = _dataset_aware_rerank(
                merged, query=query or search_query, places=places, k=k
            )
            method = f"{method}+place_scan"
        else:
            note = (
                f"No relevant chunks found in the '{city}' corpus for "
                f"{', '.join(places[:2])}. Knowledge tips unavailable (data missing)."
            )
            return KnowledgeResult(snippets=[], missing_data=True, notes=note)

    if not docs:
        all_docs = list(_bm25_all_docs())
        city_docs = _filter_by_city(all_docs, city)
        if not city_docs:
            note = (
                f"No multi-source RAG corpus loaded for '{city}'. "
                "Knowledge tips unavailable (data missing)."
            )
        else:
            note = (
                f"No relevant chunks found in the '{city}' corpus for this query. "
                "Knowledge tips unavailable (data missing)."
            )
        logger.info("knowledge_search city=%s missing_data=True method=%s", city, method)
        return KnowledgeResult(snippets=[], missing_data=True, notes=note)

    snippets: list[KnowledgeSnippet] = []
    for doc in docs:
        topic = (topics[0] if topics else None) or "tips"
        citation = _doc_to_source(doc)
        snippets.append(
            KnowledgeSnippet(
                topic=str(topic),
                text=doc.page_content.strip(),
                citations=[citation],
                uncertainty=None,
            )
        )

    notes = (
        f"Retrieved {len(snippets)} cited chunk(s) for {city} via {method} "
        f"(query={search_query!r})."
    )
    logger.info(
        "knowledge_search city=%s hits=%d method=%s missing_data=False",
        city,
        len(snippets),
        method,
    )
    return KnowledgeResult(snippets=snippets, missing_data=False, notes=notes)


def sources_from_knowledge(result: KnowledgeResult) -> list[Source]:
    """Deduplicate citation Sources for itinerary.sources[] / UI panel."""
    seen: set[str] = set()
    out: list[Source] = []
    for snip in result.snippets:
        for src in snip.citations:
            key = src.source_id or f"{src.title}|{src.url}|{src.snippet}"
            if key in seen:
                continue
            seen.add(key)
            out.append(src)
    return out


def clear_retriever_cache() -> None:
    _bm25_all_docs.cache_clear()
    _cached_chroma_store.cache_clear()
