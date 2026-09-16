"""Typed plan models (README section 4.1.4)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

StepStatus = Literal["pending", "running", "done", "failed", "incomplete", "denied", "skipped"]


class PlanInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source: Literal["attachment", "step", "kb_query"] = Field(alias="from")
    ref: str


class PlanStep(BaseModel):
    id: str
    title: str = ""
    tool: str | None = None
    model_task: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    default_args: dict[str, Any] | None = None
    inputs: list[PlanInput] = Field(default_factory=list)
    output_type: str = "records"
    output_schema: str | None = None
    side_effect: bool = False
    status: StepStatus = "pending"
    budget_s: float = 0.0
    note: str | None = None

    @property
    def action(self) -> str:
        return self.tool or self.model_task or "?"

    def step_refs(self) -> list[str]:
        return [i.ref for i in self.inputs if i.source == "step"]


class Deliverable(BaseModel):
    type: Literal["docx", "xlsx", "pptx", "code", "md"]
    name: str | None = None


class Plan(BaseModel):
    goal: str = ""
    steps: list[PlanStep]
    deliverables: list[Deliverable] = Field(default_factory=list)
    source: Literal["template", "model"] = "model"
    template: str | None = None
    template_version: int | None = None
    errors: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    est_time_s: float = 0.0
    repairs: int = 0
    valid: bool = False

    def step(self, step_id: str) -> PlanStep | None:
        return next((s for s in self.steps if s.id == step_id), None)

    def to_typed(self) -> dict[str, Any]:
        steps = []
        for s in self.steps:
            action = {"tool": s.tool} if s.tool else {"model_task": s.model_task}
            steps.append({"id": s.id, "title": s.title, "action": action, "args": s.args,
                          "inputs": [i.model_dump(by_alias=True) for i in s.inputs],
                          "output_type": s.output_type, "side_effect": s.side_effect})
        return {"goal": self.goal, "steps": steps, "deliverables": [d.model_dump(exclude_none=True) for d in self.deliverables]}

    @classmethod
    def from_typed(cls, data: dict[str, Any], source: Literal["template", "model"] = "model") -> Plan:
        steps = []
        for s in data.get("steps") or []:
            action = s.get("action") or {}
            steps.append(PlanStep(id=s["id"], title=s.get("title", ""), tool=action.get("tool"),
                                  model_task=action.get("model_task"), args=dict(s.get("args") or {}),
                                  inputs=[PlanInput.model_validate(i) for i in s.get("inputs") or []],
                                  output_type=s.get("output_type", "records"),
                                  side_effect=bool(s.get("side_effect", False))))
        return cls(goal=str(data.get("goal", "")), steps=steps,
                   deliverables=[Deliverable.model_validate(d) for d in data.get("deliverables") or []],
                   source=source)
