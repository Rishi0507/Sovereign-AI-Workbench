"""The plant graph: equipment, the documents that show it, and what is recorded against it.

Nodes and edges are filtered by the reading ceiling of the signed-in user in the chosen workspace,
so the shape of the graph never reveals equipment a person may not see.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from workbench.api.deps import current_user, get_rt, workspace_for
from workbench.core.labels import Label, User
from workbench.kb.graph import Node, node_id
from workbench.runtime import Runtime

router = APIRouter(tags=["graph"])

KIND_TITLES = {"tag": "Equipment", "class": "Equipment class", "document": "Document", "clause": "Clause",
               "inspection": "Inspection", "vendor": "Vendor", "po": "Purchase order"}


def _name(kind: str, key: str, props: dict[str, Any]) -> str:
    """A name a person would use, rather than the key the graph stores."""
    if kind == "inspection":
        quantity = str(props.get("quantity", "reading")).replace("_", " ").capitalize()
        value = props.get("value")
        unit = props.get("unit", "")
        measured = f"{value} {unit}".strip() if value is not None else ""
        return f"{quantity} {measured} on {props.get('date', '')}".replace("  ", " ").strip()
    if kind == "clause":
        return str(props.get("heading") or key)
    if kind == "class":
        return str(key).replace("_", " ")
    return str(props.get("name") or props.get("title") or key)


def _node(node: Node) -> dict[str, Any]:
    props = dict(node.props)
    return {"id": node.id, "kind": node.kind, "key": node.key, "title": KIND_TITLES.get(node.kind, node.kind),
            "label": node.label.model_dump(mode="json"), "label_display": node.label.display(),
            "name": _name(node.kind, node.key, props), "props": props}


def _readable(rt: Runtime, ceiling: Label, node: Node) -> bool:
    return ceiling.dominates(node.label)


@router.get("/graph")
def graph(workspace: str | None = None, tag: str | None = None, depth: int = 2,
          user: User = Depends(current_user), rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    """The whole graph the user may see, or the neighbourhood of one tag."""
    ws_id = workspace or next((w.id for w in rt.policy.workspaces.values()
                               if rt.policy.can_access_workspace(user, w)), None)
    if ws_id is None:
        raise HTTPException(403, "no workspace access")
    ws = workspace_for(rt, user, ws_id)
    ceiling = rt.policy.retrieval_ceiling(user, ws)
    g = rt.kb.graph

    if tag:
        start = g.node(node_id("tag", tag))
        if start is None or not _readable(rt, ceiling, start):
            raise HTTPException(404, f"{tag} is not in the plant graph")
        nodes = {start.id: start}
        edges = []
        for node, edge in g.neighbours(tag, ceiling=ceiling, depth=max(1, min(depth, 3))):
            nodes[node.id] = node
            edges.append(edge)
    else:
        nodes = {n.id: n for n in g.nodes() if _readable(rt, ceiling, n)}
        edges = [e for n in nodes.values() for e in g.edges_of(n.id) if e.src in nodes and e.dst in nodes]

    seen: set[tuple[str, str, str]] = set()
    unique = []
    for e in edges:
        key = (e.src, e.dst, e.kind)
        if key in seen or e.src not in nodes or e.dst not in nodes:
            continue
        seen.add(key)
        unique.append({"source": e.src, "target": e.dst, "kind": e.kind, "record": e.source_record})
    return {"workspace": ws.id, "workspace_title": ws.title, "ceiling": ceiling.display(),
            "nodes": [_node(n) for n in nodes.values()], "edges": unique}
