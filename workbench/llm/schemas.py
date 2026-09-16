"""JSON Schema loading and validation for model outputs."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from workbench.settings import default_root


@lru_cache(maxsize=64)
def _load(root: str, name: str) -> dict[str, Any]:
    path = Path(root) / "schemas" / (name if name.endswith(".json") else f"{name}.json")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(data)
    return data


def load_schema(name: str, root: Path | None = None) -> dict[str, Any]:
    name = Path(name).name
    return _load(str(root or default_root()), name)


def validation_errors(obj: Any, schema: dict[str, Any], limit: int = 5) -> list[str]:
    validator = Draft202012Validator(schema)
    out = []
    for err in sorted(validator.iter_errors(obj), key=lambda e: list(e.absolute_path)):
        path = "/".join(str(p) for p in err.absolute_path) or "(root)"
        out.append(f"{path}: {err.message}")
        if len(out) >= limit:
            break
    return out
