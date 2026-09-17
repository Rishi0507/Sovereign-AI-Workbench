"""Task state persisted in SQLite, plus the task store."""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import literal_column, select

from workbench.core.clock import Clock, SystemClock
from workbench.core.db import Database, tasks_table
from workbench.core.errors import NotFound
from workbench.core.labels import Label
from workbench.planning.plan_schema import Plan

TaskStatus = Literal[
    "queued", "routing", "planning", "awaiting_plan", "waiting_tide", "running", "awaiting_action",
    "rendering", "awaiting_deliverable", "completed", "failed", "handed_back", "rejected", "cancelled",
]
TERMINAL = frozenset({"completed", "failed", "handed_back", "rejected", "cancelled"})


class StepTrace(BaseModel):
    n: int
    ts: str
    step_id: str | None = None
    kind: Literal["route", "plan", "decide", "tool", "model", "default", "gate", "escalation", "info", "error",
                  "sandbox", "check", "finish"]
    model: str | None = None
    purpose: str | None = None
    tool: str | None = None
    args: dict[str, Any] | None = None
    args_hash: str | None = None
    ok: bool = True
    summary: str = ""
    records: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    latency_s: float = 0.0
    cached_tokens: int = 0
    prompt_tokens: int = 0
    spec: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class Gate(BaseModel):
    id: str
    kind: Literal["plan", "action", "deliverable", "template_choice"]
    status: Literal["pending", "approved", "rejected", "edited"] = "pending"
    step_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    decided_by: str | None = None
    decided_at: str | None = None
    note: str | None = None


class FigureDecision(BaseModel):
    figure: str
    action: Literal["correct", "link", "confirm"]
    record_id: str | None = None
    value: str | None = None
    note: str | None = None
    by: str
    at: str


class DeliverableState(BaseModel):
    file_id: str
    relpath: str
    kind: str
    label: Label
    step_id: str
    provenance: dict[str, Any] = Field(default_factory=dict)
    claims: list[dict[str, Any]] = Field(default_factory=list)
    status: Literal["draft", "approved", "rejected"] = "draft"
    final_file_id: str | None = None


class TaskState(BaseModel):
    id: str
    workspace: str
    user: str
    text: str
    attachments: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    parent_id: str | None = None
    status: TaskStatus = "queued"
    status_note: str = ""
    created_at: str
    updated_at: str
    route: dict[str, Any] | None = None
    model: str | None = None
    escalations: list[dict[str, Any]] = Field(default_factory=list)
    plan: Plan | None = None
    plan_history: list[dict[str, Any]] = Field(default_factory=list)
    template_options: list[str] = Field(default_factory=list)
    trace: list[StepTrace] = Field(default_factory=list)
    outputs: dict[str, Any] = Field(default_factory=dict)
    gates: list[Gate] = Field(default_factory=list)
    deliverables: list[DeliverableState] = Field(default_factory=list)
    checks: list[dict[str, Any]] = Field(default_factory=list)
    acknowledged: list[str] = Field(default_factory=list)
    figure_decisions: list[FigureDecision] = Field(default_factory=list)
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    label: Label | None = None
    label_floor: Label | None = None
    template_fallbacks: int = 0
    revisions: int = 0
    children: list[str] = Field(default_factory=list)
    steps_used: int = 0
    revision_no: int = 0  # increases on every save; clients use it to notice changes

    def gate(self, gate_id: str) -> Gate:
        for g in self.gates:
            if g.id == gate_id:
                return g
        raise NotFound(f"gate {gate_id} not found")

    def pending_gate(self) -> Gate | None:
        return next((g for g in self.gates if g.status == "pending"), None)


class TaskStore:
    def __init__(self, db: Database, clock: Clock | None = None) -> None:
        self.db = db
        self.clock = clock or SystemClock()
        self._lock = threading.RLock()
        self._cache: dict[str, TaskState] = {}

    def now(self) -> str:
        return self.clock.now().isoformat()

    def create(self, state: TaskState) -> TaskState:
        with self._lock, self.db.tx() as conn:
            conn.execute(tasks_table.insert().values(
                id=state.id, workspace=state.workspace, user_id=state.user, parent_id=state.parent_id,
                status=state.status, created_at=state.created_at, updated_at=state.updated_at,
                data=state.model_dump_json()))
            self._cache[state.id] = state
        return state

    def get(self, task_id: str) -> TaskState:
        with self._lock:
            if task_id in self._cache:
                return self._cache[task_id].model_copy(deep=True)
        with self.db.read() as conn:
            row = conn.execute(select(tasks_table.c.data).where(tasks_table.c.id == task_id)).first()
        if row is None:
            raise NotFound(f"task {task_id} not found")
        state = TaskState.model_validate_json(row[0])
        with self._lock:
            self._cache[task_id] = state
        return state.model_copy(deep=True)

    def save(self, state: TaskState) -> TaskState:
        state.updated_at = self.now()
        with self._lock, self.db.tx() as conn:
            cached = self._cache.get(state.id)
            state.revision_no = max(state.revision_no, cached.revision_no if cached else 0) + 1
            conn.execute(tasks_table.update().where(tasks_table.c.id == state.id).values(
                status=state.status, updated_at=state.updated_at, data=state.model_dump_json()))
            self._cache[state.id] = state.model_copy(deep=True)
        return state

    def followups(self, task_id: str) -> list[TaskState]:
        """Follow-up tasks of a conversation, oldest first."""
        stmt = (select(tasks_table.c.id).where(tasks_table.c.parent_id == task_id)
                .order_by(tasks_table.c.created_at, literal_column("rowid")))  # rowid keeps insertion order
        with self.db.read() as conn:
            ids = [r[0] for r in conn.execute(stmt).all()]
        out = [self.get(i) for i in ids]
        return [t for t in out if t.meta.get("followup_of")]

    def list(self, workspace: str | None = None, limit: int = 100) -> list[TaskState]:
        stmt = select(tasks_table.c.data).order_by(tasks_table.c.created_at.desc()).limit(limit)
        if workspace:
            stmt = stmt.where(tasks_table.c.workspace == workspace)
        with self.db.read() as conn:
            rows = conn.execute(stmt).all()
        return [TaskState.model_validate_json(r[0]) for r in rows]


def iso_now(clock: Clock) -> str:
    return clock.now().isoformat()


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)
