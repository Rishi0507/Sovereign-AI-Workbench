"""``run_python``: run a script in the sandbox and record the result."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from workbench.core.ids import sha256_text
from workbench.tools.registry import ToolContext, ToolResult, ToolSpec, obj
from workbench.tools.sandbox import SandboxResult

SCHEMA = obj({
    "script": {"type": "string", "minLength": 1, "maxLength": 100000},
    "files": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
    "inputs": {"type": "object"},
    "timeout_s": {"type": "integer", "minimum": 1, "maximum": 300},
}, ["script"])


def parse_result_line(stdout: str) -> Any:
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith(("{", "[")):
            try:
                return json.loads(line)
            except ValueError:
                return None
    return None


def prepare_job(ctx: ToolContext, run_id: str, files: list[str], inputs: dict[str, Any]) -> Path:
    job = ctx.job_root / run_id
    job.mkdir(parents=True, exist_ok=True)
    for rel in files:
        src = ctx.rt.files.resolve(ctx.workspace, rel)
        shutil.copy2(src, job / src.name)
    for name, value in inputs.items():
        safe = Path(name).name
        (job / safe).write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return job


def execute(ctx: ToolContext, script: str, run_id: str, files: list[str], inputs: dict[str, Any],
            timeout_s: int, inputs_records: list[str] | None = None,
            attempt: int | None = None) -> tuple[SandboxResult, str, Path]:
    job = prepare_job(ctx, run_id, files, inputs)
    (job / "script.py").write_text(script, encoding="utf-8")
    result = ctx.rt.sandbox.run(job, "script.py", timeout_s, run_id=run_id, label=ctx.label().marking())
    parsed = parse_result_line(result.stdout_tail)
    body = {**result.model_dump(), "script_sha256": sha256_text(script), "result": parsed,
            "attempt": attempt, "script": script}
    status = "passed" if result.exit_code == 0 else ("timed out" if result.timed_out else f"exit {result.exit_code}")
    rec = ctx.rt.ledger.add(
        ctx.task_id, "sandbox_result",
        summary=(f"sandbox {run_id}: {status} in {result.duration_ms} ms, {len(result.new_files)} new file(s)"
                 + (f", {len(result.net_attempts)} blocked network attempt(s)" if result.net_attempts else "")
                 + (f"; {result.traceback_head[-1]}" if result.traceback_head else "")),
        body=body, label=ctx.label(), produced_by=ctx.call_id, inputs=inputs_records or [],
        confidence="high" if result.exit_code == 0 else "low",
    )
    return result, rec.id, job


def run_python(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    run_id = f"{ctx.task_id}-{ctx.call_id}".replace(".", "-")[:64]
    inputs = dict(args.get("inputs") or {})
    input_records = [str(r) for v in inputs.values() if isinstance(v, dict) for r in v.get("records", [])]
    result, rid, _job = execute(ctx, str(args["script"]), run_id, list(args.get("files") or []), inputs,
                                int(args.get("timeout_s") or ctx.rt.settings.sandbox_timeout_s), input_records)
    ok = result.exit_code == 0 and not result.timed_out
    return ToolResult(ok=ok, summary=f"exit {result.exit_code}; stdout: {result.stdout_tail[-300:]}",
                      records=[rid], body={"records": [rid], "result": parse_result_line(result.stdout_tail),
                                           "exit_code": result.exit_code},
                      error=None if ok else "\n".join(result.traceback_head) or result.stderr_tail[-500:])


SPECS = [ToolSpec(name="run_python", description="Run a Python script in the network-less sandbox.",
                  input_schema=SCHEMA, handler=run_python, output_type="sandbox_result")]
