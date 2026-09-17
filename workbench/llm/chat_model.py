"""Conversational replies written by a real local model.

The deterministic backend stays in charge of plans, tool steps and documents. This wrapper sends
only ``chat.reply`` requests to a small instruct model on loopback (llama.cpp or vLLM) so greetings
and follow-up questions get a natural, varied answer. The suggestion buttons still come from the
workspace files the user may read, so the model never invents a file or a path. When the model
server is unreachable the deterministic reply is used and marked as such.
"""

from __future__ import annotations

import json
import random
import re
from typing import Any

from workbench.core.errors import ServiceUnavailable
from workbench.llm import conversation
from workbench.llm.base import ChatMessage, LLMBackend, LLMRequest, LLMResponse, last_user
from workbench.llm.openai_compat import OpenAICompatBackend

SYSTEM = (
    "You are the assistant inside Sovereign AI Workbench, an offline workbench for plant engineers. "
    "It drafts approval notes from inspection reports, summarises contracts, analyses sensor readings "
    "with a tested script, compares vendor offers and answers questions from the procedures library, "
    "with every figure traced to its source and every file approved by a person before it is final.\n"
    "Rules: reply in one to three short, friendly sentences of plain text. No lists, no markdown, no "
    "emoji. Never state facts, figures or contents of any document; you have not read them. If the "
    "user wants something done with a document, say what you can do and point to the buttons below "
    "your reply or the + button for attaching a file. Do not repeat earlier replies word for word. "
    "Never address the user by a name and never call them Sovereign AI Workbench."
)
MARKUP = re.compile(r"[*_`#]+")
HINTS = {
    "greeting": ("The user is greeting you. Greet them back warmly in your own words, without names or "
                 "the time of day, and ask what they would like to work on."),
    "thanks": "The user is thanking you. Acknowledge it in one short sentence and offer further help.",
    "help": "The user asks what you can do. Explain it briefly in your own words without naming buttons.",
    "vague": "The message is too vague to act on. Say so kindly and ask what they need.",
    "offtopic": ("The user asked about something outside this workbench, such as the weather or the news. "
                 "You have no internet, no sensors and no knowledge of the outside world, so say plainly that "
                 "you cannot know that. Never guess or invent an answer. Then ask what they need from their "
                 "files."),
}


class ConversationalBackend:
    """Routes ``chat.reply`` to a chat model and everything else to ``base``."""

    def __init__(self, base: LLMBackend, model: str, endpoint: str, timeout_s: float = 90.0,
                 max_tokens: int = 110, chat: OpenAICompatBackend | None = None) -> None:
        self.base = base
        self.name = base.name
        self.model = model
        self.max_tokens = max_tokens
        self.client = chat or OpenAICompatBackend(lambda _m: endpoint, cache_salt_mode="prefix", timeout_s=timeout_s)

    def __getattr__(self, item: str) -> Any:
        return getattr(self.base, item)

    def chat(self, req: LLMRequest) -> LLMResponse:
        if req.purpose != "chat.reply":
            return self.base.chat(req)
        fallback = self.base.chat(req)
        value = dict(fallback.parsed or json.loads(fallback.text))
        try:
            resp = self.client.chat(self.request(req, value))
        except ServiceUnavailable:
            value["note"] = "The local chat model is not running, so this is a standard reply."
            return LLMResponse(text=json.dumps(value, ensure_ascii=False), parsed=value)
        text = clean(resp.text)
        if not text:
            return fallback
        value["text"] = text
        value["model"] = self.model
        return LLMResponse(text=json.dumps(value, ensure_ascii=False), parsed=value, usage=resp.usage,
                           spec={"model": self.model}, latency_s=resp.latency_s)

    def request(self, req: LLMRequest, value: dict[str, Any]) -> LLMRequest:
        meta = req.meta
        messages = [ChatMessage(role="system", content=SYSTEM)]
        text = str(meta.get("task_text") or last_user(req))
        kind = conversation.intent(text, list(meta.get("attachments") or [])) or ""
        if kind in {"vague", ""}:
            # Only messages that lean on earlier turns carry them: a CPU reads prompts slowly.
            for turn in list(meta.get("history") or [])[-3:]:
                messages.append(ChatMessage(role="user", content=str(turn.get("text", ""))[:200]))
                if turn.get("reply"):
                    messages.append(ChatMessage(role="assistant", content=str(turn["reply"])[:200]))
        if kind.startswith("needs:"):
            action = kind.split(":", 1)[1]
            offered = ", ".join(s["label"].split(" ", 1)[-1] for s in value.get("suggestions") or []) or "none"
            hint = (f"The user wants you to {action} a document but did not say which. Ask them to pick one "
                    f"of the buttons below ({offered}) or attach a file with the + button.")
        else:
            hint = HINTS.get(kind, "Answer briefly and helpfully.")
        messages.append(ChatMessage(role="user", content=f"{text}\n\n(Note for you, not from the user: {hint})"))
        # Warmth helps a greeting, precision helps everything else.
        temperature = 0.9 if kind in {"greeting", "thanks"} else 0.5
        return LLMRequest(model=self.model, purpose="chat.reply", messages=messages, temperature=temperature,
                          seed=random.randint(1, 2**31 - 1), max_tokens=self.max_tokens, task_id=req.task_id)


def clean(text: str) -> str:
    """Plain prose: markup stripped, cut at the last full sentence, at most four sentences."""
    text = re.sub(r"\s*" + chr(0x2014) + r"\s*", ", ", MARKUP.sub("", text)).strip().strip('"').strip()
    sentences = re.findall(r"[^.!?]+[.!?]+", text)
    if not sentences:
        return text
    return " ".join(s.strip() for s in sentences[:4])
