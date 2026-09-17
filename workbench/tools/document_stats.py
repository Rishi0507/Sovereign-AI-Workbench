"""``document_stats``: counted facts about attached documents (pages, words, characters, tables).

The counts are computed from the extracted text, never estimated by a model, and each result is
a ``calc_result`` record so it can be cited like any other figure.
"""

from __future__ import annotations

import re
from typing import Any

from workbench.core.errors import ToolError
from workbench.core.models import TypedValue
from workbench.documents.readers import display_name
from workbench.tools.registry import ToolContext, ToolResult, ToolSpec

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 12},
    },
    "anyOf": [{"required": ["path"]}, {"required": ["paths"]}],
}

# A word is a number (5.6, 4,85,00,000) or a run of letters, with inner apostrophes and hyphens.
WORD_RE = re.compile(r"\d+(?:[.,]\d+)*|[^\W\d_]+(?:['\-][^\W\d_]+)*", re.UNICODE)

# Lines that only repeat the classification marking are page furniture, not document text.
MARKING_LINE = re.compile(r"^\s*(UNCLASSIFIED|RESTRICTED|CONFIDENTIAL|SECRET)\b[\w\s\-·]*$", re.IGNORECASE)


def count_words(text: str) -> int:
    body = "\n".join(line for line in text.splitlines() if not MARKING_LINE.match(line))
    return len(WORD_RE.findall(body))


def _fmt(n: int) -> str:
    return f"{n:,}"


def document_stats(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    rt = ctx.rt
    paths = list(args.get("paths") or []) or [str(args["path"])]
    record_ids: list[str] = []
    stats = []
    for rel in paths:
        frec = rt.files.find(ctx.workspace, rel)
        if frec is None:
            raise ToolError(f"{rel} is not registered in workspace {ctx.workspace}")
        path = rt.files.resolve(ctx.workspace, rel)
        pages = rt.reader.read(path)
        text = "\n".join(p.text for p in pages)
        item = {
            "document": display_name(path), "path": rel, "pages": len(pages), "words": count_words(text),
            "characters": len(re.sub(r"\s", "", text)), "tables": sum(len(p.tables) for p in pages),
        }
        stats.append(item)
        statement = (f"{item['document']} has {_fmt(item['pages'])} page{'s' if item['pages'] != 1 else ''} "
                     f"and {_fmt(item['words'])} words ({_fmt(item['characters'])} characters without spaces).")
        rec = rt.ledger.add(
            ctx.task_id, "calc_result", summary=statement, body={**item, "statement": statement,
                                                                "method": "words counted in the extracted text, "
                                                                          "marking lines excluded"},
            label=frec.label, confidence="high", produced_by=ctx.call_id,
            fields={name: TypedValue(kind="quantity", raw=str(item[name]), normalised=str(item[name]),
                                     magnitude=float(item[name]), unit=None)
                    for name in ("pages", "words", "characters", "tables")},
        )
        record_ids.append(rec.id)
    summary = "; ".join(f"{s['document']}: {_fmt(s['pages'])} pages, {_fmt(s['words'])} words" for s in stats)
    return ToolResult(ok=True, summary=summary, records=record_ids, body={"records": record_ids, "stats": stats})


SPECS = [ToolSpec(name="document_stats",
                  description="Count pages, words, characters and tables in attached documents.",
                  input_schema=SCHEMA, handler=document_stats, output_type="facts", budget_key="document_stats")]
