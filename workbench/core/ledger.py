"""Append-only evidence ledger (README section 4.1.3).

Every attachment, user message and tool observation becomes a typed, labelled, anchored, hashed
record. There is no update or delete method.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select

from workbench.core.clock import Clock, SystemClock
from workbench.core.db import Database, ledger_table
from workbench.core.errors import NotFound
from workbench.core.ids import record_id, sha256_json
from workbench.core.labels import Label, high_water
from workbench.core.models import (
    CONTROL_KINDS,
    Anchor,
    Confidence,
    LedgerRecord,
    RecordKind,
    TypedValue,
)

SUMMARY_MAX = 300


def _summary(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= SUMMARY_MAX else text[: SUMMARY_MAX - 1] + "…"


class Ledger:
    def __init__(self, db: Database, clock: Clock | None = None) -> None:
        self.db = db
        self.clock = clock or SystemClock()

    def add(
        self,
        task_id: str,
        kind: RecordKind,
        *,
        summary: str,
        body: str | dict[str, Any] | list[Any],
        label: Label,
        anchor: Anchor | None = None,
        confidence: Confidence | None = None,
        produced_by: str | None = None,
        inputs: Iterable[str] = (),
        fields: dict[str, TypedValue] | None = None,
    ) -> LedgerRecord:
        trust = "control" if kind in CONTROL_KINDS else "data"
        with self.db.tx() as conn:
            seq = int(conn.execute(
                select(func.count()).select_from(ledger_table).where(ledger_table.c.task_id == task_id)
            ).scalar_one()) + 1
            rec = LedgerRecord(
                id=record_id(task_id, seq), task_id=task_id, seq=seq, kind=kind, trust=trust,
                anchor=anchor, label=label, confidence=confidence, produced_by=produced_by,
                inputs=list(inputs), summary=_summary(summary), body=body, fields=fields or {},
                created_at=self.clock.now(),
            )
            rec = rec.model_copy(update={"hash": sha256_json(rec.hash_payload())})
            conn.execute(ledger_table.insert().values(
                id=rec.id, task_id=task_id, seq=seq, kind=kind, hash=rec.hash,
                created_at=rec.created_at.isoformat(), data=rec.model_dump_json(),
            ))
        return rec

    def get(self, rid: str) -> LedgerRecord:
        with self.db.read() as conn:
            row = conn.execute(select(ledger_table.c.data).where(ledger_table.c.id == rid)).first()
        if row is None:
            raise NotFound(f"record {rid} not found")
        return LedgerRecord.model_validate_json(row[0])

    def maybe(self, rid: str) -> LedgerRecord | None:
        try:
            return self.get(rid)
        except NotFound:
            return None

    def for_task(self, task_id: str) -> list[LedgerRecord]:
        with self.db.read() as conn:
            rows = conn.execute(
                select(ledger_table.c.data).where(ledger_table.c.task_id == task_id).order_by(ledger_table.c.seq)
            ).all()
        return [LedgerRecord.model_validate_json(r[0]) for r in rows]

    def high_water(self, task_id: str, floor: Label | None = None) -> Label:
        labels = [r.label for r in self.for_task(task_id)]
        if floor is not None:
            labels.append(floor)
        return high_water(labels)

    def walk_inputs(self, rid: str) -> list[LedgerRecord]:
        """Return the record and every record it was computed from, breadth first."""
        seen: set[str] = set()
        out: list[LedgerRecord] = []
        queue: deque[str] = deque([rid])
        while queue:
            cur = queue.popleft()
            if cur in seen:
                continue
            seen.add(cur)
            rec = self.maybe(cur)
            if rec is None:
                continue
            out.append(rec)
            queue.extend(rec.inputs)
        return out

    def verify(self, task_id: str) -> bool:
        return all(sha256_json(r.hash_payload()) == r.hash for r in self.for_task(task_id))

    def export(self, task_id: str) -> str:
        return "\n".join(json.dumps(r.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
                         for r in self.for_task(task_id)) + "\n"

    def replay_source(self, task_id: str) -> list[LedgerRecord]:
        """Records that, with the fixed seed, reproduce a run: control records and attachments."""
        return [r for r in self.for_task(task_id) if r.trust == "control" or r.kind == "attachment"]
