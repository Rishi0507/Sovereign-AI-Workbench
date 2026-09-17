"""Workspace-jailed file tools: ``list_files``, ``read_file``, ``write_file``."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from workbench.core.errors import PolicyError, ToolError
from workbench.core.models import Anchor
from workbench.documents.pipeline import text_fields
from workbench.documents.readers import TEXT_EXTS, TextFileReader, sidecar_for
from workbench.tools.registry import ToolContext, ToolResult, ToolSpec, obj

LIST_SCHEMA = obj({"area": {"enum": ["inputs", "drafts", "final"]}})
READ_SCHEMA = obj({"path": {"type": "string", "minLength": 1, "maxLength": 300}}, ["path"])
WRITE_SCHEMA = obj({"path": {"type": "string", "pattern": "^drafts/", "maxLength": 300},
                    "content": {"type": "string", "maxLength": 200000}}, ["path", "content"])


def list_files(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    files = ctx.rt.files.list(ctx.workspace, args.get("area"))
    rows = [{"path": f.relpath, "size": f.size, "label": f.label.display()} for f in files]
    return ToolResult(ok=True, summary=f"{len(rows)} file(s) in {args.get('area') or 'workspace'}", body=rows)


def read_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    rel = str(args["path"])
    path = ctx.rt.files.resolve(ctx.workspace, rel)
    suffix = path.suffix.lower()
    if suffix == ".pdf" or (sidecar_for(path) is not None and suffix != ".md"):
        from workbench.tools.read_document import read_document

        return read_document({"path": rel, "mode": "full"}, ctx)
    if suffix not in TEXT_EXTS:
        raise ToolError(f"cannot read {suffix} files as text")
    rec_file = ctx.rt.files.find(ctx.workspace, rel)
    if rec_file is None:
        raise ToolError(f"{rel} is not registered in the workspace")
    page = TextFileReader().read(path)[0]
    anchor = Anchor(doc=path.name, page=1)
    detected = ctx.rt.policy.policy.detect(page.text)
    label = rec_file.label.join(detected) if detected else rec_file.label
    body = page.text
    for t in page.tables:
        body += f"\n\nTable {t.id}: {t.title}\n{t.markdown()}"
    rec = ctx.rt.ledger.add(ctx.task_id, "ocr_text", summary=f"{path.name} (text file): {page.text[:160]}",
                            body=body, label=label, anchor=anchor, confidence="high", produced_by=ctx.call_id,
                            fields=text_fields(page.text, anchor, ctx.rt.settings.date_dayfirst))
    columns = page.tables[0].header if page.tables and suffix == ".csv" else []
    return ToolResult(ok=True, summary=f"read {rel} ({len(page.text)} chars)", records=[rec.id],
                      body={"records": [rec.id], "columns": columns, "doc": path.name})


def write_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    rel = PurePosixPath(str(args["path"]))
    if rel.parts[0] != "drafts" or len(rel.parts) < 2:
        raise PolicyError("write_file may only write under drafts/")
    name = PurePosixPath(*rel.parts[1:]).as_posix()
    overwrite_ok = bool(ctx.meta.get("action_approved"))
    rec = ctx.rt.files.write_draft(ctx.workspace, name, str(args["content"]).encode("utf-8"), ctx.label(),
                                   ctx.task_id, overwrite_ok)
    return ToolResult(ok=True, summary=f"wrote {rec.relpath}", files=[rec.id], body={"file": rec.relpath})


SPECS = [
    ToolSpec(name="list_files", description="List files in the task workspace with their labels.",
             input_schema=LIST_SCHEMA, handler=list_files, output_type="file_list"),
    ToolSpec(name="read_file", description="Read a workspace file into the evidence ledger (PDFs go through read_document).",
             input_schema=READ_SCHEMA, handler=read_file, output_type="document"),
    ToolSpec(name="write_file", description="Write a text file under drafts/. Overwriting needs approval.",
             input_schema=WRITE_SCHEMA, handler=write_file, side_effect=True, output_type="file"),
]
