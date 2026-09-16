"""Embedders. ``HashingEmbedder`` is deterministic and needs no downloads."""

from __future__ import annotations

import hashlib
from typing import Protocol

import numpy as np

from workbench.core.normalise import tokens


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> np.ndarray: ...


class HashingEmbedder:
    """Signed feature hashing over word unigrams, bigrams and character n-grams, L2-normalised."""

    def __init__(self, dim: int = 768, ngram: tuple[int, int] = (3, 5)) -> None:
        self.dim = dim
        self.ngram = ngram

    def _features(self, text: str) -> list[tuple[str, float]]:
        toks = tokens(text)
        feats: list[tuple[str, float]] = [(f"w:{t}", 1.0) for t in toks]
        feats += [(f"b:{a}_{b}", 0.7) for a, b in zip(toks, toks[1:], strict=False)]
        lo, hi = self.ngram
        for t in toks:
            padded = f"<{t}>"
            for n in range(lo, hi + 1):
                for i in range(max(0, len(padded) - n + 1)):
                    feats.append((f"c{n}:{padded[i:i + n]}", 0.3))
        return feats

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for feat, weight in self._features(text):
                h = int.from_bytes(hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest(), "little")
                idx = h % self.dim
                sign = 1.0 if (h >> 63) & 1 else -1.0
                out[row, idx] += sign * weight
            norm = float(np.linalg.norm(out[row]))
            if norm > 0:
                out[row] /= norm
        return out
