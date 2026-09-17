"""Request dependencies: runtime access and the development-only user mapping.

DEV ONLY: the ``X-User`` header (or the ``wb_user`` cookie set by the UI) names a user from
``config/users.yaml``. The directory-server integration replaces this in production.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request

from workbench.agent.state import TaskState
from workbench.core.errors import NotFound, PolicyError
from workbench.core.labels import User
from workbench.runtime import Runtime


def get_rt(request: Request) -> Runtime:
    rt: Runtime = request.app.state.rt
    return rt


def current_user(request: Request, rt: Runtime = Depends(get_rt)) -> User:
    uid = request.headers.get("X-User") or request.cookies.get("wb_user")
    if not uid:
        raise HTTPException(401, "missing X-User header (development authentication)")
    try:
        return rt.policy.user(uid)
    except PolicyError as exc:
        raise HTTPException(401, str(exc)) from exc


def workspace_for(rt: Runtime, user: User, ws: str) -> Any:
    try:
        workspace = rt.policy.workspace(ws)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    if not rt.policy.can_access_workspace(user, workspace):
        raise HTTPException(403, f"no access to workspace {ws}")
    return workspace


def task_for(rt: Runtime, user: User, task_id: str) -> TaskState:
    try:
        state = rt.tasks.get(task_id)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    workspace_for(rt, user, state.workspace)
    label = state.label or rt.ledger.high_water(task_id)
    if not rt.policy.can_read(user, rt.policy.workspace(state.workspace), label):
        raise HTTPException(403, f"task {task_id} is {label.display()}, above your clearance")
    return state


def require_role(user: User, *roles: str) -> None:
    if not set(roles) & set(user.roles):
        raise HTTPException(403, f"requires one of the roles: {', '.join(roles)}")
