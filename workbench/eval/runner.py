"""Evaluation runner: every metric, the demo traces, and ``reports/eval.md``."""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from workbench.agent.approvals import AutoApprove
from workbench.agent.loop import Orchestrator
from workbench.eval import metrics
from workbench.eval.datasets import load
from workbench.eval.routing import evaluate_routing
from workbench.eval.write_registry import outcome_quality, propose
from workbench.runtime import Runtime, setup_environment
from workbench.settings import Settings

TRACES = {
    "A": ("plant-a", "engineer1", "Draft an approval note for this inspection report", ["inputs/inspection_P108B.pdf"]),
    "B": ("plant-a", "engineer1", "Write a Python script to parse these pressure readings and flag anomalies",
          ["inputs/pressure_readings.csv"]),
    "C": ("plant-a", "engineer1", "Summarise this vendor contract", ["inputs/vendor_contract.pdf"]),
    "D": ("plant-a", "engineer1", "Compute the required wall thickness for this pipe per the attached data",
          ["inputs/pipe_data.md"]),
    "E": ("proc", "buyer1", "Compare these three vendor offers against the tender conditions and recommend one",
          ["inputs/offer_a.pdf", "inputs/offer_b.pdf", "inputs/offer_c.pdf", "inputs/tender_conditions.pdf"]),
}


def isolated_settings(base: Settings, tmp: Path) -> Settings:
    over = {
        "data_dir": str(tmp / "var"), "workspaces_root": str(tmp / "var" / "workspaces"),
        "db_path": str(tmp / "var" / "eval.db"), "audit_path": str(tmp / "var" / "audit.jsonl"),
        "secret_key_path": str(tmp / "var" / "secret.key"), "kb_dir": str(tmp / "var" / "kb"),
        "asset_register": str(tmp / "var" / "reference" / "asset_register.csv"), "time_scale": 0.0,
        "egress_guard": False,
    }
    return base.model_copy(update=over)


def run_trace(rt: Runtime, key: str, **meta: Any) -> tuple[Any, float]:
    ws, user, text, atts = TRACES[key]
    orch = Orchestrator(rt, AutoApprove())
    state = orch.create_task(ws, user, text, atts, meta=meta or None)
    started = time.perf_counter()
    done = orch.run(state.id)
    return done, round(time.perf_counter() - started, 2)


def trace_row(key: str, state: Any, seconds: float, rt: Runtime) -> dict[str, Any]:
    sandbox_runs = sum(1 for r in rt.ledger.for_task(state.id) if r.kind == "sandbox_result")
    counts: dict[str, int] = {}
    for c in state.checks:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    prov = {"sourced": 0, "derived": 0, "unsourced": 0}
    for d in state.deliverables:
        for k, v in (d.provenance.get("counts") or {}).items():
            prov[k] += v
    return {"trace": key, "status": state.status, "route": (state.route or {}).get("profile", {}).get("task_type"),
            "model": state.model, "template": state.plan.template if state.plan else None,
            "steps": len(state.plan.steps) if state.plan else 0, "fallbacks": state.template_fallbacks,
            "escalations": len(state.escalations), "repairs": state.plan.repairs if state.plan else 0,
            "sandbox_runs": sandbox_runs, "checks": counts, "provenance": prov,
            "files": [d.relpath for d in state.deliverables], "label": state.label.display() if state.label else "",
            "seconds": seconds}


