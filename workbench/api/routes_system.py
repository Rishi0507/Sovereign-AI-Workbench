"""Health, users, models, jobs, egress, audit, templates and knowledge-base reports."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from workbench import __version__
from workbench.api.deps import current_user, get_rt, require_role, task_for
from workbench.core.errors import NotFound, PolicyError, ServiceUnavailable
from workbench.core.labels import User
from workbench.registry import serve_cmd
from workbench.runtime import Runtime
from workbench.security import egress_guard

router = APIRouter(tags=["system"])


@router.get("/health")
def health(rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    h = rt.health()
    sandbox_ok = h["sandboxd"].get("status") == "ok"
    egress_ok = h["egressd"].get("status") == "ok"
    return {"status": "ok" if sandbox_ok and egress_ok else "degraded", "version": __version__,
            "egress_guard": egress_guard.installed(), **h}


@router.get("/me")
def me(user: User = Depends(current_user), rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    return {"id": user.id, "name": user.name, "roles": user.roles, "groups": user.groups,
            "clearance": user.clearance.display(), "dev_auth": True}


@router.get("/users")
def users(rt: Runtime = Depends(get_rt)) -> list[dict[str, Any]]:
    """DEV ONLY: the user switcher lists the stand-in directory."""
    return [{"id": u.id, "name": u.name, "roles": u.roles, "clearance": u.clearance.display()}
            for u in rt.policy.users.values()]


def _allowance(rt: Runtime) -> list[dict[str, Any]]:
    """What each hosted model reports about the calls and tokens it has left."""
    usage = getattr(rt.backend, "usage", None)
    return list(usage.snapshot()) if usage is not None else []


@router.get("/models")
def models(user: User = Depends(current_user), rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    ps = rt.settings.profile_settings()
    remote = getattr(rt.backend, "config", None)
    hosted: dict[str, str] = dict(getattr(remote, "models", {}) or {})
    entries = []
    for m in rt.registry.models:
        try:
            argv = serve_cmd.build(m, rt.settings.profile, ps)
        except Exception:
            argv = []
        entries.append({**m.model_dump(mode="json"), "pool_profile": m.pool_for(ps.all_resident),
                        "state": rt.pool.state.get(m.name, "cold"), "serve_argv": argv,
                        "hosted_as": hosted.get(m.name), "measured_speedup": m.speculative_speedup,
                        "can_call_tools": m.can_call_tools})
    served: dict[str, int] = {}
    for t in rt.tasks.list(limit=500):
        if t.model:
            served[t.model] = served.get(t.model, 0) + 1
    proposed = rt.settings.config_dir / "models.proposed.yaml"
    return {"profile": rt.settings.profile, "backend": rt.settings.llm_backend, "registry_version": rt.registry.version,
            "models": entries, "routing": rt.registry.routing.model_dump(), "pool": rt.pool.snapshot(),
            "cache": rt.cache.snapshot(), "tasks_served": served, "allowance": _allowance(rt),
            "proposed_diff": proposed.read_text(encoding="utf-8") if proposed.is_file() else None,
            "speculative_note": "n/a (no GPU): acceptance length and tokens/s appear once vLLM reports them"}


class ShadowBody(BaseModel):
    confirm: bool = False


@router.post("/models/{name}/shadow-eval")
def shadow_eval(name: str, user: User = Depends(current_user), rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    require_role(user, "admin")
    from workbench.registry.shadow import run_shadow

    try:
        return run_shadow(rt, name, user.id)
    except (NotFound, PolicyError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/models/{name}/promote")
def promote(name: str, body: ShadowBody, user: User = Depends(current_user), rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    require_role(user, "admin")
    from workbench.registry.shadow import promote as do_promote

    try:
        return do_promote(rt, name, body.confirm, user.id)
    except (NotFound, PolicyError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/jobs")
def jobs(user: User = Depends(current_user), rt: Runtime = Depends(get_rt)) -> list[dict[str, Any]]:
    out = []
    for j in rt.jobs.list():
        try:
            task = rt.tasks.get(j.task_id)
        except NotFound:
            continue
        ws = rt.policy.workspaces.get(task.workspace)
        if ws and rt.policy.can_access_workspace(user, ws):
            out.append({**j.model_dump(), "task_text": task.text[:120], "workspace": task.workspace,
                        "status": task.status})
    return out


@router.delete("/jobs/{job_id}")
def cancel(job_id: str, user: User = Depends(current_user), rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    try:
        return rt.jobs.cancel(job_id, user.id).model_dump()
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except PolicyError as exc:
        raise HTTPException(403, str(exc)) from exc


@router.get("/egress")
def egress(rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    try:
        snap = rt.egress.snapshot()
        return {"status": "ok", **snap, "guard_blocked_in_process": egress_guard.blocked_count()}
    except ServiceUnavailable as exc:
        return {"status": "offline", "message": "monitor offline", "error": str(exc)}


@router.post("/egress/test")
def egress_test(user: User = Depends(current_user), rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    try:
        result = rt.egress.run_test()
    except ServiceUnavailable as exc:
        raise HTTPException(503, f"monitor offline: {exc}") from exc
    rt.audit.append({"type": "egress.test", "by": user.id, "pass": result.get("pass"),
                     "checks": [{"name": c["name"], "pass": c["pass"]} for c in result.get("checks", [])]})
    return result


@router.get("/egress/events")
def egress_events(since: int = 0, limit: int = 100, rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    try:
        return {"status": "ok", "events": rt.egress.events(since, min(limit, 1000))}
    except ServiceUnavailable as exc:
        return {"status": "offline", "events": [], "error": str(exc)}


@router.get("/audit/verify")
def audit_verify(rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    return rt.audit.verify_detail()


@router.get("/audit/tail")
def audit_tail(n: int = 40, user: User = Depends(current_user), rt: Runtime = Depends(get_rt)) -> list[dict[str, Any]]:
    require_role(user, "security_officer", "admin", "document_owner")
    return [e.model_dump() for e in rt.audit.tail(min(n, 500))]


class TemplateDraftBody(BaseModel):
    task_id: str
    name: str | None = None


@router.get("/templates")
def templates(user: User = Depends(current_user), rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    rt.templates.reload()
    drafts_dir = rt.template_drafts_dir
    drafts = sorted(p.name for p in drafts_dir.glob("*.v*.yaml")) if drafts_dir.is_dir() else []
    return {"templates": [{"name": t.name, "version": t.version, "description": t.description,
                           "match": t.match.model_dump(), "steps": [s.id for s in t.steps]}
                          for t in rt.templates.templates.values()], "drafts": drafts}


@router.post("/templates/drafts", status_code=201)
def save_template(body: TemplateDraftBody, user: User = Depends(current_user), rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    from workbench.planning.promotion import save_as_template

    state = task_for(rt, user, body.task_id)
    if state.status != "completed" or state.plan is None:
        raise HTTPException(409, "only an approved, completed run can become a template")
    route = str((state.route or {}).get("profile", {}).get("task_type", "general"))
    try:
        path = save_as_template(state.plan, state.text, route, state.attachments,
                                rt.template_drafts_dir, user.id, body.name)
    except PolicyError as exc:
        raise HTTPException(409, str(exc)) from exc
    rt.audit.append({"type": "template.draft", "task": state.id, "draft": path.name, "by": user.id})
    return {"draft": path.name}


@router.post("/templates/drafts/{name}/approve")
def approve_template(name: str, user: User = Depends(current_user), rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    from workbench.planning.promotion import approve_template as approve

    require_role(user, "admin", "document_owner")
    drafts = rt.template_drafts_dir
    path = drafts / Path(name).name
    author = None
    for entry in reversed(rt.audit.tail(500)):
        if entry.event.get("type") == "template.draft" and entry.event.get("draft") == path.name:
            author = entry.event.get("by")
            break
    try:
        target = approve(path, rt.approved_templates_dir, user.id, author)
    except (NotFound, PolicyError) as exc:
        raise HTTPException(409, str(exc)) from exc
    rt.templates.reload()
    rt.audit.append({"type": "template.approved", "template": target.name, "reviewer": user.id, "author": author})
    return {"template": target.name}


@router.get("/kb/impact/{doc_number}/{revision}")
def kb_impact(doc_number: str, revision: str, user: User = Depends(current_user),
              rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    require_role(user, "document_owner", "admin", "security_officer")
    from workbench.kb.impact import impact_report

    if rt.kb.revisions.info(doc_number, revision) is None:
        raise HTTPException(404, f"{doc_number} Rev {revision} is not in the knowledge base")
    return impact_report(rt.kb, doc_number, revision)


@router.get("/kb/stats")
def kb_stats(user: User = Depends(current_user), rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    return rt.kb.stats()
