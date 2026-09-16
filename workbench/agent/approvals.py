"""Approval gates: plan, side-effecting action, deliverable, and template choice."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from workbench.core.errors import ApprovalRequired, NotFound
from workbench.planning.plan_schema import Plan


@dataclass
class PlanDecision:
    kind: Literal["approve", "edit", "reject"]
    plan: Plan | None = None
    by: str = "auto"
    note: str | None = None


@dataclass
class ActionDecision:
    approved: bool
    by: str = "auto"
    note: str | None = None


@dataclass
class DeliverableDecision:
    kind: Literal["approve", "reject"]
    by: str = "auto"
    note: str | None = None
    acknowledged: list[str] = field(default_factory=list)


@dataclass
class ChoiceDecision:
    choice: str
    by: str = "auto"


class ApprovalGate(Protocol):
    def approve_plan(self, task_id: str, gate_id: str, plan: Plan, cancelled: Any = None) -> PlanDecision: ...

    def approve_action(self, task_id: str, gate_id: str, action: dict[str, Any],
                       cancelled: Any = None) -> ActionDecision: ...

    def approve_deliverable(self, task_id: str, gate_id: str, draft: dict[str, Any],
                            cancelled: Any = None) -> DeliverableDecision: ...

    def choose_template(self, task_id: str, gate_id: str, options: list[str],
                        cancelled: Any = None) -> ChoiceDecision: ...


class AutoApprove:
    """Approves everything and records ``auto`` as the decider. For tests and the headless demo."""

    def __init__(self, deny_actions: set[str] | None = None, reject_plans: bool = False) -> None:
        self.deny_actions = deny_actions or set()
        self.reject_plans = reject_plans

    def approve_plan(self, task_id: str, gate_id: str, plan: Plan, cancelled: Any = None) -> PlanDecision:
        return PlanDecision("reject" if self.reject_plans else "approve")

    def approve_action(self, task_id: str, gate_id: str, action: dict[str, Any],
                       cancelled: Any = None) -> ActionDecision:
        return ActionDecision(approved=action.get("tool") not in self.deny_actions)

    def approve_deliverable(self, task_id: str, gate_id: str, draft: dict[str, Any],
                            cancelled: Any = None) -> DeliverableDecision:
        return DeliverableDecision("approve", acknowledged=list(draft.get("mismatches", [])))

    def choose_template(self, task_id: str, gate_id: str, options: list[str],
                        cancelled: Any = None) -> ChoiceDecision:
        return ChoiceDecision(options[0])


class Cancelled(Exception):
    pass


class QueueApprove:
    """Blocks the job thread until a user decides through the API."""

    def __init__(self, poll_s: float = 0.2) -> None:
        self._events: dict[str, threading.Event] = {}
        self._decisions: dict[str, Any] = {}
        self._lock = threading.Lock()
        self.poll_s = poll_s

    def _wait(self, gate_id: str, cancelled: Any) -> Any:
        with self._lock:
            event = self._events.setdefault(gate_id, threading.Event())
        while not event.wait(self.poll_s):
            if cancelled is not None and cancelled():
                raise Cancelled(gate_id)
        with self._lock:
            self._events.pop(gate_id, None)
            return self._decisions.pop(gate_id)

    def open(self, gate_id: str) -> None:
        with self._lock:
            self._events.setdefault(gate_id, threading.Event())

    def pending(self) -> list[str]:
        with self._lock:
            return list(self._events)

    def decide(self, gate_id: str, decision: Any) -> None:
        with self._lock:
            event = self._events.get(gate_id)
            if event is None:
                raise NotFound(f"no job is waiting on gate {gate_id}")
            if gate_id in self._decisions:
                raise ApprovalRequired(f"gate {gate_id} is already decided")
            self._decisions[gate_id] = decision
            event.set()

    def approve_plan(self, task_id: str, gate_id: str, plan: Plan, cancelled: Any = None) -> PlanDecision:
        result: PlanDecision = self._wait(gate_id, cancelled)
        return result

    def approve_action(self, task_id: str, gate_id: str, action: dict[str, Any],
                       cancelled: Any = None) -> ActionDecision:
        result: ActionDecision = self._wait(gate_id, cancelled)
        return result

    def approve_deliverable(self, task_id: str, gate_id: str, draft: dict[str, Any],
                            cancelled: Any = None) -> DeliverableDecision:
        result: DeliverableDecision = self._wait(gate_id, cancelled)
        return result

    def choose_template(self, task_id: str, gate_id: str, options: list[str],
                        cancelled: Any = None) -> ChoiceDecision:
        result: ChoiceDecision = self._wait(gate_id, cancelled)
        return result
