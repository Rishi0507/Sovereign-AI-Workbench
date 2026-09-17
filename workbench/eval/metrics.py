"""Evaluation metrics (README section 5.6, PRD section 4.15), computed with the configured backend."""

from __future__ import annotations

import csv
import json
import random
import statistics
import time
from datetime import date
from pathlib import Path
from typing import Any

from workbench.agent.approvals import AutoApprove
from workbench.agent.loop import Orchestrator
from workbench.checks import provenance
from workbench.core.labels import Label
from workbench.core.normalise import norm_date, norm_tag
from workbench.documents.dual_read import CRITICAL_KINDS, normalise_field, reconcile
from workbench.documents.readers import FixtureReader
from workbench.llm import structured
from workbench.llm.base import ChatMessage, LLMRequest
from workbench.llm.schemas import load_schema
from workbench.planning.compiler import CompileContext, compile_plan, compile_with_repair
from workbench.planning.plan_schema import Plan
from workbench.runtime import Runtime
from workbench.tools.registry import ToolContext


def _ratio(num: float, den: float) -> float:
    return round(num / den, 4) if den else 0.0


def _task(rt: Runtime, workspace: str, user: str, text: str, attachments: list[str]) -> tuple[Orchestrator, str]:
    orch = Orchestrator(rt, AutoApprove())
    state = orch.create_task(workspace, user, text, attachments, meta={"evaluation": True})
    return orch, state.id


def _ctx(rt: Runtime, orch: Orchestrator, task_id: str, model: str) -> ToolContext:
    return orch.tool_context(rt.tasks.get(task_id), "eval", model, "eval-call", {"action_approved": True})


def _extract(rt: Runtime, workspace: str, doc: str, model: str) -> tuple[dict[str, Any], str, Orchestrator]:
    user = "engineer1" if workspace == "plant-a" else "buyer1"
    orch, tid = _task(rt, workspace, user, "Extract the inspection findings", [doc])
    ctx = _ctx(rt, orch, tid, model)
    read = rt.tools.execute("read_document", {"path": doc, "mode": "findings"}, ctx)
    req = LLMRequest(model=model, purpose="extract.findings", task_id=tid, context_records=read.records,
                     messages=[ChatMessage(role="user", content="Extract the findings.")])
    value = structured.call(rt.backend, req, load_schema("findings"), rt.settings.max_retries).value
    return value, tid, orch


def extraction(rt: Runtime, rows: list[dict[str, Any]], model: str) -> dict[str, Any]:
    tp = fp = fn = 0
    per_doc = []
    for row in rows:
        f, _, _ = _extract(rt, row["workspace"], row["doc"], model)
        got: set[tuple[str, str]] = {("tag", norm_tag(f["equipment_tag"]["value"]))}
        got |= {("m", f"{norm_tag(m['tag'])}:{m['value']:g}") for m in f["measurements"]}
        for key in ("report_date", "inspection_date", "next_inspection_date", "calibration_valid_until"):
            if f.get(key):
                try:
                    got.add((key, norm_date(f[key]["value"])))
                except ValueError:
                    got.add((key, f[key]["value"]))
        if f.get("po_number"):
            got.add(("po", f["po_number"]["value"]))
        want: set[tuple[str, str]] = {("tag", norm_tag(row["equipment_tag"]))}
        want |= {("m", f"{norm_tag(t)}:{v:g}") for t, v in row["measurements"]}
        want |= {(k, v) for k, v in row["dates"].items()}
        if row.get("po_number"):
            want.add(("po", row["po_number"]))
        tp += len(got & want)
        fp += len(got - want)
        fn += len(want - got)
        per_doc.append({"doc": row["doc"], "missed": sorted(map(str, want - got)), "extra": sorted(map(str, got - want))})
    return {"precision": _ratio(tp, tp + fp), "recall": _ratio(tp, tp + fn), "documents": per_doc}


