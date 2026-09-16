"""Code-block protocol for small coder models (README section 4.1.1).

The coder returns exactly one fenced Python block. The orchestrator runs it in the sandbox and
feeds back the exit code, the stdout tail and the first traceback lines until the script passes
or ``max_code_iters`` is reached. Each attempt is a ``sandbox_result`` record; the passing script
and its outputs are written to ``drafts/``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from workbench.agent.state import TaskState
from workbench.llm.base import ChatMessage, LLMRequest
from workbench.llm.prompts import render_prompt
from workbench.planning.plan_schema import PlanStep
from workbench.tools import run_python

if TYPE_CHECKING:
    from workbench.agent.loop import Orchestrator

BLOCK_RE = re.compile(r"```(?:python|py)?[ \t]*\r?\n(.*?)```", re.DOTALL)
STDOUT_FEEDBACK = 2000


def extract_block(text: str) -> tuple[str | None, str | None]:
    blocks = BLOCK_RE.findall(text)
    if len(blocks) != 1:
        return None, f"expected exactly one fenced python block, found {len(blocks)}"
    return blocks[0].strip() + "\n", None


def _slug(text: str) -> str:
    words = re.findall(r"[a-z]+", text.lower())
    keep = [w for w in words if w not in {"a", "the", "and", "to", "of", "these", "this", "write", "python", "script"}]
    return "_".join(keep[:3]) or "script"


def run_code_task(orch: Orchestrator, state: TaskState, step: PlanStep,
                  cancelled: Callable[[], bool]) -> tuple[bool, dict[str, Any]]:
    rt = orch.rt
    files = [a for a in state.attachments if not a.lower().endswith((".pdf", ".ocr.json"))]
    focus = orch._focus(state, step)
    columns = orch._columns(state)
    instruction = render_prompt("code_write", files=", ".join(PurePosixPath(f).name for f in files) or "none",
                                text=state.text)
    ctx = orch._context(state, step, focus, instruction)
    records: list[str] = []
    previous = ""
    feedback: dict[str, Any] = {}
    for attempt in range(1, rt.settings.max_code_iters + 1):
        if cancelled():
            break
        orch._count_step(state)
        purpose = "code.write" if attempt == 1 else "code.fix"
        req = LLMRequest(model=state.model or "", purpose=purpose, messages=ctx.all_messages(), max_tokens=3072,
                         context_records=sorted(focus),
                         meta={"step_id": step.id, "task_text": state.text, "files": files, "columns": columns,
                               "attempt": attempt, "previous_script": previous, "feedback": feedback})
        resp = orch._chat(state, req, ctx)
        script, problem = extract_block(resp.text)
        ctx.append(ChatMessage(role="assistant", content=resp.text[:6000]))
        if script is None:
            orch._trace(state, "model", step_id=step.id, model=state.model, purpose=purpose, ok=False,
                        summary=problem or "no code block", error=problem)
            ctx.append(ChatMessage(role="user", content=f"{problem}. Return exactly one ```python block."))
            continue
        run_id = f"{state.id}-{step.id}-{attempt}"[:64]
        ctx_tool = orch.tool_context(state, step.id, state.model or "", f"code-{attempt}")
        result, rid, job = run_python.execute(ctx_tool, script, run_id, files, {}, rt.settings.sandbox_timeout_s,
                                              sorted(focus), attempt)
        records.append(rid)
        passed = result.exit_code == 0 and not result.timed_out
        orch._trace(state, "sandbox", step_id=step.id, model=state.model, purpose=purpose, tool="run_python",
                    ok=passed, records=[rid], latency_s=round(result.duration_ms / 1000, 3),
                    spec=resp.spec, cached_tokens=int(resp.usage.get("sim_cached_tokens", 0)),
                    summary=(f"attempt {attempt}: exit {result.exit_code}"
                             + (f", {result.traceback_head[-1]}" if result.traceback_head else "")
                             + (f", {len(result.net_attempts)} blocked network attempt(s)"
                                if result.net_attempts else "")))
        if passed:
            name = _slug(state.text) + ".py"
            label = orch.label(state)
            saved = [rt.files.write_draft(state.workspace, name, script.encode("utf-8"), label, state.id, True)]
            for f in result.new_files:
                src = job / f.path
                if src.is_file() and not f.path.startswith("."):
                    saved.append(rt.files.write_draft(state.workspace, PurePosixPath(f.path).name, src.read_bytes(),
                                                      label, state.id, True))
            return True, {"records": records, "files": [s.id for s in saved], "script_file": saved[0].relpath,
                          "result": run_python.parse_result_line(result.stdout_tail), "attempts": attempt,
                          "value": {"script": saved[0].relpath, "attempts": attempt}}
        previous = script
        head = list(result.traceback_head)
        last = next((ln for ln in reversed(result.stderr_tail.splitlines()) if ln.strip()), "")
        if last and last not in head:
            head.append(last)
        feedback = {"exit_code": result.exit_code, "stdout_tail": result.stdout_tail[-STDOUT_FEEDBACK:],
                    "traceback_head": head, "new_files": [f.path for f in result.new_files]}
        ctx.append(ChatMessage(role="user", content=render_prompt(
            "code_fix", exit_code=result.exit_code, stdout_tail=feedback["stdout_tail"],
            traceback_head="\n".join(head) or result.stderr_tail[-800:])))
    return False, {"records": records}
