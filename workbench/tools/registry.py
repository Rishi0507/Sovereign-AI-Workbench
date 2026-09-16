"""Tool specs, JSON schemas and execution with short structured observations.

No tool performs network I/O and no tool accepts an argument that changes a classification label.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from workbench.core.errors import WorkbenchError
from workbench.core.labels import Label
from workbench.llm.schemas import validation_errors

if TYPE_CHECKING:
    from workbench.runtime import Runtime


class ToolResult(BaseModel):
    ok: bool
    summary: str
    body: Any = None
    records: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    error: str | None = None
    latency_s: float = 0.0

    def observation(self, limit: int) -> str:
        payload = {"ok": self.ok, "summary": self.summary, "records": self.records, "files": self.files}
        if self.error:
            payload["error"] = self.error
        text = json.dumps(payload, ensure_ascii=False)
        return text if len(text) <= limit else text[: limit - 20] + "...(truncated)"


@dataclass
class ToolContext:
    rt: Runtime
    task_id: str
    workspace: str
    user: str
    step_id: str
    call_id: str
    label_floor: Label
    model: str
    attachments: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    cancelled: Callable[[], bool] = lambda: False

    def label(self) -> Label:
        return self.rt.ledger.high_water(self.task_id, self.label_floor)

    @property
    def job_root(self) -> Path:
        return self.rt.settings.path(self.rt.settings.workspaces_root) / "_jobs" / self.task_id


Handler = Callable[[dict[str, Any], ToolContext], ToolResult]


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    side_effect: bool = False
    output_type: str = "records"
    budget_key: str | None = None
    handler: Any = Field(default=None, exclude=True)

    def openai_tool(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.input_schema}}


class ToolRegistry:
    def __init__(self, observation_limit: int = 2000) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self.observation_limit = observation_limit

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"duplicate tool {spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return sorted(self._specs)

    def specs(self) -> list[ToolSpec]:
        return [self._specs[n] for n in self.names()]

    def validate(self, name: str, args: dict[str, Any]) -> list[str]:
        spec = self._specs.get(name)
        if spec is None:
            return [f"unknown tool {name!r}"]
        return validation_errors(args, spec.input_schema)

    def prompt_schemas(self, names: list[str] | None = None) -> str:
        chosen = [s for s in self.specs() if names is None or s.name in names]
        return json.dumps([{"name": s.name, "description": s.description, "side_effect": s.side_effect,
                            "input_schema": s.input_schema} for s in chosen], sort_keys=True, ensure_ascii=False)

    def execute(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        spec = self._specs.get(name)
        started = time.perf_counter()
        if spec is None:
            return ToolResult(ok=False, summary=f"unknown tool {name}", error="unknown_tool")
        errors = self.validate(name, args)
        if errors:
            return ToolResult(ok=False, summary=f"invalid arguments for {name}", error="; ".join(errors))
        try:
            result: ToolResult = spec.handler(args, ctx)
        except WorkbenchError as exc:
            result = ToolResult(ok=False, summary=f"{name} failed: {exc}", error=f"{exc.__class__.__name__}: {exc}")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result = ToolResult(ok=False, summary=f"{name} failed: {exc}", error=f"{exc.__class__.__name__}: {exc}")
        result.latency_s = round(time.perf_counter() - started, 4)
        return result


def obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "properties": properties, "required": required or []}
