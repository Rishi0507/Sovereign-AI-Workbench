"""Versioned plan templates: load, match, instantiate, and placeholder resolution."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from workbench.core.errors import ConfigError
from workbench.planning.plan_schema import Deliverable, Plan, PlanInput, PlanStep

EXT_KINDS = {".pdf": "pdf", ".png": "image", ".jpg": "image", ".jpeg": "image", ".tif": "image",
             ".tiff": "image", ".md": "text", ".txt": "text", ".csv": "csv", ".json": "text"}
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*(?:\[\d+\])?(?:\.[A-Za-z0-9_]+)*)\}")


class TemplateMatch(BaseModel):
    route: list[str] = Field(default_factory=list)
    attachments: list[str] = Field(default_factory=list)
    intent_keywords: list[str] = Field(default_factory=list)


class TemplateStep(BaseModel):
    id: str
    title: str = ""
    tool: str | None = None
    model_task: str | None = None
    default_args: dict[str, Any] = Field(default_factory=dict)
    output_schema: str | None = None
    output_type: str | None = None
    side_effect: bool = False
    inputs: list[str] = Field(default_factory=list)


class PlanTemplate(BaseModel):
    name: str
    version: int
    description: str = ""
    match: TemplateMatch
    deliverables: list[Deliverable] = Field(default_factory=list)
    steps: list[TemplateStep]
    path: str | None = None

    def instantiate(self, goal: str) -> Plan:
        steps = []
        for s in self.steps:
            inputs = [PlanInput(source="step", ref=r) for r in s.inputs]
            steps.append(PlanStep(id=s.id, title=s.title, tool=s.tool, model_task=s.model_task,
                                  args=dict(s.default_args), default_args=dict(s.default_args) if s.tool else None,
                                  inputs=inputs, output_type=s.output_type or "records",
                                  output_schema=Path(s.output_schema).stem if s.output_schema else None,
                                  side_effect=s.side_effect))
        return Plan(goal=goal, steps=steps, deliverables=list(self.deliverables), source="template",
                    template=self.name, template_version=self.version)


def attachment_kind(name: str) -> str:
    lower = name.lower()
    if lower.endswith(".ocr.json"):
        return "pdf"
    return EXT_KINDS.get(Path(lower).suffix, "other")


class TemplateLibrary:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.templates: dict[str, PlanTemplate] = {}
        self.reload()

    def reload(self) -> None:
        self.templates = {}
        for path in sorted(self.directory.glob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            try:
                tpl = PlanTemplate.model_validate({**data, "path": path.name})
            except ValueError as exc:
                raise ConfigError(f"invalid template {path.name}: {exc}") from exc
            self.templates[tpl.name] = tpl

    def get(self, name: str) -> PlanTemplate | None:
        return self.templates.get(name)

    def match(self, route: str, text: str, attachments: list[str]) -> list[PlanTemplate]:
        low = f" {text.lower()} "
        kinds = {attachment_kind(a) for a in attachments}
        out = []
        for tpl in self.templates.values():
            m = tpl.match
            if m.route and route not in m.route:
                continue
            if m.attachments and (not attachments or not kinds <= set(m.attachments)):
                continue
            if m.intent_keywords and not any(re.search(rf"\b{re.escape(k.lower())}\b", low)
                                             for k in m.intent_keywords):
                continue
            out.append(tpl)
        return sorted(out, key=lambda t: t.name)


# -- placeholder resolution -----------------------------------------------------------------


def _lookup(env: dict[str, Any], path: str) -> Any:
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\[(\d+)\])?(.*)$", path)
    if not m:
        return None
    value = env.get(m.group(1))
    if m.group(2) is not None:
        try:
            value = value[int(m.group(2))] if value is not None else None
        except (IndexError, TypeError, KeyError):
            return None
    for part in [p for p in m.group(3).split(".") if p]:
        if value is None:
            return None
        if isinstance(value, dict):
            if part not in value and isinstance(value.get("value"), dict):
                value = value["value"].get(part)
            else:
                value = value.get(part)
        elif isinstance(value, list) and part.isdigit():
            value = value[int(part)] if int(part) < len(value) else None
        else:
            return None
    if isinstance(value, dict) and set(value) >= {"value", "record", "confidence"}:
        return value["value"]
    return value


VERBATIM_KEYS = frozenset({"script", "content"})


def resolve(value: Any, env: dict[str, Any]) -> Any:
    """Replace ``{name}`` placeholders. A value that is exactly one placeholder keeps its type.

    Code and file content (``script``, ``content``) are passed through untouched.
    """
    if isinstance(value, str):
        full = PLACEHOLDER_RE.fullmatch(value.strip())
        if full:
            return _lookup(env, full.group(1))
        if not PLACEHOLDER_RE.search(value):
            return value

        def sub(m: re.Match[str]) -> str:
            got = _lookup(env, m.group(1))
            return "" if got is None else str(got)

        return " ".join(PLACEHOLDER_RE.sub(sub, value).split())
    if isinstance(value, list):
        return [resolve(v, env) for v in value]
    if isinstance(value, dict):
        return {k: (v if k in VERBATIM_KEYS else resolve(v, env)) for k, v in value.items()}
    return value


def placeholders(value: Any) -> list[str]:
    if isinstance(value, str):
        return PLACEHOLDER_RE.findall(value)
    if isinstance(value, list):
        return [p for v in value for p in placeholders(v)]
    if isinstance(value, dict):
        return [p for v in value.values() for p in placeholders(v)]
    return []
