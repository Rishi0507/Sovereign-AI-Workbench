"""Clause-level diff between two revisions with explicit numeric limit comparison."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from workbench.core.normalise import find_quantities
from workbench.kb.chunking import Chunk

Status = Literal["unchanged", "amended", "added", "withdrawn"]


class LimitChange(BaseModel):
    old: float | None
    new: float | None
    unit: str | None


class ClauseDiff(BaseModel):
    clause: str
    heading: str
    status: Status
    old_text: str | None = None
    new_text: str | None = None
    limits: list[LimitChange] = Field(default_factory=list)


def _key(chunk: Chunk) -> str:
    if chunk.clause_ids:
        return chunk.clause_ids[-1]
    return chunk.section.split(" > ")[-1].lower()


def _body(text: str) -> str:
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def diff_revisions(old: list[Chunk], new: list[Chunk]) -> list[ClauseDiff]:
    old_map = {_key(c): c for c in old}
    new_map = {_key(c): c for c in new}
    keys = list(dict.fromkeys([*old_map, *new_map]))
    out: list[ClauseDiff] = []
    for key in keys:
        o, n = old_map.get(key), new_map.get(key)
        ref = n if n is not None else o
        heading = re.sub(r"^\d+(?:\.\d+)*\s+", "", ref.section.split(" > ")[-1]) if ref else key
        if o is None and n is not None:
            out.append(ClauseDiff(clause=key, heading=heading, status="added", new_text=_body(n.text)))
            continue
        if n is None and o is not None:
            out.append(ClauseDiff(clause=key, heading=heading, status="withdrawn", old_text=_body(o.text)))
            continue
        assert o is not None and n is not None
        ob, nb = _body(o.text), _body(n.text)
        if ob == nb:
            out.append(ClauseDiff(clause=key, heading=heading, status="unchanged"))
            continue
        oq = [q for q in find_quantities(ob) if q.unit]
        nq = [q for q in find_quantities(nb) if q.unit]
        limits = []
        for i in range(max(len(oq), len(nq))):
            a = oq[i] if i < len(oq) else None
            b = nq[i] if i < len(nq) else None
            if a is None or b is None or a.magnitude != b.magnitude or a.unit != b.unit:
                limits.append(LimitChange(old=a.magnitude if a else None, new=b.magnitude if b else None,
                                          unit=(b or a).unit if (b or a) else None))
        out.append(ClauseDiff(clause=key, heading=heading, status="amended", old_text=ob, new_text=nb, limits=limits))
    return out
