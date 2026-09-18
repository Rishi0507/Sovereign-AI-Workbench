"""Repository hygiene: the UI script parses and no text file contains an em dash."""

from __future__ import annotations

import hashlib
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


def test_public_samples_are_attributed() -> None:
    """Every publicly sourced file records where it came from and under what licence."""
    public = ROOT / "fixtures" / "public"
    if not public.is_dir():
        return
    sources = (public / "SOURCES.md").read_text(encoding="utf-8")
    files = [p for p in public.iterdir() if p.suffix.lower() not in {".md"} and not p.name.startswith("_")]
    assert files, "the public sample folder is empty"
    for path in files:
        assert f"## {path.name}" in sources, f"{path.name} is not listed in SOURCES.md"
        block = sources.split(f"## {path.name}", 1)[1].split("\n## ", 1)[0]
        for field in ("**Title:**", "**Author:**", "**Licence:**", "**Source:**", "**SHA-256:**"):
            assert field in block, f"{path.name} has no {field} line"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest in block, f"{path.name} does not match the digest recorded for it"
