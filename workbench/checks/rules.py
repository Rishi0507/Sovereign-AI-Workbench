"""Consistency rule specifications loaded from ``rules/consistency/*.yaml``."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from workbench.core.errors import ConfigError

COMPARATORS = frozenset({"eq_normalised", "exists_normalised", "gte", "lte", "lte_date", "gte_date", "fuzzy_party"})


class RuleSpec(BaseModel):
    name: str
    description: str = ""
    type: Literal["compare", "trend"] = "compare"
    applies_to: list[str] = Field(default_factory=list)
    left: str | None = None
    right: dict[str, Any] | None = None
    compare: str | None = None
    aggregate: Literal["each", "min", "max"] = "each"
    report: Literal["all", "failures_only"] = "all"
    threshold: float | None = None
    on_fail: Literal["mismatch"] = "mismatch"
    quantity: str | None = None
    tag: str | None = None
    limit: dict[str, Any] | None = None
    horizon: dict[str, Any] | None = None
    source_file: str = ""

    def validate_spec(self) -> None:
        if self.type == "compare":
            if not self.left or not self.right or self.compare not in COMPARATORS:
                raise ConfigError(f"rule {self.name}: compare rules need left, right and a known comparator")
        else:
            if not self.quantity or not self.limit or not self.horizon:
                raise ConfigError(f"rule {self.name}: trend rules need quantity, limit and horizon")


def load_rules(directory: Path) -> list[RuleSpec]:
    rules: list[RuleSpec] = []
    for path in sorted(directory.glob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not doc:
                continue
            spec = RuleSpec.model_validate({**doc, "source_file": path.name})
            spec.validate_spec()
            rules.append(spec)
    names = [r.name for r in rules]
    if len(names) != len(set(names)):
        raise ConfigError("duplicate consistency rule names")
    return rules
