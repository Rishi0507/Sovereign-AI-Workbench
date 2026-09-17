"""Sub-agent delegation (README section 4.1).

A child task gets a fresh ledger that inherits the parent's label as a floor, is routed on its
own profile, runs its own loop, and returns a short summary plus files. The parent receives a
``model_output`` record whose inputs are the child's records.
"""

from __future__ import annotations

from typing import Any

from workbench.agent.approvals import (
    ActionDecision,
    AutoApprove,
    ChoiceDecision,
    DeliverableDecision,
    PlanDecision,
)
from workbench.core.errors import ToolError
from workbench.tools.registry import ToolContext, ToolResult, ToolSpec, obj

SCHEMA = obj({
    "task": {"type": "string", "minLength": 5, "maxLength": 2000},
    "type": {"enum": ["code", "document", "general"]},
    "files": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
}, ["task", "type"])


class InheritedApproval(AutoApprove):
    """The parent's approved plan covers the child's plan; side effects still use the parent's gate."""

    def __init__(self, parent_gate: Any, parent_task: str) -> None:
        super().__init__()
        self.parent_gate = parent_gate
        self.parent_task = parent_task

    def approve_plan(self, task_id: str, gate_id: str, plan: Any, cancelled: Any = None) -> PlanDecision:
        return PlanDecision("approve", by=f"inherited:{self.parent_task}")

    def approve_action(self, task_id: str, gate_id: str, action: dict[str, Any],
                       cancelled: Any = None) -> ActionDecision:
        return ActionDecision(True, by=f"inherited:{self.parent_task}")

    def choose_template(self, task_id: str, gate_id: str, options: list[str], cancelled: Any = None) -> ChoiceDecision:
        return ChoiceDecision(options[0], by=f"inherited:{self.parent_task}")

    def approve_deliverable(self, task_id: str, gate_id: str, draft: dict[str, Any],
                            cancelled: Any = None) -> DeliverableDecision:
        return DeliverableDecision("approve", by=f"inherited:{self.parent_task}",
                                   acknowledged=list(draft.get("mismatches", [])))


def delegate(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    from workbench.agent.loop import Orchestrator

    rt = ctx.rt
    parent = rt.tasks.get(ctx.task_id)
    text = str(args["task"])
    if args["type"] == "code" and "script" not in text.lower():
        text = f"Write a Python script to {text[0].lower()}{text[1:]}"
    floor = ctx.label()
    child_orch = Orchestrator(rt, InheritedApproval(None, ctx.task_id))
    child = child_orch.create_task(ctx.workspace, ctx.user, text, list(args.get("files") or []),
                                   meta={"delegated_by": ctx.task_id, "type": args["type"]},
                                   parent_id=ctx.task_id, label_floor=floor)
    parent.children.append(child.id)
    rt.tasks.save(parent)
    done = child_orch.run(child.id, ctx.cancelled)
    child_records = rt.ledger.for_task(done.id)
    if done.status != "completed":
        raise ToolError(f"sub-agent {done.id} ended with status {done.status}: {done.error or done.status_note}")
    returned = [r.id for r in child_records if r.kind in {"sandbox_result", "calc_result", "model_output",
                                                          "kb_chunk", "ocr_text"}]
    files = [d.final_file_id or d.file_id for d in done.deliverables]
    code = done.result.get("code") or {}
    files += [f for f in code.get("files", []) if f not in files]
    summary = (f"sub-agent {done.id} ({done.model}) finished: " + (done.result.get("summary") or "")
               + (f" script {code.get('script_file')} after {code.get('attempts')} attempt(s)" if code else ""))
    rec = rt.ledger.add(ctx.task_id, "model_output", summary=summary[:300],
                        body={"child_task": done.id, "model": done.model, "summary": summary,
                              "result": code.get("result"), "files": files, "records": returned},
                        label=rt.ledger.high_water(done.id, floor), produced_by=ctx.call_id, inputs=returned,
                        confidence="medium")
    return ToolResult(ok=True, summary=summary, records=[rec.id], files=files,
                      body={"records": [rec.id], "child_task": done.id, "result": code.get("result"),
                            "files": files})


SPEC = ToolSpec(name="delegate", description="Run a sub-agent with a fresh context (code, document or general).",
                input_schema=SCHEMA, handler=delegate, output_type="delegated", budget_key="delegate")
