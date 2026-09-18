"""Hosted models behind an OpenAI-compatible API.

The workbench keeps its own model names; this layer maps each one to a hosted model, paces the
calls so a rate limit is not exhausted in a burst, waits when the service asks it to, and records
the allowance each model has left. When the service keeps refusing, the caller falls back to the
offline rules instead of failing the task.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from workbench.core.errors import RateLimited, ServiceUnavailable
from workbench.llm.base import ChatMessage, LLMRequest, LLMResponse
from workbench.llm.openai_compat import OpenAICompatBackend

LIMIT_FIELDS = ("limit_requests", "remaining_requests", "reset_requests",
                "limit_tokens", "remaining_tokens", "reset_tokens")


@dataclass
class Pacing:
    min_interval_s: float = 1.5
    max_concurrency: int = 1
    max_retries: int = 4
    max_wait_s: float = 45.0


@dataclass
class RemoteConfig:
    base_url: str
    key_env: str
    models: dict[str, str] = field(default_factory=dict)
    default: str = ""
    chat: str = ""
    reasoning_effort: str = ""
    cache_salt: bool = False
    pacing: Pacing = field(default_factory=Pacing)

    @classmethod
    def load(cls, path: Path) -> RemoteConfig:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        pacing = Pacing(**{k: v for k, v in (data.get("pacing") or {}).items() if k in Pacing.__annotations__})
        return cls(base_url=str(data.get("base_url", "")), key_env=str(data.get("key_env", "")),
                   models={str(k): str(v) for k, v in (data.get("models") or {}).items()},
                   default=str(data.get("default", "")), chat=str(data.get("chat", "")),
                   reasoning_effort=str(data.get("reasoning_effort") or ""),
                   cache_salt=bool(data.get("cache_salt", False)), pacing=pacing)

    def api_key(self) -> str:
        return os.environ.get(self.key_env, "").strip()


class Throttle:
    """One call at a time, spaced apart, shared by every caller in the process."""

    def __init__(self, pacing: Pacing) -> None:
        self.pacing = pacing
        self._gate = threading.Semaphore(max(1, pacing.max_concurrency))
        self._lock = threading.Lock()
        self._next_at = 0.0

    def __enter__(self) -> Throttle:
        self._gate.acquire()
        while True:
            with self._lock:
                now = time.monotonic()
                wait = self._next_at - now
                if wait <= 0:
                    self._next_at = now + self.pacing.min_interval_s
                    return self
            time.sleep(min(wait, 1.0))

    def __exit__(self, *_exc: object) -> None:
        self._gate.release()

    def hold(self, seconds: float) -> None:
        """Do not call again before this many seconds have passed."""
        with self._lock:
            self._next_at = max(self._next_at, time.monotonic() + seconds)


class Usage:
    """What each hosted model reports about the allowance left, as of its last answer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._models: dict[str, dict[str, Any]] = {}

    def record(self, model: str, headers: httpx.Headers, status: int) -> None:
        seen = {field: headers.get(f"x-ratelimit-{field.replace('_', '-')}") for field in LIMIT_FIELDS}
        if not any(seen.values()) and status < 400:
            return
        with self._lock:
            entry = self._models.setdefault(model, {"model": model, "calls": 0, "rate_limited": 0})
            entry.update({k: v for k, v in seen.items() if v is not None})
            entry["calls"] += 1
            entry["at"] = time.time()
            if status == 429:
                entry["rate_limited"] += 1

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted((dict(v) for v in self._models.values()), key=lambda e: str(e["model"]))


def _model_missing(message: str) -> bool:
    lowered = message.lower()
    return "404" in lowered and ("does not exist" in lowered or "model_not_found" in lowered)


def _tool_conflict(message: str) -> bool:
    lowered = message.lower()
    return "tool_use_failed" in lowered or "model called a tool" in lowered or "tool/function calling" in lowered


def _schema_refused(message: str) -> bool:
    lowered = message.lower()
    return "400" in lowered and ("schema" in lowered or "response_format" in lowered or "tool" in lowered)


