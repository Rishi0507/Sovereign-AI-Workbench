"""Plan compiler: checks, default-argument derivation and the repair loop (README section 4.1.4)."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

from workbench.core.errors import InvalidModelOutput
from workbench.core.labels import Label
from workbench.llm import structured
from workbench.llm.base import ChatMessage, LLMBackend, LLMRequest
from workbench.llm.prompts import render_prompt
from workbench.llm.schemas import load_schema, validation_errors
from workbench.planning.model_tasks import MODEL_TASKS, RENDERERS, TOOL_ACCEPTS, TOOL_OUTPUTS
from workbench.planning.plan_schema import Plan, PlanStep
from workbench.planning.templates import PLACEHOLDER_RE
from workbench.tools.registry import ToolRegistry

DOC_KIND = {"note": "approval_note", "summary": "summary", "answer": "answer", "calc_result": "calc_sheet"}
XLSX_KIND = {"comparison": "offer_comparison", "calc_result": "calc_sheet", "findings": "findings"}


@dataclass
class CompileContext:
    tools: ToolRegistry
    task_label: Label
    workspace_ceiling: Label
    max_steps: int
    budgets: dict[str, float]
    attachments: list[str] = field(default_factory=list)
    model_task_budget: float = 4.0


def _strip_placeholders(args: dict[str, Any], schema: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Drop arguments that are pure placeholders (resolved at run time) before schema validation."""
    args = copy.deepcopy(args)
    schema = copy.deepcopy(schema)
    for key in list(args):
        v = args[key]
        if isinstance(v, str) and PLACEHOLDER_RE.fullmatch(v.strip()):
            del args[key]
            if key in schema.get("required", []):
                schema["required"] = [r for r in schema["required"] if r != key]
        elif isinstance(v, dict) and any(isinstance(x, str) and PLACEHOLDER_RE.fullmatch(x.strip())
                                         for x in v.values()):
            args[key] = {k: ({} if isinstance(x, str) and PLACEHOLDER_RE.fullmatch(x.strip()) else x)
                         for k, x in v.items()}
            prop = schema.get("properties", {}).get(key, {})
            if prop.get("type") != "object":
                del args[key]
    schema.pop("anyOf", None)
    # Required arguments that the model writes when the step runs (a script, for example) are
    # enforced at execution time; the compiler type-checks what the plan already provides.
    schema.pop("required", None)
    return args, schema


def derive_default_args(step: PlanStep, plan: Plan, attachments: list[str]) -> dict[str, Any] | None:
    """Arguments fully determined by the step's inputs, so the deterministic fallback applies."""
    refs = {r: plan.step(r) for r in step.step_refs()}
    by_type = {s.output_type: sid for sid, s in refs.items() if s is not None}
    att = [i.ref for i in step.inputs if i.source == "attachment"] or attachments
    tool = step.tool
    if tool == "read_document" and att:
        mode = "tables" if step.output_type == "tables" else ("findings" if step.output_type == "findings" else "full")
        return {"paths": att, "mode": mode} if len(att) > 1 else {"path": att[0], "mode": mode}
    if tool == "read_file" and att:
        return {"path": att[0]}
    if tool == "check_consistency" and "findings" in by_type:
        against: list[Any] = ["asset_register"]
        against += [f"{{{sid}}}" for t, sid in by_type.items() if t in {"kb_passages", "graph_facts"}]
        return {"facts": f"{{{by_type['findings']}}}", "against": against}
    if tool == "calculate" and "calc_inputs" in by_type:
        src = by_type["calc_inputs"]
        return {k: f"{{{src}.{k}}}" for k in ("expression", "variables", "result_name", "result_unit", "title")}
    if tool == "make_docx":
        for t, kind in DOC_KIND.items():
            if t in by_type:
                return {"template": kind, "data": f"{{{by_type[t]}}}"}
    if tool == "make_xlsx":
        for t, kind in XLSX_KIND.items():
            if t in by_type:
                key = {"comparison": "comparison", "calc_result": "calc", "findings": "findings"}[t]
                return {"kind": kind, "data": {key: f"{{{by_type[t]}}}"}}
    if tool == "make_pptx" and "deck" in by_type:
        return {"data": f"{{{by_type['deck']}}}"}
    if tool == "search_kb" and step.args.get("queries"):
        return dict(step.args)
    return None