def run_all(settings: Settings, reliability_runs: int = 3, write_registry: bool = False,
            refresh: bool = False, out_dir: Path | None = None) -> dict[str, Any]:
    root = settings.root
    tmp = Path(tempfile.mkdtemp(prefix="wb-eval-"))
    try:
        s = isolated_settings(settings, tmp)
        setup_environment(s)
        rt = Runtime(s, approvals=AutoApprove())
        sandbox_note = rt.sandbox.health()
        if sandbox_note.get("status") != "ok" and s.sandbox == "sandboxd":
            rt.close()
            s = s.model_copy(update={"sandbox": "fake", "egress": "fake"})
            rt = Runtime(s, approvals=AutoApprove())
            sandbox_note = {"status": "fallback", "detail": "sandboxd unavailable; used the in-process fake sandbox"}
        try:
            report: dict[str, Any] = {"backend": s.llm_backend, "profile": s.profile, "sandbox": sandbox_note,
                                      "registry_version": rt.registry.version}
            traces = {}
            rows = []
            for key in TRACES:
                state, seconds = run_trace(rt, key)
                traces[key] = state
                rows.append(trace_row(key, state, seconds, rt))
            old, old_s = run_trace(rt, "A", report_date="2023-06-01")
            rows.append(trace_row("A (as of 2023)", old, old_s, rt))
            free, free_s = run_trace(rt, "A", templates_disabled=True)
            rows.append(trace_row("A (no template)", free, free_s, rt))
            report["traces"] = rows
            vl, coder = "qwen3-vl-8b", "qwen2.5-coder-7b"
            report["routing"] = evaluate_routing(rt.router, load(root, "routing"))
            report["extraction"] = metrics.extraction(rt, load(root, "extraction"), vl)
            report["dual_read"] = metrics.dual_read(rt, vl)
            report["consistency"] = metrics.consistency(rt, load(root, "consistency"), vl)
            report["revision"] = metrics.revision(rt, load(root, "revision"))
            report["graph"] = metrics.graph_recall(rt, load(root, "graph"))
            report["provenance"] = metrics.provenance_detection(rt, load(root, "provenance"), traces["A"])
            report["citations"] = metrics.citation_rate(rt, traces["A"])
            report["citations_as_of"] = metrics.citation_rate(rt, old)
            report["plan_compiler"] = metrics.plan_compiler(rt, load(root, "plan_compiler"), "gpt-oss-20b")
            report["labels"] = metrics.labels(rt, load(root, "labels"))
            report["code"] = metrics.code(rt, load(root, "code"))
            completions = {"A": 0, "B": 0}
            fallbacks = 0
            for _ in range(reliability_runs):
                for key in completions:
                    st, _secs = run_trace(rt, key)
                    completions[key] += st.status == "completed"
                    fallbacks += st.template_fallbacks
            report["reliability"] = {"runs": reliability_runs,
                                     "completion": {k: v / reliability_runs for k, v in completions.items()},
                                     "template_fallbacks": fallbacks,
                                     "trace_a_without_template": free.status}
            report["cache"] = metrics.cache(rt)
            report["speculative"] = {"status": "n/a (no GPU)",
                                     "detail": "acceptance length, tokens/s and output equality need vLLM"}
            report["pool"] = {k: v for k, v in rt.pool.snapshot().items() if k != "events"}
            if write_registry:
                measured = {vl: {"document": round((report["extraction"]["precision"] +
                                                   report["extraction"]["recall"]) / 2, 2),
                                 "vision": report["dual_read"]["accepted_accuracy"]},
                            coder: {"code": report["code"]["pass_rate"]},
                            "gpt-oss-20b": {"agentic": report["plan_compiler"]["valid_after_repair"]}}
                note = ""
                if s.llm_backend != "openai":
                    note = ("Measured with the offline heuristic backend, so these values describe the pipeline "
                            "rather than a model. Re-run with llm_backend=openai on the target GPU before approving.")
                outcomes = outcome_quality(rt.db) if refresh else None
                report["proposed_registry"] = propose(rt.registry, measured,
                                                      settings.config_dir / "models.proposed.yaml",
                                                      s.llm_backend, outcomes, note)
        finally:
            rt.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out = out_dir or settings.path(settings.reports_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "eval.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out / "eval.md").write_text(markdown(report), encoding="utf-8")
    return report


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def markdown(r: dict[str, Any]) -> str:
    lines = ["# Evaluation report", "",
             f"Backend: `{r['backend']}` · profile {r['profile']} · registry {r['registry_version']} · "
             f"sandbox: {r['sandbox'].get('status')}", ""]
    if r["backend"] == "heuristic":
        lines += ["> Measured with the deterministic offline backend. The numbers exercise the pipeline, the rules",
                  "> and the evaluation code; model quality must be re-measured with real models on the target GPU.",
                  ""]
    lines += ["## Demo traces", "", "| Trace | Status | Route | Model | Template | Steps | Fallbacks | Escalations "
              "| Repairs | Sandbox runs | Mismatches | Unsourced | Label | Seconds |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for t in r["traces"]:
        lines.append(f"| {t['trace']} | {t['status']} | {t['route']} | {t['model']} | {t['template'] or 'compiled'} "
                     f"| {t['steps']} | {t['fallbacks']} | {t['escalations']} | {t['repairs']} | {t['sandbox_runs']} "
                     f"| {t['checks'].get('mismatch', 0)} | {t['provenance']['unsourced']} | {t['label']} "
                     f"| {t['seconds']} |")
    rows = [
        ("Routing accuracy (50 prompts)", _pct(r["routing"]["accuracy"])),
        ("Routing selection regret (mean quality lost)", f"{r['routing']['mean_regret']:.3f}"),
        ("Extraction precision", _pct(r["extraction"]["precision"])),
        ("Extraction recall", _pct(r["extraction"]["recall"])),
        ("Dual-read agreement rate", _pct(r["dual_read"]["agreement_rate"])),
        ("Dual-read error rate among agreed values", _pct(r["dual_read"]["error_rate_among_agreed"])),
        ("Dual-read share marked uncertain", _pct(r["dual_read"]["uncertain_share"])),
        ("Consistency detection rate", _pct(r["consistency"]["detection_rate"])),
        ("Consistency false-alarm rate", _pct(r["consistency"]["false_alarm_rate"])),
        ("Correct-revision rate", _pct(r["revision"]["correct_revision_rate"])),
        ("Graph recall@k (with expansion)", _pct(r["graph"]["recall_at_k"])),
        ("Graph recall@k (text only)", _pct(r["graph"]["recall_at_k_without_graph"])),
        ("Number provenance: orphan detection", _pct(r["provenance"]["orphan_detection_rate"])),
        ("Number provenance: figures resolved in generated note", _pct(r["provenance"]["resolved_share_on_generated_draft"])),
        ("Citation verification rate (Trace A)", _pct(r["citations"]["verified_rate"])),
        ("Currency warnings on the 2023 report", str(r["citations_as_of"]["currency_warnings"])),
        ("Plan validity before repair (20 untemplated)", _pct(r["plan_compiler"]["valid_before_repair"])),
        ("Plan validity after repair", _pct(r["plan_compiler"]["valid_after_repair"])),
        ("Label propagation", _pct(r["labels"]["propagation_rate"])),
        ("Code pass rate (10 tasks, hidden tests)", _pct(r["code"]["pass_rate"])),
        ("Code mean fix iterations", str(r["code"]["mean_iterations"])),
        ("Reliability: Trace A completion", _pct(r["reliability"]["completion"]["A"])),
        ("Reliability: Trace B completion", _pct(r["reliability"]["completion"]["B"])),
        ("Template fallbacks across reliability runs", str(r["reliability"]["template_fallbacks"])),
        ("Trace A without its template", r["reliability"]["trace_a_without_template"]),
        ("Cache hits across label partitions (target 0)", str(r["cache"]["cross_partition_hits"])),
        ("Speculative decoding", r["speculative"]["status"]),
        ("Tides turned", str(r["pool"]["tide_count"])),
    ]
    lines += ["", "## Metrics", "", "| Metric | Value |", "|---|---|"]
    lines += [f"| {a} | {b} |" for a, b in rows]
    lines += ["", "## Prefix cache by label partition (simulated)", "",
              "| Partition | Label | Requests | Hit rate |", "|---|---|---|---|"]
    lines += [f"| {p['partition']} | {p['label']} | {p['requests']} | {_pct(p['hit_rate'])} |"
              for p in r["cache"]["partitions"]]
    misses = [x for x in r["routing"]["rows"] if not x["ok"]]
    if misses:
        lines += ["", "## Routing misses", ""] + [f"- `{m['id']}` chose {m['chosen']}, expected {m['expected']}"
                                                  for m in misses]
    failed_code = [t for t in r["code"]["tasks"] if not t["ok"]]
    if failed_code:
        lines += ["", "## Code tasks that failed their hidden test", ""]
        lines += [f"- `{t['id']}`: status {t['status']}, attempts {t['attempts']}" for t in failed_code]
    if r.get("proposed_registry"):
        lines += ["", "## Proposed registry diff", "", "Written to `config/models.proposed.yaml` for review.", ""]
        for model, change in r["proposed_registry"]["models"].items():
            for route, d in change["diff"].items():
                lines.append(f"- {model} · {route}: {d['from']} to {d['to']} ({d['source']})")
    return "\n".join(lines) + "\n"
