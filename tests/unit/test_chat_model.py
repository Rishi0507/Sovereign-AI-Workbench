"""The chat model writes conversational replies; everything else stays with the base backend."""

from __future__ import annotations

import json
from typing import Any

import httpx

from workbench.llm.base import ChatMessage, LLMRequest, LLMResponse
from workbench.llm.chat_model import ConversationalBackend, clean
from workbench.llm.openai_compat import OpenAICompatBackend

SUGGESTIONS = [{"label": "Summarise the contract", "text": "Summarise this vendor contract",
                "attachments": ["inputs/vendor_contract.pdf"]}]


class Base:
    name = "heuristic"

    def __init__(self) -> None:
        self.purposes: list[str] = []
        self.seen: list[LLMRequest] = []

    def chat(self, req: LLMRequest) -> LLMResponse:
        self.purposes.append(req.purpose)
        self.seen.append(req)
        value = {"text": "Hello! Fixed reply.", "suggestions": SUGGESTIONS}
        return LLMResponse(text=json.dumps(value), parsed=value)


def backend(handler: Any) -> tuple[ConversationalBackend, Base]:
    base = Base()
    client = OpenAICompatBackend(lambda _m: "http://127.0.0.1:8010/v1", transport=httpx.MockTransport(handler))
    return ConversationalBackend(base, "qwen2.5-1.5b-instruct", "http://127.0.0.1:8010/v1", chat=client), base


def request(purpose: str = "chat.reply", **meta: Any) -> LLMRequest:
    return LLMRequest(model="m", purpose=purpose, messages=[ChatMessage(role="user", content="hi")],
                      meta={"task_text": "hi there", "files": [{"name": "vendor_contract.pdf",
                                                                "path": "inputs/vendor_contract.pdf"}], **meta})


def test_reply_text_comes_from_the_model_and_buttons_from_the_workspace() -> None:
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "**Hi!** Want me to summarise the "
                                                                             "contract? Pick a button"}}]})

    be, base = backend(handler)
    history = [{"text": "summarise any document", "reply": "Which document should I summarise?"}]
    value = be.chat(request(task_text="continue", history=history)).parsed
    assert value == {"text": "Hi! Want me to summarise the contract?", "suggestions": SUGGESTIONS,
                     "model": "qwen2.5-1.5b-instruct"}
    body = seen[0]
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    assert body["messages"][-1]["content"].startswith("continue")
    assert "too vague" in body["messages"][-1]["content"]
    # A greeting does not need the earlier turns, so the prompt stays short.
    be.chat(request(history=history))
    assert [m["role"] for m in seen[1]["messages"]] == ["system", "user"]
    assert "greeting you" in seen[1]["messages"][-1]["content"]
    assert body["temperature"] > 0 and "response_format" not in body
    assert base.purposes == ["chat.reply", "chat.reply"]


def test_a_request_without_a_file_names_the_offered_files() -> None:
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "Which one?"}}]})

    be, _ = backend(handler)
    be.chat(request(task_text="summarise any document"))
    note = seen[0]["messages"][-1]["content"]
    assert "wants you to summarise a document" in note and "(the contract)" in note


def test_other_purposes_never_reach_the_chat_model() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("chat model called")

    be, base = backend(handler)
    be.chat(request("plan.write"))
    assert base.purposes == ["plan.write"]


def test_unreachable_model_falls_back_with_a_note() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    be, _ = backend(handler)
    value = be.chat(request()).parsed or {}
    assert value["text"] == "Hello! Fixed reply." and "model" not in value
    assert "not running" in value["note"]


def test_clean_keeps_whole_sentences_only() -> None:
    assert clean('"Sure. I can help with that. Just pick"') == "Sure. I can help with that."
    assert clean("No punctuation at all") == "No punctuation at all"
    assert clean("One. Two. Three. Four. Five.") == "One. Two. Three. Four."


def test_questions_outside_the_workbench_are_not_searched() -> None:
    from workbench.llm import conversation

    assert conversation.intent("hi how is the weather today", []) == "offtopic"
    assert conversation.intent("tell me a joke", []) == "offtopic"
    # A question about a document stays a document question, even with an everyday word in it.
    assert conversation.intent("What does the report say about the news of delays", []) is None
    assert conversation.intent("does this match the tender conditions", []) is None
    text = conversation.reply("how are you?", "offtopic", [], [])["text"]
    assert "documents and procedures on this machine" in text


def test_a_message_that_needs_no_documents_is_planned_as_a_reply() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        answer = "CHAT" if "12 times 8" in body["messages"][-1]["content"] else "DOCS"
        return httpx.Response(200, json={"choices": [{"message": {"content": answer}}]})

    be, base = backend(handler)
    plan = request("plan.write", task_text="what is 12 times 8")
    be.chat(plan)
    assert plan.meta.get("force_chat") is None  # the request itself is not mutated
    assert base.seen[-1].meta["force_chat"] is True

    be.chat(request("plan.write", task_text="what did the inspector find"))
    assert "force_chat" not in base.seen[-1].meta


def test_a_message_naming_plant_work_is_never_sent_to_the_router() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("router called")

    be, base = backend(handler)
    be.chat(request("plan.write", task_text="Which pumps are governed by SOP-MECH-014?"))
    assert "force_chat" not in base.seen[-1].meta