def dual_read(rt: Runtime, model: str) -> dict[str, Any]:
    reader = FixtureReader()
    agreed = agreed_wrong = uncertain = accepted = accepted_right = total = 0
    for ws in ("plant-a",):
        root = rt.files.ws_root(ws) / "inputs"
        for side in sorted(root.glob("*.ocr.json")):
            for page in reader.read(side):
                for region in page.regions:
                    if not region.needs_vlm or region.field_kind not in CRITICAL_KINDS or region.truth is None:
                        continue
                    total += 1
                    out = reconcile(region, rt.backend, model, dayfirst=rt.settings.date_dayfirst)
                    truth = normalise_field(region.field_kind, region.truth)
                    correct = out.value.normalised == truth
                    if out.status == "agree":
                        agreed += 1
                        agreed_wrong += not correct
                    if out.status == "uncertain":
                        uncertain += 1
                    else:
                        accepted += 1
                        accepted_right += correct
    return {"fields": total, "agreement_rate": _ratio(agreed, total), "error_rate_among_agreed": _ratio(agreed_wrong, agreed),
            "uncertain_share": _ratio(uncertain, total), "accepted_accuracy": _ratio(accepted_right, accepted)}


def consistency(rt: Runtime, rows: list[dict[str, Any]], model: str) -> dict[str, Any]:
    expected_total = detected = false_alarms = clean_checks = 0
    details = []
    for row in rows:
        findings, tid, orch = _extract(rt, "plant-a", row["doc"], model)
        ctx = _ctx(rt, orch, tid, model)
        env = {"equipment_tag": findings["equipment_tag"]["value"]}
        rd = findings.get("report_date") or findings.get("inspection_date")
        as_of = norm_date(rd["value"]) if rd else None
        ground = rt.tools.execute("search_kb", {
            "queries": ["minimum casing wall thickness acceptance criteria",
                        f"past approval notes {env['equipment_tag']}"],
            "top_k": 8, "as_of": as_of, "tags": [env["equipment_tag"]]}, ctx)
        res = rt.tools.execute("check_consistency", {"facts": findings, "against": ["asset_register", ground.body]}, ctx)
        mismatches = sorted({r["rule"] for r in res.body["results"] if r["status"] == "mismatch"})
        expected = set(row["expected_mismatches"])
        expected_total += len(expected)
        detected += len(expected & set(mismatches))
        if not expected:
            clean_checks += len(res.body["results"])
            false_alarms += len(mismatches)
        else:
            false_alarms += len(set(mismatches) - expected)
        details.append({"doc": row["doc"], "mismatches": mismatches, "expected": sorted(expected)})
    return {"detection_rate": _ratio(detected, expected_total),
            "false_alarm_rate": _ratio(false_alarms, clean_checks), "reports": details}


def revision(rt: Runtime, rows: list[dict[str, Any]]) -> dict[str, Any]:
    user, ws = rt.policy.user("engineer1"), rt.policy.workspace("plant-a")
    correct = 0
    out = []
    for row in rows:
        res = rt.kb.search([row["query"]], 5, date.fromisoformat(row["as_of"]), user, ws, rt.policy)
        hit = next((h for h in res.hits if h.chunk.doc_number == row["doc_number"]
                    and row["clause"] in h.chunk.clause_ids), None)
        ok = hit is not None and hit.chunk.revision == row["expected_revision"]
        correct += ok
        out.append({"id": row["id"], "ok": ok, "found": hit.chunk.revision if hit else None})
    return {"correct_revision_rate": _ratio(correct, len(rows)), "questions": out}


def graph_recall(rt: Runtime, rows: list[dict[str, Any]]) -> dict[str, Any]:
    user, ws = rt.policy.user("engineer1"), rt.policy.workspace("plant-a")
    hits_with = hits_without = 0
    out = []
    for row in rows:
        today = rt.clock.now().date()
        res = rt.kb.search([row["query"]], row["k"], today, user, ws, rt.policy)
        found = row["expected_doc"] in {h.chunk.doc_number for h in res.hits}
        plain = rt.kb.search([row["query"]], row["k"], today, user, ws, rt.policy, expand_graph=False)
        found_plain = row["expected_doc"] in {h.chunk.doc_number for h in plain.hits}
        hits_with += found
        hits_without += found_plain
        out.append({"id": row["id"], "with_graph": found, "text_only": found_plain})
    return {"recall_at_k": _ratio(hits_with, len(rows)), "recall_at_k_without_graph": _ratio(hits_without, len(rows)),
            "questions": out}


