"""Classification labels, the high-water mark and the policy engine (README section 6.5).

The model never receives an API to change labels. Labels only move up during a task, and a
downgrade needs an authorised requester, a stated reason and a different authorised approver.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from workbench.core.errors import ConfigError, NotFound, PolicyError


class Level(IntEnum):
    UNCLASSIFIED = 0
    RESTRICTED = 1
    CONFIDENTIAL = 2
    SECRET = 3

    @classmethod
    def parse(cls, value: object) -> Level:
        if isinstance(value, Level):
            return value
        if isinstance(value, int):
            return cls(value)
        if isinstance(value, str):
            key = value.strip().upper()
            if key.isdigit():
                return cls(int(key))
            try:
                return cls[key]
            except KeyError as exc:
                raise ValueError(f"unknown classification level {value!r}") from exc
        raise ValueError(f"cannot parse level from {value!r}")

    @property
    def title(self) -> str:
        return self.name.capitalize()


class Label(BaseModel):
    model_config = ConfigDict(frozen=True)

    level: Level
    compartments: frozenset[str] = frozenset()

    @field_validator("level", mode="before")
    @classmethod
    def _level(cls, v: object) -> Level:
        return Level.parse(v)

    @field_validator("compartments", mode="before")
    @classmethod
    def _comps(cls, v: object) -> frozenset[str]:
        if v is None:
            return frozenset()
        if isinstance(v, str):
            return frozenset(p for p in (x.strip() for x in v.split(",")) if p)
        if isinstance(v, (list, tuple, set, frozenset)):
            return frozenset(str(x) for x in v)
        raise ValueError("compartments must be a list")

    @field_serializer("level")
    def _ser_level(self, v: Level) -> str:
        return v.title

    @field_serializer("compartments")
    def _ser_comps(self, v: frozenset[str]) -> list[str]:
        return sorted(v)

    def join(self, other: Label) -> Label:
        return Label(level=max(self.level, other.level), compartments=self.compartments | other.compartments)

    def dominates(self, other: Label) -> bool:
        return self.level >= other.level and self.compartments >= other.compartments

    def display(self) -> str:
        if self.compartments:
            return f"{self.level.title} · {' · '.join(sorted(self.compartments))}"
        return self.level.title

    def marking(self) -> str:
        parts = [self.level.name]
        parts.extend(sorted(self.compartments))
        return " // ".join(parts)

    @classmethod
    def parse(cls, value: object) -> Label:
        if isinstance(value, Label):
            return value
        if isinstance(value, dict):
            return cls.model_validate(value)
        if isinstance(value, str):
            head, _, rest = value.partition("+")
            comps = [c.strip() for c in rest.split("+") if c.strip()] if rest else []
            return cls(level=Level.parse(head), compartments=frozenset(comps))
        raise ValueError(f"cannot parse label from {value!r}")

    @classmethod
    def lowest(cls) -> Label:
        return cls(level=Level.UNCLASSIFIED)


def high_water(labels: list[Label]) -> Label:
    out = Label.lowest()
    for lab in labels:
        out = out.join(lab)
    return out


class LabelPolicy(BaseModel):
    levels: list[str]
    default_upload_label: str = "Restricted"
    compartments: list[str] = Field(default_factory=list)
    downgrade_roles: list[str] = Field(default_factory=list)
    detect_markings: bool = True
    markings: dict[str, list[str]] = Field(default_factory=dict)
    compartment_markings: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("levels")
    @classmethod
    def _levels(cls, v: list[str]) -> list[str]:
        expected = [lvl.title for lvl in Level]
        if [x.capitalize() for x in v] != expected:
            raise ValueError(f"levels must be {expected} in order")
        return v

    def default_label(self) -> Label:
        return Label(level=Level.parse(self.default_upload_label))

    def detect(self, text: str) -> Label | None:
        """Return the highest marking found in ``text``, or None if no marking is present."""
        if not self.detect_markings or not text:
            return None
        found: Level | None = None
        for name, strings in self.markings.items():
            lvl = Level.parse(name)
            for s in strings:
                pattern = r"(?<![A-Za-z])" + re.escape(s) + r"(?![A-Za-z])"
                if re.search(pattern, text, flags=re.IGNORECASE if s.isascii() else 0):
                    if found is None or lvl > found:
                        found = lvl
                    break
        if found is None:
            return None
        comps = set()
        for comp, strings in self.compartment_markings.items():
            if any(s.lower() in text.lower() for s in strings):
                comps.add(comp)
        return Label(level=found, compartments=frozenset(comps))


class User(BaseModel):
    id: str
    name: str = ""
    groups: list[str] = Field(default_factory=list)
    clearance: Label
    roles: list[str] = Field(default_factory=list)


class Workspace(BaseModel):
    id: str
    title: str = ""
    ceiling: Label
    acl_groups: list[str] = Field(default_factory=list)


class DowngradeRequest(BaseModel):
    id: str
    artifact: str
    before: Label
    after: Label
    requester: str
    reason: str
    status: Literal["pending", "approved", "rejected"] = "pending"
    approver: str | None = None
    decided_at: datetime | None = None
    note: str | None = None


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"missing config file {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping")
    return data


class PolicyEngine:
    def __init__(self, policy: LabelPolicy, users: dict[str, User], workspaces: dict[str, Workspace]) -> None:
        self.policy = policy
        self.users = users
        self.workspaces = workspaces

    @classmethod
    def from_config(cls, config_dir: Path) -> PolicyEngine:
        policy = LabelPolicy.model_validate(_load_yaml(config_dir / "labels.yaml"))
        users: dict[str, User] = {}
        for uid, raw in (_load_yaml(config_dir / "users.yaml").get("users") or {}).items():
            clearance = Label(level=Level.parse(raw.get("clearance", "Unclassified")),
                              compartments=frozenset(raw.get("compartments") or []))
            users[uid] = User(id=uid, name=raw.get("name", uid), groups=raw.get("groups") or [],
                              clearance=clearance, roles=raw.get("roles") or [])
        workspaces: dict[str, Workspace] = {}
        for wid, raw in (_load_yaml(config_dir / "workspaces.yaml").get("workspaces") or {}).items():
            workspaces[wid] = Workspace(id=wid, title=raw.get("title", wid), ceiling=Label.parse(raw["ceiling"]),
                                        acl_groups=raw.get("acl_groups") or [])
        for comp in {c for u in users.values() for c in u.clearance.compartments}:
            if comp not in policy.compartments:
                raise ConfigError(f"unknown compartment {comp!r} in users.yaml")
        return cls(policy, users, workspaces)

    def user(self, uid: str) -> User:
        try:
            return self.users[uid]
        except KeyError as exc:
            raise PolicyError(f"unknown user {uid!r}") from exc

    def workspace(self, wid: str) -> Workspace:
        try:
            return self.workspaces[wid]
        except KeyError as exc:
            raise NotFound(f"unknown workspace {wid!r}") from exc

    def can_access_workspace(self, user: User, workspace: Workspace) -> bool:
        return bool(set(user.groups) & set(workspace.acl_groups))

    def require_workspace(self, user: User, workspace: Workspace) -> None:
        if not self.can_access_workspace(user, workspace):
            raise PolicyError(f"user {user.id} has no access to workspace {workspace.id}")

    def retrieval_ceiling(self, user: User, workspace: Workspace) -> Label:
        return Label(
            level=min(user.clearance.level, workspace.ceiling.level),
            compartments=user.clearance.compartments & workspace.ceiling.compartments,
        )

    def can_read(self, user: User, workspace: Workspace, label: Label) -> bool:
        return self.can_access_workspace(user, workspace) and self.retrieval_ceiling(user, workspace).dominates(label)

    def can_place(self, label: Label, workspace: Workspace) -> bool:
        return workspace.ceiling.dominates(label)

    def can_downgrade(self, user: User) -> bool:
        return bool(set(user.roles) & set(self.policy.downgrade_roles))

    def request_downgrade(self, req_id: str, artifact: str, before: Label, after: Label,
                          user: User, reason: str) -> DowngradeRequest:
        if not self.can_downgrade(user):
            raise PolicyError(f"user {user.id} has no downgrade authority")
        if not reason.strip():
            raise PolicyError("a downgrade needs a stated reason")
        if not before.dominates(after) or before == after:
            raise PolicyError("a downgrade must lower the label")
        return DowngradeRequest(id=req_id, artifact=artifact, before=before, after=after,
                                requester=user.id, reason=reason.strip())

    def decide_downgrade(self, req: DowngradeRequest, approver: User, approve: bool,
                         when: datetime, note: str | None = None) -> DowngradeRequest:
        if req.status != "pending":
            raise PolicyError(f"downgrade {req.id} is already {req.status}")
        if approver.id == req.requester:
            raise PolicyError("the requester cannot approve their own downgrade")
        if not self.can_downgrade(approver):
            raise PolicyError(f"user {approver.id} has no downgrade authority")
        return req.model_copy(update={"status": "approved" if approve else "rejected",
                                      "approver": approver.id, "decided_at": when, "note": note})


def sidecar_path(path: Path) -> Path:
    return path.with_name(path.name + ".label.json")


def write_label_sidecar(path: Path, label: Label, extra: dict[str, Any] | None = None) -> Path:
    side = sidecar_path(path)
    payload = {"label": label.model_dump(mode="json"), **(extra or {})}
    side.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return side


def read_label_sidecar(path: Path) -> Label | None:
    side = sidecar_path(path)
    if not side.is_file():
        return None
    data = json.loads(side.read_text(encoding="utf-8"))
    return Label.parse(data["label"])
