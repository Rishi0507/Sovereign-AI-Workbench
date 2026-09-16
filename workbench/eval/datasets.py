"""Evaluation dataset loading (``fixtures/eval/*.jsonl``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load(root: Path, name: str) -> list[dict[str, Any]]:
    path = root / "fixtures" / "eval" / f"{name}.jsonl"
    if not path.is_file():
        raise FileNotFoundError(f"evaluation dataset {path} is missing")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
