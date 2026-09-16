"""Stage 1 classifier: a small CPU model returns a constrained task profile."""

from __future__ import annotations

from typing import Any

from workbench.llm import structured
from workbench.llm.base import ChatMessage, LLMBackend, LLMRequest
from workbench.llm.prompts import render_prompt
from workbench.llm.schemas import load_schema
from workbench.router.types import TaskInput


def classify(backend: LLMBackend, model: str, task: TaskInput, max_retries: int = 2) -> dict[str, Any]:
    schema = load_schema("task_profile")
    attachments = [
        {"name": a.name, "type": a.ext, "pages": a.pages, "scanned": a.scanned, "needs_visual": a.needs_visual}
        for a in task.attachments
    ]
    req = LLMRequest(
        model=model,
        purpose="route.classify",
        messages=[
            ChatMessage(role="system", content=render_prompt("route_classify")),
            ChatMessage(role="user", content=render_prompt("route_classify_user", text=task.text,
                                                            attachments=attachments)),
        ],
        max_tokens=128,
        task_id=task.id,
        meta={"attachments": attachments},
    )
    return structured.call(backend, req, schema, max_retries=max_retries).value
