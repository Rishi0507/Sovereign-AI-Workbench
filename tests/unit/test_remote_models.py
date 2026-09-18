"""Hosted models: name mapping, pacing, waiting out a refusal, and the allowance report."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest

from workbench.core.errors import RateLimited
from workbench.llm.base import ChatMessage, LLMRequest, LLMResponse
from workbench.llm.remote import Pacing, RemoteBackend, RemoteConfig, Throttle
from workbench.llm.select import BackendSelector

CONFIG = RemoteConfig(base_url="https://api.example.com/openai/v1", key_env="TEST_MODEL_KEY",
                      models={"gpt-oss-20b": "openai/gpt-oss-20b"}, default="llama-3.3-70b-versatile",
                      chat="llama-3.1-8b-instant",
                      pacing=Pacing(min_interval_s=0.0, max_concurrency=1, max_retries=3, max_wait_s=1.0))


def answer(content: str = "hello", headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": {}},
                          headers=headers or {})


def request(model: str = "gpt-oss-20b") -> LLMRequest:
    return LLMRequest(model=model, purpose="plan.write", messages=[ChatMessage(role="user", content="hi")])


def backend(handler: Any, **kwargs: Any) -> RemoteBackend:
    return RemoteBackend(CONFIG, transport=httpx.MockTransport(handler), **kwargs)


def test_workbench_model_names_map_to_hosted_ones() -> None:
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append({**json.loads(req.content), "auth": req.headers.get("authorization")})
        return answer()

    be = backend(handler)
    be.chat(request("gpt-oss-20b"))
    be.chat(request("granite-3.3-8b"))       # not mapped, so the default answers
    be.chat(request("llama-3.1-8b-instant"))  # already a hosted name, so it is kept
    assert [s["model"] for s in seen] == ["openai/gpt-oss-20b", "llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
    assert seen[0]["auth"] is None  # no key set in the environment, so no header is sent


def test_the_key_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "secret-value")
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers.get("authorization") or "")
        return answer()

    backend(handler).chat(request())
    assert seen == ["Bearer secret-value"]


def test_calls_are_spaced_out_and_never_run_in_parallel() -> None:
    throttle = Throttle(Pacing(min_interval_s=0.2, max_concurrency=1))
    started = time.monotonic()
    for _ in range(3):
        with throttle:
            pass
    assert time.monotonic() - started >= 0.4


def test_a_refusal_is_waited_out_then_given_up_on() -> None:
    calls: list[float] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(time.monotonic())
        return httpx.Response(429, json={"error": "rate limit"}, headers={"retry-after": "0.05"})

    be = backend(handler)
    with pytest.raises(RateLimited):
        be.chat(request())
    assert len(calls) > 1  # it tried again rather than failing at once
    assert be.usage.snapshot()[0]["rate_limited"] >= 1


def test_the_allowance_is_reported_per_model() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return answer(headers={"x-ratelimit-limit-requests": "1000", "x-ratelimit-remaining-requests": "998",
                               "x-ratelimit-remaining-tokens": "17500", "x-ratelimit-reset-requests": "2m59s"})

    be = backend(handler)
    be.chat(request())
    entry = be.usage.snapshot()[0]
    assert entry["model"] == "openai/gpt-oss-20b"
    assert entry["remaining_requests"] == "998" and entry["limit_requests"] == "1000"
    assert entry["reset_requests"] == "2m59s" and entry["calls"] == 1


class Offline:
    name = "heuristic"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, req: LLMRequest) -> LLMResponse:
        self.calls += 1
        return LLMResponse(text="offline answer")


def test_the_rules_answer_when_asked_for_and_when_the_service_gives_up() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limit"}, headers={"retry-after": "0.01"})

    offline = Offline()
    selector = BackendSelector(backend(handler), offline)
    assert selector.chat(request()).text == "offline answer"   # service exhausted
    assert selector.fallbacks == 1

    quiet = Offline()
    never = BackendSelector(backend(lambda req: answer("from the model")), quiet)
    asked = request()
    asked.meta["offline"] = True
    assert never.chat(asked).text == "offline answer"          # asked for the rules directly
    assert quiet.calls == 1
    assert never.chat(request()).text == "from the model"


def test_a_model_that_refuses_strict_schemas_is_asked_for_plain_json() -> None:
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body)
        if (body.get("response_format") or {}).get("type") == "json_schema":
            return httpx.Response(400, json={"error": {"message": "response_format json_schema is not supported"}})
        return answer('{"ok": true}')

    be = backend(handler)
    asking = request()
    asking.json_schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    assert be.chat(asking).text == '{"ok": true}'
    assert [(b.get("response_format") or {}).get("type") for b in seen] == ["json_schema", "json_object"]
    assert "matching this schema" in seen[1]["messages"][-1]["content"]

    # The refusal is remembered, so the next call asks for plain JSON straight away.
    be.chat(asking)
    assert (seen[2].get("response_format") or {}).get("type") == "json_object"
