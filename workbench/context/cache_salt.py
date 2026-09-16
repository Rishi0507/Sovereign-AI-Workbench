"""Label-salted prefix cache (README section 3.0.3)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

from workbench.core.labels import Label

BLOCK_CHARS = 64  # ~16 tokens per simulated KV block


def load_or_create_key(path: Path) -> bytes:
    if path.is_file():
        return bytes.fromhex(path.read_text(encoding="utf-8").strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    path.write_text(key.hex(), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


def cache_salt(label: Label, key: bytes) -> str:
    message = f"{int(label.level)}|{sorted(label.compartments)}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:32]


class CacheSimulator:
    """Simulates vLLM automatic prefix caching with salt partitioning.

    A request's prompt is split into fixed-size blocks; each block is keyed by the hash of the
    salt and the whole prefix up to that block, like vLLM's block hashing. A block counts as a
    hit only if the same key was seen before, so different salts can never share hits.
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        self.stats: dict[str, dict[str, int]] = defaultdict(lambda: {"requests": 0, "hit_tokens": 0,
                                                                     "prompt_tokens": 0})
        self.partition_labels: dict[str, str] = {}

    def observe(self, salt: str, prompt: str, label: str = "") -> tuple[int, int]:
        h = hashlib.sha256(salt.encode())
        hits = 0
        blocks = len(prompt) // BLOCK_CHARS
        keys = []
        for i in range(blocks):
            h.update(prompt[i * BLOCK_CHARS:(i + 1) * BLOCK_CHARS].encode("utf-8"))
            keys.append(h.copy().hexdigest())
        with self._lock:
            for k in keys:
                if k in self._seen:
                    hits += 1
                else:
                    break
            self._seen.update(keys)
            st = self.stats[salt]
            st["requests"] += 1
            st["hit_tokens"] += hits * BLOCK_CHARS // 4
            st["prompt_tokens"] += len(prompt) // 4
            if label:
                self.partition_labels[salt] = label
        return hits * BLOCK_CHARS // 4, len(prompt) // 4

    def cross_partition_hits(self, prompt: str, salt_a: str, salt_b: str) -> int:
        """Hits a request with ``salt_b`` would get after only ``salt_a`` saw the prompt."""
        probe = CacheSimulator()
        probe.observe(salt_a, prompt)
        hits, _ = probe.observe(salt_b, prompt)
        return hits

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            out = []
            for salt, st in self.stats.items():
                rate = st["hit_tokens"] / st["prompt_tokens"] if st["prompt_tokens"] else 0.0
                out.append({"partition": salt[:8], "label": self.partition_labels.get(salt, ""), **st,
                            "hit_rate": round(rate, 3)})
            return sorted(out, key=lambda r: r["label"])
