"""``check_consistency``: deterministic comparison of extracted facts with reference sources."""

from __future__ import annotations

from typing import Any

from workbench.checks.engine import CheckContext, CheckResult
from workbench.tools.registry import ToolContext, ToolResult, ToolSpec, obj

SCHEMA = obj({
    "facts": {"type": "object"},
    "against": {"type": "array", "items": {"type": ["string", "object"]}},
}, ["facts"])


def _record_ids(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [str(r) for r in value.get("records", [])]
    if isinstance(value, str) and value.startswith("R-"):
        return [value]
    return []


def check_consistency(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    rt = ctx.rt
    facts = dict(args["facts"])
    if "value" in facts and isinstance(facts.get("value"), dict):
        facts = facts["value"]
    all_records = rt.ledger.for_task(ctx.task_id)
    wanted = {rid for ref in args.get("against") or [] for rid in _record_ids(ref)}
    fact_ids = {str(v.get("record")) for v in facts.values() if isinstance(v, dict) and v.get("record")}
    fact_ids |= {str(m.get("record")) for m in facts.get("measurements") or []}
    records = [r for r in all_records
               if r.id in wanted or r.id in fact_ids or (not wanted and r.kind in {"kb_chunk", "graph_fact"})]
    cctx = CheckContext(task_id=ctx.task_id, facts=facts, ledger=rt.ledger, records=records,
                        produced_by=ctx.call_id, document_type=str(facts.get("document_type", "inspection_report")),
                        dayfirst=rt.settings.date_dayfirst, party_threshold=rt.settings.party_match_threshold)
    results: list[CheckResult] = rt.checks.run(cctx)
    used = {r.left_record for r in results} | fact_ids
    for rec in all_records:
        if rec.kind == "vlm_read" and rec.confidence == "uncertain" and rec.id not in used:
            body = rec.body if isinstance(rec.body, dict) else {}
            res = CheckResult(
                id=f"C{len(results) + 1}", rule="dual_read", description="Critical field read by OCR and VLM",
                status="not_checked", left_value=str(body.get("ocr_value")), right_value=str(body.get("vlm_value")),
                left_anchor=rec.anchor, left_record=rec.id,
                note=f"OCR and VLM disagree on {body.get('field_kind')}; zoomed re-read chose "
                     f"{body.get('zoomed_choice')}. Not used in any check.",
                extra={"crop": body.get("crop"), "zoomed_crop": body.get("zoomed_crop")},
            )
            results.append(rt.checks._record(cctx, res, [rec.id]))
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    ids = [r.record_id for r in results if r.record_id]
    summary = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in sorted(counts.items()))
    return ToolResult(ok=True, summary=f"{len(results)} check(s): {summary}", records=ids,
                      body={"records": ids, "results": [r.model_dump(mode="json") for r in results],
                            "counts": counts})


SPECS = [ToolSpec(name="check_consistency",
                  description="Compare extracted facts with the asset register, retrieved limits and tag history.",
                  input_schema=SCHEMA, handler=check_consistency, output_type="check_results",
                  budget_key="check_consistency")]
