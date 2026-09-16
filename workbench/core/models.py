"""Evidence ledger data models (README section 4.1.3)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from workbench.core.labels import Label

RecordKind = Literal[
    "user_input", "plan", "attachment", "ocr_text", "vlm_read", "kb_chunk", "graph_fact",
    "sandbox_result", "calc_result", "check_result", "model_output", "tool_output",
]
CONTROL_KINDS: frozenset[str] = frozenset({"user_input", "plan"})
SOURCE_KINDS: frozenset[str] = frozenset({"ocr_text", "vlm_read", "kb_chunk", "graph_fact", "attachment"})
COMPUTE_KINDS: frozenset[str] = frozenset({"sandbox_result", "calc_result"})
Confidence = Literal["high", "medium", "low", "uncertain"]


class Anchor(BaseModel):
    doc: str
    revision: str | None = None
    page: int | None = None
    region: tuple[float, float, float, float] | None = None

    def cite(self) -> str:
        parts = [self.doc]
        if self.revision:
            parts.append(f"Rev {self.revision}")
        if self.page is not None:
            parts.append(f"p. {self.page}")
        return ", ".join(parts)


class TypedValue(BaseModel):
    kind: Literal["tag", "quantity", "date", "party", "po", "clause", "text"]
    raw: str
    normalised: str
    magnitude: float | None = None
    unit: str | None = None
    confidence: Confidence = "high"
    anchor: Anchor | None = None
    record: str | None = None


class LedgerRecord(BaseModel):
    id: str
    task_id: str
    seq: int
    kind: RecordKind
    trust: Literal["control", "data"]
    anchor: Anchor | None = None
    label: Label
    confidence: Confidence | None = None
    produced_by: str | None = None
    inputs: list[str] = Field(default_factory=list)
    summary: str
    body: str | dict[str, Any] | list[Any]
    fields: dict[str, TypedValue] = Field(default_factory=dict)
    hash: str = ""
    created_at: datetime

    def hash_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"hash"})

    def body_text(self) -> str:
        if isinstance(self.body, str):
            return self.body
        import json

        return json.dumps(self.body, ensure_ascii=False, sort_keys=True)

    def source(self) -> str:
        return self.anchor.cite() if self.anchor else (self.produced_by or self.kind)