def compile_plan(plan: Plan, ctx: CompileContext) -> Plan:
    plan = plan.model_copy(deep=True)
    errors: list[str] = []
    notes: list[str] = []
    produced: dict[str, str] = {}
    produced_types: set[str] = set()
    for n, step in enumerate(plan.steps, start=1):
        label = f"step {n}"
        spec = ctx.tools.get(step.tool) if step.tool else None
        mt = MODEL_TASKS.get(step.model_task) if step.model_task else None
        # 1. action exists
        if step.tool and spec is None:
            errors.append(f'{label}: unknown tool "{step.tool}"')
            continue
        if step.model_task and mt is None:
            errors.append(f'{label}: unknown model task "{step.model_task}"')
            continue
        if not step.tool and not step.model_task:
            errors.append(f"{label}: no tool or model task")
            continue
        # 2. arguments type-check
        if spec is not None:
            args, schema = _strip_placeholders(step.args, spec.input_schema)
            if step.args or step.default_args is None:
                derived = derive_default_args(step, plan, ctx.attachments)
                if not step.args and derived:
                    step.args = derived
                    args, schema = _strip_placeholders(step.args, spec.input_schema)
                for err in validation_errors(args, schema):
                    errors.append(f"{label}: argument {err}")
            allowed = TOOL_OUTPUTS.get(spec.name, [spec.output_type])
            if step.output_type not in allowed:
                if step.output_type == "records":
                    step.output_type = allowed[0]
                else:
                    errors.append(f'{label}: tool "{spec.name}" cannot produce "{step.output_type}"')
        elif mt is not None and step.output_type in {"records", ""}:
            step.output_type = mt.output_type
        # 3. data flow
        accepts = mt.accepts if mt is not None else TOOL_ACCEPTS.get(step.tool or "", ["*"])
        for inp in step.inputs:
            if inp.source == "attachment" and inp.ref not in ctx.attachments:
                errors.append(f'{label}: attachment "{inp.ref}" is not attached to the task')
            if inp.source != "step":
                continue
            src_type = produced.get(inp.ref)
            if src_type is None and inp.ref in produced_types:
                src_type = inp.ref
            if src_type is None:
                errors.append(f'{label}: input "{inp.ref}" is not produced by any earlier step')
                continue
            if "*" not in accepts and src_type not in accepts:
                errors.append(f'{label}: input "{inp.ref}" has type "{src_type}" but '
                              f'{step.action} accepts {", ".join(accepts) or "no step inputs"}')
        for ph in {p.split(".")[0].split("[")[0] for p in _all_placeholders(step.args)}:
            if ph in {s.id for s in plan.steps[n - 1:]}:
                errors.append(f'{label}: argument refers to "{ph}", which has not run yet')
        # 4. side effects are gated
        needs_gate = bool(spec and spec.side_effect) or bool(mt and mt.side_effect)
        if needs_gate and not step.side_effect:
            step.side_effect = True
            notes.append(f"{label}: approval gate inserted for side effect of {step.action}")
        # default args
        if spec is not None and step.default_args is None:
            step.default_args = derive_default_args(step, plan, ctx.attachments)
        step.budget_s = ctx.budgets.get(step.tool or "", ctx.model_task_budget) if step.tool else ctx.model_task_budget
        produced[step.id] = step.output_type
        produced_types.add(step.output_type)
    # 5. deliverables have renderers
    actions = {s.tool or s.model_task for s in plan.steps}
    for d in plan.deliverables:
        renderers = RENDERERS.get(d.type, [])
        if not any(r in actions for r in renderers):
            errors.append(f'deliverable "{d.type}" has no renderer step ({" or ".join(renderers)})')
    # 6. labels fit the workspace
    if not ctx.workspace_ceiling.dominates(ctx.task_label):
        errors.append(f"task label {ctx.task_label.display()} exceeds the workspace ceiling "
                      f"{ctx.workspace_ceiling.display()}")
    # 7. step count
    if len(plan.steps) > ctx.max_steps:
        errors.append(f"plan has {len(plan.steps)} steps; the limit is {ctx.max_steps}")
    ids = [s.id for s in plan.steps]
    if len(ids) != len(set(ids)):
        errors.append("step ids must be unique")
    # 8. time estimate
    plan.est_time_s = round(sum(s.budget_s for s in plan.steps), 1)
    plan.errors = errors
    plan.notes = notes
    plan.valid = not errors
    return plan


def _all_placeholders(value: Any) -> list[str]:
    from workbench.planning.templates import placeholders

    return placeholders(value)


def compile_with_repair(backend: LLMBackend, model: str, plan: Plan, ctx: CompileContext, max_repairs: int,
                        base_messages: list[ChatMessage], task_id: str | None = None,
                        max_retries: int = 2) -> tuple[Plan, list[dict[str, Any]]]:
    history: list[dict[str, Any]] = []
    compiled = compile_plan(plan, ctx)
    history.append({"attempt": 0, "errors": compiled.errors})
    repairs = 0
    while not compiled.valid and repairs < max_repairs:
        repairs += 1
        prompt = render_prompt("plan_repair", errors=compiled.errors,
                               plan_json=json.dumps(compiled.to_typed(), ensure_ascii=False))
        req = LLMRequest(model=model, purpose="plan.repair", task_id=task_id,
                         messages=[*base_messages, ChatMessage(role="user", content=prompt)],
                         max_tokens=2048,
                         meta={"errors": compiled.errors, "plan": compiled.to_typed(), "repair": repairs})
        try:
            fixed = structured.call(backend, req, load_schema("typed_plan"), max_retries).value
        except InvalidModelOutput as exc:
            history.append({"attempt": repairs, "errors": [str(exc)]})
            break
        candidate = Plan.from_typed(fixed)
        compiled = compile_plan(candidate, ctx)
        history.append({"attempt": repairs, "errors": compiled.errors})
    compiled.repairs = repairs
    return compiled, history
