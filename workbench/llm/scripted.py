"""Fixture-scripted backend used to force exact failure paths in tests.

A script (``fixtures/scripts/<name>.yaml``) is an ordered list of steps::

    steps:
      - match: {purpose: step.decide, contains: "extract"}
        times: 2                      # optional, default 1
        response: {text: "not json"}  # or {parsed: {...}} or {json: {...}}

For each request the first step with remaining uses whose ``match`` fits is consumed. Requests
that no step matches are passed to the fallback backend (the heuristic backend by default).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import yaml

from workbench.llm.base import LLMBackend, LLMRequest, LLMResponse


class ScriptedBackend:
    name = "scripted"

    def __init__(self, steps: list[dict[str, Any]], fallback: LLMBackend | None = None) -> None:
        self.steps = [dict(s, _left=int(s.get("times", 1))) for s in steps]
        self.fallback = fallback
        self.log: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    @classmethod
    def from_file(cls, path: Path, fallback: LLMBackend | None = None) -> ScriptedBackend:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(list(data.get("steps") or []), fallback)

    @staticmethod
    def _matches(match: dict[str, Any], req: LLMRequest) -> bool:
        if "purpose" in match and match["purpose"] != req.purpose:
            return False
        if "model" in match and match["model"] != req.model:
            return False
        if "contains" in match:
            blob = "\n".join(m.content for m in req.messages)
            if str(match["contains"]) not in blob:
                return False
        if "step" in match and req.meta.get("step_id") != match["step"]:
            return False
        return True

    def chat(self, req: LLMRequest) -> LLMResponse:
        with self._lock:
            for step in self.steps:
                if step["_left"] > 0 and self._matches(step.get("match") or {}, req):
                    step["_left"] -= 1
                    self.log.append({"purpose": req.purpose, "model": req.model, "scripted": True})
                    resp = step.get("response") or {}
                    if "json" in resp:
                        return LLMResponse(text=json.dumps(resp["json"]))
                    if "parsed" in resp:
                        return LLMResponse(text=json.dumps(resp["parsed"]), parsed=resp["parsed"])
                    return LLMResponse(text=str(resp.get("text", "")))
        if self.fallback is None:
            raise RuntimeError(f"scripted backend has no step for purpose {req.purpose}")
        self.log.append({"purpose": req.purpose, "model": req.model, "scripted": False})
        return self.fallback.chat(req)

    def remaining(self) -> int:
        return sum(s["_left"] for s in self.steps)