class RemoteBackend:
    """An OpenAI-compatible service, paced and with its own model names."""

    name = "groq"

    def __init__(self, config: RemoteConfig, usage: Usage | None = None, throttle: Throttle | None = None,
                 timeout_s: float = 120.0, transport: httpx.BaseTransport | None = None) -> None:
        self.config = config
        self._no_schema: set[str] = set()
        self._plain: set[str] = set()
        self._missing: set[str] = set()
        self.usage = usage or Usage()
        self.throttle = throttle or Throttle(config.pacing)
        key = config.api_key()
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        self.client = OpenAICompatBackend(lambda _m: config.base_url, timeout_s=timeout_s, transport=transport,
                                          headers=headers, allowlist=self._allowlist(), observer=self.usage.record)

    def _allowlist(self) -> set[tuple[str, int]]:
        host = httpx.URL(self.config.base_url).host
        return {(host, 443), (host, 80)}

    def hosted_name(self, model: str) -> str:
        name = self._hosted_name(model)
        return self.config.default if name in self._missing and self.config.default else name

    def _hosted_name(self, model: str) -> str:
        if model in self.config.models:
            return self.config.models[model]
        if model in set(self.config.models.values()) or model == self.config.chat:
            return model
        return self.config.default or model

    def _prepared(self, req: LLMRequest, hosted: str) -> LLMRequest:
        """The request as this hosted model wants it, after what it has already refused."""
        meta = req.meta
        if self.config.reasoning_effort:
            meta = {**meta, "reasoning_effort": self.config.reasoning_effort}
        update: dict[str, Any] = {"model": hosted, "meta": meta}
        if not self.config.cache_salt:
            # A hosted service has no partitioned prefix cache to salt, and refuses the field.
            update["cache_salt"] = None
        if req.json_schema is not None and req.tools:
            # Asking for JSON and offering tools at the same time is refused; the JSON is what is read.
            update["tools"] = None
        prepared = req.model_copy(update=update)
        if prepared.json_schema is None or (hosted not in self._no_schema and hosted not in self._plain):
            return prepared
        # The shape goes in the prompt instead: either the model refuses strict schemas, or it answers
        # a JSON request with a tool call, which the service rejects.
        schema = json.dumps(prepared.json_schema, separators=(",", ":"))
        messages = [*prepared.messages,
                    ChatMessage(role="user", content=f"Return only JSON matching this schema, and never a "
                                                     f"tool call: {schema}")]
        meta = dict(prepared.meta)
        if hosted not in self._plain:
            meta["json_object"] = True
        return prepared.model_copy(update={"json_schema": None, "messages": messages, "meta": meta})

    def chat(self, req: LLMRequest) -> LLMResponse:
        hosted = self.hosted_name(req.model)
        pacing = self.config.pacing
        waited = 0.0
        last = ""
        for attempt in range(max(1, pacing.max_retries)):
            with self.throttle:
                try:
                    return self.client.chat(self._prepared(req, hosted))
                except RateLimited as exc:
                    last = str(exc)
                    pause = min(exc.retry_after_s or 2.0 * (attempt + 1), pacing.max_wait_s - waited)
                    # Everything else waits too: the allowance is shared by the whole service.
                    self.throttle.hold(max(pause, 0.0))
                except ServiceUnavailable as exc:
                    if _tool_conflict(str(exc)) and hosted not in self._plain:
                        # Asked for JSON, answered with a tool call: ask in words instead.
                        self._plain.add(hosted)
                        return self.client.chat(self._prepared(req, hosted))
                    if _model_missing(str(exc)) and hosted != self.config.default and self.config.default:
                        # The service does not offer this model to this account: use the default instead.
                        self._missing.add(hosted)
                        hosted = self.config.default
                        return self.client.chat(self._prepared(req, hosted))
                    if req.json_schema is None or hosted in self._no_schema or not _schema_refused(str(exc)):
                        raise
                    self._no_schema.add(hosted)
                    return self.client.chat(self._prepared(req, hosted))
            if pause <= 0 or waited + pause >= pacing.max_wait_s:
                break
            waited += pause
            time.sleep(pause)
        raise RateLimited(f"{hosted} is rate limited and did not recover: {last}")
