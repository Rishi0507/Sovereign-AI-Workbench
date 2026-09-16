"""Rerankers. ``LexicalReranker`` scores token overlap plus exact tag and number matches."""

from __future__ import annotations

from typing import Protocol

from workbench.core.normalise import NUMBER_RE, find_tags, norm_tag, parse_number, tokens


class Reranker(Protocol):
    def score(self, query: str, text: str) -> float: ...


class LexicalReranker:
    def score(self, query: str, text: str) -> float:
        q = set(tokens(query))
        if not q:
            return 0.0
        t = set(tokens(text))
        overlap = len(q & t) / len(q)
        q_tags = {norm_tag(x) for x in find_tags(query)}
        t_tags = {norm_tag(x) for x in find_tags(text)}
        tag_bonus = 0.25 * len(q_tags & t_tags) / len(q_tags) if q_tags else 0.0
        q_nums = {parse_number(m.group(0)) for m in NUMBER_RE.finditer(query)}
        t_nums = {parse_number(m.group(0)) for m in NUMBER_RE.finditer(text)}
        num_bonus = 0.15 * len(q_nums & t_nums) / len(q_nums) if q_nums else 0.0
        return round(min(1.0, overlap + tag_bonus + num_bonus), 4)
