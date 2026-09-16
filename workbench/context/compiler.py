"""Context compiler: the prompt is compiled from the ledger in a fixed order (README section 4.1.3).

Order: (1) system prompt, (2) tool schemas, (3) plan with step statuses, (4) records in ledger
order, (5) the step instruction. Control records render as plain text; data records render as
quoted ``<record>`` blocks. Within a step the message list only grows at the end; older records
are shown as summaries (one ``recall`` away) and that only changes at step boundaries.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field

from workbench.core.ids import sha256_text
from workbench.core.labels import Label
from workbench.core.models import LedgerRecord
from workbench.llm.base import ChatMessage
from workbench.llm.prompts import render_prompt
from workbench.planning.plan_schema import Plan


ALWAYS_FULL = frozenset({"model_output", "check_result", "calc_result", "graph_fact"})


@dataclass
class CompiledContext:
    messages: list[ChatMessage]
    prefix_hash: str
    record_ids: list[str]
    salt: str
    extra: list[ChatMessage] = field(default_factory=list)

    def all_messages(self) -> list[ChatMessage]:
        return [*self.messages, *self.extra]

    def prompt_text(self) -> str:
        return "\n".join(f"{m.role}: {m.content}" for m in self.all_messages())

    def append(self, *messages: ChatMessage) -> None:
        self.extra.extend(messages)


def render_record(rec: LedgerRecord, full: bool, inline_chars: int) -> str:
    if rec.trust == "control":
        body = rec.body if isinstance(rec.body, str) else json.dumps(rec.body, ensure_ascii=False, sort_keys=True)
        return f"[{rec.id}] {rec.kind}: {body}"
    text = rec.body_text()
    attrs = (f'id="{rec.id}" kind="{rec.kind}" source="{html.escape(rec.source(), quote=True)}" '
             f'label="{rec.label.display()}"')
    if rec.confidence:
        attrs += f' confidence="{rec.confidence}"'
    if full and (len(text) <= inline_chars or rec.kind in ALWAYS_FULL):
        content = text
    else:
        content = f"{rec.summary}\n(use recall(\"{rec.id}\") for full text)"
    content = content.replace("</record>", "&lt;/record&gt;")
    return f"<record {attrs}>\n{content}\n</record>"


def plan_view(plan: Plan | None, current: str | None) -> str:
    if plan is None:
        return "Plan: not yet approved."
    lines = ["Approved plan (step status):"]
    for i, s in enumerate(plan.steps, start=1):
        marker = "->" if s.id == current else "  "
        lines.append(f"{marker} {i}. {s.id} [{s.status}] {s.action}"
                     + (f" inputs={','.join(s.step_refs())}" if s.step_refs() else ""))
    return "\n".join(lines)


def compile_context(
    *,
    workspace: str,
    label: Label,
    salt: str,
    salt_mode: str,
    tool_schemas: str,
    plan: Plan | None,
    step_id: str | None,
    records: list[LedgerRecord],
    focus: set[str],
    recalled: set[str],
    instruction: str,
    inline_chars: int,
) -> CompiledContext:
    system = render_prompt("agent_system", workspace=workspace, label=label.display())
    if salt_mode == "prefix":
        system = f"[salt:{salt}]\n{system}"
    messages = [
        ChatMessage(role="system", content=system),
        ChatMessage(role="system", content="Tools (JSON schemas):\n" + tool_schemas),
        ChatMessage(role="system", content=plan_view(plan, step_id)),
    ]
    blocks = []
    included = []
    for rec in records:
        if rec.trust == "control" or rec.id in focus or rec.id in recalled:
            blocks.append(render_record(rec, full=rec.id in focus or rec.id in recalled or rec.trust == "control",
                                        inline_chars=inline_chars if rec.id not in recalled else 10**9))
            included.append(rec.id)
    messages.append(ChatMessage(role="user", content="Evidence ledger:\n" + ("\n".join(blocks) or "(empty)")))
    prefix_hash = sha256_text(salt + "\n".join(m.content for m in messages))
    messages.append(ChatMessage(role="user", content=instruction))
    return CompiledContext(messages=messages, prefix_hash=prefix_hash, record_ids=included, salt=salt)
