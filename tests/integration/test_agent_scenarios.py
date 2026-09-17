"""Agent-loop failure paths driven by scripted model output (PRD phase 7 exit criteria)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tests.helpers import ROOT, run_trace
from workbench.agent.approvals import AutoApprove
from workbench.llm.heuristic import HeuristicBackend
from workbench.llm.scripted import ScriptedBackend
from workbench.runtime import Runtime


def scripted(make_runtime: Callable[..., Runtime], name: str, **kw: Any) -> tuple[Runtime, ScriptedBackend]:
    rt = make_runtime(**kw)
    backend = ScriptedBackend.from_file(ROOT / "fixtures" / "scripts" / f"{name}.yaml", HeuristicBackend(rt.ledger))
    rt.backend = backend
    rt.router.backend = backend
    return rt, backend


def kinds(state: Any) -> list[str]:
    return [t.summary for t in state.trace]


def test_invalid_json_twice_runs_the_template_default(make_runtime: Callable[..., Runtime]) -> None:
    rt, backend = scripted(make_runtime, "invalid_json_twice")
    state = run_trace(rt, "A")
    assert backend.remaining() == 0
    decides = [t for t in state.trace if t.kind == "decide" and t.step_id == "extract"]
    assert [d.ok for d in decides] == [False, False, False]
    assert any("TEMPLATE_DEFAULT" in s for s in kinds(state))
    assert state.template_fallbacks == 1
    assert state.status == "completed"


def test_invalid_draft_is_marked_incomplete(make_runtime: Callable[..., Runtime]) -> None:
    rt, _ = scripted(make_runtime, "invalid_draft")
    state = run_trace(rt, "A")
    from docx import Document

    assert state.plan.step("draft").status == "incomplete"
    assert state.plan.step("render").status == "done"
    assert state.status == "completed"
    doc = Document(str(rt.files.open_path(state.deliverables[0].final_file_id)))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "Incomplete steps" in text and "complete these sections manually" in text


def test_two_tool_failures_fall_back_to_default(make_runtime: Callable[..., Runtime]) -> None:
    rt, backend = scripted(make_runtime, "tool_failures_default")
    state = run_trace(rt, "A")
    ground = [t for t in state.trace if t.step_id == "ground" and t.kind in {"tool", "default"}]
    assert [t.ok for t in ground] == [False, False, True]
    assert ground[-1].kind == "default"
    assert state.status == "completed" and state.template_fallbacks == 1
    assert backend.remaining() == 0


def test_two_failures_without_default_escalate_after_a_tide(make_runtime: Callable[..., Runtime]) -> None:
    rt, backend = scripted(make_runtime, "tool_failures_escalation")
    state = run_trace(rt, "B")
    assert backend.remaining() == 0
    assert state.escalations and state.escalations[0]["from"] == "qwen2.5-coder-7b"
    assert state.escalations[0]["to"] == "gpt-oss-20b"
    assert rt.pool.tide_count >= 1
    assert state.model == "gpt-oss-20b"
    assert state.status == "completed"
    tides = [e for e in rt.pool.events if e["event"] == "tide"]
    assert tides[0]["direction"] == "to_swap"
    assert rt.pool.tide == "resident", "the tide reverses once the swap job finishes"


def test_no_escalation_target_hands_back(make_runtime: Callable[..., Runtime]) -> None:
    rt, _ = scripted(make_runtime, "no_escalation_target")
    state = run_trace(rt, "E")
    assert state.status == "handed_back"
    assert "no escalation target" in (state.error or "")
    assert any(t.step_id == "score" and not t.ok for t in state.trace)


def test_step_limit_reports_failure(make_runtime: Callable[..., Runtime]) -> None:
    rt = make_runtime(max_steps=3)
    state = run_trace(rt, "A")
    assert state.status == "failed" and "step limit" in (state.error or "")


def test_denied_action_lets_the_loop_continue(make_runtime: Callable[..., Runtime]) -> None:
    rt = make_runtime(approvals=AutoApprove(deny_actions={"make_docx"}))
    state = run_trace(rt, "A")
    assert state.plan.step("render").status == "denied"
    assert any(t.summary == "DENIED_BY_USER" for t in state.trace)
    assert state.status == "completed" and not state.deliverables


def test_rejected_plan_stops_before_any_tool(make_runtime: Callable[..., Runtime]) -> None:
    rt = make_runtime(approvals=AutoApprove(reject_plans=True))
    state = run_trace(rt, "A")
    assert state.status == "rejected"
    assert not [t for t in state.trace if t.kind == "tool"]


def test_two_code_blocks_are_rejected(make_runtime: Callable[..., Runtime]) -> None:
    rt, backend = scripted(make_runtime, "two_code_blocks")
    state = run_trace(rt, "B")
    assert backend.remaining() == 0
    first = next(t for t in state.trace if t.kind == "model" and t.step_id == "code")
    assert not first.ok and "exactly one" in (first.error or "")
    assert state.status == "completed"


def test_ambiguous_templates_ask_for_a_choice(make_runtime: Callable[..., Runtime]) -> None:
    rt = make_runtime()
    task = rt.orchestrator.create_task("plant-a", "engineer1",
                                       "Draft an approval note and a findings table in excel",
                                       ["inputs/inspection_P108B.pdf"])
    state = rt.jobs.run_inline(task.id)
    choice = next(g for g in state.gates if g.kind == "template_choice")
    assert choice.payload["options"] == ["approval_note_from_scan", "inspection_findings_to_xlsx"]
    assert state.plan.template == "approval_note_from_scan"
    assert state.status == "completed"


def test_delegation_inherits_the_label_floor(make_runtime: Callable[..., Runtime]) -> None:
    from workbench.core.labels import Label, Level

    rt = make_runtime()
    parent = rt.orchestrator.create_task("plant-a", "engineer1", "Inspect the contract", ["inputs/vendor_contract.pdf"])
    ctx = rt.orchestrator.tool_context(rt.tasks.get(parent.id), "s", "qwen3-vl-8b", "call-1")
    res = rt.tools.execute("delegate", {"task": "compute the mean of these pressure readings", "type": "code",
                                        "files": ["inputs/pressure_readings.csv"]}, ctx)
    assert res.ok, res.error
    child = rt.tasks.get(res.body["child_task"])
    assert child.parent_id == parent.id and child.status == "completed"
    assert child.model == "qwen2.5-coder-7b"
    assert child.label_floor == Label(level=Level.CONFIDENTIAL)
    assert all(rt.files.get(f).label.level == Level.CONFIDENTIAL for f in res.files)
    summary = rt.ledger.get(res.records[0])
    assert summary.inputs and all(i.startswith(f"R-{child.id}-") for i in summary.inputs)
    assert rt.tasks.get(parent.id).children == [child.id]


def test_determinism_of_ledger_hashes(make_runtime: Callable[..., Runtime]) -> None:
    from workbench.core.clock import FixedClock

    hashes = []
    for _ in range(2):
        rt = make_runtime(clock=FixedClock())
        task = rt.orchestrator.create_task("plant-a", "engineer1", "Draft an approval note for this inspection report",
                                           ["inputs/inspection_P108B.pdf"], task_id="T0000D001")
        rt.jobs.run_inline(task.id)
        hashes.append([r.hash for r in rt.ledger.for_task(task.id)])
    assert hashes[0] == hashes[1] and len(hashes[0]) > 20
