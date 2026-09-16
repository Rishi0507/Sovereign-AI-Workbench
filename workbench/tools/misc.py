"""``recall`` and ``finish``."""

from __future__ import annotations

from typing import Any

from workbench.core.errors import PolicyError
from workbench.tools.registry import ToolContext, ToolResult, ToolSpec, obj

RECALL_SCHEMA = obj({"id": {"type": "string", "pattern": "^R-[A-Za-z0-9]+-[0-9]+$"}}, ["id"])
FINISH_SCHEMA = obj({"summary": {"type": "string", "maxLength": 2000}})


def recall(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    rec = ctx.rt.ledger.get(str(args["id"]))
    if rec.task_id != ctx.task_id and rec.task_id not in ctx.meta.get("child_tasks", []):
        raise PolicyError("recall is limited to records of the current task")
    return ToolResult(ok=True, summary=f"recalled {rec.id} ({rec.kind})", records=[rec.id],
                      body={"records": [rec.id], "recall": rec.id, "text": rec.body_text()})


def finish(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    return ToolResult(ok=True, summary=str(args.get("summary") or "finished"), body={"finished": True})


SPECS = [
    ToolSpec(name="recall", description="Return the full body of a ledger record of this task.",
             input_schema=RECALL_SCHEMA, handler=recall, output_type="record", budget_key="recall"),
    ToolSpec(name="finish", description="End the agent loop.", input_schema=FINISH_SCHEMA, handler=finish,
             output_type="none", budget_key="finish"),
]
