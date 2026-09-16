"""Routing explanation: one log line plus a JSON object for the UI."""

from __future__ import annotations

from typing import Any

from workbench.core.clock import iso
from workbench.router.types import RouteDecision


def _tok(n: int) -> str:
    return f"{n / 1000:.0f}k" if n >= 1000 else str(n)


def log_line(d: RouteDecision) -> str:
    p = d.profile
    mod = "[" + ",".join(p.modalities) + "]" + (" (decoupled)" if p.decoupled else "")
    head = (f"{iso(d.ts)} task={d.task_id} profile={{type={p.task_type} mod={mod} "
            f"tok≈{_tok(p.est_input_tokens)} lang=[{','.join(p.languages)}] cx={p.complexity} "
            f"conf={p.confidence:.2f}}}")
    parts = []
    for c in d.candidates:
        if not c.ok:
            parts.append(f"{c.model} ✗ {c.reason}")
            continue
        q = "n/a" if c.quality is None else f"{c.quality:.2f}"
        step = (c.cost_s or 0.0) - c.wait_s
        cost = f"{step:.1f}s" + (f"+tide {c.wait_s:.0f}s" if c.wait_s else "")
        mark = "✓" if c.meets_threshold else ("✗ below threshold" if c.quality is not None else "✗ no quality")
        parts.append(f"{c.model} q={q} cost={cost} {mark}")
    tail = f"threshold={d.threshold:.2f} → {d.chosen}"
    if d.below_threshold:
        tail += " (below_threshold)"
    if d.used_fallback:
        tail += " (fallback)"
    if d.queued_for_tide:
        tail += " (queued for tide)"
    return f"{head}\n  candidates: {' | '.join(parts)}\n  {tail}"


def as_json(d: RouteDecision) -> dict[str, Any]:
    data = d.model_dump(mode="json")
    data["log_line"] = d.log_line or log_line(d)
    return data
