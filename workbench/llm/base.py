"""LLM backend protocol and message types (PRD section 4.2)."""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None


class LLMRequest(BaseModel):
    model: str
    purpose: str
    messages: list[ChatMessage]
    json_schema: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    temperature: float = 0.0
    seed: int = 7
    max_tokens: int = 1024
    cache_salt: str | None = None
    images: list[bytes] = Field(default_factory=list)
    context_records: list[str] = Field(default_factory=list)
    task_id: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    text: str
    parsed: dict[str, Any] | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    spec: dict[str, Any] = Field(default_factory=dict)
    latency_s: float = 0.0


class LLMBackend(Protocol):
    name: str

    def chat(self, req: LLMRequest) -> LLMResponse: ...


def last_user(req: LLMRequest) -> str:
    for m in reversed(req.messages):
        if m.role == "user":
            return m.content
    return ""


def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)
