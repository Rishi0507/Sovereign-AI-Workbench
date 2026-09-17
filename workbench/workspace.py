"""Workspace file store: jailed paths, labels on every file, sharing and downgrades.

Each workspace is a folder with ``inputs/``, ``drafts/`` and ``final/``. Every file carries a
label in a ``<file>.label.json`` sidecar and in the database. The agent only sees its own
workspace; paths are resolved with symlink and escape checks.
"""

from __future__ import annotations

import os
import shutil
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import select

from workbench.core.clock import Clock, SystemClock
from workbench.core.db import Database, files_table
from workbench.core.errors import JailError, NotFound, PolicyError
from workbench.core.ids import new_id, sha256_file
from workbench.core.labels import Label, PolicyEngine, read_label_sidecar, write_label_sidecar

Area = Literal["inputs", "drafts", "final"]
AREAS: tuple[Area, ...] = ("inputs", "drafts", "final")
HIDDEN_SUFFIXES = (".label.json",)
BAD_CHARS = set("\\:*?\"<>|\x00")


class FileRecord(BaseModel):
    id: str
    workspace: str
    area: str
    relpath: str
    label: Label
    sha256: str
    size: int
    task_id: str | None = None
    created_by: str | None = None
    created_at: str

    @property
    def name(self) -> str:
        return PurePosixPath(self.relpath).name


def clean_relpath(relpath: str) -> PurePosixPath:
    """Reject absolute paths, parent references, odd Unicode and reserved characters."""
    if not relpath or relpath != relpath.strip():
        raise JailError("empty or padded path")
    normal = unicodedata.normalize("NFKC", relpath)
    if normal != relpath:
        raise JailError(f"path {relpath!r} contains characters that change under Unicode normalisation")
    if any(unicodedata.category(ch) in {"Cf", "Cc", "Co", "Cs"} for ch in relpath):
        raise JailError("path contains control or format characters")
    if any(ch in BAD_CHARS for ch in relpath):
        raise JailError("path contains reserved characters")
    p = PurePosixPath(relpath)
    if p.is_absolute() or relpath.startswith("/") or any(part in {"..", ".", ""} for part in relpath.split("/")):
        raise JailError(f"path {relpath!r} escapes the workspace")
    return p


