"""Revision chains by document number (README section 4.7.1)."""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from pydantic import BaseModel


class RevisionInfo(BaseModel):
    doc_number: str
    revision: str
    effective_from: date
    superseded_by: str | None = None
    superseded_on: date | None = None
    title: str = ""


class RevisionIndex:
    def __init__(self) -> None:
        self.chains: dict[str, list[RevisionInfo]] = {}

    def build(self, items: list[RevisionInfo]) -> None:
        grouped: dict[str, dict[str, RevisionInfo]] = defaultdict(dict)
        for it in items:
            grouped[it.doc_number][it.revision] = it
        chains: dict[str, list[RevisionInfo]] = {}
        for doc, revs in grouped.items():
            ordered = sorted(revs.values(), key=lambda r: r.effective_from)
            linked = []
            for i, rev in enumerate(ordered):
                nxt = ordered[i + 1] if i + 1 < len(ordered) else None
                linked.append(rev.model_copy(update={
                    "superseded_by": nxt.revision if nxt else None,
                    "superseded_on": nxt.effective_from if nxt else None,
                }))
            chains[doc] = linked
        self.chains = chains

    def in_force(self, doc_number: str, as_of: date) -> str | None:
        current = None
        for rev in self.chains.get(doc_number, []):
            if rev.effective_from <= as_of:
                current = rev.revision
        return current

    def info(self, doc_number: str, revision: str) -> RevisionInfo | None:
        return next((r for r in self.chains.get(doc_number, []) if r.revision == revision), None)

    def previous(self, doc_number: str, revision: str) -> RevisionInfo | None:
        chain = self.chains.get(doc_number, [])
        for i, r in enumerate(chain):
            if r.revision == revision and i > 0:
                return chain[i - 1]
        return None

    def is_superseded(self, doc_number: str, revision: str, on: date) -> bool:
        info = self.info(doc_number, revision)
        return bool(info and info.superseded_on and info.superseded_on <= on)
