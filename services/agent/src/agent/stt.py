"""Speech-to-text for voice input (MediaRecorder → Gemini / OpenAI Whisper)."""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

from agent.nodes.llm_utils import default_chat_model, llm_api_key, llm_provider

logger = logging.getLogger(__name__)

_TRANSCRIBE_PROMPT = (
    "Transcribe the user's spoken travel-planning request into plain English text. "
    "Return ONLY the transcript — no quotes, labels, or commentary. "
    "If the audio is empty or unintelligible, return an empty string."
)


def transcribe_audio(data: bytes, *, mime_type: str = "audio/webm") -> str:
    """Transcribe raw audio bytes using the configured LLM provider."""
    if not data:
        return ""
    key = llm_api_key()
    if not key:
        raise RuntimeError(
            "No API key for STT. Set GOOGLE_API_KEY (Gemini) or OPENAI_API_KEY."
        )

    mime = (mime_type or "audio/webm").split(";")[0].strip().lower()
    if mime in {"audio/mp4", "audio/m4a", "video/mp4"}:
        mime = "audio/mp4"
    elif mime in {"audio/mpeg", "audio/mp3"}:
        mime = "audio/mpeg"
    elif mime not in {"audio/webm", "audio/wav", "audio/ogg", "audio/flac", "audio/mp4"}:
        mime = "audio/webm"

    provider = llm_provider()
    if provider == "openai":
        return _transcribe_openai(data, mime_type=mime, api_key=key)
    return _transcribe_gemini(data, mime_type=mime, api_key=key)


def _transcribe_gemini(data: bytes, *, mime_type: str, api_key: str) -> str:
    model_name = os.getenv("STT_MODEL") or default_chat_model()
    b64 = base64.b64encode(data).decode("ascii")

    # Prefer LangChain multimodal (already a project dependency).
    try:
        from langchain_core.messages import HumanMessage
        from langchain_google_genai import ChatGoogleGenerativeAI

        llm = ChatGoogleGenerativeAI(
            model=model_name,
            google_api_key=api_key,
            temperature=0,
        )
        msg = HumanMessage(
            content=[
                {"type": "text", "text": _TRANSCRIBE_PROMPT},
                {
                    "type": "media",
                    "mime_type": mime_type,
                    "data": b64,
                },
            ]
        )
        result = llm.invoke([msg])
        text = _content_to_text(result.content).strip()
        if text:
            return text
    except Exception:
        logger.exception("LangChain Gemini STT failed; trying google.generativeai")

    try:
        import google.generativeai as genai

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name)
        response = model.generate_content(
            [
                _TRANSCRIBE_PROMPT,
                {"mime_type": mime_type, "data": data},
            ]
        )
        return (getattr(response, "text", None) or "").strip()
    except Exception as exc:
        logger.exception("Gemini STT failed")
        raise RuntimeError(f"Speech transcription failed: {exc}") from exc


def _transcribe_openai(data: bytes, *, mime_type: str, api_key: str) -> str:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "openai package required for Whisper STT when LLM_PROVIDER=openai"
        ) from exc

    ext = "webm"
    if "wav" in mime_type:
        ext = "wav"
    elif "mpeg" in mime_type or "mp3" in mime_type:
        ext = "mp3"
    elif "mp4" in mime_type or "m4a" in mime_type:
        ext = "m4a"
    elif "ogg" in mime_type:
        ext = "ogg"

    client = OpenAI(api_key=api_key)
    transcript = client.audio.transcriptions.create(
        model=os.getenv("STT_MODEL") or "whisper-1",
        file=(f"speech.{ext}", data, mime_type),
    )
    return (getattr(transcript, "text", None) or str(transcript) or "").strip()


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif hasattr(block, "text"):
                parts.append(str(getattr(block, "text") or ""))
        return " ".join(parts)
    return str(content)