class FileStore:
    def __init__(self, root: Path, db: Database, policy: PolicyEngine, clock: Clock | None = None) -> None:
        self.root = root
        self.db = db
        self.policy = policy
        self.clock = clock or SystemClock()
        self.root.mkdir(parents=True, exist_ok=True)

    # -- paths --------------------------------------------------------------------------------

    def ws_root(self, workspace: str) -> Path:
        self.policy.workspace(workspace)
        path = self.root / workspace
        for area in AREAS:
            (path / area).mkdir(parents=True, exist_ok=True)
        return path

    def resolve(self, workspace: str, relpath: str, must_exist: bool = True) -> Path:
        rel = clean_relpath(relpath)
        base = self.ws_root(workspace).resolve()
        target = base.joinpath(*rel.parts)
        cur = base
        for part in rel.parts:
            cur = cur / part
            if cur.is_symlink():
                raise JailError(f"symlink in path: {relpath}")
        real = target.resolve()
        try:
            real.relative_to(base)
        except ValueError as exc:
            raise JailError(f"path {relpath!r} resolves outside the workspace") from exc
        if must_exist and not real.exists():
            raise NotFound(f"{relpath} not found in workspace {workspace}")
        return real

    def relpath(self, workspace: str, path: Path) -> str:
        return path.resolve().relative_to(self.ws_root(workspace).resolve()).as_posix()

    # -- registry -----------------------------------------------------------------------------

    def _row_to_record(self, row: Any) -> FileRecord:
        return FileRecord(id=row.id, workspace=row.workspace, area=row.area, relpath=row.relpath,
                          label=Label.model_validate_json(row.label), sha256=row.sha256, size=row.size,
                          task_id=row.task_id, created_by=row.created_by, created_at=row.created_at)

    def get(self, file_id: str) -> FileRecord:
        with self.db.read() as conn:
            row = conn.execute(select(files_table).where(files_table.c.id == file_id)).first()
        if row is None:
            raise NotFound(f"file {file_id} not found")
        return self._row_to_record(row)

    def find(self, workspace: str, relpath: str) -> FileRecord | None:
        with self.db.read() as conn:
            row = conn.execute(select(files_table).where(files_table.c.workspace == workspace,
                                                         files_table.c.relpath == relpath)).first()
        return self._row_to_record(row) if row else None

    def register(self, workspace: str, relpath: str, label: Label, task_id: str | None = None,
                 user: str | None = None) -> FileRecord:
        path = self.resolve(workspace, relpath)
        write_label_sidecar(path, label, {"task_id": task_id, "created_by": user})
        area = PurePosixPath(relpath).parts[0]
        digest, size = sha256_file(path), path.stat().st_size
        existing = self.find(workspace, relpath)
        with self.db.tx() as conn:
            if existing:
                conn.execute(files_table.update().where(files_table.c.id == existing.id).values(
                    label=label.model_dump_json(), sha256=digest, size=size,
                    task_id=task_id or existing.task_id))
                fid = existing.id
            else:
                fid = new_id("F")
                conn.execute(files_table.insert().values(
                    id=fid, workspace=workspace, area=area, relpath=relpath, label=label.model_dump_json(),
                    sha256=digest, size=size, task_id=task_id, created_by=user,
                    created_at=self.clock.now().isoformat()))
        return self.get(fid)

    def sync(self, workspace: str, detect: Any = None) -> int:
        """Register files that exist on disk but not in the database."""
        base = self.ws_root(workspace)
        added = 0
        for area in AREAS:
            for path in sorted((base / area).rglob("*")):
                if not path.is_file() or path.is_symlink() or path.name.endswith(HIDDEN_SUFFIXES):
                    continue
                rel = path.relative_to(base).as_posix()
                if path.name.endswith(".ocr.json") and any(
                    (path.parent / (path.name[: -len(".ocr.json")] + ext)).is_file() for ext in (".pdf", ".png", ".jpg")
                ):
                    continue
                if self.find(workspace, rel):
                    continue
                label = read_label_sidecar(path)
                if label is None:
                    label = (detect(path) if detect else None) or self.policy.policy.default_label()
                self.register(workspace, rel, label)
                added += 1
        return added

    def list(self, workspace: str, area: str | None = None) -> list[FileRecord]:
        stmt = select(files_table).where(files_table.c.workspace == workspace)
        if area:
            stmt = stmt.where(files_table.c.area == area)
        with self.db.read() as conn:
            rows = conn.execute(stmt.order_by(files_table.c.area, files_table.c.relpath)).all()
        out = []
        for r in rows:
            rec = self._row_to_record(r)
            if (self.root / workspace / rec.relpath).is_file():
                out.append(rec)
        return out

    # -- operations ---------------------------------------------------------------------------

    def upload(self, workspace: str, filename: str, data: bytes, label: Label | None, user: str,
               detected: Label | None = None) -> FileRecord:
        name = PurePosixPath(filename.replace("\\", "/")).name
        clean_relpath(name)
        if name.endswith(HIDDEN_SUFFIXES):
            raise PolicyError("reserved file name")
        path = self.resolve(workspace, f"inputs/{name}", must_exist=False)
        if path.exists():
            raise PolicyError(f"inputs/{name} already exists")
        path.write_bytes(data)
        chosen = label or self.policy.policy.default_label()
        final = chosen.join(detected) if detected else chosen
        ws = self.policy.workspace(workspace)
        if not self.policy.can_place(final, ws):
            path.unlink()
            raise PolicyError(f"label {final.display()} is above the ceiling of workspace {workspace}")
        return self.register(workspace, f"inputs/{name}", final, user=user)

    def write_draft(self, workspace: str, name: str, data: bytes, label: Label, task_id: str,
                    overwrite_ok: bool) -> FileRecord:
        clean_relpath(name)
        # each task writes into its own folder, so drafts of different tasks never collide
        rel = f"drafts/{task_id}/{name}"
        path = self.resolve(workspace, rel, must_exist=False)
        if path.exists() and not overwrite_ok:
            raise PolicyError(f"{rel} exists; overwriting needs approval")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self.register(workspace, rel, label, task_id=task_id)

    def promote_to_final(self, file_id: str, user: str) -> FileRecord:
        rec = self.get(file_id)
        if rec.area != "drafts":
            raise PolicyError("only drafts can be moved to final/")
        src = self.resolve(rec.workspace, rec.relpath)
        rel = "final/" + PurePosixPath(rec.relpath).relative_to("drafts").as_posix()
        dst = self.resolve(rec.workspace, rel, must_exist=False)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return self.register(rec.workspace, rel, rec.label, task_id=rec.task_id, user=user)

    def share(self, file_id: str, target: str, user_id: str) -> FileRecord:
        rec = self.get(file_id)
        user = self.policy.user(user_id)
        ws = self.policy.workspace(target)
        self.policy.require_workspace(user, self.policy.workspace(rec.workspace))
        self.policy.require_workspace(user, ws)
        if not self.policy.can_place(rec.label, ws):
            raise PolicyError(f"{rec.name} is {rec.label.display()}; workspace {target} has ceiling "
                              f"{ws.ceiling.display()}")
        src = self.resolve(rec.workspace, rec.relpath)
        dst = self.resolve(target, f"inputs/{src.name}", must_exist=False)
        shutil.copy2(src, dst)
        return self.register(target, f"inputs/{src.name}", rec.label, task_id=rec.task_id, user=user_id)

    def relabel(self, file_id: str, label: Label) -> FileRecord:
        """Used only by the downgrade flow after an approved request."""
        rec = self.get(file_id)
        path = self.resolve(rec.workspace, rec.relpath)
        from workbench.tools.render_common import restamp

        restamp(path, label)
        return self.register(rec.workspace, rec.relpath, label, task_id=rec.task_id)

    def open_path(self, file_id: str) -> Path:
        rec = self.get(file_id)
        return self.resolve(rec.workspace, rec.relpath)

    def remove_symlinks_supported(self) -> bool:
        return hasattr(os, "symlink")
