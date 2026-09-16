"""Plant graph in SQLite (README section 4.7.2).

Nodes: equipment tags, classes, documents (per revision), clauses, vendors, POs, inspections.
Edges come from the asset register, tags found in documents (high or medium confidence only),
clause applicability declared in SOP front matter, and inspection records.
"""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select

from workbench.core.db import Database, edges_table, nodes_table
from workbench.core.labels import Label, Level
from workbench.core.normalise import find_tags, norm_tag, parse_iso
from workbench.kb.chunking import Chunk


class Node(BaseModel):
    id: str
    kind: str
    key: str
    label: Label
    props: dict[str, Any] = Field(default_factory=dict)


class Edge(BaseModel):
    src: str
    dst: str
    kind: str
    source_record: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None


def node_id(kind: str, key: str) -> str:
    return f"{kind}:{norm_tag(key) if kind == 'tag' else key}"


class PlantGraph:
    def __init__(self, db: Database) -> None:
        self.db = db

    def clear(self) -> None:
        with self.db.tx() as conn:
            conn.execute(delete(edges_table))
            conn.execute(delete(nodes_table))

    def add_node(self, kind: str, key: str, label: Label, **props: Any) -> str:
        nid = node_id(kind, key)
        with self.db.tx() as conn:
            existing = conn.execute(select(nodes_table.c.props, nodes_table.c.label)
                                    .where(nodes_table.c.id == nid)).first()
            if existing is None:
                conn.execute(nodes_table.insert().values(
                    id=nid, kind=kind, key=key, label=label.model_dump_json(), props=json.dumps(props, default=str)))
            else:
                merged = {**json.loads(existing[0]), **props}
                lab = Label.model_validate_json(existing[1]).join(label)
                conn.execute(nodes_table.update().where(nodes_table.c.id == nid)
                             .values(props=json.dumps(merged, default=str), label=lab.model_dump_json()))
        return nid

    def add_edge(self, src: str, dst: str, kind: str, source_record: str | None = None,
                 valid_from: str | None = None, valid_to: str | None = None) -> None:
        with self.db.tx() as conn:
            dup = conn.execute(select(edges_table.c.id).where(
                edges_table.c.src == src, edges_table.c.dst == dst, edges_table.c.kind == kind)).first()
            if dup is None:
                conn.execute(edges_table.insert().values(src=src, dst=dst, kind=kind, source_record=source_record,
                                                         valid_from=valid_from, valid_to=valid_to))

    def node(self, nid: str) -> Node | None:
        with self.db.read() as conn:
            row = conn.execute(select(nodes_table).where(nodes_table.c.id == nid)).first()
        if row is None:
            return None
        return Node(id=row.id, kind=row.kind, key=row.key, label=Label.model_validate_json(row.label),
                    props=json.loads(row.props))

    def nodes(self, kind: str | None = None) -> list[Node]:
        stmt = select(nodes_table)
        if kind:
            stmt = stmt.where(nodes_table.c.kind == kind)
        with self.db.read() as conn:
            rows = conn.execute(stmt.order_by(nodes_table.c.id)).all()
        return [Node(id=r.id, kind=r.kind, key=r.key, label=Label.model_validate_json(r.label),
                     props=json.loads(r.props)) for r in rows]

    def edges_of(self, nid: str) -> list[Edge]:
        with self.db.read() as conn:
            rows = conn.execute(select(edges_table).where(
                or_(edges_table.c.src == nid, edges_table.c.dst == nid)).order_by(edges_table.c.id)).all()
        return [Edge(src=r.src, dst=r.dst, kind=r.kind, source_record=r.source_record,
                     valid_from=r.valid_from, valid_to=r.valid_to) for r in rows]

    def neighbours(self, tag: str, as_of: date | None = None, ceiling: Label | None = None,
                   depth: int = 2) -> list[tuple[Node, Edge]]:
        """Neighbours of a tag up to ``depth`` hops, filtered by validity date and label."""
        start = node_id("tag", tag)
        if self.node(start) is None:
            return []
        seen = {start}
        frontier = [start]
        out: list[tuple[Node, Edge]] = []
        for _ in range(depth):
            nxt = []
            for nid in frontier:
                for e in self.edges_of(nid):
                    other = e.dst if e.src == nid else e.src
                    if other in seen:
                        continue
                    if as_of and e.valid_from and parse_iso(e.valid_from) > as_of:
                        continue
                    if as_of and e.valid_to and parse_iso(e.valid_to) <= as_of:
                        continue
                    n = self.node(other)
                    if n is None or (ceiling is not None and not ceiling.dominates(n.label)):
                        continue
                    # do not walk from a class to every other tag of the class
                    if n.kind == "tag":
                        continue
                    seen.add(other)
                    out.append((n, e))
                    if n.kind in {"vendor", "po", "inspection"}:
                        continue
                    nxt.append(other)
            frontier = nxt
        return out

    def tags_of_class(self, cls: str) -> list[str]:
        cid = node_id("class", cls)
        tags = []
        for e in self.edges_of(cid):
            if e.kind == "is_a" and e.dst == cid:
                n = self.node(e.src)
                if n:
                    tags.append(n.key)
        return sorted(tags)

    def readings(self, tag: str, quantity: str, as_of: date | None = None) -> list[Node]:
        out = []
        for n, e in self.neighbours(tag, as_of=as_of, depth=1):
            if n.kind == "inspection" and e.kind == "inspected" and n.props.get("quantity") == quantity:
                out.append(n)
        return sorted(out, key=lambda n: str(n.props.get("date")))

    # -- builders ------------------------------------------------------------------------------

    def load_asset_register(self, path: Path, label: Label | None = None) -> int:
        lab = label or Label(level=Level.RESTRICTED)
        count = 0
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                tag = self.add_node("tag", row["tag"], lab, tag=row["tag"], description=row.get("description", ""),
                                    cls=row.get("class", ""))
                if row.get("class"):
                    self.add_edge(tag, self.add_node("class", row["class"], lab), "is_a", "asset_register")
                if row.get("vendor"):
                    self.add_edge(tag, self.add_node("vendor", row["vendor"], lab), "supplied_by", "asset_register")
                if row.get("po_number"):
                    self.add_edge(tag, self.add_node("po", row["po_number"], lab), "ordered_on", "asset_register")
                if row.get("pid_sheet"):
                    self.add_edge(tag, self.add_node("document", row["pid_sheet"], lab, doc_number=row["pid_sheet"]),
                                  "shown_on", "asset_register")
                count += 1
        return count

    def load_inspections(self, path: Path, label: Label | None = None) -> int:
        lab = label or Label(level=Level.RESTRICTED)
        count = 0
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                key = f"{norm_tag(row['tag'])}@{row['date']}@{row['quantity']}@{row.get('location', '')}"
                insp = self.add_node("inspection", key, lab, tag=row["tag"], date=row["date"],
                                     quantity=row["quantity"], value=float(row["value"]), unit=row["unit"],
                                     location=row.get("location", ""), report=row.get("report", ""))
                tag = self.add_node("tag", row["tag"], lab, tag=row["tag"])
                self.add_edge(tag, insp, "inspected", f"inspections_history:{row.get('report', '')}",
                              valid_from=row["date"])
                count += 1
        return count

    def load_chunks(self, chunks: list[Chunk]) -> None:
        for c in chunks:
            if c.workspace:
                continue
            doc_key = c.doc_number + (f"@{c.revision}" if c.revision else "")
            doc = self.add_node("document", doc_key, c.label, doc_number=c.doc_number, revision=c.revision,
                                title=c.title, doc_type=c.doc_type)
            valid_from = c.effective_from.isoformat() if c.effective_from else None
            for cls in c.applies_to_classes:
                self.add_edge(self.add_node("class", cls, Label.lowest()), doc, "governed_by", c.doc_id,
                              valid_from=valid_from)
            for clause in c.clause_ids:
                cl = self.add_node("clause", f"{doc_key}/{clause}", c.label, clause=clause, doc=doc_key,
                                   heading=c.section.split(" > ")[-1])
                self.add_edge(doc, cl, "has_clause", c.id, valid_from=valid_from)
            tags = set(c.tags) | set(find_tags(c.text))
            for t in tags:
                tid = node_id("tag", t)
                if self.node(tid) is None:
                    continue  # only tags known from the asset register become edges
                kind = "concerns" if c.doc_type == "approval_note" else "mentioned_in"
                if c.doc_type == "pid":
                    kind = "shown_on"
                self.add_edge(tid, doc, kind, c.id, valid_from=valid_from)
            if c.doc_type == "pid":
                self.add_edge(node_id("document", c.doc_number), doc, "revision_of", c.id)

    def add_fact_edge(self, tag: str, doc: str, record_id: str, confidence: str, label: Label) -> bool:
        """Edges from extracted facts are added only for high or medium confidence."""
        if confidence not in {"high", "medium"}:
            return False
        tid = self.add_node("tag", tag, label, tag=tag)
        did = self.add_node("document", doc, label, doc_number=doc)
        self.add_edge(tid, did, "mentioned_in", record_id)
        return True
