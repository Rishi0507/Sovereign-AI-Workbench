"""Offline ingestion entry point used by ``workbench ingest``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from workbench.kb.retrieve import KnowledgeBase


def ingest(kb: KnowledgeBase, source: Path, asset_register: Path | None = None) -> dict[str, Any]:
    if not source.is_dir():
        raise FileNotFoundError(f"knowledge-base source {source} does not exist")
    return kb.ingest_dir(source, asset_register)
