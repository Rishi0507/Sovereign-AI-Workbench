"""Schema-constrained model calls with bounded retries."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from workbench.core.errors import InvalidModelOutput
from workbench.llm.base import ChatMessage, LLMBackend, LLMRequest, LLMResponse
from workbench.llm.schemas import validation_errors

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def parse_json_text(text: str) -> Any:
    m = _FENCE.match(text)
    body = m.group(1) if m else text.strip()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        start, end = body.find("{"), body.rfind("}")
        if start != -1 and end > start:
            return json.loads(body[start : end + 1])
        raise


@dataclass
class StructuredResult:
    value: dict[str, Any]
    response: LLMResponse
    attempts: int
    errors: list[str] = field(default_factory=list)


def call(backend: LLMBackend, req: LLMRequest, schema: dict[str, Any], max_retries: int = 2) -> StructuredResult:
    """Validate the model output against ``schema``; retry with the validation error appended."""
    current = req.model_copy(update={"json_schema": schema})
    errors: list[str] = []
    raw_attempts: list[str] = []
    for attempt in range(max_retries + 1):
        resp = backend.chat(current)
        raw_attempts.append(resp.text)
        problem: str
        obj: Any = resp.parsed
        if obj is None:
            try:
                obj = parse_json_text(resp.text)
            except (json.JSONDecodeError, ValueError) as exc:
                obj = None
                problem = f"output is not valid JSON ({exc.__class__.__name__}: {exc})"
        if obj is not None:
            if not isinstance(obj, dict):
                problem = "output must be a JSON object"
            else:
                found = validation_errors(obj, schema)
                if not found:
                    return StructuredResult(obj, resp, attempt + 1, errors)
                problem = "; ".join(found)
        errors.append(problem)
        if attempt < max_retries:
            current = current.model_copy(update={
                "messages": [
                    *current.messages,
                    ChatMessage(role="assistant", content=resp.text[:4000]),
                    ChatMessage(role="user", content=(
                        "Your previous output failed validation: " + problem +
                        "\nReturn only one JSON object that matches the schema. No prose, no code fences."
                    )),
                ],
                "meta": {**current.meta, "retry": attempt + 1, "validation_error": problem},
            })
    raise InvalidModelOutput(
        f"{req.purpose}: output invalid after {max_retries + 1} attempts: {errors[-1]}", raw_attempts
    )
