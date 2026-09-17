"""Repository hygiene: the UI script parses and no text file contains an em dash."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEXT_SUFFIXES = {".py", ".go", ".md", ".yaml", ".yml", ".json", ".j2", ".html", ".css", ".js", ".toml", ".sh",
                 ".conf", ".service", ".txt", ".jsonl"}
SKIP_DIRS = {".git", ".venv", "var", "run", "bin", "reports", "node_modules", "__pycache__", ".mypy_cache",
             ".ruff_cache", ".pytest_cache", ".hypothesis"}


def _text_files() -> list[Path]:
    out = []
    for path in ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.is_file() and (path.suffix in TEXT_SUFFIXES or path.name in {"Makefile", "Dockerfile"}):
            out.append(path)
    return out


def test_no_em_dashes() -> None:
    dash = chr(0x2014)
    offenders = [str(p.relative_to(ROOT)) for p in _text_files() if dash in p.read_text("utf-8", errors="ignore")]
    assert offenders == []


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_ui_script_parses() -> None:
    script = ROOT / "workbench" / "ui" / "static" / "app.js"
    result = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
