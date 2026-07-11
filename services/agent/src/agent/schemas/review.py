"""Reviewer agent verdict — approve or request targeted revisions."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ReviewStatus = Literal["approve", "revise"]


class ReviewIssue(BaseModel):
    code: Literal[
        "feasibility_duration",
        "feasibility_travel",
        "feasibility_pace",
        "grounding_osm",
        "grounding_citation",
        "edit_scope",
        "missing_data",
        "other",
    ]
    message: str
    section: str | None = Field(
        default=None, description="e.g. day2.afternoon or trip.pace"
    )


class ReviewerVerdict(BaseModel):
    status: ReviewStatus
    issues: list[ReviewIssue] = Field(default_factory=list)
    affected_sections: list[str] = Field(default_factory=list)
    notes: str | None = None
