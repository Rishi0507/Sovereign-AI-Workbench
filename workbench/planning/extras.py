"""Small additions a plan needs when the request asks for more than its template covers."""

from __future__ import annotations

import re

from workbench.planning.model_tasks import MODEL_TASKS
from workbench.planning.plan_schema import Plan, PlanInput, PlanStep

COUNT_ASK = re.compile(
    r"\b(?:how\s+many|number\s+of|count(?:\s+(?:of|the))?|total)\b[^.?!]{0,40}?\b(?:words?|pages?|characters?|tables?)\b"
    r"|\b(?:word|page|character)\s+count\b",
    re.IGNORECASE,
)
READABLE = (".pdf", ".ocr.json", ".md", ".txt", ".csv", ".json")


def wants_counts(text: str) -> bool:
    return bool(COUNT_ASK.search(text))


def add_requested_facts(plan: Plan, text: str, attachments: list[str]) -> bool:
    """Add a ``document_stats`` step when the request asks for counts the plan does not produce.

    Returns True when the plan was changed. The step is read-only and goes right after the
    first reading step, so later steps can use the counted facts.
    """
    docs = [a for a in attachments if a.lower().endswith(READABLE)]
    if not docs or not wants_counts(text) or any(s.tool == "document_stats" for s in plan.steps):
        return False
    step_id = "stats" if all(s.id != "stats" for s in plan.steps) else f"stats{len(plan.steps)}"
    step = PlanStep(id=step_id, title="Count pages and words", tool="document_stats",
                    args={"paths": docs}, default_args={"paths": docs},
                    inputs=[PlanInput(source="attachment", ref=d) for d in docs], output_type="facts")
    after = next((i + 1 for i, s in enumerate(plan.steps) if s.tool in {"read_document", "read_file"}), 0)
    plan.steps.insert(after, step)
    # Steps that write the answer should see the counted facts.
    for later in plan.steps[after + 1:]:
        spec = MODEL_TASKS.get(later.model_task or "")
        if spec is not None and ("facts" in spec.accepts or "*" in spec.accepts):
            later.inputs.append(PlanInput(source="step", ref=step_id))
    return True
