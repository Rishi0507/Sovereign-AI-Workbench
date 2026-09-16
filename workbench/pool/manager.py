"""Tidal pool manager: GPU memory alternates between resident models and a swap-slot model.

Swap jobs wait in their own queue. A tide turns when the oldest swap job has waited longer than
``max_wait_s`` or ``max_queue`` jobs are waiting, but never sooner than ``min_resident_s`` after
the residents last woke. Running resident steps finish before the residents are put to sleep.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel

from workbench.core.audit import AuditLog
from workbench.core.clock import Clock, SystemClock
from workbench.core.errors import WorkbenchError
from workbench.pool.control import VLLMControl
from workbench.registry.models import Registry

State = Literal["awake", "asleep", "cold"]


class TidePolicy(BaseModel):
    max_wait_s: float = 90
    max_queue: int = 3
    min_resident_s: float = 120


class PoolPolicy(BaseModel):
    swap_slot: str | None = None
    tide: TidePolicy = TidePolicy()

    @classmethod
    def load(cls, path: Path) -> PoolPolicy:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
        return cls.model_validate(data or {})


class Cancelled(WorkbenchError):
    pass


@dataclass
class SwapTicket:
    job_id: str
    model: str
    submitted: float
    released: threading.Event = field(default_factory=threading.Event)
    tide: int = 0


class PoolManager:
    def __init__(
        self,
        registry: Registry,
        policy: PoolPolicy,
        control: VLLMControl,
        clock: Clock | None = None,
        time_scale: float = 1.0,
        all_resident: bool = False,
        audit: AuditLog | None = None,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.control = control
        self.clock = clock or SystemClock()
        self.scale = time_scale
        self.all_resident = all_resident
        self.audit = audit
        self.state: dict[str, State] = {}
        self.tide: Literal["resident", "switching", "swap"] = "resident"
        self.tide_count = 0
        self.switch_durations: list[float] = []
        self.events: deque[dict[str, Any]] = deque(maxlen=200)
        self._cond = threading.Condition()
        self._queue: deque[SwapTicket] = deque()
        self._running_resident = 0
        self._running_swap = 0
        self._released_this_tide = 0
        self._last_resident_start = self.clock.monotonic()
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._ticker: threading.Thread | None = None
        self._stop = threading.Event()

    # -- configuration -------------------------------------------------------------------------

    def pool_of(self, model: str) -> str:
        entry = self.registry.maybe(model)
        if entry is None:
            return "cold"
        return entry.pool_for(self.all_resident)

    def is_swap(self, model: str) -> bool:
        return self.pool_of(model) == "swap"

    def residents(self) -> list[str]:
        return [m.name for m in self.registry.active() if self.pool_of(m.name) == "resident"]

    def swaps(self) -> list[str]:
        return [m.name for m in self.registry.active() if self.pool_of(m.name) == "swap"]

    def _emit(self, kind: str, **data: Any) -> None:
        event = {"event": kind, "t": round(self.clock.monotonic(), 3), **data}
        self.events.append(event)
        if self.audit is not None:
            self.audit.append({"type": f"pool.{kind}", **data})
        for fn in self._listeners:
            fn(event)

    def subscribe(self, fn: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(fn)

    # -- lifecycle -------------------------------------------------------------------------------

    def startup(self) -> list[str]:
        """Swap-slot servers start and sleep first, then residents start."""
        order: list[str] = []
        with self._cond:
            for m in self.registry.models:
                self.state[m.name] = "cold"
            for name in self.swaps():
                self.control.wake(name)
                self.control.sleep(name, level=1)
                self.state[name] = "asleep"
                order += [f"start:{name}", f"sleep:{name}"]
            for name in self.residents():
                self.control.wake(name)
                self.state[name] = "awake"
                order.append(f"start:{name}")
            self._last_resident_start = self.clock.monotonic()
        self._emit("startup", order=order)
        return order

    def start_ticker(self, interval_s: float = 0.2) -> None:
        if self._ticker is not None:
            return

        def loop() -> None:
            while not self._stop.wait(interval_s):
                try:
                    self.maybe_turn_tide()
                except Exception as exc:  # keep ticking; the event log shows the failure
                    self._emit("error", message=str(exc))

        self._ticker = threading.Thread(target=loop, name="pool-ticker", daemon=True)
        self._ticker.start()

    def stop(self) -> None:
        self._stop.set()

    # -- estimates -------------------------------------------------------------------------------

    def expected_wait(self, model: str) -> float:
        """Unscaled estimate in seconds of how long a new job for ``model`` waits before it runs."""
        pool = self.pool_of(model)
        if pool != "swap":
            return 0.0
        with self._cond:
            if self.tide == "swap" and self.state.get(model) == "awake":
                return 0.0
            t = self.policy.tide
            if len(self._queue) + 1 >= t.max_queue:
                trigger = 0.0
            elif self._queue:
                age = (self.clock.monotonic() - self._queue[0].submitted) / (self.scale or 1.0)
                trigger = max(0.0, t.max_wait_s - age)
            else:
                trigger = t.max_wait_s
        entry = self.registry.get(model)
        return trigger + (entry.latency.wake_s or 0.0) + self._resident_sleep_estimate()

    def _resident_sleep_estimate(self) -> float:
        total = 0.0
        for name in self.residents():
            total += (self.registry.get(name).latency.wake_s or 0.0) * 0.25
        return total

    # -- resident side -------------------------------------------------------------------------------

    @contextmanager
    def resident_step(self, model: str, cancelled: Callable[[], bool] | None = None) -> Iterator[None]:
        if self.pool_of(model) != "resident":
            yield
            return
        with self._cond:
            while not (self.tide == "resident" and self.state.get(model) == "awake"):
                if cancelled and cancelled():
                    raise Cancelled("job cancelled while waiting for residents")
                self._cond.wait(0.1)
            self._running_resident += 1
        try:
            yield
        finally:
            with self._cond:
                self._running_resident -= 1
                self._cond.notify_all()

    # -- swap side -------------------------------------------------------------------------------

    def submit_swap_job(self, job_id: str, model: str) -> SwapTicket:
        ticket = SwapTicket(job_id=job_id, model=model, submitted=self.clock.monotonic())
        with self._cond:
            if (self.tide == "swap" and self.state.get(model) == "awake"
                    and self._released_this_tide < self.policy.tide.max_queue):
                self._release(ticket)
            else:
                self._queue.append(ticket)
            self._cond.notify_all()
        self._emit("swap_submitted", job=job_id, model=model, queue=len(self._queue))
        return ticket

    def _release(self, ticket: SwapTicket) -> None:
        ticket.tide = self.tide_count
        self._running_swap += 1
        self._released_this_tide += 1
        ticket.released.set()

    def wait_for_release(self, ticket: SwapTicket, cancelled: Callable[[], bool] | None = None,
                         poll_s: float = 0.05) -> None:
        while not ticket.released.is_set():
            if cancelled and cancelled():
                with self._cond:
                    if ticket in self._queue:
                        self._queue.remove(ticket)
                raise Cancelled("job cancelled while waiting for tide")
            self.maybe_turn_tide()
            ticket.released.wait(poll_s)

    def finish_swap_job(self, ticket: SwapTicket) -> None:
        with self._cond:
            self._running_swap = max(0, self._running_swap - 1)
            self._cond.notify_all()
        self._emit("swap_finished", job=ticket.job_id)
        self.maybe_turn_tide()

    def queue_snapshot(self) -> list[dict[str, Any]]:
        now = self.clock.monotonic()
        with self._cond:
            return [{"job": t.job_id, "model": t.model, "waited_s": round(now - t.submitted, 2)} for t in self._queue]

    # -- tides -------------------------------------------------------------------------------

    def maybe_turn_tide(self, now: float | None = None) -> bool:
        now = self.clock.monotonic() if now is None else now
        t = self.policy.tide
        with self._cond:
            if self.tide == "resident" and self._queue:
                if now - self._last_resident_start < t.min_resident_s * self.scale:
                    return False
                oldest = now - self._queue[0].submitted
                if oldest >= t.max_wait_s * self.scale or len(self._queue) >= t.max_queue:
                    self.tide = "switching"
                    turn = "to_swap"
                else:
                    return False
            elif self.tide == "swap" and self._running_swap == 0:
                if self._queue and self._released_this_tide < t.max_queue:
                    while self._queue and self._released_this_tide < t.max_queue:
                        self._release(self._queue.popleft())
                    return False
                self.tide = "switching"
                turn = "to_resident"
            else:
                return False
        if turn == "to_swap":
            self._turn_to_swap()
        else:
            self._turn_to_resident()
        return True

    def _turn_to_swap(self) -> None:
        started = self.clock.monotonic()
        with self._cond:
            while self._running_resident > 0:
                self._cond.wait(0.1)
            swap_model = self._queue[0].model
        for name in self.residents():
            self.control.sleep(name, level=1)
            self.state[name] = "asleep"
        self.control.wake(swap_model)
        with self._cond:
            self.state[swap_model] = "awake"
            self.tide = "swap"
            self.tide_count += 1
            self._released_this_tide = 0
            batch = 0
            while self._queue and batch < self.policy.tide.max_queue:
                ticket = self._queue.popleft()
                self._release(ticket)
                batch += 1
            self._cond.notify_all()
        duration = self.clock.monotonic() - started
        self.switch_durations.append(duration)
        self._emit("tide", direction="to_swap", model=swap_model, released=batch, tide=self.tide_count,
                   duration_s=round(duration, 3))

    def _turn_to_resident(self) -> None:
        started = self.clock.monotonic()
        for name in self.swaps():
            if self.state.get(name) == "awake":
                self.control.sleep(name, level=1)
                self.state[name] = "asleep"
        for name in self.residents():
            self.control.wake(name)
            self.state[name] = "awake"
        with self._cond:
            self.tide = "resident"
            self._last_resident_start = self.clock.monotonic()
            self._cond.notify_all()
        duration = self.clock.monotonic() - started
        self.switch_durations.append(duration)
        self._emit("tide", direction="to_resident", tide=self.tide_count, duration_s=round(duration, 3))

    def snapshot(self) -> dict[str, Any]:
        with self._cond:
            return {
                "tide": self.tide,
                "tide_count": self.tide_count,
                "state": dict(self.state),
                "swap_queue": len(self._queue),
                "running_resident": self._running_resident,
                "running_swap": self._running_swap,
                "switch_durations_s": [round(d, 3) for d in self.switch_durations[-10:]],
                "policy": self.policy.model_dump(),
                "time_scale": self.scale,
                "events": list(self.events)[-20:],
            }
