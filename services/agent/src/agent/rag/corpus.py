"""Load RAG corpus documents from data/rag/corpus/ (nested JSON allowed)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from agent.rag.paths import corpus_dir

logger = logging.getLogger(__name__)

DATASET_ALIASES: dict[str, str] = {
    "wikivoyage": "wikivoyage",
    "wikipedia": "wikipedia",
    "osm": "openstreetmap",
    "openstreetmap": "openstreetmap",
    "tourism": "other",
    "google_places": "other",
    "google": "other",
    "curated_places": "other",
    "curated": "other",
    "other": "other",
}


@dataclass(frozen=True)
class CorpusDoc:
    city: str
    title: str
    source: str
    url: str
    text: str
    license: str = "CC BY-SA 4.0"
    path: str | None = None
    dataset: str = "other"
    place_name: str | None = None
    aliases: tuple[str, ...] = field(default_factory=tuple)
    atomic: bool = False  # Prefer one-chunk-per-doc for structured cards


def _infer_dataset(raw: dict, path: Path) -> str:
    explicit = str(raw.get("dataset") or "").strip().lower()
    if explicit in DATASET_ALIASES:
        return explicit if explicit not in {"osm"} else "openstreetmap"
    if explicit == "osm":
        return "openstreetmap"
    src = str(raw.get("source") or "").lower()
    parts = {p.lower() for p in path.parts}
    if "wikipedia" in src or "wikipedia" in parts:
        return "wikipedia"
    if "wikivoyage" in src or path.name.lower().startswith("jaipur.json"):
        return "wikivoyage"
    if "osm" in src or "openstreetmap" in src or "osm" in parts:
        return "openstreetmap"
    if "tourism" in src or "tourism" in parts:
        return "tourism"
    if "google" in src or "google" in parts:
        return "google_places"
    if "curated" in src or "curated" in parts:
        return "curated_places"
    return "other"


def _normalize_dataset_for_source(dataset: str) -> str:
    """Map corpus dataset → itinerary Source.dataset Literal."""
    key = (dataset or "other").lower()
    if key in {"openstreetmap", "osm"}:
        return "openstreetmap"
    if key == "wikivoyage":
        return "wikivoyage"
    if key == "wikipedia":
        return "wikipedia"
    return "other"


def _entry_to_doc(raw: dict, path: Path, city_fallback: str) -> CorpusDoc | None:
    text = (raw.get("text") or "").strip()
    if not text:
        return None
    dataset = _infer_dataset(raw, path)
    aliases_raw = raw.get("aliases") or []
    aliases = tuple(
        str(a).strip() for a in aliases_raw if str(a).strip()
    )
    place = str(raw.get("place_name") or raw.get("name") or "").strip() or None
    atomic_flag = bool(
        raw.get("atomic")
        or dataset
        in {"openstreetmap", "google_places", "curated_places", "tourism"}
        or (dataset == "wikipedia" and len(text) < 6000)
    )
    return CorpusDoc(
        city=str(raw.get("city") or city_fallback).strip(),
        title=str(raw.get("title") or place or path.stem).strip(),
        source=str(raw.get("source") or dataset).strip(),
        url=str(raw.get("url") or "").strip(),
        text=text,
        license=str(raw.get("license") or "CC BY-SA 4.0"),
        path=str(path),
        dataset=dataset,
        place_name=place,
        aliases=aliases,
        atomic=atomic_flag,
    )


def _load_file(path: Path) -> list[CorpusDoc]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Skip bad corpus file %s: %s", path.name, exc)
        return []

    city_fallback = path.stem.replace("_", " ").title()
    if path.parent.name not in {"corpus", "wikipedia", "osm", "tourism", "google", "curated"}:
        # e.g. corpus/wikipedia/hawa_mahal.json
        pass
    if path.parent.name != "corpus":
        city_fallback = "Jaipur"

    docs: list[CorpusDoc] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                doc = _entry_to_doc(item, path, city_fallback)
                if doc:
                    docs.append(doc)
        return docs

    if not isinstance(raw, dict):
        return []

    # Multi-doc bundle
    bundle = raw.get("documents") or raw.get("places") or raw.get("entries")
    if isinstance(bundle, list):
        city = str(raw.get("city") or city_fallback).strip()
        for item in bundle:
            if not isinstance(item, dict):
                continue
            merged = {**item}
            merged.setdefault("city", city)
            merged.setdefault("source", raw.get("source"))
            merged.setdefault("dataset", raw.get("dataset"))
            merged.setdefault("license", raw.get("license"))
            doc = _entry_to_doc(merged, path, city)
            if doc:
                docs.append(doc)
        return docs

    doc = _entry_to_doc(raw, path, city_fallback)
    return [doc] if doc else []


def load_corpus_docs(directory: Path | None = None) -> list[CorpusDoc]:
    root = directory or corpus_dir()
    if not root.is_dir():
        logger.warning("RAG corpus directory missing: %s", root)
        return []

    docs: list[CorpusDoc] = []
    for path in sorted(root.rglob("*.json")):
        # Skip non-corpus side files if any
        if path.name.startswith("_"):
            continue
        docs.extend(_load_file(path))
    logger.info("Loaded %d RAG corpus docs from %s", len(docs), root)
    return docs


def load_place_aliases(directory: Path | None = None) -> dict[str, list[str]]:
    """Map alias/lower-name → canonical place names from corpus metadata."""
    alias_map: dict[str, list[str]] = {}
    for doc in load_corpus_docs(directory):
        names = []
        if doc.place_name:
            names.append(doc.place_name)
        names.append(doc.title)
        names.extend(doc.aliases)
        canon = (doc.place_name or doc.title).strip()
        if not canon:
            continue
        for n in names:
            key = n.lower().strip()
            if not key:
                continue
            alias_map.setdefault(key, [])
            if canon not in alias_map[key]:
                alias_map[key].append(canon)
    # Built-in spelling variants
    builtins = {
        "niota dam": ["Nevta Dam"],
        "neota dam": ["Nevta Dam"],
        "nevta dam": ["Nevta Dam"],
        "moon gate": ["Moon Gate"],
        "amarnath cafe": ["Amarnath Cafe"],
        "amarnath café": ["Amarnath Cafe"],
        "anoki museum": ["Anokhi Museum"],
        # STT / spelling variants for Amer (Amber) Fort
        "amir fort": ["Amer Fort", "Amber Fort"],
        "amer fort": ["Amer Fort", "Amber Fort"],
        "amber fort": ["Amer Fort", "Amber Fort"],
        "amer": ["Amer Fort", "Amber Fort"],
        "amber": ["Amer Fort", "Amber Fort"],
        "amir": ["Amer Fort", "Amber Fort"],
    }
    for k, vals in builtins.items():
        alias_map.setdefault(k, [])
        for v in vals:
            if v not in alias_map[k]:
                alias_map[k].append(v)
    return alias_map


# Re-export helper for Source mapping
source_dataset_from_corpus = _normalize_dataset_for_source
