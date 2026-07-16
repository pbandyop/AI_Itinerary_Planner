"""Shared helpers for agentic LangGraph nodes (LLM + heuristic fallback).

Default provider is **Gemini** (`LLM_PROVIDER=gemini`). Set `GOOGLE_API_KEY`
(or `GEMINI_API_KEY`). OpenAI remains available via `LLM_PROVIDER=openai`.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-4o-mini",
}


def llm_provider() -> str:
    """Return 'gemini' | 'openai' (default gemini)."""
    raw = (os.getenv("LLM_PROVIDER") or "gemini").strip().lower()
    if raw in {"openai", "gpt"}:
        return "openai"
    return "gemini"


def llm_api_key() -> str | None:
    provider = llm_provider()
    if provider == "openai":
        return os.getenv("OPENAI_API_KEY") or None
    return (
        os.getenv("GOOGLE_API_KEY")
        or os.getenv("GEMINI_API_KEY")
        or None
    )


def default_chat_model() -> str:
    override = os.getenv("LLM_MODEL")
    if override:
        return override
    return _DEFAULT_MODELS[llm_provider()]


def llm_enabled(flag_env: str, *, default: str = "true") -> bool:
    if not llm_api_key():
        return False
    return os.getenv(flag_env, default).lower() not in {"0", "false", "no"}


def get_chat_model(*, model: str | None = None, temperature: float = 0) -> Any:
    """Build a LangChain chat model for the configured provider."""
    provider = llm_provider()
    model_name = model or default_chat_model()
    key = llm_api_key()
    if not key:
        raise RuntimeError(
            "No LLM API key set. For Gemini set GOOGLE_API_KEY; "
            "for OpenAI set OPENAI_API_KEY and LLM_PROVIDER=openai."
        )

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model_name, temperature=temperature, api_key=key)

    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
    except ImportError as exc:
        raise RuntimeError(
            "langchain-google-genai is required for Gemini. "
            "pip install langchain-google-genai"
        ) from exc

    return ChatGoogleGenerativeAI(
        model=model_name,
        temperature=temperature,
        google_api_key=key,
    )


def chat_json(
    *,
    system: str,
    human: str,
    model_env: str,
    default_model: str | None = None,
    temperature: float = 0,
) -> dict[str, Any] | None:
    """Invoke the configured chat model and parse a JSON object from the response."""
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM client unavailable: %s", exc)
        return None

    try:
        model = os.getenv(model_env) or default_model or default_chat_model()
        llm = get_chat_model(model=model, temperature=temperature)
        resp = llm.invoke(
            [SystemMessage(content=system), HumanMessage(content=human)]
        )
        content = resp.content
        if isinstance(content, list):
            # Gemini sometimes returns content blocks
            text = "".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            ).strip()
        else:
            text = str(content).strip()
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM JSON call failed (%s): %s", llm_provider(), exc)
    return None


_WEBSITE_IN_TEXT_RE = re.compile(
    r"(?:Website|Official site)\s*:\s*(https?://[^\s|,;]+)",
    re.I,
)


def preferred_source_url(source: Any, *, text: str | None = None) -> str | None:
    """Prefer an official website mentioned in the card text, else citation url."""
    blob_parts: list[str] = []
    if text:
        blob_parts.append(text)
    if isinstance(source, dict):
        for key in ("snippet", "text"):
            if source.get(key):
                blob_parts.append(str(source[key]))
        url = (source.get("url") or "").strip() or None
        title = source.get("title")
    else:
        snip = getattr(source, "snippet", None)
        if snip:
            blob_parts.append(str(snip))
        url = (getattr(source, "url", None) or "").strip() or None
        title = getattr(source, "title", None)
    blob = " ".join(blob_parts)
    m = _WEBSITE_IN_TEXT_RE.search(blob)
    if m:
        return m.group(1).rstrip(").,]\"'")
    # Google Places maps links are still valid citations when no website field.
    return url


def format_source_cite(source: Any, *, text: str | None = None) -> str:
    """Inline citation with a link whenever a URL is available."""
    if not source:
        return ""
    if isinstance(source, dict):
        title = (source.get("title") or "source").strip()
    else:
        title = (getattr(source, "title", None) or "source").strip()
    url = preferred_source_url(source, text=text)
    if url:
        return f" (Source: {title} - {url})"
    return f" (Source: {title})"


def ensure_source_link(reply: str, source: Any, *, text: str | None = None) -> str:
    """Guarantee the user-facing answer ends with a source link when we have one."""
    if not (reply or "").strip() or not source:
        return reply
    url = preferred_source_url(source, text=text)
    if url and url in reply:
        return reply
    # Drop a trailing bare (Source: …) so we can re-attach with the URL.
    cleaned = re.sub(r"\s*\(Source:\s*[^)]*\)\s*$", "", reply.rstrip()).rstrip()
    cite = format_source_cite(source, text=text)
    if not cite:
        return cleaned
    # If we already have Source: but no URL and still no URL, leave as-is.
    if "Source:" in reply and not url:
        return reply
    return f"{cleaned}{cite}"


def compose_grounded_reply(
    *,
    user_query: str,
    sources: list[dict[str, Any]],
    role_hint: str = "travel tip",
) -> str | None:
    """LLM answer grounded only in retrieved RAG snippets (cited). Fallback None."""
    if not sources or not llm_enabled("RAG_LLM", default="true"):
        return None
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
    except Exception as exc:  # noqa: BLE001
        logger.warning("RAG LLM unavailable: %s", exc)
        return None

    blocks: list[str] = []
    for i, src in enumerate(sources[:5], start=1):
        text = re.sub(r"\s+", " ", str(src.get("text") or "")).strip()
        if not text:
            continue
        # Drop catalog-line noise like leading "32 Albert Hall…" indices when possible
        text = re.sub(r"^\d+\s+", "", text)
        title = src.get("title") or "Source"
        url = preferred_source_url(src, text=text) or (src.get("url") or "")
        cite = f"{title}" + (f" ({url})" if url else "")
        # Hours answers need full weekday lines; other tips stay compact.
        cap = 1200 if role_hint == "opening_hours" else 500
        blocks.append(f"[{i}] {text[:cap]}\n    — {cite}")
    if not blocks:
        return None

    if role_hint == "opening_hours":
        system = (
            "You are a Jaipur travel assistant. Answer ONLY the opening hours for the "
            "place the user asked about, using ONLY the numbered sources. "
            "Reply in one short sentence (two max). "
            "Include the place name and the hours/days from the source. "
            "Do NOT include address, phone, rating, types, or paste the whole source card. "
            "A source link will be attached automatically — do not invent hours. "
            "If hours are not in the sources, reply exactly: "
            "NO_HOURS"
        )
        human = (
            f"User question: {user_query}\n\n"
            "Retrieved sources:\n"
            + "\n\n".join(blocks)
            + "\n\nWrite only the opening-hours answer now."
        )
        min_len = 12
    elif role_hint == "optional stop color":
        system = (
            "You are adding optional guide color about a place already justified "
            "by the planner. Use ONLY the numbered sources. Write 1–2 short "
            "sentences of traveler-facing flavor (what it’s known for). "
            "Cite (Source: Title - URL) when a URL is given. Do NOT explain why the itinerary algorithm "
            "chose the stop. Do not invent facts. If sources are off-topic, "
            "return exactly: NO_COLOR"
        )
        human = (
            f"User question: {user_query}\n\n"
            "Retrieved sources:\n" + "\n\n".join(blocks) + "\n\n"
            "Write the grounded answer now."
        )
        min_len = 20
    else:
        system = (
            "You are a helpful Jaipur travel assistant. Answer ONLY using the numbered "
            "sources below. Do not invent facts. Write 2–4 short, friendly sentences "
            "that directly answer the user — do not dump the raw source card. "
            "Cite sources inline like (Source: Title - URL) when a URL is given. "
            "If the sources do not answer, "
            "say you don't have a cited guide tip for that — do not guess. "
            f"Tone: clear {role_hint} for a traveler; no catalogs, phone dumps, or ratings lists "
            "unless the user asked for them."
        )
        human = (
            f"User question: {user_query}\n\n"
            "Retrieved sources:\n" + "\n\n".join(blocks) + "\n\n"
            "Write the grounded answer now."
        )
        min_len = 20
    try:
        llm = get_chat_model(temperature=0.2)
        resp = llm.invoke(
            [SystemMessage(content=system), HumanMessage(content=human)]
        )
        content = resp.content
        if isinstance(content, list):
            text = "".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            ).strip()
        else:
            text = str(content).strip()
        text = re.sub(r"\s+", " ", text).strip()
        upper = text.upper()
        if upper.startswith("NO_HOURS") or upper == "NO_HOURS":
            return None
        if upper.startswith("NO_COLOR") or upper == "NO_COLOR":
            return None
        if len(text) < min_len:
            return None
        # Always attach a source link when the corpus provided one.
        primary = sources[0] if sources else None
        if primary:
            text = ensure_source_link(
                text, primary, text=str(primary.get("text") or "")
            )
        return text[:1200]
    except Exception as exc:  # noqa: BLE001
        logger.warning("compose_grounded_reply failed: %s", exc)
        return None


def compact_itinerary(itin: Any) -> dict[str, Any]:
    """Shrink itinerary for LLM context (names/durations only)."""
    if itin is None:
        return {}
    days = []
    for day in getattr(itin, "days", []) or []:
        stops = []
        for s in day.all_stops:
            stops.append(
                {
                    "name": s.name,
                    "duration_min": s.duration_min,
                    "osm_id": s.osm_id,
                    "has_citation": bool(s.citations),
                    "uncertainty": s.uncertainty,
                }
            )
        days.append(
            {
                "day_index": day.day_index,
                "theme": day.theme,
                "total_duration_min": day.total_duration_min,
                "stop_count": len(stops),
                "stops": stops,
            }
        )
    trip = getattr(itin, "trip", None)
    return {
        "city": getattr(trip, "city", None),
        "num_days": getattr(trip, "num_days", None),
        "pace": getattr(trip, "pace", None),
        "traveler_profile": getattr(trip, "traveler_profile", None),
        "interests": list(getattr(trip, "interests", []) or []),
        "constraints": list(getattr(trip, "constraints", []) or [])[:6],
        "summary": getattr(itin, "summary", None),
        "days": days,
        "uncertainty_notes": list(getattr(itin, "uncertainty_notes", []) or []),
    }
