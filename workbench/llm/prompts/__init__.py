"""Prompt templates (Jinja2). Templates are plain files next to this module."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

PROMPT_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def _env() -> Environment:
    return Environment(loader=FileSystemLoader(str(PROMPT_DIR)), undefined=StrictUndefined,
                       trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=False, autoescape=False)


def render_prompt(name: str, **kwargs: Any) -> str:
    return _env().get_template(f"{name}.j2").render(**kwargs).strip()


def prompt_names() -> list[str]:
    return sorted(p.stem for p in PROMPT_DIR.glob("*.j2"))
