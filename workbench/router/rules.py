"""Stage 1 rules: deterministic routing signals (README section 4.2.1)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from workbench.router.types import AttachmentInfo

CODE_EXTENSIONS = frozenset({".py", ".ipynb", ".js", ".ts", ".sql", ".sh"})
DOC_EXTENSIONS = frozenset({".pdf"})
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"})
TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\):|^\s*File \"[^\"]+\", line \d+", re.MULTILINE)
CODE_INTENT_RE = re.compile(r"\b(write|fix|debug)\b.*\b(script|code|python|function)\b", re.IGNORECASE | re.DOTALL)
DELIVERABLE_WORDS = {
    "docx": r"\b(word|docx|note|memo|letter|report)\b",
    "xlsx": r"\b(excel|xlsx|spreadsheet|workbook|sheet)\b",
    "pptx": r"\b(pptx|deck|slides?|presentation)\b",
}


@dataclass(frozen=True)
class RuleResult:
    route: str
    rule: str


def deliverable_kinds(text: str) -> set[str]:
    return {kind for kind, pattern in DELIVERABLE_WORDS.items() if re.search(pattern, text, re.IGNORECASE)}


def apply_rules(text: str, attachments: Sequence[AttachmentInfo]) -> RuleResult | None:
    """Return the route decided by a strong signal, or None so the classifier decides."""
    exts = {a.ext.lower() for a in attachments}
    if exts & CODE_EXTENSIONS:
        return RuleResult("code", "code_attachment")
    if TRACEBACK_RE.search(text):
        return RuleResult("code", "stack_trace")
    if CODE_INTENT_RE.search(text):
        return RuleResult("code", "code_intent")
    if len(attachments) >= 2 or len(deliverable_kinds(text)) >= 2:
        return RuleResult("agentic", "multi_file" if len(attachments) >= 2 else "multi_deliverable")
    if len(attachments) == 1:
        a = attachments[0]
        if a.ext.lower() in IMAGE_EXTENSIONS:
            return RuleResult("vision", "attachment:image")
        if a.ext.lower() in DOC_EXTENSIONS:
            return RuleResult("document", "attachment:pdf")
    return None
