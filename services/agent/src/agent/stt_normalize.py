"""Normalize common STT mishears for travel-planner voice turns."""

from __future__ import annotations

import re


# Whole-utterance confirm mishears (e.g. "Can fun" / bare "firm" → confirm).
_CONFIRM_UTTERANCE_RE = re.compile(
    r"^\s*("
    r"yes|yeah|yep|yup|y|ok|okay|sure|confirm|confirmed|"
    r"firm|can\s*fun|can\s*firm|come\s*firm|con\s*firm|"
    r"confurm|confrom|conform|confem|confurm|confirms|"
    r"looks\s+good|go\s+ahead|proceed|sounds\s+good|please\s+do|do\s+it"
    r")\s*[.!]?\s*$",
    re.I,
)

_CONFIRM_TOKEN_RE = re.compile(
    r"\b("
    r"yes|yeah|yep|yup|confirm|confirmed|sure|ok(?:ay)?|firm|"
    r"go\s+ahead|proceed|looks\s+good|sounds\s+good|"
    r"can\s*fun|can\s*firm|come\s*firm|con\s*firm|"
    r"confurm|confrom|conform|confem"
    r")\b",
    re.I,
)

# Pace: packed misheard as packt / pac / pact / pack / pat / fact / packet
_PACKED_TOKEN_RE = re.compile(
    r"\b("
    r"packed|packt|packet|packe|packd|pact|pac|pat|fact|"
    r"pack(?:ed)?"
    r")\b",
    re.I,
)

_PACKED_WORDS = frozenset(
    {
        "packed",
        "packt",
        "packet",
        "packe",
        "packd",
        "pact",
        "pac",
        "pat",
        "fact",
        "pack",
    }
)

# Whole utterance that is only a pace answer.
_PACKED_UTTERANCE_RE = re.compile(
    r"^\s*("
    r"packed|packt|packet|packe|packd|pact|pac|pat|fact|pack|"
    r"busy|intense|full[\s-]?day"
    r")\s*(?:pace)?\s*[.!]?\s*$",
    re.I,
)


def normalize_stt_message(message: str) -> str:
    """Rewrite frequent STT errors before intent / slot parsing."""
    text = (message or "").strip()
    if not text:
        return text
    text = re.sub(r"\s+", " ", text)

    if _CONFIRM_UTTERANCE_RE.match(text):
        return "confirm"

    if _PACKED_UTTERANCE_RE.match(text):
        return "packed"

    # Replace pace tokens in longer phrases ("2 days pat heritage…").
    def _pace_sub(m: re.Match[str]) -> str:
        raw = m.group(1).lower()
        if raw in _PACKED_WORDS:
            return "packed"
        return m.group(0)

    text = _PACKED_TOKEN_RE.sub(_pace_sub, text)

    # Isolated confirm-ish phrases inside short replies.
    if len(text.split()) <= 3 and _CONFIRM_TOKEN_RE.search(text):
        # Avoid rewriting "confirm day 2" style edits — only short yes/confirm.
        if not re.search(r"\b(day|add|remove|swap|change|make)\b", text, re.I):
            return "confirm"

    return text
