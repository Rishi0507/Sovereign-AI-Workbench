"""Citation verification (README section 4.6).

A sentence citing ``[R-...]`` is verified when every number, tag and date in it appears in the
cited records (after normalisation) and the reranker says the records support the sentence.
Cited clauses that are superseded on the draft date raise a currency warning.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from workbench.checks.provenance import MARKER_RE, numbers_in
from workbench.core.ledger import Ledger
from workbench.core.models import LedgerRecord
from workbench.core.normalise import find_dates, find_tags, norm_tag, to_base, units_compatible
from workbench.kb.rerank import Reranker
from workbench.kb.revisions import RevisionIndex

SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


class ClaimCheck(BaseModel):
    id: str
    location: str
    sentence: str
    records: list[str]
    status: Literal["verified", "unverified"]
    score: float
    reasons: list[str] = Field(default_factory=list)
    currency_warning: str | None = None


def _record_text(rec: LedgerRecord) -> str:
    base = rec.body if isinstance(rec.body, str) else json.dumps(rec.body, ensure_ascii=False)
    extra = " ".join(tv.raw for tv in rec.fields.values())
    return f"{rec.summary}\n{base}\n{extra}"


def split_claims(location: str, text: str) -> list[tuple[str, str]]:
    return [(location, s.strip()) for s in SENTENCE_RE.split(text) if s.strip()]


def verify(claims: list[tuple[str, str]], ledger: Ledger, reranker: Reranker, min_score: float,
           today: date | None = None, revisions: RevisionIndex | None = None,
           dayfirst: bool = True) -> list[ClaimCheck]:
    out: list[ClaimCheck] = []
    for location, sentence in claims:
        ids = [x.strip() for m in MARKER_RE.finditer(sentence) for x in m.group(1).split(",")]
        if not ids:
            continue
        clean = MARKER_RE.sub("", sentence).strip()
        reasons: list[str] = []
        records = [r for r in (ledger.maybe(i) for i in ids) if r is not None]
        missing = [i for i in ids if i not in {r.id for r in records}]
        if missing:
            reasons.append(f"cited record(s) not found: {', '.join(missing)}")
        texts = [_record_text(r) for r in records]
        blob = "\n".join(texts)
        rec_tags = {norm_tag(t) for t in find_tags(blob)}
        for t in find_tags(clean):
            if norm_tag(t) not in rec_tags:
                reasons.append(f"tag {t} not in cited record")
        rec_dates = {d.iso for d in find_dates(blob, dayfirst)} | set(re.findall(r"\d{4}-\d{2}-\d{2}", blob))
        for d in find_dates(clean, dayfirst):
            if d.iso not in rec_dates:
                reasons.append(f"date {d.raw} not in cited record")
        rec_nums = [(m, u) for t in texts for _raw, m, u in numbers_in(t)]
        for raw, mag, unit in numbers_in(clean):
            ok = False
            for m2, u2 in rec_nums:
                if unit and u2 and not units_compatible(unit, u2):
                    continue
                a, b = (to_base(mag, unit), to_base(m2, u2)) if unit and u2 else (mag, m2)
                if abs(a - b) <= 1e-6 * max(1.0, abs(a)):
                    ok = True
                    break
            if not ok:
                reasons.append(f"figure {raw} not in cited record")
        score = max((reranker.score(clean, t) for t in texts), default=0.0)
        if score < min_score:
            reasons.append(f"support score {score:.2f} below {min_score:.2f}")
        warning = None
        if today and revisions:
            for r in records:
                if r.kind == "kb_chunk" and r.anchor is not None and r.anchor.revision:
                    doc, rev = r.anchor.doc, r.anchor.revision
                    if revisions.is_superseded(doc, rev, today):
                        info = revisions.info(doc, rev)
                        warning = (f"{doc} Rev {rev} is superseded by Rev {info.superseded_by} "
                                   f"on {info.superseded_on}" if info else f"{doc} Rev {rev} is superseded")
        out.append(ClaimCheck(id=f"C{len(out) + 1}", location=location, sentence=sentence, records=ids,
                              status="unverified" if reasons else "verified", score=round(score, 3),
                              reasons=reasons, currency_warning=warning))
    return out


def claims_from_note(note: dict[str, Any]) -> list[tuple[str, str]]:
    claims: list[tuple[str, str]] = []
    for section in ("summary", "findings", "recommendation", "overall"):
        for i, item in enumerate(note.get(section) or []):
            text = item.get("text", "") if isinstance(item, dict) else str(item)
            claims += split_claims(f"{section} {i + 1}", text)
    for s_index, sec in enumerate(note.get("sections") or []):
        for i, pt in enumerate(sec.get("points") or []):
            claims += split_claims(f"section {s_index + 1} point {i + 1}", pt.get("text", ""))
    for i, item in enumerate(note.get("answer") or []):
        claims += split_claims(f"answer {i + 1}", item.get("text", ""))
    return claims
