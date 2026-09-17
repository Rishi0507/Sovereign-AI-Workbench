"""Conversational replies for the offline backend.

A message that is small talk, a question about the workbench, or a request without the document
it needs is answered with a short reply and concrete next steps instead of a search. The
suggestions only name files that exist in the workspace and that the user may read.
"""

from __future__ import annotations

import re
from typing import Any

GREETING = re.compile(r"^\s*(hi|hello|hey|hiya|good\s+(morning|afternoon|evening)|namaste)\b[\s!.,]*(there)?[\s!.]*$", re.I)
THANKS = re.compile(r"^\s*(thanks|thank\s+you|thx|great|cool|nice|perfect|awesome)\b[^?]*$", re.I)
HELP = re.compile(r"\b(what\s+can\s+you\s+do|help\b|how\s+do(es)?\s+(i|this|you)\s+(use|work)|who\s+are\s+you|what\s+are\s+you)", re.I)
VAGUE = re.compile(r"^\s*(continue|go\s+on|next|more|yes|yeah|yep|no|nope|ok|okay|sure|proceed|do\s+it|again)\b[\s!.]*$", re.I)
ACTIONS = [
    ("summarise", re.compile(r"\bsummar(i[sz]e|y)\b", re.I), (".pdf",)),
    ("compare", re.compile(r"\bcompare\b", re.I), (".pdf",)),
    ("draft an approval note for", re.compile(r"\bapproval\s+note\b", re.I), (".pdf",)),
    ("analyse", re.compile(r"\b(analy[sz]e|parse|anomal)", re.I), (".csv",)),
    ("read", re.compile(r"\b(read|review|check|count)\b", re.I), (".pdf", ".md", ".csv")),
]
CANONICAL = {
    "summarise": "Summarise this document",
    "draft an approval note for": "Draft an approval note for this inspection report",
    "analyse": "Write a Python script to parse these pressure readings and flag anomalies",
}
OFFTOPIC = re.compile(r"\b(weather|temperature outside|what time is it|today'?s date|tell me a joke|news|"
                      r"cricket|football|match|who won|how are you|your name|sing|recipe|movie)\b", re.I)
DOC_WORDS = re.compile(r"\b(document|report|file|contract|offer|data|sheet|readings|notes|pdf|it|this|that)\b", re.I)

# Anything that sounds like plant or document work is searched, whatever a small router model thinks.
DOMAIN = re.compile(r"\b(sop|procedure|clause|section|revision|drawing|spec|specification|pump|valve|motor|seal|"
                    r"bearing|vibration|pressure|flow|thickness|corrosion|inspection|maintenance|asset|register|"
                    r"report|contract|tender|vendor|offer|quotation|warranty|guarantee|damages|delivery|invoice|"
                    r"reading|readings|anomaly|anomalies|chart|table|page|word count|approval|note|draft|summary|"
                    r"[a-z]{1,3}-\d{2,4})\b", re.I)

CAPABILITIES = ("I can draft approval notes from inspection reports, summarise contracts, analyse sensor readings "
                "with a tested script, compare vendor offers, and answer questions from your procedures, "
                "with every figure traced to its source.")


def intent(text: str, attachments: list[str]) -> str | None:
    """``greeting``, ``thanks``, ``help``, ``vague``, ``needs:<action>`` or None for a real task."""
    if GREETING.search(text):
        return "greeting"
    if not attachments and OFFTOPIC.search(text) and not DOC_WORDS.search(text):
        return "offtopic"
    if HELP.search(text):
        return "help"
    if VAGUE.search(text):
        return "vague"
    if not attachments:
        for action, pattern, _exts in ACTIONS:
            if pattern.search(text) and (DOC_WORDS.search(text) or len(text.split()) <= 4):
                return f"needs:{action}"
    if THANKS.search(text) and len(text.split()) <= 5:
        return "thanks"
    return None


def _starters(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def find(pattern: str) -> dict[str, Any] | None:
        return next((f for f in files if re.search(pattern, f["name"], re.I)), None)

    out = []
    inspection = find(r"^inspection_.*\.pdf$")
    if inspection:
        out.append({"label": "Draft an approval note", "text": "Draft an approval note for this inspection report",
                    "attachments": [inspection["path"]]})
    contract = find(r"contract.*\.pdf$")
    if contract:
        out.append({"label": "Summarise the contract", "text": "Summarise this vendor contract",
                    "attachments": [contract["path"]]})
    readings = find(r"\.csv$")
    if readings:
        out.append({"label": f"Analyse {readings['name']}",
                    "text": "Write a Python script to parse these pressure readings and flag anomalies",
                    "attachments": [readings["path"]]})
    out.append({"label": "Ask about a procedure", "text": "Which pumps are governed by SOP-MECH-014?", "attachments": []})
    return out[:4]


def reply(text: str, kind: str, files: list[dict[str, Any]], history: list[dict[str, Any]]) -> dict[str, Any]:
    if kind == "greeting":
        return {"text": f"Hello! {CAPABILITIES} What would you like to start with?", "suggestions": _starters(files)}
    if kind == "thanks":
        return {"text": "You're welcome. Is there anything else I can prepare?", "suggestions": _starters(files)}
    if kind == "help":
        return {"text": f"{CAPABILITIES} Attach a file with the + button, or pick one of these.",
                "suggestions": _starters(files)}
    if kind == "offtopic":
        return {"text": "I work only with the documents and procedures on this machine, so I cannot answer that. "
                        "Tell me what you need from your files.", "suggestions": _starters(files)}
    if kind == "vague":
        if history:
            last = history[-1]["text"]
            return {"text": f"I'm not sure what to do next with \"{last}\". Tell me what you need, "
                            "or pick one of these.", "suggestions": _starters(files)}
        return {"text": f"Tell me what you need. {CAPABILITIES}", "suggestions": _starters(files)}
    action = kind.split(":", 1)[1] if kind.startswith("needs:") else "use"
    exts = next((e for a, _p, e in ACTIONS if a == action), (".pdf",))
    matching = [f for f in files if f["name"].lower().endswith(exts)]
    if action == "draft an approval note for":
        matching = [f for f in matching if f["name"].lower().startswith("inspection")] or matching
    request = CANONICAL.get(action, text)
    verb = action[0].upper() + action[1:]
    if not matching:
        return {"text": f"Which document should I {action}? Attach it with the + button and send your request again.",
                "suggestions": []}
    if action == "compare":
        offers = [f for f in matching if re.search(r"offer|tender|quot", f["name"], re.I)] or matching
        return {"text": "Which documents should I compare? Attach them with the + button, or use these.",
                "suggestions": [{"label": f"Compare {len(offers)} files",
                                 "text": "Compare these vendor offers against the tender conditions and recommend one",
                                 "attachments": [f["path"] for f in offers[:6]]}]}
    return {"text": f"Which document should I {action}? Pick one below, or attach another with the + button.",
            "suggestions": [{"label": f"{verb} {f['name']}", "text": request, "attachments": [f["path"]]}
                            for f in matching[:4]]}
