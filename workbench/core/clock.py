"""Injectable clocks so that runs are reproducible in tests."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC).replace(microsecond=0)

    def monotonic(self) -> float:
        import time

        return time.monotonic()


class FixedClock:
    """A clock that only moves when told to. ``tick`` seconds are added on every ``now`` call."""

    def __init__(self, start: datetime | None = None, tick: float = 0.0) -> None:
        self._now = start or datetime(2026, 9, 15, 10, 0, 0, tzinfo=UTC)
        self._tick = tick
        self._mono = 0.0
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            value = self._now
            if self._tick:
                self._now = self._now + timedelta(seconds=self._tick)
            return value

    def monotonic(self) -> float:
        with self._lock:
            return self._mono

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now = self._now + timedelta(seconds=seconds)
            self._mono += seconds


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
