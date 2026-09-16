"""Stage 2: hard constraints with a logged reason per rejected model (README section 4.2.2)."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel, Field

from workbench.registry.models import ModelEntry, Registry
from workbench.router.types import TaskProfile


class WaitEstimator(Protocol):
    def pool_of(self, model: str) -> str: ...

    def expected_wait(self, model: str) -> float: ...


class WorkspaceProvenance(BaseModel):
    developers: list[str] = Field(default_factory=list)
    licences: list[str] = Field(default_factory=list)
    origins: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)

    def allows(self, entry: ModelEntry) -> bool:
        p = entry.provenance
        if self.developers and p.developer not in self.developers:
            return False
        if self.licences and p.licence not in self.licences:
            return False
        if self.origins and (p.origin or "") not in self.origins:
            return False
        return not (self.models and entry.name not in self.models)


class ProvenancePolicy(BaseModel):
    workspaces: dict[str, WorkspaceProvenance] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> ProvenancePolicy:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
        return cls.model_validate(data or {})

    def allows(self, workspace: str, entry: ModelEntry) -> bool:
        rule = self.workspaces.get(workspace)
        return True if rule is None else rule.allows(entry)


def check_entry(
    entry: ModelEntry,
    route: str,
    profile: TaskProfile,
    workspace: str,
    pool: WaitEstimator,
    provenance: ProvenancePolicy,
    output_reserve: int,
    interactive_limit_s: float | None,
    check_pool: bool = True,
) -> str | None:
    """Return None when ``entry`` may serve the task, else the rejection reason."""
    if entry.status != "active":
        return "status"
    if any(m not in entry.modalities for m in profile.modalities):
        return "modality"
    if entry.max_context < profile.est_input_tokens + output_reserve:
        return "context"
    if profile.needs_tools and not entry.can_call_tools and not (route == "code" and entry.protocol == "code_block"):
        return "tools"
    if route not in entry.serves:
        return "route"
    if not provenance.allows(workspace, entry):
        return "provenance"
    if check_pool:
        pool_state = pool.pool_of(entry.name)
        if pool_state == "cold":
            return "pool"
        if pool_state == "swap" and interactive_limit_s is not None and pool.expected_wait(entry.name) > interactive_limit_s:
            return "pool"
    return None


def filter_candidates(
    registry: Registry,
    route: str,
    profile: TaskProfile,
    workspace: str,
    pool: WaitEstimator,
    provenance: ProvenancePolicy,
    output_reserve: int,
    interactive_limit_s: float | None = None,
) -> tuple[list[ModelEntry], list[tuple[str, str]]]:
    candidates: list[ModelEntry] = []
    rejections: list[tuple[str, str]] = []
    for entry in registry.models:
        if entry.status == "retired":
            continue
        reason = check_entry(entry, route, profile, workspace, pool, provenance, output_reserve, interactive_limit_s)
        if reason is None:
            candidates.append(entry)
        else:
            rejections.append((entry.name, reason))
    return candidates, rejections
