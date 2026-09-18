"""Chooses between the hosted models and the offline rules for each call.

The offline backend stays available at all times. A request marked ``offline`` in its meta is
answered by the rules. Everything else goes to the hosted models, and falls back to the rules when
the service is unreachable or out of allowance, so a task never fails because of a rate limit.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from workbench.core.errors import ServiceUnavailable
from workbench.llm.base import LLMBackend, LLMRequest, LLMResponse


class BackendSelector:
    def __init__(self, primary: LLMBackend, offline: LLMBackend,
                 on_fallback: Callable[[str, str], None] | None = None) -> None:
        self.primary = primary
        self.offline = offline
        self.on_fallback = on_fallback
        self.name = primary.name
        self.fallbacks = 0

    def __getattr__(self, item: str) -> Any:
        return getattr(self.primary, item)

    def chat(self, req: LLMRequest) -> LLMResponse:
        if req.meta.get("offline"):
            return self.offline.chat(req)
        try:
            return self.primary.chat(req)
        except ServiceUnavailable as exc:
            self.fallbacks += 1
            if self.on_fallback is not None:
                self.on_fallback(req.purpose, str(exc))
            return self.offline.chat(req)
