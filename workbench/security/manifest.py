"""SHA-256 manifest verification for weights, wheels and image archives (README section 6.4).

Manifest format, one entry per line: ``<sha256>  <relative path>`` (the ``sha256sum`` format).
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel

from workbench.core.ids import sha256_file

log = logging.getLogger(__name__)


class Mismatch(BaseModel):
    path: str
    expected: str
    actual: str | None
    problem: str


def parse_manifest(text: str) -> list[tuple[str, str]]:
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, rel = line.partition("  ")
        if len(digest) != 64 or not rel:
            raise ValueError(f"malformed manifest line: {line!r}")
        entries.append((digest.lower(), rel.lstrip("*")))
    return entries


def verify(manifest_path: Path, root: Path) -> list[Mismatch]:
    problems: list[Mismatch] = []
    base = root.resolve()
    for expected, rel in parse_manifest(manifest_path.read_text(encoding="utf-8")):
        target = (base / rel).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            problems.append(Mismatch(path=rel, expected=expected, actual=None, problem="path escapes root"))
            continue
        if not target.is_file():
            problems.append(Mismatch(path=rel, expected=expected, actual=None, problem="missing"))
            continue
        actual = sha256_file(target)
        if actual != expected:
            problems.append(Mismatch(path=rel, expected=expected, actual=actual, problem="hash mismatch"))
    return problems


def write_manifest(root: Path, files: list[Path]) -> str:
    return "".join(f"{sha256_file(f)}  {f.resolve().relative_to(root.resolve()).as_posix()}\n" for f in sorted(files))


def verify_signature(manifest_path: Path, signature_path: Path) -> object:
    """Signature verification hook. Not implemented in this build."""
    log.warning("manifest signature verification is not implemented in this build (%s)", signature_path)
    return NotImplemented
