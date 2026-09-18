"""OpenAI-compatible backend for vLLM (or any compatible server on loopback / the allowlist)."""

from __future__ import annotations

import base64
import ipaddress
import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx

from workbench.core.errors import PolicyError, RateLimited, ServiceUnavailable
from workbench.llm.base import LLMRequest, LLMResponse


def is_loopback_host(host: str) -> bool:
    if host in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def check_endpoint(url: str, allowlist: set[tuple[str, int]] | None = None) -> None:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if is_loopback_host(host):
        return
    if allowlist and (host, port) in allowlist:
        return
    raise PolicyError(f"endpoint {url} is neither loopback nor allowlisted")


def _retry_after(headers: httpx.Headers) -> float:
    """Seconds the service asks us to wait, from any of the headers it may use."""
    for name in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        raw = (headers.get(name) or "").strip().lower()
        if not raw:
            continue
        try:
            if raw.endswith("ms"):
                return float(raw[:-2]) / 1000
            return float(raw.rstrip("s"))
        except ValueError:
            continue
    return 0.0


class OpenAICompatBackend:
    name = "openai"

    def __init__(
        self,
        resolve_endpoint: Callable[[str], str],
        cache_salt_mode: str = "request",
        timeout_s: float = 120.0,
        allowlist: set[tuple[str, int]] | None = None,
        transport: httpx.BaseTransport | None = None,
        headers: dict[str, str] | None = None,
        observer: Callable[[str, httpx.Headers, int], None] | None = None,
    ) -> None:
        self.resolve_endpoint = resolve_endpoint
        self.cache_salt_mode = cache_salt_mode
        self.allowlist = allowlist or set()
        self.observer = observer
        self.client = httpx.Client(timeout=timeout_s, transport=transport, trust_env=False,
                                   headers=headers or {})

    def _body(self, req: LLMRequest) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        for i, m in enumerate(req.messages):
            content: Any = m.content
            if i == 0 and m.role == "system" and req.cache_salt and self.cache_salt_mode == "prefix":
                content = f"[salt:{req.cache_salt}]\n{content}"
            if m.role == "user" and req.images and i == len(req.messages) - 1:
                parts: list[dict[str, Any]] = [{"type": "text", "text": m.content}]
                for img in req.images:
                    b64 = base64.b64encode(img).decode("ascii")
                    parts.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
                content = parts
            entry: dict[str, Any] = {"role": m.role, "content": content}
            if m.name:
                entry["name"] = m.name
            messages.append(entry)
        body: dict[str, Any] = {
            "model": req.model,
            "messages": messages,
            "temperature": req.temperature,
            "seed": req.seed,
            "max_tokens": req.max_tokens,
        }
        if req.meta.get("reasoning_effort"):
            # Thinking models spend output tokens before answering; keep that budget short.
            body["reasoning_effort"] = req.meta["reasoning_effort"]
        if req.meta.get("json_object"):
            body["response_format"] = {"type": "json_object"}
        elif req.json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": req.purpose.replace(".", "_"), "schema": req.json_schema, "strict": True},
            }
        if req.tools:
            body["tools"] = req.tools
        if req.cache_salt and self.cache_salt_mode == "request":
            body["cache_salt"] = req.cache_salt
        return body

    def chat(self, req: LLMRequest) -> LLMResponse:
        endpoint = self.resolve_endpoint(req.model).rstrip("/")
        check_endpoint(endpoint, self.allowlist)
        started = time.perf_counter()
        try:
            resp = self.client.post(f"{endpoint}/chat/completions", json=self._body(req))
        except httpx.HTTPError as exc:
            raise ServiceUnavailable(f"model server for {req.model} unreachable: {exc}") from exc
        if self.observer is not None:
            self.observer(req.model, resp.headers, resp.status_code)
        if resp.status_code in {429, 503}:
            raise RateLimited(f"model server for {req.model} returned {resp.status_code}: {resp.text[:200]}",
                              retry_after_s=_retry_after(resp.headers))
        if resp.status_code >= 400:
            raise ServiceUnavailable(f"model server for {req.model} returned {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        parsed: dict[str, Any] | None = None
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            fn = tool_calls[0].get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments")}
            parsed = {"tool": fn.get("name"), "args": args}
            text = text or json.dumps(parsed)
        usage = dict(data.get("usage") or {})
        details = usage.get("prompt_tokens_details") or {}
        if isinstance(details, dict) and "cached_tokens" in details:
            usage["cached_tokens"] = details["cached_tokens"]
        return LLMResponse(text=text, parsed=parsed, usage=usage, spec=dict(data.get("spec") or {}),
                           latency_s=time.perf_counter() - started)
