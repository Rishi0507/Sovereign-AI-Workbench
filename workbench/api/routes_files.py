"""Workspace files, sharing and downgrades."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select

from workbench.api.deps import current_user, get_rt, require_role, workspace_for
from workbench.core.db import downgrades_table
from workbench.core.errors import NotFound, PolicyError
from workbench.core.ids import new_id, sha256_file
from workbench.core.labels import DowngradeRequest, Label, User
from workbench.documents import preview as file_preview
from workbench.documents.readers import CompositeReader
from workbench.runtime import Runtime
from workbench.workspace import FileRecord

router = APIRouter(tags=["files"])
MAX_UPLOAD = 50 * 1024 * 1024


def file_view(rec: FileRecord) -> dict[str, Any]:
    return {"id": rec.id, "workspace": rec.workspace, "area": rec.area, "path": rec.relpath, "name": rec.name,
            "label": rec.label.model_dump(mode="json"), "label_display": rec.label.display(),
            "marking": rec.label.marking(), "size": rec.size, "sha256": rec.sha256, "task_id": rec.task_id,
            "created_at": rec.created_at, "created_by": rec.created_by}


def _file(rt: Runtime, user: User, fid: str) -> FileRecord:
    try:
        rec = rt.files.get(fid)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    ws = workspace_for(rt, user, rec.workspace)
    if not rt.policy.can_read(user, ws, rec.label):
        raise HTTPException(403, f"{rec.name} is {rec.label.display()}, above your clearance")
    return rec


@router.get("/workspaces")
def list_workspaces(user: User = Depends(current_user), rt: Runtime = Depends(get_rt)) -> list[dict[str, Any]]:
    out = []
    for ws in rt.policy.workspaces.values():
        if rt.policy.can_access_workspace(user, ws):
            out.append({"id": ws.id, "title": ws.title, "ceiling": ws.ceiling.model_dump(mode="json"),
                        "ceiling_display": ws.ceiling.display(),
                        "retrieval_ceiling": rt.policy.retrieval_ceiling(user, ws).display()})
    return out


@router.get("/workspaces/{ws}/files")
def list_files(ws: str, area: str | None = None, user: User = Depends(current_user),
               rt: Runtime = Depends(get_rt)) -> list[dict[str, Any]]:
    workspace = workspace_for(rt, user, ws)
    rt.files.sync(ws)
    return [file_view(f) for f in rt.files.list(ws, area) if rt.policy.can_read(user, workspace, f.label)]


@router.post("/workspaces/{ws}/files", status_code=201)
async def upload(ws: str, file: UploadFile = File(...), level: str | None = Form(None),
                 compartments: str = Form(""), user: User = Depends(current_user),
                 rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    workspace_for(rt, user, ws)
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "file is larger than 50 MB")
    try:
        chosen = Label.parse({"level": level, "compartments": [c for c in compartments.split(",") if c.strip()]}) \
            if level else None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if chosen is not None and not user.clearance.dominates(chosen):
        raise HTTPException(403, "you cannot upload above your own clearance")
    name = file.filename or "upload.bin"
    detected = None
    tmp = rt.settings.path(rt.settings.data_dir) / "uploads" / new_id("U")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp.with_name(tmp.name + "_" + name.replace("/", "_").replace("\\", "_"))
    tmp_path.write_bytes(data)
    try:
        pages = CompositeReader().read(tmp_path)
        detected = rt.policy.policy.detect("\n".join(p.text for p in pages))
    except (ValueError, OSError, RuntimeError):
        detected = None
    finally:
        tmp_path.unlink(missing_ok=True)
    try:
        rec = rt.files.upload(ws, name, data, chosen, user.id, detected)
    except PolicyError as exc:
        raise HTTPException(409, str(exc)) from exc
    rt.audit.append({"type": "file.upload", "workspace": ws, "file": rec.relpath, "sha256": rec.sha256,
                     "label": rec.label.display(), "chosen": chosen.display() if chosen else None,
                     "detected": detected.display() if detected else None, "by": user.id})
    return file_view(rec)


@router.get("/files/{fid}")
def file_meta(fid: str, user: User = Depends(current_user), rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    return file_view(_file(rt, user, fid))


@router.get("/files/{fid}/preview")
def preview(fid: str, user: User = Depends(current_user), rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    """The readable content of a file, so it can be checked without downloading it."""
    rec = _file(rt, user, fid)
    rt.audit.append({"type": "file.preview", "file": rec.relpath, "workspace": rec.workspace, "by": user.id})
    return {**file_preview.build(rt.files.open_path(fid), rt.reader), "name": rec.name}


@router.get("/files/{fid}/download")
def download(fid: str, user: User = Depends(current_user), rt: Runtime = Depends(get_rt)) -> FileResponse:
    rec = _file(rt, user, fid)
    rt.audit.append({"type": "file.download", "file": rec.relpath, "workspace": rec.workspace, "by": user.id})
    return FileResponse(rt.files.open_path(fid), filename=rec.name)


class ShareBody(BaseModel):
    workspace: str


@router.post("/files/{fid}/share")
def share(fid: str, body: ShareBody, user: User = Depends(current_user),
          rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    rec = _file(rt, user, fid)
    try:
        shared = rt.files.share(fid, body.workspace, user.id)
    except (PolicyError, NotFound) as exc:
        rt.audit.append({"type": "file.share_refused", "file": rec.relpath, "from": rec.workspace,
                         "to": body.workspace, "label": rec.label.display(), "by": user.id, "reason": str(exc)})
        raise HTTPException(403, str(exc)) from exc
    rt.audit.append({"type": "file.shared", "file": rec.relpath, "from": rec.workspace, "to": body.workspace,
                     "label": rec.label.display(), "by": user.id})
    return file_view(shared)


class DowngradeBody(BaseModel):
    level: str
    compartments: list[str] = []
    reason: str


def _save_downgrade(rt: Runtime, req: DowngradeRequest) -> None:
    with rt.db.tx() as conn:
        exists = conn.execute(select(downgrades_table.c.id).where(downgrades_table.c.id == req.id)).first()
        values = {"artifact": req.artifact, "status": req.status, "data": req.model_dump_json()}
        if exists:
            conn.execute(downgrades_table.update().where(downgrades_table.c.id == req.id).values(**values))
        else:
            conn.execute(downgrades_table.insert().values(id=req.id, **values))


def _load_downgrade(rt: Runtime, rid: str) -> DowngradeRequest:
    with rt.db.read() as conn:
        row = conn.execute(select(downgrades_table.c.data).where(downgrades_table.c.id == rid)).first()
    if row is None:
        raise HTTPException(404, f"downgrade {rid} not found")
    return DowngradeRequest.model_validate_json(row[0])


@router.post("/files/{fid}/downgrade", status_code=201)
def request_downgrade(fid: str, body: DowngradeBody, user: User = Depends(current_user),
                      rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    rec = _file(rt, user, fid)
    try:
        after = Label.parse({"level": body.level, "compartments": body.compartments})
        req = rt.policy.request_downgrade(new_id("D"), fid, rec.label, after, user, body.reason)
    except (PolicyError, ValueError) as exc:
        raise HTTPException(403, str(exc)) from exc
    _save_downgrade(rt, req)
    rt.audit.append({"type": "downgrade.requested", "id": req.id, "file": rec.relpath, "before": rec.label.display(),
                     "after": after.display(), "reason": req.reason, "by": user.id})
    return json.loads(req.model_dump_json())


@router.get("/downgrades")
def list_downgrades(user: User = Depends(current_user), rt: Runtime = Depends(get_rt)) -> list[dict[str, Any]]:
    with rt.db.read() as conn:
        rows = conn.execute(select(downgrades_table.c.data)).all()
    out = []
    for r in rows:
        req = DowngradeRequest.model_validate_json(r[0])
        try:
            rec = rt.files.get(req.artifact)
        except NotFound:
            continue
        if rt.policy.can_access_workspace(user, rt.policy.workspace(rec.workspace)):
            out.append({**json.loads(req.model_dump_json()), "file": file_view(rec)})
    return out


class DowngradeDecision(BaseModel):
    approve: bool
    note: str | None = None


@router.post("/downgrades/{rid}/decision")
def decide_downgrade(rid: str, body: DowngradeDecision, user: User = Depends(current_user),
                     rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    require_role(user, *rt.policy.policy.downgrade_roles)
    req = _load_downgrade(rt, rid)
    rec = _file(rt, user, req.artifact)
    try:
        decided = rt.policy.decide_downgrade(req, user, body.approve, rt.clock.now(), body.note)
    except PolicyError as exc:
        raise HTTPException(403, str(exc)) from exc
    before_sha = rec.sha256
    after_sha = before_sha
    archive_path = None
    if decided.status == "approved":
        path = rt.files.open_path(rec.id)
        archive = rt.settings.path(rt.settings.data_dir) / "archive" / rid / path.name
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(path.read_bytes())
        if sha256_file(archive) != before_sha:
            raise HTTPException(500, "archived copy does not match the registered hash")
        after_sha = rt.files.relabel(rec.id, decided.after).sha256
        archive_path = str(archive)
    _save_downgrade(rt, decided)
    rt.audit.append({"type": f"downgrade.{decided.status}", "id": rid, "file": rec.relpath,
                     "before": decided.before.display(), "after": decided.after.display(),
                     "reason": decided.reason, "requester": decided.requester, "approver": user.id,
                     "before_sha256": before_sha, "after_sha256": after_sha, "before_copy": archive_path,
                     "note": body.note})
    return json.loads(decided.model_dump_json())
