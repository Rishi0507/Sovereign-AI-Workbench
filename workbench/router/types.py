"""Router data types."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from workbench.core.labels import Label


class AttachmentInfo(BaseModel):
    """Measured facts about an attachment, gathered without reading it into the ledger."""

    name: str
    path: str
    ext: str
    pages: int = 1
    chars: int = 0
    scanned: bool = False
    needs_visual: bool = False
    script: str = "latin"
    label: Label
    columns: list[str] = Field(default_factory=list)
    size: int = 0


class TaskInput(BaseModel):
    id: str
    text: str
    workspace: str
    user: str
    attachments: list[AttachmentInfo] = Field(default_factory=list)
    label: Label
    interactive_limit_s: float | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class TaskProfile(BaseModel):
    task_type: str
    rule: str | None = None
    modalities: list[str]
    needs_tools: bool
    complexity: str
    needs_visual_after_extract: bool
    est_input_tokens: int
    languages: list[str]
    label: Label
    confidence: float
    classifier_confidence: float
    decoupled: bool = False


class Candidate(BaseModel):
    model: str
    ok: bool
    reason: str | None = None
    quality: float | None = None
    cost_s: float | None = None
    wait_s: float = 0.0
    meets_threshold: bool = False


class RouteDecision(BaseModel):
    task_id: str
    ts: datetime
    profile: TaskProfile
    threshold: float
    candidates: list[Candidate]
    chosen: str
    below_threshold: bool = False
    used_fallback: bool = False
    queued_for_tide: bool = False
    registry_version: str = ""
    log_line: str = ""
