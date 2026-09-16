"""``read_document``: OCR/VLM pipeline into the ledger, plus workspace indexing for follow-ups."""

from __future__ import annotations

from typing import Any

from workbench.core.errors import ToolError
from workbench.documents.pipeline import ReadContext
from workbench.documents.pipeline import read_document as run_pipeline
from workbench.documents.readers import display_name
from workbench.tools.registry import ToolContext, ToolResult, ToolSpec

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 12},
        "mode": {"enum": ["full", "findings", "tables"]},
    },
    "anyOf": [{"required": ["path"]}, {"required": ["paths"]}],
}


def read_document(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    paths = list(args.get("paths") or []) or [str(args["path"])]
    mode = str(args.get("mode") or "full")
    rt = ctx.rt
    documents = []
    record_ids: list[str] = []
    preread = ctx.meta.get("preread") or {}
    for rel in paths:
        if rel in preread:
            documents.append(preread[rel])
            record_ids += list(preread[rel]["records"])
            continue
        path = rt.files.resolve(ctx.workspace, rel)
        frec = rt.files.find(ctx.workspace, rel)
        if frec is None:
            raise ToolError(f"{rel} is not registered in workspace {ctx.workspace}")
        rctx = ReadContext(task_id=ctx.task_id, ledger=rt.ledger, backend=rt.backend, vlm_model=ctx.model,
                           policy=rt.policy.policy, evidence_dir=rt.evidence_dir(ctx.task_id),
                           produced_by=ctx.call_id, dayfirst=rt.settings.date_dayfirst,
                           max_retries=rt.settings.max_retries)
        result = run_pipeline(path, frec.label, rctx, mode)
        record_ids += [r.id for r in result.records]
        for d in result.dual_read:
            if d.get("field_kind") == "tag" and d.get("status") in {"agree", "resolved"}:
                rt.kb.graph.add_fact_edge(str(d["value"]), display_name(path), str(d["record"]),
                                          "high" if d["status"] == "agree" else "medium", result.label)
        if result.engine == "pymupdf" and path.suffix.lower() == ".pdf":
            ws = rt.policy.workspace(ctx.workspace)
            rt.kb.index_document(display_name(path), display_name(path),
                                 [(p.page, p.text) for p in result.pages], result.label, ctx.workspace,
                                 ws.acl_groups, rel)
        documents.append({
            "doc": result.doc, "path": rel, "engine": result.engine, "pages": len(result.pages),
            "label": result.label.display(), "records": [r.id for r in result.records],
            "tables": [{"page": p.page, "id": t.id, "title": t.title, "header": t.header, "rows": t.rows}
                       for p in result.pages for t in p.tables],
            "uncertain": result.uncertain, "dual_read": result.dual_read, "summary": result.summary(),
        })
    summary = "; ".join(d["summary"] for d in documents)
    return ToolResult(ok=True, summary=summary, records=record_ids,
                      body={"records": record_ids, "documents": documents, "mode": mode})


SPECS = [ToolSpec(name="read_document",
                  description="Read PDFs and scans (OCR plus VLM with dual-read on critical fields) into the ledger.",
                  input_schema=SCHEMA, handler=read_document, output_type="document", budget_key="read_document")]
