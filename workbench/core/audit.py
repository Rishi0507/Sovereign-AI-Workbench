"""Hash-chained, append-only JSONL audit log (README section 6.2)."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from workbench.core.clock import Clock, SystemClock, iso
from workbench.core.ids import canonical_json, sha256_text

GENESIS = "0" * 64


class AuditEntry(BaseModel):
    seq: int
    ts: str
    event: dict[str, Any]
    prev_hash: str
    hash: str


def _entry_hash(seq: int, ts: str, event: dict[str, Any], prev_hash: str) -> str:
    return sha256_text(canonical_json({"seq": seq, "ts": ts, "event": event, "prev_hash": prev_hash}))


class AuditLog:
    def __init__(self, path: Path, clock: Clock | None = None,
                 signer: Callable[[dict[str, Any]], str | None] | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()
        self.signer = signer
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._seq, self._last = self._scan_tail()

    def _scan_tail(self) -> tuple[int, str]:
        if not self.path.is_file():
            return 0, GENESIS
        seq, last = 0, GENESIS
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    data = json.loads(line)
                    seq, last = int(data["seq"]), str(data["hash"])
        return seq, last

    def append(self, event: dict[str, Any]) -> AuditEntry:
        event = json.loads(canonical_json(event))
        with self._lock:
            seq = self._seq + 1
            ts = iso(self.clock.now())
            h = _entry_hash(seq, ts, event, self._last)
            entry = AuditEntry(seq=seq, ts=ts, event=event, prev_hash=self._last, hash=h)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(canonical_json(entry.model_dump()) + "\n")
            self._seq, self._last = seq, h
        return entry

    def entries(self) -> list[AuditEntry]:
        if not self.path.is_file():
            return []
        with self.path.open("r", encoding="utf-8") as fh:
            return [AuditEntry.model_validate_json(line) for line in fh if line.strip()]

    def tail(self, n: int = 50) -> list[AuditEntry]:
        return self.entries()[-n:]

    def verify(self) -> bool:
        return bool(self.verify_detail()["ok"])

    def verify_detail(self) -> dict[str, Any]:
        prev = GENESIS
        count = 0
        if not self.path.is_file():
            return {"ok": True, "entries": 0, "latest_hash": GENESIS, "broken_at": None}
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                count += 1
                try:
                    data = json.loads(line)
                    ok = (data["prev_hash"] == prev and
                          _entry_hash(int(data["seq"]), data["ts"], data["event"], data["prev_hash"]) == data["hash"]
                          and int(data["seq"]) == count)
                except (ValueError, KeyError):
                    ok = False
                if not ok:
                    return {"ok": False, "entries": count, "latest_hash": prev, "broken_at": count}
                prev = data["hash"]
        return {"ok": True, "entries": count, "latest_hash": prev, "broken_at": None}

    def latest_hash(self) -> str:
        with self._lock:
            return self._last

    def daily_summary(self, day: date) -> dict[str, Any]:
        """Unsigned daily summary. ``signer`` is the hook for the organization's signing key."""
        prefix = day.isoformat()
        entries = [e for e in self.entries() if e.ts.startswith(prefix)]
        counts: dict[str, int] = {}
        for e in entries:
            kind = str(e.event.get("type", "unknown"))
            counts[kind] = counts.get(kind, 0) + 1
        summary: dict[str, Any] = {
            "date": prefix,
            "entries": len(entries),
            "by_type": dict(sorted(counts.items())),
            "first_hash": entries[0].hash if entries else None,
            "last_hash": entries[-1].hash if entries else None,
        }
        summary["signature"] = self.signer(summary) if self.signer else None
        return summary
