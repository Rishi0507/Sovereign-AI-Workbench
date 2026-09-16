"""Identifier helpers and canonical hashing."""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any


def new_id(prefix: str, nbytes: int = 4) -> str:
    return f"{prefix}{secrets.token_hex(nbytes).upper()}"


def new_task_id() -> str:
    return new_id("T")


def record_id(task_id: str, seq: int) -> str:
    return f"R-{task_id}-{seq}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def sha256_file(path: str | Any, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()
