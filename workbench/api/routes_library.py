"""The library: every generated document and every decision, across the tasks a user may see."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from workbench.agent.state import Gate, TaskState
from workbench.api.deps import current_user, get_rt
from workbench.api.routes_files import file_view
from workbench.core.errors import NotFound
from workbench.core.labels import User
from workbench.runtime import Runtime

router = APIRouter(tags=["library"])

GATE_NAMES = {"plan": "Plan", "action": "Step", "deliverable": "Final approval", "template_choice": "Approach"}


def _decision(gate: Gate) -> dict[str, Any]:
    title = GATE_NAMES.get(gate.kind, gate.kind)
    if gate.kind == "action":
        title = str(gate.payload.get("title") or gate.payload.get("tool") or "Step")
    return {"id": gate.id, "kind": gate.kind, "title": title, "status": gate.status, "by": gate.decided_by,
            "at": gate.decided_at, "note": gate.note}


def _files(rt: Runtime, state: TaskState) -> list[dict[str, Any]]:
    out = []
    for d in state.deliverables:
        try:
            rec = rt.files.get(d.final_file_id or d.file_id)
        except NotFound:
            continue
        out.append({**file_view(rec), "final": bool(d.final_file_id), "status": d.status, "kind": d.kind})
    return out


@router.get("/library")
def library(workspace: str | None = None, user: User = Depends(current_user),
            rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    """Tasks the user may see, each with its files, its decisions and what it is waiting for."""
    tasks = []
    for state in rt.tasks.list(workspace, limit=500):
        ws = rt.policy.workspaces.get(state.workspace)
        if ws is None or not rt.policy.can_access_workspace(user, ws):
            continue
        label = state.label or rt.ledger.high_water(state.id, state.label_floor)
        if not rt.policy.can_read(user, ws, label):
            continue
        pending = state.pending_gate()
        tasks.append({
            "id": state.id, "text": state.text, "workspace": state.workspace, "workspace_title": ws.title,
            "user": state.user, "status": state.status, "created_at": state.created_at,
            "updated_at": state.updated_at, "label_display": label.display(),
            "label": label.model_dump(mode="json"), "files": _files(rt, state),
            "decisions": [_decision(g) for g in state.gates if g.status != "pending"],
            "waiting_for": pending.kind if pending else None,
        })
    return {"tasks": tasks}