def provenance_detection(rt: Runtime, rows: list[dict[str, Any]], trace_a: Any) -> dict[str, Any]:
    from docx import Document

    deliverable = next(d for d in trace_a.deliverables if d.relpath.endswith(".docx"))
    base = rt.files.open_path(deliverable.final_file_id or deliverable.file_id)
    records = rt.ledger.for_task(trace_a.id)
    baseline = provenance.check_file(base, records, rt.ledger, rt.settings.number_tolerance)
    seeded_total = flagged = 0
    for row in rows:
        doc = Document(str(base))
        for figure in row["seeded"]:
            doc.add_paragraph(f"An additional reading of {figure} was noted.")
        path = rt.settings.path(rt.settings.data_dir) / f"prov-{row['id']}.docx"
        doc.save(str(path))
        report = provenance.check_file(path, records, rt.ledger, rt.settings.number_tolerance)
        orphans = {f.raw for f in report.figures if f.status == "unsourced"}
        seeded_total += len(row["seeded"])
        flagged += sum(1 for s in row["seeded"] if s in orphans)
    counts = baseline.counts
    return {"orphan_detection_rate": _ratio(flagged, seeded_total),
            "resolved_share_on_generated_draft": _ratio(counts["sourced"] + counts["derived"], sum(counts.values())),
            "baseline_counts": counts}


def citation_rate(rt: Runtime, state: Any) -> dict[str, Any]:
    claims = [c for d in state.deliverables for c in d.claims]
    verified = sum(1 for c in claims if c["status"] == "verified")
    return {"claims": len(claims), "verified_rate": _ratio(verified, len(claims)),
            "currency_warnings": sum(1 for c in claims if c.get("currency_warning"))}


def plan_compiler(rt: Runtime, rows: list[dict[str, Any]], model: str) -> dict[str, Any]:
    valid_before = valid_after = 0
    out = []
    for row in rows:
        ws = rt.policy.workspace(row["workspace"])
        cctx = CompileContext(tools=rt.tools, task_label=Label.lowest(), workspace_ceiling=ws.ceiling,
                              max_steps=rt.settings.max_plan_steps, budgets=dict(rt.settings.step_budgets_s),
                              attachments=row["attachments"])
        if "plan" in row:
            typed = row["plan"]
        else:
            req = LLMRequest(model=model, purpose="plan.write", messages=[ChatMessage(role="user", content=row["text"])],
                             meta={"task_text": row["text"], "attachments": row["attachments"]})
            typed = structured.call(rt.backend, req, load_schema("typed_plan"), rt.settings.max_retries).value
        first = compile_plan(Plan.from_typed(typed), cctx)
        repaired, _ = compile_with_repair(rt.backend, model, Plan.from_typed(typed), cctx, rt.settings.max_repairs,
                                          [ChatMessage(role="user", content=row["text"])])
        valid_before += first.valid
        valid_after += repaired.valid
        out.append({"id": row["id"], "valid_first": first.valid, "valid_after_repair": repaired.valid,
                    "errors": repaired.errors})
    return {"valid_before_repair": _ratio(valid_before, len(rows)),
            "valid_after_repair": _ratio(valid_after, len(rows)), "plans": out}


def labels(rt: Runtime, rows: list[dict[str, Any]]) -> dict[str, Any]:
    correct = 0
    out = []
    for row in rows:
        orch = Orchestrator(rt, AutoApprove())
        state = orch.create_task(row["workspace"], row["user"], row["text"], row["attachments"])
        done = orch.run(state.id)
        expected = Label.parse(row["expected"])
        marks = [d.label for d in done.deliverables] or [done.label or Label.lowest()]
        ok = done.status == "completed" and all(m == expected for m in marks)
        correct += ok
        out.append({"id": row["id"], "status": done.status, "label": [m.display() for m in marks],
                    "expected": expected.display(), "ok": ok})
    return {"propagation_rate": _ratio(correct, len(rows)), "tasks": out}


