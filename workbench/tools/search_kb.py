"""``search_kb`` and ``graph_lookup``: grounded retrieval into the ledger."""

from __future__ import annotations

from datetime import date
from typing import Any

from workbench.core.models import Anchor, TypedValue
from workbench.core.normalise import find_quantities, norm_date, norm_tag
from workbench.tools.registry import ToolContext, ToolResult, ToolSpec, obj

SEARCH_SCHEMA = obj({
    "queries": {"type": "array", "items": {"type": "string", "minLength": 2}, "minItems": 1, "maxItems": 6},
    "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
    "as_of": {"type": ["string", "null"]},
    "tags": {"type": "array", "items": {"type": "string"}},
    "doc": {"type": ["string", "null"]},
}, ["queries"])
GRAPH_SCHEMA = obj({"tag": {"type": "string", "minLength": 2}, "as_of": {"type": ["string", "null"]}}, ["tag"])


def _as_of(value: Any, ctx: ToolContext) -> date:
    if value:
        return date.fromisoformat(norm_date(str(value), ctx.rt.settings.date_dayfirst))
    return ctx.rt.clock.now().date()


def _existing(ctx: ToolContext, kind: str) -> dict[str, str]:
    out = {}
    for r in ctx.rt.ledger.for_task(ctx.task_id):
        if r.kind == kind:
            out[r.summary.split(" :: ", 1)[0]] = r.id
    return out


def add_graph_facts(ctx: ToolContext, nodes: list[Any]) -> list[str]:
    existing = _existing(ctx, "graph_fact")
    ids = []
    for n in nodes:
        p = n.props
        key = f"history {p.get('tag')} {p.get('date')} {p.get('location')}"
        if key in existing:
            ids.append(existing[key])
            continue
        anchor = Anchor(doc=f"inspection {p.get('report')}")
        value = f"{p.get('value')} {p.get('unit')}"
        rec = ctx.rt.ledger.add(
            ctx.task_id, "graph_fact",
            summary=f"{key} :: {p.get('quantity')} {value} ({p.get('report')})",
            body={k: p.get(k) for k in ("tag", "date", "quantity", "value", "unit", "location", "report")},
            label=n.label, anchor=anchor, confidence="high", produced_by=ctx.call_id,
            fields={"quantity": TypedValue(kind="quantity", raw=value, normalised=value,
                                           magnitude=float(p.get("value")), unit="millimeter"
                                           if p.get("unit") == "mm" else p.get("unit"), anchor=anchor),
                    "date": TypedValue(kind="date", raw=str(p.get("date")), normalised=str(p.get("date"))),
                    "tag": TypedValue(kind="tag", raw=str(p.get("tag")), normalised=norm_tag(str(p.get("tag"))))},
        )
        ids.append(rec.id)
    return ids


def search_kb(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    rt = ctx.rt
    user = rt.policy.user(ctx.user)
    ws = rt.policy.workspace(ctx.workspace)
    as_of = _as_of(args.get("as_of"), ctx)
    doc = args.get("doc")
    if doc and not str(doc).startswith(f"{ctx.workspace}/") and rt.kb.has_document(ctx.workspace, str(doc)):
        doc = f"{ctx.workspace}/{doc}"
    result = rt.kb.search([str(q) for q in args["queries"]], int(args.get("top_k") or 8), as_of, user, ws,
                          rt.policy, extra_tags=list(args.get("tags") or []), today=rt.clock.now().date(),
                          doc_filter=doc)
    existing = _existing(ctx, "kb_chunk")
    ids: list[str] = []
    hits = []
    for h in result.hits:
        c = h.chunk
        key = f"[{c.id}]"
        if key in existing:
            ids.append(existing[key])
        else:
            anchor = Anchor(doc=c.doc_number, revision=c.revision, page=c.page)
            heading = c.section.split(" > ")[-1]
            fields = {f"quantity[{i}]": TypedValue(kind="quantity", raw=q.raw, normalised=f"{q.magnitude:g} {q.unit}",
                                                   magnitude=q.magnitude, unit=q.unit, anchor=anchor)
                      for i, q in enumerate(find_quantities(c.text)) if q.unit}
            for cl in c.clause_ids:
                fields[f"clause:{cl}"] = TypedValue(kind="clause", raw=cl, normalised=cl, anchor=anchor)
            rec = rt.ledger.add(
                ctx.task_id, "kb_chunk",
                summary=f"{key} :: {c.cite()} {heading}: {' '.join(c.text.split())[:160]}",
                body=c.text, label=c.label, anchor=anchor, confidence="high", produced_by=ctx.call_id,
                fields=fields,
            )
            ids.append(rec.id)
            existing[key] = rec.id
        hits.append({"record": ids[-1], "cite": c.cite(), "section": c.section, "score": h.score, "via": h.via,
                     "currency_warning": h.currency_warning})
    fact_ids = add_graph_facts(ctx, result.graph_facts)
    return ToolResult(
        ok=True,
        summary=(f"{len(hits)} passage(s) as of {as_of.isoformat()}"
                 + (f", {len(fact_ids)} earlier reading(s) from the plant graph" if fact_ids else "")
                 + (f"; filtered {result.filtered_out}" if result.filtered_out else "")),
        records=ids + fact_ids,
        body={"records": ids + fact_ids, "hits": hits, "graph_facts": fact_ids, "as_of": as_of.isoformat(),
              "expanded_tags": result.expanded_tags, "filtered_out": result.filtered_out},
    )


def graph_lookup(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    rt = ctx.rt
    ceiling = rt.policy.retrieval_ceiling(rt.policy.user(ctx.user), rt.policy.workspace(ctx.workspace))
    as_of = _as_of(args.get("as_of"), ctx)
    rows = rt.kb.graph.neighbours(str(args["tag"]), as_of=as_of, ceiling=ceiling)
    inspections = [n for n, _e in rows if n.kind == "inspection"]
    fact_ids = add_graph_facts(ctx, inspections)
    body = [{"node": n.id, "kind": n.kind, "edge": e.kind, "props": n.props} for n, e in rows]
    return ToolResult(ok=True, summary=f"{len(rows)} neighbour(s) of {args['tag']}", records=fact_ids,
                      body={"records": fact_ids, "neighbours": body})


SPECS = [
    ToolSpec(name="search_kb", description="Hybrid, graph-expanded, revision-aware search of the knowledge base.",
             input_schema=SEARCH_SCHEMA, handler=search_kb, output_type="kb_passages", budget_key="search_kb"),
    ToolSpec(name="graph_lookup", description="Neighbours of an equipment tag in the plant graph.",
             input_schema=GRAPH_SCHEMA, handler=graph_lookup, output_type="graph_facts", budget_key="graph_lookup"),
]
