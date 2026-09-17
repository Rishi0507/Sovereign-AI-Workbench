"""The orchestrator: route, plan, compile, approve, execute, check, deliver (README section 4.1).

The model fills one step at a time. The orchestrator owns control flow, labels, approvals,
fallbacks and escalation, so a model error or an injected instruction cannot add steps,
skip a gate or lower a marking.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import date
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from workbench.agent.approvals import (
    ActionDecision,
    ApprovalGate,
    Cancelled,
    DeliverableDecision,
    PlanDecision,
)
from workbench.agent.state import (
    TERMINAL,
    DeliverableState,
    FigureDecision,
    Gate,
    StepTrace,
    TaskState,
)
from workbench.checks import citations, provenance
from workbench.context.cache_salt import cache_salt
from workbench.context.compiler import CompiledContext, compile_context
from workbench.core.errors import InvalidModelOutput, PolicyError, WorkbenchError
from workbench.core.ids import new_id, new_task_id, sha256_json
from workbench.core.labels import Label
from workbench.core.models import Anchor, LedgerRecord
from workbench.core.normalise import norm_date
from workbench.documents.readers import TextFileReader, display_name
from workbench.llm import structured
from workbench.llm.base import ChatMessage, LLMRequest, LLMResponse
from workbench.llm.prompts import render_prompt
from workbench.llm.schemas import load_schema, validation_errors
from workbench.planning.compiler import CompileContext, compile_plan, compile_with_repair
from workbench.planning.model_tasks import MODEL_TASKS, SCHEMA_TO_TASK, ModelTaskSpec
from workbench.planning.plan_schema import Plan, PlanStep
from workbench.planning.templates import resolve
from workbench.pool.manager import Cancelled as PoolCancelled
from workbench.pool.manager import SwapTicket
from workbench.router.types import AttachmentInfo, RouteDecision, TaskInput
from workbench.tools.registry import ToolContext, ToolResult

if TYPE_CHECKING:
    from workbench.runtime import Runtime

MAP_BATCH_PAGES = 8


class HandBack(WorkbenchError):
    pass


class StepLimit(WorkbenchError):
    pass


class Orchestrator:
    def __init__(self, rt: Runtime, approvals: ApprovalGate) -> None:
        self.rt = rt
        self.approvals = approvals
        self._tickets: dict[str, SwapTicket] = {}

    # ------------------------------------------------------------------------------------------
    # helpers

    @property
    def s(self) -> Any:
        return self.rt.settings

    def _now(self) -> str:
        return self.rt.clock.now().isoformat()

    def _trace(self, state: TaskState, kind: str, **kw: Any) -> StepTrace:
        entry = StepTrace(n=len(state.trace) + 1, ts=self._now(), kind=kind, **kw)  # type: ignore[arg-type]
        state.trace.append(entry)
        self.rt.tasks.save(state)
        if kind in {"tool", "model", "default", "decide", "sandbox"}:
            self.rt.audit.append({
                "type": "step", "task": state.id, "step": entry.step_id, "kind": kind, "model": entry.model,
                "purpose": entry.purpose, "tool": entry.tool, "args_hash": entry.args_hash, "ok": entry.ok,
                "record_ids": entry.records, "latency_s": entry.latency_s, "cached_tokens": entry.cached_tokens,
                "spec": entry.spec,
            })
        return entry

    def _status(self, state: TaskState, status: str, note: str = "") -> None:
        state.status = status  # type: ignore[assignment]
        state.status_note = note
        state.label = self.label(state)
        self.rt.tasks.save(state)

    def label(self, state: TaskState) -> Label:
        return self.rt.ledger.high_water(state.id, state.label_floor)

    def _salt(self, state: TaskState) -> str:
        return cache_salt(self.label(state), self.rt.secret_key)

    def _gate_open(self, gate_id: str) -> None:
        opener = getattr(self.approvals, "open", None)
        if opener is not None:
            opener(gate_id)

    def _new_gate(self, state: TaskState, kind: str, payload: dict[str, Any], step_id: str | None = None) -> Gate:
        gate = Gate(id=new_id("G"), kind=kind, payload=payload, step_id=step_id)  # type: ignore[arg-type]
        self._gate_open(gate.id)
        state.gates.append(gate)
        return gate

    def _close_gate(self, task_id: str, gate_id: str, status: str, by: str, note: str | None) -> TaskState:
        state = self.rt.tasks.get(task_id)
        gate = state.gate(gate_id)
        gate.status = status  # type: ignore[assignment]
        gate.decided_by = by
        gate.decided_at = self._now()
        gate.note = note
        self.rt.tasks.save(state)
        self.rt.audit.append({"type": f"gate.{gate.kind}", "task": task_id, "gate": gate_id, "decision": status,
                              "by": by, "note": note, "step": gate.step_id})
        return state

    # ------------------------------------------------------------------------------------------
    # task creation

    def attachment_info(self, workspace: str, rel: str) -> tuple[AttachmentInfo, dict[str, Any]]:
        rt = self.rt
        path = rt.files.resolve(workspace, rel)
        frec = rt.files.find(workspace, rel)
        if frec is None:
            rt.files.sync(workspace)
            frec = rt.files.find(workspace, rel)
        if frec is None:
            raise PolicyError(f"{rel} is not a registered workspace file")
        reader = rt.reader
        ext = ".pdf" if path.name.endswith(".ocr.json") else path.suffix.lower()
        pages, chars, scanned, needs_visual, script, columns = 1, 0, False, False, "latin", []
        try:
            if ext == ".csv" or ext in {".md", ".txt"}:
                page = TextFileReader().read(path)[0]
                chars, script = len(page.text), page.script
                if page.tables and ext == ".csv":
                    columns = page.tables[0].header
            else:
                read = reader.read(path)
                pages = len(read)
                chars = sum(len(p.text) for p in read)
                scanned = any(p.scanned for p in read)
                needs_visual = any(r.needs_vlm for p in read for r in p.regions) or scanned
                scripts = {p.script for p in read}
                script = "mixed" if len(scripts) > 1 or "mixed" in scripts else next(iter(scripts), "latin")
        except (OSError, ValueError):
            pass
        info = AttachmentInfo(name=display_name(path), path=rel, ext=ext, pages=pages, chars=chars,
                              scanned=scanned, needs_visual=needs_visual, script=script, label=frec.label,
                              columns=columns, size=frec.size)
        return info, {"file_id": frec.id, "sha256": frec.sha256}

    def create_task(self, workspace: str, user: str, text: str, attachments: list[str],
                    meta: dict[str, Any] | None = None, parent_id: str | None = None,
                    label_floor: Label | None = None, task_id: str | None = None) -> TaskState:
        rt = self.rt
        u = rt.policy.user(user)
        ws = rt.policy.workspace(workspace)
        rt.policy.require_workspace(u, ws)
        if not text.strip():
            raise PolicyError("the task needs a request")
        rt.files.sync(workspace)
        infos = []
        for rel in attachments:
            info, extra = self.attachment_info(workspace, rel)
            if not rt.policy.can_read(u, ws, info.label):
                raise PolicyError(f"{u.id} may not read {rel} ({info.label.display()})")
            infos.append((info, extra))
        now = self._now()
        state = TaskState(id=task_id or new_task_id(), workspace=workspace, user=user, text=text.strip(),
                          attachments=list(attachments), meta=dict(meta or {}), parent_id=parent_id,
                          created_at=now, updated_at=now, label_floor=label_floor)
        rt.tasks.create(state)
        rt.ledger.add(state.id, "user_input", summary=text.strip(), body=text.strip(),
                      label=rt.policy.policy.default_label() if label_floor is None else label_floor,
                      produced_by="user")
        for info, extra in infos:
            rt.ledger.add(state.id, "attachment", summary=f"attachment {info.name} ({info.pages} page(s))",
                          body={**info.model_dump(mode="json", exclude={"label"}), **extra},
                          label=info.label, anchor=Anchor(doc=info.name), produced_by="user", confidence="high")
        state.label = self.label(state)
        rt.tasks.save(state)
        rt.audit.append({"type": "task.created", "task": state.id, "workspace": workspace, "user": user,
                         "attachments": attachments, "parent": parent_id, "label": state.label.display()})
        return state

    # ------------------------------------------------------------------------------------------
    # run

    def run(self, task_id: str, cancelled: Callable[[], bool] = lambda: False) -> TaskState:
        state = self.rt.tasks.get(task_id)
        try:
            self._route(state)
            self._plan(state, cancelled)
            state = self.rt.tasks.get(task_id)
            if state.status in TERMINAL:
                return state
            self._execute(state, cancelled)
            if state.status in TERMINAL:
                return state
            self._release(state)
            self._deliver(state, cancelled)
        except (Cancelled, PoolCancelled):
            state = self.rt.tasks.get(task_id)
            self._status(state, "cancelled", "cancelled by user")
            self.rt.audit.append({"type": "task.cancelled", "task": task_id})
        except HandBack as exc:
            state = self.rt.tasks.get(task_id)
            state.error = str(exc)
            self._status(state, "handed_back", str(exc))
            self.rt.audit.append({"type": "task.handed_back", "task": task_id, "reason": str(exc)})
        except StepLimit as exc:
            state = self.rt.tasks.get(task_id)
            state.error = str(exc)
            self._status(state, "failed", str(exc))
            self.rt.audit.append({"type": "task.failed", "task": task_id, "reason": str(exc)})
        except Exception as exc:
            state = self.rt.tasks.get(task_id)
            state.error = f"{exc.__class__.__name__}: {exc}"
            self._trace(state, "error", ok=False, summary=state.error, error=state.error)
            self._status(state, "failed", state.error)
            self.rt.audit.append({"type": "task.failed", "task": task_id, "reason": state.error})
        finally:
            self._release(state)
            self._record_outcome(task_id)
        return self.rt.tasks.get(task_id)

    # ------------------------------------------------------------------------------------------
    # routing

    def task_input(self, state: TaskState) -> TaskInput:
        infos = []
        for rec in self.rt.ledger.for_task(state.id):
            if rec.kind == "attachment" and isinstance(rec.body, dict):
                infos.append(AttachmentInfo.model_validate({**rec.body, "label": rec.label}))
        return TaskInput(id=state.id, text=state.text, workspace=state.workspace, user=state.user,
                         attachments=infos, label=self.label(state),
                         interactive_limit_s=self.s.interactive_latency_limit_s, meta=state.meta)

    def _route(self, state: TaskState) -> None:
        self._status(state, "routing")
        started = time.perf_counter()
        decision = self.rt.router.route(self.task_input(state))
        forced = state.meta.get("force_model")
        if forced:
            # Evaluation-only override (shadow onboarding); not accepted from the API.
            self.rt.registry.get(str(forced))
            decision = decision.model_copy(update={"chosen": str(forced),
                                                   "log_line": decision.log_line + f" (evaluation override: {forced})"})
        state.route = decision.model_dump(mode="json")
        state.model = decision.chosen
        self._trace(state, "route", model=decision.chosen, purpose="route.classify",
                    summary=decision.log_line.splitlines()[-1].strip(), latency_s=round(time.perf_counter() - started, 3))
        self.rt.audit.append({"type": "route", "task": state.id, "chosen": decision.chosen,
                              "registry_version": decision.registry_version, "log": decision.log_line})
        if decision.profile.decoupled:
            docs = [a for a in state.attachments if a.lower().endswith((".pdf", ".ocr.json"))]
            if docs:
                result = self._call_tool(state, "preread", "read_document", {"paths": docs, "mode": "full"},
                                         decision.chosen)
                if result.ok:
                    state.meta.setdefault("preread", {})
                    for d in result.body.get("documents", []):
                        state.meta["preread"][d["path"]] = d
                    self._trace(state, "info", step_id="preread", ok=True,
                                summary="attachments read into the ledger; modality decoupled to text",
                                records=result.records)
        self.rt.tasks.save(state)

    def route_decision(self, state: TaskState) -> RouteDecision:
        return RouteDecision.model_validate(state.route)

    # ------------------------------------------------------------------------------------------
    # model slots (tidal pool)

    def _acquire(self, state: TaskState, cancelled: Callable[[], bool]) -> None:
        model = state.model or ""
        if not self.rt.pool.is_swap(model) or state.id in self._tickets:
            return
        prev = state.status
        wait = self.rt.pool.expected_wait(model)
        self._status(state, "waiting_tide", f"waiting for {model}, about {wait:.0f} s")
        ticket = self.rt.pool.submit_swap_job(state.id, model)
        self._tickets[state.id] = ticket
        self.rt.pool.wait_for_release(ticket, cancelled)
        self._status(state, prev if prev != "waiting_tide" else "running")

    def _release(self, state: TaskState) -> None:
        ticket = self._tickets.pop(state.id, None)
        if ticket is not None:
            self.rt.pool.finish_swap_job(ticket)

    @contextmanager
    def _slot(self, state: TaskState, cancelled: Callable[[], bool]) -> Iterator[None]:
        model = state.model or ""
        if self.rt.pool.is_swap(model):
            self._acquire(state, cancelled)
            yield
        else:
            with self.rt.pool.resident_step(model, cancelled):
                yield

    def _chat(self, state: TaskState, req: LLMRequest, ctx: CompiledContext | None) -> LLMResponse:
        salt = ctx.salt if ctx else self._salt(state)
        req = req.model_copy(update={"cache_salt": salt, "task_id": state.id})
        started = time.perf_counter()
        resp = self.rt.backend.chat(req)
        prompt = "\n".join(m.content for m in req.messages)
        hit, total = self.rt.cache.observe(salt, prompt, self.label(state).display())
        resp.usage.setdefault("sim_cached_tokens", hit)
        resp.usage.setdefault("sim_prompt_tokens", total)
        resp.latency_s = resp.latency_s or round(time.perf_counter() - started, 4)
        return resp

    def _structured(self, state: TaskState, req: LLMRequest, schema: dict[str, Any],
                    ctx: CompiledContext | None) -> tuple[dict[str, Any], LLMResponse, int]:
        orch = self

        class _Bound:
            name = "bound"

            def chat(self, r: LLMRequest) -> LLMResponse:
                return orch._chat(state, r, ctx)

        result = structured.call(_Bound(), req, schema, self.s.max_retries)
        return result.value, result.response, result.attempts

    def _count_step(self, state: TaskState) -> None:
        state.steps_used += 1
        if state.steps_used > self.s.max_steps:
            raise StepLimit(f"step limit of {self.s.max_steps} reached")

    # ------------------------------------------------------------------------------------------
    # planning

    def _compile_ctx(self, state: TaskState) -> CompileContext:
        ws = self.rt.policy.workspace(state.workspace)
        budgets = dict(self.s.step_budgets_s)
        return CompileContext(tools=self.rt.tools, task_label=self.label(state), workspace_ceiling=ws.ceiling,
                              max_steps=self.s.max_plan_steps, budgets=budgets, attachments=list(state.attachments),
                              model_task_budget=self.s.budget("model_task"))

    def _model_plan(self, state: TaskState, cancelled: Callable[[], bool]) -> tuple[Plan, list[dict[str, Any]]]:
        base = self._base_messages(state, None, set())
        prompt = render_prompt(
            "plan_write", tools=", ".join(self.rt.tools.names()),
            model_tasks=", ".join(f"{k} ({v.description})" for k, v in MODEL_TASKS.items()),
            max_steps=self.s.max_plan_steps, text=state.text, attachments=", ".join(state.attachments) or "none")
        route = self.route_decision(state)
        req = LLMRequest(model=state.model or "", purpose="plan.write", max_tokens=2048,
                         messages=[*base, ChatMessage(role="user", content=prompt)],
                         meta={"task_text": state.text, "attachments": state.attachments, "route": route.profile.task_type,
                               "complexity": route.profile.complexity, "columns": self._columns(state),
                               "parent_id": state.parent_id, "meta": state.meta})
        with self._slot(state, cancelled):
            self._count_step(state)
            value, resp, _attempts = self._structured(state, req, load_schema("typed_plan"), None)
            self._trace(state, "plan", model=state.model, purpose="plan.write", latency_s=resp.latency_s,
                        summary=f"model wrote a plan with {len(value.get('steps', []))} step(s)",
                        cached_tokens=int(resp.usage.get("sim_cached_tokens", 0)))
            plan = Plan.from_typed(value)

            class _Bound:
                name = "bound"

                def chat(inner: Any, r: LLMRequest) -> LLMResponse:
                    r = r.model_copy(update={"meta": {**r.meta, "task_text": state.text,
                                                      "attachments": state.attachments}})
                    return self._chat(state, r, None)

            compiled, history = compile_with_repair(_Bound(), state.model or "", plan, self._compile_ctx(state),
                                                    self.s.max_repairs, base, state.id, self.s.max_retries)
        for h in history:
            self._trace(state, "plan", model=state.model, purpose="plan.compile" if h["attempt"] == 0 else "plan.repair",
                        ok=not h["errors"], summary=("compiled cleanly" if not h["errors"]
                                                     else "; ".join(h["errors"])[:600]))
        return compiled, history

    def _plan(self, state: TaskState, cancelled: Callable[[], bool]) -> None:
        self._status(state, "planning")
        route = self.route_decision(state)
        use_templates = not state.meta.get("templates_disabled")
        matches = self.rt.templates.match(route.profile.task_type, state.text, state.attachments) if use_templates else []
        history: list[dict[str, Any]] = []
        if len(matches) > 1:
            names = [t.name for t in matches]
            state.template_options = names
            gate = self._new_gate(state, "template_choice", {"options": names})
            self._status(state, "awaiting_plan", "choose a template")
            choice = self.approvals.choose_template(state.id, gate.id, names, cancelled)
            state = self._close_gate(state.id, gate.id, "approved", choice.by, choice.choice)
            matches = [t for t in matches if t.name == choice.choice] or matches[:1]
        if matches:
            tpl = matches[0]
            plan = compile_plan(tpl.instantiate(state.text), self._compile_ctx(state))
            self._trace(state, "plan", summary=f"template {tpl.name} v{tpl.version} matched", ok=plan.valid,
                        purpose="plan.template")
        else:
            plan, history = self._model_plan(state, cancelled)
        state.plan = plan
        state.plan_history = history
        self.rt.tasks.save(state)
        self._release(state)
        while True:
            gate = self._new_gate(state, "plan", {"plan": plan.model_dump(mode="json"), "errors": plan.errors})
            self._status(state, "awaiting_plan", "plan has problems" if plan.errors else "waiting for plan approval")
            decision: PlanDecision = self.approvals.approve_plan(state.id, gate.id, plan, cancelled)
            status = {"approve": "approved", "edit": "edited", "reject": "rejected"}[decision.kind]
            state = self._close_gate(state.id, gate.id, status, decision.by, decision.note)
            if decision.kind == "reject":
                self._status(state, "rejected", "plan rejected")
                return
            if decision.kind == "edit" and decision.plan is not None:
                plan = compile_plan(decision.plan.model_copy(update={"source": plan.source, "template": plan.template,
                                                                     "template_version": plan.template_version}),
                                    self._compile_ctx(state))
                state.plan = plan
                self._trace(state, "plan", summary="plan edited by user", ok=plan.valid, purpose="plan.edit")
                continue
            if not plan.valid:
                self._trace(state, "error", ok=False, summary="an invalid plan cannot run: " + "; ".join(plan.errors))
                self._status(state, "failed", "plan is invalid")
                return
            break
        state.plan = plan
        self.rt.ledger.add(state.id, "plan", summary=f"approved plan: {' -> '.join(s.id for s in plan.steps)}",
                           body=plan.to_typed(), label=Label.lowest(), produced_by=decision.by)
        self.rt.tasks.save(state)

    # ------------------------------------------------------------------------------------------
    # execution

    def _columns(self, state: TaskState) -> dict[str, list[str]]:
        out = {}
        for rec in self.rt.ledger.for_task(state.id):
            if rec.kind == "attachment" and isinstance(rec.body, dict) and rec.body.get("columns"):
                out[str(rec.body["path"])] = list(rec.body["columns"])
        return out

    def env(self, state: TaskState) -> dict[str, Any]:
        env: dict[str, Any] = {"attachments": list(state.attachments), "task_text": state.text,
                               "attachment_stem": PurePosixPath(state.attachments[0]).stem if state.attachments else ""}
        env.update(state.outputs)
        findings = next((o["value"] for o in state.outputs.values()
                         if isinstance(o, dict) and isinstance(o.get("value"), dict)
                         and "equipment_tag" in o["value"]), None)
        if findings:
            tag = findings.get("equipment_tag") or {}
            if tag.get("confidence") != "uncertain":
                env["equipment_tag"] = tag.get("value")
            rd = findings.get("report_date") or findings.get("inspection_date")
            if rd and rd.get("value"):
                with suppress(ValueError):
                    env["report_date"] = norm_date(str(rd["value"]), self.s.date_dayfirst)
        for key in ("report_date", "equipment_tag"):
            if state.meta.get(key):
                env[key] = state.meta[key]
        return env

    def _focus(self, state: TaskState, step: PlanStep) -> set[str]:
        ids: set[str] = set()
        for ref in step.step_refs():
            out = state.outputs.get(ref)
            if isinstance(out, dict):
                ids.update(str(r) for r in out.get("records", []))
        for inp in step.inputs:
            if inp.source == "attachment":
                ids.update(r.id for r in self.rt.ledger.for_task(state.id)
                           if r.kind == "attachment" and isinstance(r.body, dict) and r.body.get("path") == inp.ref)
        if not step.step_refs():
            ids.update(r.id for r in self.rt.ledger.for_task(state.id) if r.kind == "attachment")
        return ids

    def _base_messages(self, state: TaskState, step: PlanStep | None, focus: set[str],
                       instruction: str = "") -> list[ChatMessage]:
        ctx = self._context(state, step, focus, instruction)
        return ctx.messages[:-1]

    def _context(self, state: TaskState, step: PlanStep | None, focus: set[str], instruction: str,
                 tools: list[str] | None = None) -> CompiledContext:
        label = self.label(state)
        return compile_context(
            workspace=state.workspace, label=label, salt=cache_salt(label, self.rt.secret_key),
            salt_mode=self.s.cache_salt_mode, tool_schemas=self.rt.tools.prompt_schemas(tools),
            plan=state.plan, step_id=step.id if step else None, records=self.rt.ledger.for_task(state.id),
            focus=focus, recalled=set(state.meta.get("recalled", [])), instruction=instruction,
            inline_chars=self.s.record_inline_chars,
        )

    def _set_step(self, state: TaskState, step: PlanStep, status: str, note: str | None = None) -> None:
        assert state.plan is not None
        for s in state.plan.steps:
            if s.id == step.id:
                s.status = status  # type: ignore[assignment]
                if note:
                    s.note = note
        step.status = status  # type: ignore[assignment]
        self.rt.tasks.save(state)

    def _execute(self, state: TaskState, cancelled: Callable[[], bool]) -> None:
        assert state.plan is not None
        self._status(state, "running")
        for step in state.plan.steps:
            if cancelled():
                raise Cancelled(state.id)
            if step.status == "done":
                continue
            self._set_step(state, step, "running")
            outcome = self._run_step(state, step, cancelled)
            self._set_step(state, step, outcome)
        self._call_tool(state, "finish", "finish", {"summary": "all plan steps processed"}, state.model or "")
        self._trace(state, "finish", summary="all plan steps processed")

    def _run_step(self, state: TaskState, step: PlanStep, cancelled: Callable[[], bool]) -> str:
        failures = 0
        use_default = False
        tried = {state.model or ""}
        while True:
            if cancelled():
                raise Cancelled(state.id)
            if step.tool:
                outcome = self._tool_step(state, step, use_default, cancelled)
            elif step.model_task == "write_code":
                outcome = self._code_step(state, step, cancelled)
            else:
                outcome = self._model_step(state, step, cancelled)
            if outcome in {"done", "incomplete", "denied"}:
                return outcome
            failures += 1
            if failures < 2:
                continue
            if step.tool and step.default_args and not use_default:
                use_default = True
                failures = 0
                state.template_fallbacks += 1
                continue
            route = self.route_decision(state)
            bigger = self.rt.router.escalate(route, state.model or "", state.workspace)
            if bigger is None or bigger.name in tried:
                raise HandBack(f"step {step.id} failed twice and no escalation target is available")
            tried.add(bigger.name)
            self._release(state)
            previous = state.model
            state.model = bigger.name
            state.escalations.append({"step": step.id, "from": previous, "to": bigger.name, "at": self._now()})
            self._trace(state, "escalation", step_id=step.id, model=bigger.name,
                        summary=f"escalated from {previous} to {bigger.name} after two failures"
                                + (" (tidal wake)" if self.rt.pool.is_swap(bigger.name) else ""))
            self.rt.audit.append({"type": "escalation", "task": state.id, "step": step.id, "from": previous,
                                  "to": bigger.name})
            self._acquire(state, cancelled)
            failures = 0
            use_default = False

    # -- tool steps --------------------------------------------------------------------------

    def tool_context(self, state: TaskState, step_id: str, model: str, call_id: str,
                     extra: dict[str, Any] | None = None) -> ToolContext:
        return ToolContext(rt=self.rt, task_id=state.id, workspace=state.workspace, user=state.user,
                           step_id=step_id, call_id=call_id, label_floor=state.label_floor or Label.lowest(),
                           model=model, attachments=list(state.attachments),
                           meta={**state.meta, **(extra or {}), "child_tasks": state.children})

    def _call_tool(self, state: TaskState, step_id: str, tool: str, args: dict[str, Any], model: str,
                   extra: dict[str, Any] | None = None, kind: str = "tool") -> ToolResult:
        call_id = f"call-{len(state.trace) + 1}"
        ctx = self.tool_context(state, step_id, model, call_id, extra)
        result = self.rt.tools.execute(tool, args, ctx)
        self._trace(state, kind, step_id=step_id, model=model, tool=tool, args=_short(args),
                    args_hash=sha256_json(args)[:16], ok=result.ok, summary=result.summary[:500],
                    records=result.records, files=result.files, latency_s=result.latency_s, error=result.error)
        return result

    def _decide(self, state: TaskState, step: PlanStep, suggested: dict[str, Any], env: dict[str, Any],
                cancelled: Callable[[], bool]) -> dict[str, Any] | None:
        spec = self.rt.tools.get(step.tool or "")
        assert spec is not None
        focus = self._focus(state, step)
        instruction = render_prompt("step_decide", step=step, args_json=json.dumps(suggested, ensure_ascii=False,
                                                                                   default=str)[:3000],
                                    schema_json=json.dumps(spec.input_schema))
        ctx = self._context(state, step, focus, instruction)
        schema = load_schema("tool_call")
        for attempt in range(self.s.max_retries + 1):
            self._count_step(state)
            req = LLMRequest(model=state.model or "", purpose="step.decide", messages=ctx.all_messages(),
                             tools=[spec.openai_tool()], json_schema=schema, context_records=sorted(focus),
                             meta={"step_id": step.id, "step_tool": step.tool, "suggested_args": suggested,
                                   "inputs": step.step_refs(), "attempt": attempt, "task_text": state.text,
                                   "outputs": {r: state.outputs.get(r) for r in step.step_refs()},
                                   "prefix_hash": ctx.prefix_hash})
            resp = self._chat(state, req, ctx)
            problems: list[str] = []
            try:
                obj = resp.parsed if resp.parsed is not None else structured.parse_json_text(resp.text)
            except ValueError as exc:
                obj = None
                problems.append(f"not valid JSON: {exc}")
            args: dict[str, Any] = {}
            if isinstance(obj, dict):
                problems += validation_errors(obj, schema)
                if not problems:
                    if obj["tool"] != step.tool:
                        problems.append(f"this step must call {step.tool}, not {obj['tool']}")
                    else:
                        args = resolve(obj["args"], env)
                        problems += self.rt.tools.validate(step.tool or "", args)
            elif obj is not None:
                problems.append("output must be a JSON object")
            self._trace(state, "decide", step_id=step.id, model=state.model, purpose="step.decide",
                        tool=step.tool, ok=not problems, latency_s=resp.latency_s,
                        cached_tokens=int(resp.usage.get("cached_tokens", resp.usage.get("sim_cached_tokens", 0))),
                        prompt_tokens=int(resp.usage.get("sim_prompt_tokens", 0)), spec=resp.spec,
                        summary="valid tool call" if not problems else "invalid tool call: " + "; ".join(problems)[:300],
                        error=None if not problems else "; ".join(problems))
            if not problems:
                return args
            ctx.append(ChatMessage(role="assistant", content=resp.text[:2000]),
                       ChatMessage(role="user", content="Invalid tool call: " + "; ".join(problems)
                                   + ". Return one corrected JSON tool call."))
        return None

    def _tool_step(self, state: TaskState, step: PlanStep, use_default: bool, cancelled: Callable[[], bool]) -> str:
        spec = self.rt.tools.get(step.tool or "")
        assert spec is not None
        env = self.env(state)
        suggested = resolve(step.args or step.default_args or {}, env)
        entry = self.rt.registry.maybe(state.model or "")
        kind = "tool"
        with self._slot(state, cancelled):
            if use_default:
                args = resolve(step.default_args or {}, env)
                kind = "default"
                self._trace(state, "info", step_id=step.id, summary="TEMPLATE_DEFAULT: running the step's default call")
            elif entry is not None and not entry.can_call_tools:
                args = suggested
                self._trace(state, "info", step_id=step.id, model=state.model,
                            summary=f"{state.model} uses the code-block protocol; the orchestrator issues the call")
            else:
                # The instruction shows unresolved placeholders: resolved values carry document text,
                # which must only reach the model inside quoted <record> blocks.
                decided = self._decide(state, step, dict(step.args or step.default_args or {}), env, cancelled)
                args = decided or {}
                if decided is None:
                    if step.default_args is None:
                        self._set_step(state, step, "incomplete", "no valid tool call after retries")
                        return "incomplete"
                    args = resolve(step.default_args, env)
                    kind = "default"
                    state.template_fallbacks += 1
                    self._trace(state, "info", step_id=step.id,
                                summary="TEMPLATE_DEFAULT: two invalid actions, running the step's default call")
        approved = True
        if spec.side_effect or step.side_effect:
            approved = self._action_gate(state, step, step.tool or "", args, cancelled)
        if not approved:
            self._trace(state, "gate", step_id=step.id, tool=step.tool, ok=False, summary="DENIED_BY_USER")
            return "denied"
        with self._slot(state, cancelled):
            self._count_step(state)
            result = self._call_tool(state, step.id, step.tool or "", args, state.model or "",
                                     {"action_approved": True, "preread": state.meta.get("preread", {})}, kind)
            if not result.ok:
                return "failed"
            if step.tool == "recall":
                state.meta.setdefault("recalled", []).append(result.body.get("recall"))
            output: dict[str, Any] = dict(result.body) if isinstance(result.body, dict) else {"value": result.body}
            output.setdefault("records", result.records)
            if result.files:
                output["file_id"] = result.files[0]
            state.outputs[step.id] = output
            self.rt.tasks.save(state)
            if step.output_schema:
                task_name = SCHEMA_TO_TASK.get(step.output_schema)
                mt = MODEL_TASKS.get(task_name or "")
                if mt is None:
                    return "done"
                value, rid = self._run_model_task(state, step, mt, set(result.records), cancelled)
                if value is None:
                    self._set_step(state, step, "incomplete", f"{mt.name} failed validation")
                    return "incomplete"
                state.outputs[step.id] = {"value": value, "records": [*result.records, rid], "tool": output}
                self.rt.tasks.save(state)
        return "done"

    def _action_gate(self, state: TaskState, step: PlanStep, tool: str, args: dict[str, Any],
                     cancelled: Callable[[], bool]) -> bool:
        self._release(state)
        payload = {"tool": tool, "step": step.id, "args": _short(args), "title": step.title}
        gate = self._new_gate(state, "action", payload, step.id)
        self._status(state, "awaiting_action", f"approve {tool} for step {step.id}")
        decision: ActionDecision = self.approvals.approve_action(state.id, gate.id, payload, cancelled)
        fresh = self._close_gate(state.id, gate.id, "approved" if decision.approved else "rejected",
                                 decision.by, decision.note)
        state.gates = fresh.gates
        self._status(state, "running")
        return decision.approved

    # -- model steps -------------------------------------------------------------------------

    def _run_model_task(self, state: TaskState, step: PlanStep, mt: ModelTaskSpec, focus: set[str],
                        cancelled: Callable[[], bool],
                        extra_meta: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str | None]:
        if mt.name == "summarise_document":
            return self._summarise(state, step, focus, cancelled)
        schema = load_schema(mt.schema_name or "")
        kwargs = {"record_ids": ", ".join(sorted(focus)), "question": state.text, "doc": ""}
        instruction = render_prompt(mt.prompt, **kwargs)
        ctx = self._context(state, step, focus, instruction)
        req = LLMRequest(model=state.model or "", purpose=mt.purpose, messages=ctx.all_messages(),
                         context_records=sorted(focus), max_tokens=3072,
                         meta={"step_id": step.id, "task_text": state.text, "prefix_hash": ctx.prefix_hash,
                               "outputs": {r: state.outputs.get(r) for r in step.step_refs()}, **(extra_meta or {})})
        self._count_step(state)
        try:
            value, resp, attempts = self._structured(state, req, schema, ctx)
        except InvalidModelOutput as exc:
            self._trace(state, "model", step_id=step.id, model=state.model, purpose=mt.purpose, ok=False,
                        summary=f"{mt.name}: output invalid after retries", error=str(exc)[:500])
            return None, None
        rec = self.rt.ledger.add(state.id, "model_output", summary=f"{mt.name} by {state.model}: "
                                 + json.dumps(value, ensure_ascii=False)[:200],
                                 body=value, label=self.label(state), produced_by=f"{step.id}:{mt.name}",
                                 inputs=sorted(focus), confidence="medium")
        self._trace(state, "model", step_id=step.id, model=state.model, purpose=mt.purpose, records=[rec.id],
                    summary=f"{mt.name} produced a schema-valid output ({attempts} attempt(s))",
                    latency_s=resp.latency_s, cached_tokens=int(resp.usage.get("cached_tokens",
                                                                               resp.usage.get("sim_cached_tokens", 0))),
                    prompt_tokens=int(resp.usage.get("sim_prompt_tokens", 0)), spec=resp.spec)
        return value, rec.id

    def _model_step(self, state: TaskState, step: PlanStep, cancelled: Callable[[], bool]) -> str:
        mt = MODEL_TASKS[step.model_task or ""]
        focus = self._focus(state, step)
        with self._slot(state, cancelled):
            value, rid = self._run_model_task(state, step, mt, focus, cancelled)
        if value is None:
            self._set_step(state, step, "incomplete", f"{mt.name} failed schema validation")
            if mt.schema_name == "approval_note":
                # The draft still renders, with the gap stated, so the reviewer sees what is missing.
                state.outputs[step.id] = {"value": {
                    "title": "Draft note (incomplete)", "subject": state.text, "summary": [], "findings": [],
                    "consistency_findings": [], "recommendation": [],
                    "incomplete": [f"Step '{step.title or step.id}' did not produce a valid draft after "
                                   f"{self.s.max_retries + 1} attempts; complete these sections manually."],
                }, "records": sorted(focus)}
                self.rt.tasks.save(state)
            return "incomplete"
        state.outputs[step.id] = {"value": value, "records": [rid, *sorted(focus)]}
        self.rt.tasks.save(state)
        return "done"

    def _summarise(self, state: TaskState, step: PlanStep, focus: set[str],
                   cancelled: Callable[[], bool]) -> tuple[dict[str, Any] | None, str | None]:
        pages = [r for r in self.rt.ledger.for_task(state.id) if r.id in focus and r.kind == "ocr_text"]
        if not pages:
            return None, None
        doc = pages[0].anchor.doc if pages[0].anchor else "document"
        schema = load_schema("contract_summary")
        partials: list[dict[str, Any]] = []
        map_ids: list[str] = []
        for i in range(0, len(pages), MAP_BATCH_PAGES):
            batch = pages[i:i + MAP_BATCH_PAGES]
            ids = {r.id for r in batch}
            ctx = self._context(state, step, ids, render_prompt("summarise_map", doc=doc))
            req = LLMRequest(model=state.model or "", purpose="summarise.map", messages=ctx.all_messages(),
                             context_records=sorted(ids), max_tokens=2048,
                             meta={"step_id": step.id, "doc": doc, "batch": i // MAP_BATCH_PAGES})
            if i == 0:
                self._count_step(state)
            try:
                value, resp, _ = self._structured(state, req, schema, ctx)
            except InvalidModelOutput as exc:
                self._trace(state, "model", step_id=step.id, purpose="summarise.map", ok=False, error=str(exc))
                return None, None
            rec = self.rt.ledger.add(state.id, "model_output", summary=f"section summary of {doc} pages "
                                     f"{batch[0].anchor.page if batch[0].anchor else '?'}+",
                                     body=value, label=self.label(state), produced_by=f"{step.id}:summarise.map",
                                     inputs=sorted(ids), confidence="medium")
            partials.append(value)
            map_ids.append(rec.id)
            self._trace(state, "model", step_id=step.id, model=state.model, purpose="summarise.map",
                        records=[rec.id], summary=f"mapped pages {i + 1}-{i + len(batch)}", latency_s=resp.latency_s,
                        cached_tokens=int(resp.usage.get("sim_cached_tokens", 0)))
        ids = set(map_ids)
        ctx = self._context(state, step, ids, render_prompt("summarise_reduce", doc=doc))
        req = LLMRequest(model=state.model or "", purpose="summarise.reduce", messages=ctx.all_messages(),
                         context_records=sorted(ids), max_tokens=3072,
                         meta={"step_id": step.id, "doc": doc, "partials": partials})
        try:
            value, resp, _ = self._structured(state, req, schema, ctx)
        except InvalidModelOutput as exc:
            self._trace(state, "model", step_id=step.id, purpose="summarise.reduce", ok=False, error=str(exc))
            return None, None
        rec = self.rt.ledger.add(state.id, "model_output", summary=f"summary of {doc}", body=value,
                                 label=self.label(state), produced_by=f"{step.id}:summarise.reduce",
                                 inputs=[*map_ids, *sorted(focus)], confidence="medium")
        self._trace(state, "model", step_id=step.id, model=state.model, purpose="summarise.reduce",
                    records=[rec.id], summary=f"combined {len(partials)} partial summaries", latency_s=resp.latency_s)
        return value, rec.id

    def _code_step(self, state: TaskState, step: PlanStep, cancelled: Callable[[], bool]) -> str:
        from workbench.agent.code_protocol import run_code_task

        already = any(g.step_id == step.id and g.kind == "action" and g.status == "approved" for g in state.gates)
        if not already and not self._action_gate(state, step, "write_code", {"writes": "drafts/"}, cancelled):
            self._trace(state, "gate", step_id=step.id, ok=False, summary="DENIED_BY_USER")
            return "denied"
        with self._slot(state, cancelled):
            ok, output = run_code_task(self, state, step, cancelled)
        if ok:
            state.outputs[step.id] = output
            self.rt.tasks.save(state)
            return "done"
        return "failed"

    # ------------------------------------------------------------------------------------------
    # deliverables

    def _data_output(self, state: TaskState, step: PlanStep) -> dict[str, Any] | None:
        refs = step.step_refs()
        for ref in refs:
            out = state.outputs.get(ref)
            if isinstance(out, dict) and isinstance(out.get("value"), dict):
                return dict(out["value"])
        return None

    def review(self, state: TaskState) -> None:
        """Run number provenance and citation verification on every draft deliverable."""
        records = self.rt.ledger.for_task(state.id)
        for child in state.children:
            records += self.rt.ledger.for_task(child)
        today = self.rt.clock.now().date()
        deliverables: list[DeliverableState] = []
        assert state.plan is not None
        for step in state.plan.steps:
            out = state.outputs.get(step.id)
            if not isinstance(out, dict):
                continue
            file_ids = [out["file_id"]] if out.get("file_id") else list(out.get("files", []))
            for fid in file_ids:
                frec = self.rt.files.get(fid)
                path = self.rt.files.resolve(frec.workspace, frec.relpath)
                report = provenance.check_file(path, records, self.rt.ledger, self.s.number_tolerance)
                note = self._data_output(state, step) if step.tool in {"make_docx"} else None
                claims = []
                if note:
                    claims = [c.model_dump() for c in citations.verify(
                        citations.claims_from_note(note), self.rt.ledger, self.rt.kb.reranker,
                        self.s.citation_min_score, today, self.rt.kb.revisions, self.s.date_dayfirst)]
                kind = PurePosixPath(frec.relpath).suffix.lstrip(".") or "file"
                deliverables.append(DeliverableState(
                    file_id=fid, relpath=frec.relpath, kind=kind, label=frec.label, step_id=step.id,
                    provenance={"file": report.file, "counts": report.counts,
                                "figures": [f.model_dump(mode="json") for f in report.figures
                                            if not (isinstance(f.value, float) and math.isnan(f.value))
                                            or f.formula]},
                    claims=claims))
        state.deliverables = deliverables
        state.checks = [r.body for r in records if r.kind == "check_result" and isinstance(r.body, dict)
                        and r.task_id == state.id]
        for c, rec in zip(state.checks, [r for r in records if r.kind == "check_result" and r.task_id == state.id],
                          strict=True):
            c["record_id"] = rec.id
        self.rt.tasks.save(state)

    def draft_summary(self, state: TaskState) -> dict[str, Any]:
        mismatches = [c["record_id"] for c in state.checks if c.get("status") == "mismatch"]
        decided = {d.figure for d in state.figure_decisions}
        orphans = [f"{d.file_id}:{f['id']}" for d in state.deliverables for f in d.provenance.get("figures", [])
                   if f["status"] == "unsourced" and f"{d.file_id}:{f['id']}" not in decided]
        unverified = [c["id"] for d in state.deliverables for c in d.claims if c["status"] == "unverified"]
        return {"mismatches": mismatches, "unacknowledged": [m for m in mismatches if m not in state.acknowledged],
                "orphans": orphans, "unverified_claims": unverified,
                "files": [d.relpath for d in state.deliverables]}

    def approval_blockers(self, state: TaskState) -> list[str]:
        s = self.draft_summary(state)
        out = []
        if s["unacknowledged"]:
            out.append(f"{len(s['unacknowledged'])} mismatch(es) not acknowledged")
        if s["orphans"]:
            out.append(f"{len(s['orphans'])} unsourced figure(s) not resolved")
        return out

    def _deliver(self, state: TaskState, cancelled: Callable[[], bool]) -> None:
        state = self.rt.tasks.get(state.id)
        self._status(state, "rendering", "checking provenance and citations")
        self.review(state)
        answer = next((o["value"] for o in state.outputs.values()
                       if isinstance(o, dict) and isinstance(o.get("value"), dict) and "answer" in o["value"]), None)
        if answer is not None:
            claims = citations.verify(citations.claims_from_note(answer), self.rt.ledger, self.rt.kb.reranker,
                                      self.s.citation_min_score, self.rt.clock.now().date(),
                                      self.rt.kb.revisions, self.s.date_dayfirst)
            state.result["answer"] = answer
            state.result["claims"] = [c.model_dump() for c in claims]
        code = next((o for o in state.outputs.values() if isinstance(o, dict) and o.get("script_file")), None)
        if code is not None:
            state.result["code"] = {k: code.get(k) for k in ("script_file", "result", "attempts", "files")}
        if not state.deliverables:
            state.result["summary"] = "completed without file deliverables"
            self._status(state, "completed", "done")
            self.rt.audit.append({"type": "task.completed", "task": state.id, "deliverables": []})
            return
        while True:
            summary = self.draft_summary(state)
            gate = self._new_gate(state, "deliverable", summary)
            self._status(state, "awaiting_deliverable", "review the draft")
            decision: DeliverableDecision = self.approvals.approve_deliverable(state.id, gate.id, summary, cancelled)
            state = self.rt.tasks.get(state.id)
            if decision.kind == "approve":
                for m in decision.acknowledged:
                    if m not in state.acknowledged:
                        state.acknowledged.append(m)
                if decision.by == "auto":
                    for orphan in self.draft_summary(state)["orphans"]:
                        state.figure_decisions.append(FigureDecision(figure=orphan, action="confirm", by="auto",
                                                                     at=self._now(), note="auto-approved run"))
                blockers = self.approval_blockers(state)
                if blockers:
                    state = self._close_gate(state.id, gate.id, "rejected", decision.by, "; ".join(blockers))
                    self._trace(state, "gate", ok=False, summary="approval refused: " + "; ".join(blockers))
                    continue
                state = self._close_gate(state.id, gate.id, "approved", decision.by, decision.note)
                for d in state.deliverables:
                    final = self.rt.files.promote_to_final(d.file_id, decision.by)
                    d.status = "approved"
                    d.final_file_id = final.id
                    self.rt.audit.append({"type": "deliverable.final", "task": state.id, "file": final.relpath,
                                          "sha256": final.sha256, "label": final.label.display(),
                                          "acknowledged": state.acknowledged,
                                          "figure_decisions": [f.model_dump() for f in state.figure_decisions],
                                          "by": decision.by})
                state.result["final"] = [d.final_file_id for d in state.deliverables]
                self._status(state, "completed", "deliverables approved")
                return
            state = self._close_gate(state.id, gate.id, "rejected", decision.by, decision.note)
            if state.revisions >= 2 or not decision.note:
                for d in state.deliverables:
                    d.status = "rejected"
                self._status(state, "rejected", "deliverable rejected")
                return
            state.revisions += 1
            self._revise(state, decision.note, cancelled)
            state = self.rt.tasks.get(state.id)
            self.review(state)

    def _revise(self, state: TaskState, note: str, cancelled: Callable[[], bool]) -> None:
        assert state.plan is not None
        self.rt.ledger.add(state.id, "user_input", summary=f"reviewer note: {note}", body=f"Reviewer note: {note}",
                           label=Label.lowest(), produced_by=state.user)
        first = next((i for i, s in enumerate(state.plan.steps) if s.model_task), None)
        if first is None:
            return
        for s in state.plan.steps[first:]:
            s.status = "pending"
            state.outputs.pop(s.id, None)
        state.gates = [g for g in state.gates if not (g.kind == "action" and g.status == "approved")]
        self._trace(state, "info", summary=f"revision {state.revisions}: re-running from step "
                                           f"{state.plan.steps[first].id}")
        self._execute(state, cancelled)
        self._release(state)

    # ------------------------------------------------------------------------------------------
    # outcomes

    def _record_outcome(self, task_id: str) -> None:
        from sqlalchemy import delete

        from workbench.core.db import dumps, outcomes_table

        try:
            state = self.rt.tasks.get(task_id)
        except WorkbenchError:
            return
        if state.route is None:
            return
        claims = [c for d in state.deliverables for c in d.claims]
        verified = sum(1 for c in claims if c["status"] == "verified")
        sandbox = [r for r in self.rt.ledger.for_task(task_id) if r.kind == "sandbox_result"]
        langs = (state.route.get("profile") or {}).get("languages") or ["en"]
        data = {"status": state.status, "template": state.plan.template if state.plan else None,
                "fallbacks": state.template_fallbacks, "escalations": state.escalations,
                "sandbox_runs": len(sandbox), "citation_rate": (verified / len(claims)) if claims else None}
        approved = 1 if state.status == "completed" else (0 if state.status in {"rejected", "failed"} else None)
        with self.rt.db.tx() as conn:
            conn.execute(delete(outcomes_table).where(outcomes_table.c.task_id == task_id))
            conn.execute(outcomes_table.insert().values(
                task_id=task_id, route=str((state.route.get("profile") or {}).get("task_type")),
                model=str(state.model), language=",".join(langs), approved=approved, data=dumps(data)))


def _short(value: Any, limit: int = 600) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return value
    return {"_truncated": text[:limit] + "..."}


def today_iso(clock: Any) -> str:
    d: date = clock.now().date()
    return d.isoformat()


def records_of(ledger: Any, ids: list[str]) -> list[LedgerRecord]:
    return [r for r in (ledger.maybe(i) for i in ids) if r is not None]
