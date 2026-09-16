"""Sparse retrieval with BM25 over the same chunks as the dense index."""

from __future__ import annotations

from rank_bm25 import BM25Okapi

from workbench.core.normalise import norm_tag, tokens


def analyse(text: str) -> list[str]:
    toks = tokens(text)
    from workbench.core.normalise import find_tags

    toks += [norm_tag(t).lower() for t in find_tags(text)]
    return toks or ["_empty_"]


class BM25Index:
    def __init__(self) -> None:
        self.ids: list[str] = []
        self._docs: list[list[str]] = []
        self._bm25: BM25Okapi | None = None

    def build(self, items: list[tuple[str, str]]) -> None:
        self.ids = [i for i, _ in items]
        self._docs = [analyse(t) for _, t in items]
        self._bm25 = BM25Okapi(self._docs) if self._docs else None

    def search(self, query: str, k: int) -> list[tuple[str, float]]:
        if self._bm25 is None:
            return []
        scores = self._bm25.get_scores(analyse(query))
        ranked = sorted(range(len(self.ids)), key=lambda i: (-scores[i], self.ids[i]))[:k]
        return [(self.ids[i], float(scores[i])) for i in ranked if scores[i] > 0]
