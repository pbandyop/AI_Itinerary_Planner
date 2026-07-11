"""Voice-edit patch contract — only intended sections should change."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from agent.schemas.itinerary import TimeOfDay


EditOperation = Literal[
    "relax_block",
    "swap_stop",
    "add_stop",
    "remove_stop",
    "reduce_travel",
    "make_indoor",
    "replace_block",
]


class EditTarget(BaseModel):
    day: int = Field(..., ge=1, le=4)
    block: TimeOfDay | None = None


class EditPatch(BaseModel):
    target: EditTarget
    operation: EditOperation
    payload: dict[str, Any] = Field(default_factory=dict)
    user_utterance: str | None = None
