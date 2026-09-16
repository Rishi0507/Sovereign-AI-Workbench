"""Supersession impact report (README section 4.7.1). A draft for the document owner only."""

from __future__ import annotations

import re
from typing import Any

from workbench.kb.retrieve import KnowledgeBase


def impact_report(kb: KnowledgeBase, doc_number: str, revision: str) -> dict[str, Any]:
    prev = kb.revisions.previous(doc_number, revision)
    diffs = kb.clause_diff(doc_number, revision)
    changed = [d for d in diffs if d.status in {"amended", "withdrawn"}]
    notes: list[dict[str, Any]] = []
    if prev is not None:
        for c in sorted(kb.chunks.values(), key=lambda c: c.id):
            if c.doc_type != "approval_note" or c.doc_number == doc_number:
                continue
            for d in changed:
                pattern = (rf"{re.escape(doc_number)}\s+Rev\s+{re.escape(prev.revision)}\s+clause\s+"
                           rf"{re.escape(d.clause)}\b")
                if re.search(pattern, c.text):
                    notes.append({"note": c.doc_number, "title": c.title, "page": c.page, "clause": d.clause,
                                  "status": d.status, "old": d.old_text, "new": d.new_text,
                                  "excerpt": " ".join(c.text.split())[:240]})
    classes = sorted({cls for c in kb.chunks_of(doc_number, revision) for cls in c.applies_to_classes})
    equipment = sorted({t for cls in classes for t in kb.graph.tags_of_class(cls)})
    lines = [f"# Supersession impact: {doc_number} Rev {prev.revision if prev else '?'} to Rev {revision}", "",
             "Draft for the document owner. Nothing has been changed automatically.", "", "## Clause changes", ""]
    for d in diffs:
        if d.status == "unchanged":
            continue
        limit = "; ".join(f"{lc.old} to {lc.new} {lc.unit or ''}".strip() for lc in d.limits)
        lines.append(f"- Clause {d.clause} ({d.heading}): {d.status}" + (f", limit {limit}" if limit else ""))
    lines += ["", "## Past notes citing changed clauses", ""]
    if notes:
        for n in notes:
            lines += [f"- {n['note']} ({n['title']}), p. {n['page']}, cites clause {n['clause']} ({n['status']})",
                      f"  - Old wording: {n['old']}", f"  - New wording: {n['new']}"]
    else:
        lines.append("- None")
    lines += ["", "## Equipment governed", ""]
    lines += [f"- {t}" for t in equipment] or ["- None"]
    return {"doc_number": doc_number, "revision": revision, "previous": prev.revision if prev else None,
            "clauses": [d.model_dump() for d in diffs], "notes": notes, "equipment": equipment,
            "classes": classes, "markdown": "\n".join(lines) + "\n"}
