"""Write a proposed registry diff. ``config/models.yaml`` is never edited by the evaluation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

from workbench.core.db import Database, outcomes_table
from workbench.registry.models import Registry


def outcome_quality(db: Database) -> dict[str, dict[str, float]]:
    """Approval rate per model and route from reviewed task outcomes."""
    with db.read() as conn:
        rows = conn.execute(select(outcomes_table)).all()
    agg: dict[tuple[str, str], list[int]] = {}
    for r in rows:
        if r.approved is None:
            continue
        agg.setdefault((r.model, r.route), []).append(int(r.approved))
    out: dict[str, dict[str, float]] = {}
    for (model, route), vals in agg.items():
        if len(vals) >= 3:
            out.setdefault(model, {})[route] = round(sum(vals) / len(vals), 2)
    return out


def propose(registry: Registry, measured: dict[str, dict[str, float]], path: Path, backend: str,
            outcomes: dict[str, dict[str, float]] | None = None, note: str = "") -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for m in registry.models:
        new_q = dict(m.quality)
        sources: dict[str, str] = {}
        for route, value in (measured.get(m.name) or {}).items():
            new_q[route] = value
            sources[route] = "evaluation set"
        for route, value in ((outcomes or {}).get(m.name) or {}).items():
            base = new_q.get(route)
            new_q[route] = round(value if base is None else (base + value) / 2, 2)
            sources[route] = "evaluation set + reviewed outcomes" if base is not None else "reviewed outcomes"
        diff = {r: {"from": m.quality.get(r), "to": v, "source": sources[r]}
                for r, v in new_q.items() if m.quality.get(r) != v}
        if diff:
            changes[m.name] = {"quality": new_q, "diff": diff}
    doc = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "registry_version": registry.version,
        "backend": backend,
        "note": note or ("Proposed values only. An administrator reviews this diff and copies approved values "
                         "into config/models.yaml."),
        "models": changes,
    }
    path.write_text("# Proposed quality-table update (generated, not active)\n"
                    + yaml.safe_dump(json.loads(json.dumps(doc)), sort_keys=False), encoding="utf-8")
    return doc
