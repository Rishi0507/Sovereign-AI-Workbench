"""Model registry schema, loader and validation (README section 4.2.6)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, model_validator

from workbench.core.errors import ConfigError, NotFound
from workbench.llm.openai_compat import is_loopback_host

Route = Literal["general", "document", "vision", "code", "agentic", "reasoning"]
ROUTES: tuple[str, ...] = ("general", "document", "vision", "code", "agentic", "reasoning")


class Provenance(BaseModel):
    developer: str
    licence: str
    origin: str | None = None


class Latency(BaseModel):
    step_s: float
    wake_s: float | None = None


class Speculative(BaseModel):
    method: Literal["ngram", "eagle3", "off"] = "off"
    model: str | None = None
    num_speculative_tokens: int | None = None
    prompt_lookup_min: int | None = None
    prompt_lookup_max: int | None = None

    @model_validator(mode="after")
    def _check(self) -> Speculative:
        if self.method == "eagle3" and not self.model:
            raise ValueError("speculative.method eagle3 requires a draft head `model`")
        if self.method != "off":
            n = self.num_speculative_tokens
            if n is None or not 1 <= n <= 8:
                raise ValueError("speculative.num_speculative_tokens must be within 1..8")
        return self

    def config(self) -> dict[str, Any] | None:
        if self.method == "off":
            return None
        return self.model_dump(exclude_none=True)


class ServeParams(BaseModel):
    port: int
    gpu_memory_utilization: float = Field(gt=0, le=1)
    max_model_len: int


class ModelEntry(BaseModel):
    name: str
    status: Literal["active", "shadow", "retired"]
    endpoint: str
    weights: str | None = None
    serves: list[str]
    modalities: list[Literal["text", "image"]] = Field(default_factory=lambda: ["text"])
    languages: list[str] = Field(default_factory=lambda: ["en"])
    max_context: int
    tool_parser: str | None = None
    protocol: Literal["tools", "code_block"] = "tools"
    pool: Literal["resident", "swap", "cold"] = "resident"
    escalation_for: list[str] = Field(default_factory=list)
    provenance: Provenance
    quality: dict[str, float] = Field(default_factory=dict)
    quality_by_lang: dict[str, dict[str, float]] = Field(default_factory=dict)
    latency: Latency
    speculative: Speculative = Field(default_factory=Speculative)
    speculative_speedup: float | None = None
    serve: dict[str, ServeParams] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> ModelEntry:
        host = urlparse(self.endpoint).hostname or ""
        if not is_loopback_host(host):
            raise ValueError(f"{self.name}: endpoint host {host!r} must be loopback")
        for route in self.serves:
            if route not in ROUTES:
                raise ValueError(f"{self.name}: unknown route {route!r}")
        return self

    @property
    def can_call_tools(self) -> bool:
        return self.protocol == "tools" and self.tool_parser is not None

    def quality_for(self, route: str, languages: list[str] | None = None) -> float | None:
        base = self.quality.get(route)
        if languages and route in self.quality_by_lang:
            per = [self.quality_by_lang[route][lang] for lang in languages if lang in self.quality_by_lang[route]]
            if per:
                return min(per)
        return base

    def pool_for(self, all_resident: bool) -> str:
        if all_resident and self.pool == "swap":
            return "resident"
        return self.pool


class RoutingConfig(BaseModel):
    thresholds: dict[str, float]
    low_confidence_below: float = 0.6
    fallback: dict[str, str] = Field(default_factory=dict)


class Registry(BaseModel):
    models: list[ModelEntry]
    routing: RoutingConfig
    version: str = ""
    source: str | None = None

    def get(self, name: str) -> ModelEntry:
        for m in self.models:
            if m.name == name:
                return m
        raise NotFound(f"model {name!r} not in registry")

    def maybe(self, name: str) -> ModelEntry | None:
        return next((m for m in self.models if m.name == name), None)

    def active(self) -> list[ModelEntry]:
        return [m for m in self.models if m.status == "active"]

    def validate_profile(self, profile: str) -> list[str]:
        """Return warnings; raise ConfigError for violations that are errors on this profile."""
        warnings: list[str] = []
        names = [m.name for m in self.models]
        if len(names) != len(set(names)):
            raise ConfigError("duplicate model names in registry")
        for m in self.models:
            if m.pool == "swap" and m.speculative.method != "off":
                msg = f"{m.name}: swap-slot entry should have speculative.method off"
                if profile == "S":
                    raise ConfigError(msg)
                warnings.append(msg)
        for route, name in self.routing.fallback.items():
            if name not in names:
                raise ConfigError(f"routing.fallback.{route} names unknown model {name}")
        return warnings


def load_registry(path: Path, profile: str = "S") -> Registry:
    if not path.is_file():
        raise ConfigError(f"missing registry {path}")
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    try:
        reg = Registry.model_validate({**data, "source": str(path)})
    except ValueError as exc:
        raise ConfigError(f"invalid registry {path}: {exc}") from exc
    import hashlib

    reg.version = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    reg.validate_profile(profile)
    return reg


def set_field_in_yaml(path: Path, model: str, field: str, value: str) -> None:
    """Change one scalar field of one entry, keeping the rest of the file (and comments) intact."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^\s*-\s+name:\s*{re.escape(model)}\s*$", line):
            start = i
            break
    if start is None:
        raise NotFound(f"model {model!r} not found in {path}")
    indent = len(lines[start]) - len(lines[start].lstrip()) + 2
    for j in range(start + 1, len(lines)):
        line = lines[j]
        stripped = line.lstrip()
        cur_indent = len(line) - len(stripped)
        if (stripped.startswith("- ") and cur_indent <= indent - 2) or (stripped and cur_indent < indent - 2):
            break
        if cur_indent == indent and stripped.startswith(f"{field}:"):
            comment = ""
            if "#" in stripped:
                comment = "  #" + stripped.split("#", 1)[1].rstrip("\n")
            lines[j] = " " * indent + f"{field}: {value}{comment}\n"
            path.write_text("".join(lines), encoding="utf-8")
            return
    raise NotFound(f"field {field!r} not found for model {model!r}")