def _code_csv(path: Path, column: str, seed: int) -> list[float]:
    rng = random.Random(seed)
    values = [round(50 + rng.gauss(0, 3), 3) for _ in range(120)]
    for i in (17, 63, 101):
        values[i] = round(values[i] + (60 if i % 2 else -45), 3)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", column])
        for i, v in enumerate(values):
            w.writerow([f"2026-08-01T{i // 60:02d}:{i % 60:02d}:00", v])
    return values


def _hidden_test(check: str, result: dict[str, Any] | None, values: list[float]) -> bool:
    if not isinstance(result, dict):
        return False
    if check == "mean":
        return abs(float(result.get("mean", 1e9)) - statistics.fmean(values)) < 1e-3
    if check == "max":
        return float(result.get("max", -1e9)) == max(values)
    if check.startswith("count:"):
        thr = float(check.split(":")[1])
        return int(result.get("count", -1)) == sum(1 for v in values if v > thr)
    if check == "anomalies":
        return int(result.get("anomalies", -1)) == 3
    if check == "median":
        return abs(float(result.get("median", 1e9)) - statistics.median(values)) < 1e-6
    return False


def code(rt: Runtime, rows: list[dict[str, Any]], model: str | None = None) -> dict[str, Any]:
    passed = 0
    iterations: list[int] = []
    out = []
    for row in rows:
        name = f"eval_{row['id']}.csv"
        path = rt.files.ws_root("plant-a") / "inputs" / name
        values = _code_csv(path, row["column"], row["seed"])
        rt.files.register("plant-a", f"inputs/{name}", rt.policy.policy.default_label())
        orch = Orchestrator(rt, AutoApprove())
        meta = {"force_model": model} if model else None
        state = orch.create_task("plant-a", "engineer1", row["text"], [f"inputs/{name}"], meta=meta)
        done = orch.run(state.id)
        code_out = done.result.get("code") or {}
        ok = done.status == "completed" and _hidden_test(row["check"], code_out.get("result"), values)
        passed += ok
        if code_out.get("attempts"):
            iterations.append(int(code_out["attempts"]))
        out.append({"id": row["id"], "status": done.status, "model": done.model, "ok": ok,
                    "attempts": code_out.get("attempts")})
    return {"pass_rate": _ratio(passed, len(rows)),
            "mean_iterations": round(statistics.fmean(iterations), 2) if iterations else None, "tasks": out}


def cache(rt: Runtime) -> dict[str, Any]:
    from workbench.context.cache_salt import cache_salt

    parts = rt.cache.snapshot()
    probe = "system prompt\n" + rt.tools.prompt_schemas()
    labels_ = [Label.parse(x) for x in ("Restricted", "Confidential", "Secret+VENDOR-COMMERCIAL")]
    salts = [cache_salt(lab, rt.secret_key) for lab in labels_]
    cross = sum(rt.cache.cross_partition_hits(probe, a, b) for a in salts for b in salts if a != b)
    return {"partitions": parts, "cross_partition_hits": cross,
            "note": "simulated block-level prefix cache; real cached_tokens replace it when vLLM reports them"}


def timed(fn: Any, *args: Any) -> tuple[Any, float]:
    start = time.perf_counter()
    result = fn(*args)
    return result, round(time.perf_counter() - start, 2)


def model_quality(rt: Runtime, model: str, root: Path) -> dict[str, float]:
    """Success rate per route for one model, measured on the local evaluation sets."""
    from workbench.eval.datasets import load

    entry = rt.registry.get(model)
    quality: dict[str, float] = {}
    for route in entry.serves:
        if route == "document":
            e = extraction(rt, load(root, "extraction"), model)
            quality[route] = round((e["precision"] + e["recall"]) / 2, 2)
        elif route == "vision":
            quality[route] = round(dual_read(rt, model)["accepted_accuracy"], 2)
        elif route in {"agentic", "reasoning"}:
            quality[route] = round(plan_compiler(rt, load(root, "plan_compiler"), model)["valid_after_repair"], 2)
        elif route == "general":
            quality[route] = round(revision(rt, load(root, "revision"))["correct_revision_rate"], 2)
        elif route == "code":
            quality[route] = round(code(rt, load(root, "code")[:4], model)["pass_rate"], 2)
    return quality


def dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str, ensure_ascii=False)
