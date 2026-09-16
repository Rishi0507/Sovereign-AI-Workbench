"""Routing accuracy and selection regret on the labelled prompt set."""

from __future__ import annotations

from typing import Any

from workbench.core.labels import Label, Level
from workbench.router.router import Router
from workbench.router.types import AttachmentInfo, TaskInput


def evaluate_routing(router: Router, rows: list[dict[str, Any]], workspace: str = "plant-a") -> dict[str, Any]:
    results = []
    correct = 0
    regret_total = 0.0
    for row in rows:
        atts = [AttachmentInfo(path=a["name"], label=Label(level=Level.RESTRICTED), **a) for a in row["attachments"]]
        task = TaskInput(id=row["id"], text=row["text"], workspace=workspace, user="eval", attachments=atts,
                         label=Label(level=Level.RESTRICTED))
        decision = router.route(task)
        ok = decision.chosen == row["expected_model"]
        correct += ok
        route = decision.profile.task_type
        eligible = [c for c in decision.candidates if c.ok and c.quality is not None]
        best = max((c.quality for c in eligible if c.quality is not None), default=0.0)
        chosen_q = next((c.quality for c in eligible if c.model == decision.chosen), None) or 0.0
        regret = max(0.0, best - chosen_q)
        regret_total += regret
        results.append({"id": row["id"], "text": row["text"], "route": route, "expected_route": row["expected_route"],
                        "chosen": decision.chosen, "expected": row["expected_model"], "ok": ok,
                        "regret": round(regret, 3), "threshold": decision.threshold})
    n = len(rows) or 1
    return {"accuracy": correct / n, "mean_regret": round(regret_total / n, 4),
            "route_accuracy": sum(r["route"] == r["expected_route"] for r in results) / n, "rows": results}
