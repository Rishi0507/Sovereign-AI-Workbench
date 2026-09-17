"""Deterministic offline backend (PRD section 4.2).

It implements every model purpose with rules over the ledger records named in the request, so
the whole system runs and is testable without a GPU. It reads records through a read-only ledger
handle; it never sees anything the orchestrator did not put in ``context_records`` or ``meta``.

Two behaviours are deliberately naive, as the PRD asks, so the recovery paths are exercised:
the first ``plan.write`` for an offer comparison assumes a full read also yields tables (the
compiler rejects it and ``plan.repair`` fixes it from the error text), and the first
``code.write`` guesses a column name from the request wording (the sandbox fails with
``KeyError`` and ``code.fix`` picks the real column from the CSV header).
"""

from __future__ import annotations

import difflib
import itertools
import json
import math
import re
from typing import Any

from workbench.core.ledger import Ledger
from workbench.core.models import LedgerRecord
from workbench.core.normalise import (
    DATE_RE,
    find_quantities,
    find_tags,
    norm_tag,
    tokens,
)
from workbench.documents.readers import TEXT_EXTS
from workbench.kb.rerank import LexicalReranker
from workbench.llm.base import LLMRequest, LLMResponse, last_user

CODE_WORDS = re.compile(r"\b(script|python|code|function|debug|traceback|program|sql|query)\b", re.I)
COMPARE_WORDS = re.compile(r"\b(compare|comparison|evaluate|rank|recommend|versus|vs\.?)\b", re.I)
SUMMARY_WORDS = re.compile(r"\b(summari[sz]e|summary|overview|digest)\b", re.I)
QUESTION_WORDS = re.compile(r"^\s*(what|which|when|who|how|why|is|are|does|do|can|list)\b|\?\s*$", re.I)
CALC_WORDS = re.compile(r"\b(calculate|calculation|compute|thickness for|required thickness)\b", re.I)
REASON_WORDS = re.compile(r"\b(prove|derive|root cause|why did|trade-?off|reason about)\b", re.I)
APPROVAL_WORDS = re.compile(r"\bapproval\b", re.I)
CODE_CONTEXT = re.compile(r"\b(traceback|error|sql|query|script|python|shell|code)\b", re.I)
GENERAL_VERBS = re.compile(r"\b(draft|write|prepare|create|turn|translate|search|find|list|explain|what|which|"
                           r"who|when|calculate|compute|convert|describe|hello)\b", re.I)
KEY_TERMS = ("price", "payment", "deliver", "warranty", "liquidated", "damages", "guarantee", "terminat",
             "force majeure", "confidential", "arbitration", "governing", "insur", "spares", "security",
             "tax", "training", "inspection", "scope")
GOVERNED_Q = re.compile(r"\b(?:govern(?:ed|s)?|appl(?:y|ies))\b.*?\b(?P<doc>[A-Z]{2,}(?:-[A-Z0-9]+){1,3})\b")
QUESTION_FILLER = frozenset({"what", "which", "who", "when", "where", "how", "why", "does", "do", "did", "say",
                             "says", "about", "tell", "me", "please", "there", "any"})
NUMBERED_HEADING = re.compile(r"^\d+(\.\d+)*\.?\s+\w")
SIGNAL = re.compile(r"\d|shall|must|liable|warrant|payable", re.I)


def _json(obj: Any) -> LLMResponse:
    return LLMResponse(text=json.dumps(obj, ensure_ascii=False), parsed=obj)


def _sentences(text: str) -> list[str]:
    """Sentences of a passage; markdown headings and table rows are not prose and are dropped."""
    lines = []
    for ln in text.splitlines():
        if ln.lstrip().startswith(("#", "|")):
            continue
        # a short title line is its own paragraph, so it does not run into the first sentence
        lines += ["", ln, ""] if _is_heading(ln) else [ln]
    out = []
    for para in re.split(r"\n\s*\n", "\n".join(lines)):
        flat = " ".join(para.split())
        out += [s.strip() for s in re.split(r"(?<=[.;])\s+(?=[A-Z])", flat) if s.strip()]
    return out


def _is_heading(line: str) -> bool:
    """A short line without a closing full stop, such as ``7. Warranty`` or ``CONFIDENTIAL``."""
    words = line.split()
    return 0 < len(words) <= 6 and not line.rstrip().endswith((".", ";", ",")) and ":" not in line


def _table_facts(text: str) -> list[str]:
    """Markdown table rows as short statements: ``| Design pressure | P | 4.5 MPa |`` gives
    ``Design pressure (P) is 4.5 MPa.``"""
    out = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or not line.lstrip().startswith("|") or set("".join(cells)) <= set("-: "):
            continue
        name, value = cells[0], cells[-1]
        if not re.search(r"\d", value):
            continue
        middle = f" ({cells[1]})" if len(cells) == 3 and cells[1] else ""
        out.append(f"{name}{middle} is {value}.")
    return out


def _cite(text: str, *ids: str | None) -> str:
    refs = [i for i in dict.fromkeys(ids) if i]
    text = text.rstrip()
    if not refs:
        return text
    marker = f"[{', '.join(refs)}]"
    if text.endswith("."):
        return f"{text[:-1]} {marker}."
    return f"{text} {marker}"


def _fmt(v: float) -> str:
    return f"{v:g}" if abs(v - round(v, 1)) > 1e-9 else f"{v:.1f}"


def rank_statements(question: str, recs: list[LedgerRecord]) -> list[tuple[float, str, LedgerRecord]]:
    """Score every sentence and table fact of ``recs`` against ``question``, best first.

    Terms are weighted by inverse frequency across the candidate statements, so words that occur
    everywhere (``contract`` in a contract) count for little; boilerplate repeated in three or more
    passages is demoted; a named equipment tag, a matching section heading and the user's own
    attachment each raise a statement.
    """
    q_list = [t for t in tokens(question) if t not in QUESTION_FILLER]
    q_tags = {norm_tag(t) for t in find_tags(question)}
    phrases = [f"{a} {b}" for a, b in itertools.pairwise(q_list)]
    units: dict[str, list[tuple[str | None, str]]] = {}
    for r in recs:
        if r.kind == "graph_fact":
            units[r.id] = [(None, r.summary)]
            continue
        units[r.id] = [(head, u) for head, body in _sections(r.body_text())
                       for u in [*_sentences(body), *_table_facts(body)]
                       if len(u.split()) >= 3 and not _is_heading(u)]
    all_units = [u for us in units.values() for _h, u in us]
    spread: dict[str, int] = {}
    for us in units.values():
        for u in {u for _h, u in us}:
            spread[u] = spread.get(u, 0) + 1
    common = [u for u, n in spread.items() if n >= 3 and len(u) >= 30]
    df = {t: sum(1 for u in all_units if t in set(tokens(u))) for t in set(q_list)}
    weight = {t: math.log(1 + (len(all_units) + 1) / (df[t] + 1)) for t in df}
    total = sum(weight.values()) or 1.0
    out: list[tuple[float, str, LedgerRecord]] = []
    for r in recs:
        own = 0.15 if r.kind == "ocr_text" else 0.0
        for heading, sent in units[r.id]:
            context = set(tokens(heading)) if heading and "continued" not in heading.lower() else set()
            words = set(tokens(sent))
            low = sent.lower()
            score = sum(weight[t] for t in weight if t in words or t in context) / total
            if any(c in sent for c in common):
                score -= 0.4
            if re.search(r"\d", sent):
                score += 0.1
            if len(sent.split()) <= 25:
                score += 0.05
            if any(p in low for p in phrases):
                score += 0.1
            text = sent
            if heading and context & set(weight):
                score += 0.1  # the section is about what the question asks
                if heading.lower() not in low and NUMBERED_HEADING.match(heading):
                    text = f"{heading.rstrip('.:')}: {sent}"
            if q_tags:
                # a question about named equipment is answered by statements about that equipment
                if q_tags & {norm_tag(t) for t in find_tags(sent)}:
                    score += 0.3
                else:
                    score *= 0.5
            out.append((score + own, text, r))
    out.sort(key=lambda x: (-x[0], x[2].seq))
    return out


def _sections(text: str) -> list[tuple[str | None, str]]:
    """Split a passage at markdown or numbered title lines into ``(heading, body)`` pairs."""
    out: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in text.splitlines():
        title = line.strip().lstrip("#").strip()
        if line.lstrip().startswith("#") or (_is_heading(title) and NUMBERED_HEADING.match(title)):
            out.append((title, []))
        else:
            out[-1][1].append(line)
    return [(head, "\n".join(body)) for head, body in out if any(b.strip() for b in body)]


class HeuristicBackend:
    name = "heuristic"

    def __init__(self, ledger: Ledger | None = None) -> None:
        self.ledger = ledger
        self.reranker = LexicalReranker()
        self.calls: list[str] = []

    # ------------------------------------------------------------------------------------------

    def _records(self, req: LLMRequest) -> list[LedgerRecord]:
        if self.ledger is None:
            return []
        recs = [self.ledger.maybe(i) for i in req.context_records]
        found = [r for r in recs if r is not None]
        if req.task_id:
            for r in self.ledger.for_task(req.task_id):
                if r.trust == "control" and r.id not in {x.id for x in found}:
                    found.append(r)
        return sorted(found, key=lambda r: (r.task_id, r.seq))

    def chat(self, req: LLMRequest) -> LLMResponse:
        self.calls.append(req.purpose)
        handler = getattr(self, "_" + req.purpose.replace(".", "_"), None)
        if handler is None:
            return LLMResponse(text="{}", parsed={})
        out: LLMResponse = handler(req)
        return out

    # -- routing ---------------------------------------------------------------------------

    def _route_classify(self, req: LLMRequest) -> LLMResponse:
        text = last_user(req)
        task = text.split("Attachments:")[0].replace("Task:", "").strip()
        atts = req.meta.get("attachments") or []
        words = len(task.split())
        visual = any(a.get("needs_visual") or a.get("scanned") for a in atts)
        confidence = 0.82
        if CODE_WORDS.search(task) and re.search(r"\b(write|fix|debug|script)\b", task, re.I):
            task_type = "code"
        elif COMPARE_WORDS.search(task) or len(atts) >= 3:
            task_type = "agentic"
        elif REASON_WORDS.search(task):
            task_type = "reasoning"
        elif atts and SUMMARY_WORDS.search(task):
            task_type = "document"
        elif any(a.get("scanned") for a in atts):
            task_type = "vision"
        elif CALC_WORDS.search(task) or APPROVAL_WORDS.search(task) or re.search(r"\b(draft|report|note)\b", task, re.I):
            task_type = "general"
        else:
            task_type = "general"
            confidence = 0.75 if GENERAL_VERBS.search(task) else 0.55
        from workbench.router.rules import deliverable_kinds

        code_like = task_type == "code" or bool(CODE_WORDS.search(task) and CODE_CONTEXT.search(task))
        if COMPARE_WORDS.search(task) and (re.search(r"\brecommend", task, re.I) or len(atts) >= 3):
            complexity = "high"
        elif len(deliverable_kinds(task)) >= 2 or len(atts) >= 2:
            complexity = "medium"
        elif (code_like and len(atts) <= 1) or (words <= 14 and not atts):
            complexity = "low"
        else:
            complexity = "medium"
        needs_tools = bool(atts) or bool(re.search(r"\b(draft|write|calculate|compute|compare|file|report|search)\b",
                                                   task, re.I))
        text_only = bool(COMPARE_WORDS.search(task) or SUMMARY_WORDS.search(task) or CALC_WORDS.search(task)
                         or task_type == "code")
        needs_visual = visual and not text_only
        return _json({"task_type": task_type, "needs_tools": needs_tools, "complexity": complexity,
                      "needs_visual_after_extract": needs_visual, "confidence": confidence})

    # -- planning --------------------------------------------------------------------------

    @staticmethod
    def _step(sid: str, tool: str | None = None, task: str | None = None, args: dict[str, Any] | None = None,
              inputs: list[tuple[str, str]] | None = None, out: str = "records", side: bool = False,
              title: str = "") -> dict[str, Any]:
        return {"id": sid, "title": title, "action": {"tool": tool} if tool else {"model_task": task},
                "args": args or {}, "inputs": [{"from": f, "ref": r} for f, r in (inputs or [])],
                "output_type": out, "side_effect": side}

    def _plan_write(self, req: LLMRequest) -> LLMResponse:
        text = str(req.meta.get("task_text", ""))
        atts: list[str] = list(req.meta.get("attachments") or [])
        docs = [a for a in atts if a.lower().endswith((".pdf", ".ocr.json"))]
        s = self._step
        route = req.meta.get("route")
        if route == "code" or (CODE_WORDS.search(text) and re.search(r"\b(write|fix|debug)\b", text, re.I)):
            steps = [s("code", task="write_code", inputs=[("attachment", a) for a in atts], out="code", side=True,
                       title="Write the script and test it safely")]
            return _json({"goal": text, "steps": steps, "deliverables": [{"type": "code"}]})
        if COMPARE_WORDS.search(text) and len(docs) >= 2:
            steps = [
                s("read", "read_document", inputs=[("attachment", a) for a in docs], out="document",
                  title="Read the offers and the tender conditions"),
                s("compare", task="compare_offers", inputs=[("step", "read")], out="comparison",
                  title="Compare the offers"),
                s("score", "run_python", inputs=[("step", "tables"), ("step", "compare")], out="sandbox_result",
                  title="Score the offers"),
                s("note", task="draft_recommendation", inputs=[("step", "compare"), ("step", "score")], out="note",
                  title="Write the recommendation"),
                s("sheet", "make_xlsx", inputs=[("step", "compare")], out="file", side=True,
                  title="Create the comparison spreadsheet"),
                s("doc", "make_docx", args={"template": "recommendation_note", "data": "{note}",
                                            "name": "recommendation-note.docx"},
                  inputs=[("step", "note")], out="file", side=True, title="Create the recommendation document"),
            ]
            return _json({"goal": text, "steps": steps,
                          "deliverables": [{"type": "xlsx"}, {"type": "docx"}]})
        if req.meta.get("parent_id") or (QUESTION_WORDS.search(text) and not SUMMARY_WORDS.search(text)):
            governed = GOVERNED_Q.search(text)
            if governed and not atts:
                steps = [
                    s("graph", "graph_lookup", args={"doc": governed.group("doc")}, out="graph_facts",
                      title="Look up the equipment it covers"),
                    s("answer", task="answer_question", inputs=[("step", "graph")], out="answer",
                      title="Answer with sources"),
                ]
                return _json({"goal": text, "steps": steps, "deliverables": []})
            readable = docs or [a for a in atts if a.lower().endswith(tuple(TEXT_EXTS))]
            doc = readable[0].rsplit("/", 1)[-1] if readable else None
            steps = []
            refs = []
            if readable:
                steps.append(s("read", "read_document", inputs=[("attachment", a) for a in readable],
                               out="document", title="Read the attachment"))
                refs = [("step", "read")]
            steps += [
                s("search", "search_kb", args={"queries": [text], "top_k": 6, "doc": doc},
                  inputs=refs, out="kb_passages", title="Search the procedures"),
                s("answer", task="answer_question", inputs=[("step", "search"), *refs], out="answer",
                  title="Answer with sources"),
            ]
            return _json({"goal": text, "steps": steps, "deliverables": []})
        if SUMMARY_WORDS.search(text) and docs:
            steps = [s("read", "read_document", inputs=[("attachment", docs[0])], out="document"),
                     s("summarise", task="summarise_document", inputs=[("step", "read")], out="summary",
                       title="Summarise each section"),
                     s("render", "make_docx", args={"template": "summary", "data": "{summarise}"},
                       inputs=[("step", "summarise")], out="file", side=True)]
            return _json({"goal": text, "steps": steps, "deliverables": [{"type": "docx"}]})
        if APPROVAL_WORDS.search(text) and docs:
            steps = [
                s("read", "read_document", inputs=[("attachment", docs[0])], out="document", title="Read the report"),
                s("extract", task="extract_findings", inputs=[("step", "read")], out="findings",
                  title="Pick out the findings"),
                s("ground", "search_kb", args={"queries": ["approval note structure and evidence",
                                                           "minimum casing wall thickness acceptance criteria",
                                                           "past approval notes {equipment_tag}"],
                                               "top_k": 8, "as_of": "{report_date}", "tags": ["{equipment_tag}"]},
                  inputs=[("step", "extract")], out="kb_passages", title="Look up the procedure and past readings"),
                s("check", "check_consistency", inputs=[("step", "extract"), ("step", "ground")],
                  out="check_results", title="Check the facts"),
                s("draft", task="draft_sections", inputs=[("step", "extract"), ("step", "ground"), ("step", "check")],
                  out="note", title="Write the note with sources"),
                s("render", "make_docx", inputs=[("step", "draft")], out="file", side=True, title="Create the Word document"),
            ]
            return _json({"goal": text, "steps": steps, "deliverables": [{"type": "docx"}]})
        steps = []
        refs: list[tuple[str, str]] = []
        if atts:
            tool = "read_document" if docs else "read_file"
            steps.append(s("read", tool, args={} if docs else {"path": atts[0]},
                           inputs=[("attachment", a) for a in (docs or atts[:1])], out="document"))
            refs = [("step", "read")]
        steps.append(s("search", "search_kb", args={"queries": [text], "top_k": 6}, inputs=refs, out="kb_passages"))
        steps.append(s("answer", task="answer_question", inputs=[("step", "search"), *refs], out="answer"))
        return _json({"goal": text, "steps": steps, "deliverables": []})

    def _plan_repair(self, req: LLMRequest) -> LLMResponse:
        plan = json.loads(json.dumps(req.meta.get("plan") or {"steps": [], "deliverables": []}))
        steps: list[dict[str, Any]] = plan["steps"]
        for err in req.meta.get("errors") or []:
            m = re.match(r'step (\d+): input "(\w+)" is not produced by any earlier step', err)
            if m:
                idx, ref = int(m.group(1)) - 1, m.group(2)
                if ref == "tables":
                    readers = [st for st in steps[:idx] if st["action"].get("tool") == "read_document"]
                    inputs = readers[0]["inputs"] if readers else []
                    new = self._step("tables", "read_document", inputs=[
                        (i["from"], i["ref"]) for i in inputs], out="tables", title="Pull out the price tables")
                    insert_at = next((i for i, st in enumerate(steps) if any(
                        x["ref"] in {r["id"] for r in readers} for x in st["inputs"])), idx)
                    steps.insert(insert_at, new)
                    for st in steps:
                        if st["action"].get("model_task") == "compare_offers":
                            st["inputs"] = [{"from": "step", "ref": "tables"}]
                else:
                    steps[idx]["inputs"] = [i for i in steps[idx]["inputs"] if i["ref"] != ref]
                continue
            m = re.match(r'step (\d+): unknown (?:tool|model task) "([\w-]+)"', err)
            if m:
                steps[int(m.group(1)) - 1]["_drop"] = True
                continue
            m = re.match(r'deliverable "(\w+)" has no renderer step', err)
            if m:
                kind = m.group(1)
                tool = {"docx": "make_docx", "xlsx": "make_xlsx", "pptx": "make_pptx"}.get(kind)
                last = steps[-1]["id"] if steps else None
                if tool:
                    steps.append(self._step(f"render_{kind}", tool, inputs=[("step", last)] if last else [],
                                            out="file", side=True))
        plan["steps"] = [st for st in steps if not st.pop("_drop", False)]
        return _json(plan)

    # -- tool decisions ----------------------------------------------------------------------

    def _step_decide(self, req: LLMRequest) -> LLMResponse:
        tool = str(req.meta.get("step_tool"))
        args = dict(req.meta.get("suggested_args") or {})
        if tool == "run_python" and not args.get("script"):
            outputs = req.meta.get("outputs") or {}
            comp = next((o["value"] for o in outputs.values() if isinstance(o, dict)
                         and isinstance(o.get("value"), dict) and "offers" in o["value"]), None)
            if comp is not None:
                ref = next(k for k, o in outputs.items() if isinstance(o, dict) and o.get("value") is comp)
                args = {"script": SCORING_SCRIPT, "inputs": {"comparison.json": f"{{{ref}.value}}"}}
        if tool == "search_kb" and not args.get("queries"):
            args["queries"] = [str(req.meta.get("task_text", ""))[:200] or "procedure"]
        return _json({"tool": tool, "args": args})

    # -- documents ----------------------------------------------------------------------------

    def _vlm_read_field(self, req: LLMRequest) -> LLMResponse:
        region = req.meta.get("region") or {}
        value = region.get("vlm_value")
        return _json({"value": "" if value is None else str(value)})

    def _vlm_choose_field(self, req: LLMRequest) -> LLMResponse:
        region = req.meta.get("region") or {}
        seen = region.get("vlm_value_zoomed")
        a, b = req.meta.get("a"), req.meta.get("b")
        if seen is not None and seen == b:
            choice = "B"
        elif seen is not None and seen == a:
            choice = "A"
        else:
            choice = "neither"
        return _json({"choice": choice, "value": "" if seen is None else str(seen)})

    def _vlm_read_region(self, req: LLMRequest) -> LLMResponse:
        region = req.meta.get("region") or {}
        value = region.get("vlm_value") or req.meta.get("hint") or ""
        return _json({"value": str(value)})

    @staticmethod
    def _line_value(text: str, label: str) -> str | None:
        m = re.search(rf"{label}\s*:\s*(.+)", text, re.I)
        return m.group(1).strip() if m else None

    def _extract_findings(self, req: LLMRequest) -> LLMResponse:
        recs = self._records(req)
        pages = [r for r in recs if r.kind == "ocr_text"]
        vlm = [r for r in recs if r.kind == "vlm_read" and isinstance(r.body, dict)]
        blob = "\n".join(p.body_text() for p in pages)

        def fact_from_line(label: str, kind: str) -> dict[str, Any] | None:
            for p in pages:
                raw = self._line_value(p.body_text(), label)
                if not raw:
                    continue
                value = raw
                if kind == "date":
                    m = DATE_RE.search(raw)
                    value = m.group(0) if m else raw
                elif kind in {"po", "tag"}:
                    value = raw.split()[0]
                rec_id, conf = p.id, p.confidence or "high"
                for v in vlm:
                    b = v.body
                    if b.get("field_kind") == kind and str(b.get("ocr_value", "")) and \
                            str(b.get("ocr_value")) in raw:
                        rec_id, conf, value = v.id, v.confidence or "high", str(b.get("value") or value)
                        break
                return {"value": value, "record": rec_id, "confidence": conf}
            return None

        tag = None
        for v in vlm:
            if v.body.get("field_kind") == "tag":
                tag = {"value": str(v.body.get("value")), "record": v.id, "confidence": v.confidence or "high"}
                break
        tag = tag or fact_from_line(r"Equipment Tag", "tag")
        if tag is None:
            found = find_tags(blob)
            tag = {"value": found[0], "record": pages[0].id, "confidence": "low"} if found and pages else \
                {"value": "unknown", "record": pages[0].id if pages else "R-none-0", "confidence": "uncertain"}
        measurements = []
        for p in pages:
            for block in p.body_text().split("\n\n"):
                lines = [ln for ln in block.splitlines() if ln.strip().startswith("|")]
                if len(lines) < 3:
                    continue
                header = [c.strip().lower() for c in lines[0].strip().strip("|").split("|")]
                col = next((i for i, h in enumerate(header) if "measured" in h), None)
                if col is None:
                    continue
                tag_col = next((i for i, h in enumerate(header) if h == "tag"), None)
                loc_col = next((i for i, h in enumerate(header) if "location" in h), 0)
                for ln in lines[2:]:
                    cells = [c.strip() for c in ln.strip().strip("|").split("|")]
                    qs = find_quantities(cells[col]) if col < len(cells) else []
                    if not qs:
                        continue
                    measurements.append({
                        "quantity": "wall_thickness", "tag": cells[tag_col] if tag_col is not None else tag["value"],
                        "location": cells[loc_col], "value": qs[0].magnitude,
                        "unit": {"millimeter": "mm"}.get(qs[0].unit or "", qs[0].unit or ""),
                        "record": p.id, "confidence": p.confidence or "high"})
        tags_seen: dict[str, dict[str, Any]] = {}
        for p in pages:
            for t in find_tags(p.body_text()):
                tags_seen.setdefault(norm_tag(t), {"value": t, "record": p.id, "confidence": p.confidence or "high"})
        tags_seen[norm_tag(tag["value"])] = tag
        remarks = []
        for v in vlm:
            if v.body.get("field_kind") == "handwriting" and "remark" in str(v.body.get("region", "")).lower():
                remarks.append({"text": str(v.body.get("value")), "record": v.id})
        if not remarks:
            for p in pages:
                r = self._line_value(p.body_text(), "Remarks")
                if r:
                    remarks.append({"text": r, "record": p.id})
        stamp = any(v.body.get("field_kind") == "stamp" and v.body.get("normalised") == "present"
                    and v.confidence != "uncertain" for v in vlm)
        vendor = fact_from_line(r"(?:Pump )?Vendor", "party")
        inspector = fact_from_line("Inspector", "party")
        desc = next((self._line_value(p.body_text(), "Equipment") for p in pages
                     if self._line_value(p.body_text(), "Equipment")), None)
        out = {
            "document_type": "inspection_report" if re.search(r"inspection report", blob, re.I) else "other",
            "equipment_tag": tag,
            "equipment_description": desc,
            "report_date": fact_from_line("Report Date", "date"),
            "inspection_date": fact_from_line("Inspection Date", "date"),
            "next_inspection_date": fact_from_line("Next Inspection Due", "date"),
            "calibration_valid_until": fact_from_line("Calibration valid until", "date"),
            "po_number": fact_from_line("Purchase Order", "po"),
            "vendor": vendor,
            "inspector": inspector,
            "measurements": measurements,
            "tags_mentioned": list(tags_seen.values()),
            "remarks": remarks,
            "stamp_present": stamp,
        }
        return _json(out)

    # -- drafting -------------------------------------------------------------------------------

    def _draft_sections(self, req: LLMRequest) -> LLMResponse:
        recs = self._records(req)
        findings_rec = next((r for r in recs if r.kind == "model_output" and isinstance(r.body, dict)
                             and "measurements" in r.body), None)
        f: dict[str, Any] = findings_rec.body if findings_rec and isinstance(findings_rec.body, dict) else {}
        checks = [r for r in recs if r.kind == "check_result" and isinstance(r.body, dict)]
        kb = [r for r in recs if r.kind == "kb_chunk"]
        history = [r for r in recs if r.kind == "graph_fact" and isinstance(r.body, dict)]
        tag = (f.get("equipment_tag") or {}).get("value", "the equipment")
        tag_rec = (f.get("equipment_tag") or {}).get("record")
        desc = f.get("equipment_description") or "equipment"
        summary, findings, recommendation = [], [], []
        insp = f.get("inspection_date") or {}
        if insp:
            summary.append({"text": _cite(f"An ultrasonic thickness survey of {tag} ({desc}) was carried out "
                                          f"on {insp['value']}.", tag_rec, insp.get("record"))})
        rows = [m for m in f.get("measurements") or [] if norm_tag(str(m.get("tag"))) == norm_tag(str(tag))]
        low = min(rows, key=lambda m: m["value"]) if rows else None
        if low:
            findings.append({"text": _cite(f"The minimum measured wall thickness is {_fmt(low['value'])} "
                                           f"{low['unit']} at the {low['location'].lower()}.", low["record"])})
        for m in rows:
            if m is not low:
                findings.append({"text": _cite(f"{m['location']}: {_fmt(m['value'])} {m['unit']}.", m["record"])})
        limit_rec, limit_sentence = None, None
        for r in kb:
            for sent in _sentences(r.body_text()):
                if re.search(r"minimum", sent, re.I) and re.search(r"thickness", sent, re.I) and find_quantities(sent):
                    limit_rec, limit_sentence = r, sent
                    break
            if limit_rec:
                break
        if limit_rec and limit_sentence:
            anchor = limit_rec.anchor
            clause = next((tv.raw for k, tv in limit_rec.fields.items() if k.startswith("clause:")), None)
            where = f"{anchor.doc} Rev {anchor.revision}" if anchor and anchor.revision else "The procedure"
            if clause:
                where += f" clause {clause}"
            findings.append({"text": _cite(f"{where} states: {limit_sentence}", limit_rec.id)})
        if history:
            parts = [f"{_fmt(float(h.body['value']))} {h.body['unit']} on {h.body['date']}" for h in history]
            findings.append({"text": _cite(f"Earlier readings for {tag}: {', '.join(parts)}.",
                                           *[h.id for h in history])})
        for r in f.get("remarks") or []:
            findings.append({"text": _cite(f"Inspector remark: {r['text'].rstrip('.')}.", r.get("record"))})
        if f.get("stamp_present"):
            stamp_rec = next((r.id for r in recs if r.kind == "vlm_read" and isinstance(r.body, dict)
                              and r.body.get("field_kind") == "stamp"), None)
            findings.append({"text": _cite("The QA/QC inspection stamp is present on the sign-off page.", stamp_rec)})
        cons = []
        for c in checks:
            b = c.body
            status = b.get("status")
            desc_c = b.get("description") or b.get("rule")
            phrase = {"pass": "consistent", "mismatch": "mismatch", "not_found": "reference not found",
                      "not_checked": "not checked"}.get(status, status)
            parts = []
            if b.get("left_value") is not None:
                parts.append(f"Reported {b['left_value']}" + (f"; reference {b['right_value']}." if
                                                             b.get("right_value") else "."))
            else:
                parts.append(f"{str(phrase).capitalize()}.")
            if b.get("extra", {}).get("location"):
                parts.append(f"Location: {b['extra']['location']}.")
            cites = [x for x in (b.get("left_record"), b.get("right_record")) if x]
            cons.append({"rule": desc_c, "status": status, "check": c.id,
                         "text": _cite(" ".join(parts), *cites)})
        mismatches = [c for c in checks if c.body.get("status") == "mismatch"]
        if mismatches:
            recommendation.append({"text": f"Do not return {tag} to service until the casing is repaired or "
                                           "replaced and the mismatches listed under consistency findings are resolved."})
            if limit_rec is not None:
                removal = next((s for s in _sentences(limit_rec.body_text()) if re.search(r"removed|repaired", s)),
                               None)
                if removal:
                    recommendation.append({"text": _cite(f"Procedure requirement: {removal}", limit_rec.id)})
        else:
            nxt = f.get("next_inspection_date") or {}
            text = f"{tag} meets the procedure limits and may continue in service"
            recommendation.append({"text": _cite(text + (f" until the next inspection due on {nxt['value']}."
                                                         if nxt else "."), nxt.get("record"), tag_rec)})
        for r in f.get("remarks") or []:
            if re.search(r"re-?inspect", r["text"], re.I):
                recommendation.append({"text": _cite(f"Follow the inspector's advice: {r['text'].rstrip('.')}.",
                                                     r.get("record"))})
        report_no = None
        for r in recs:
            if r.kind == "ocr_text":
                report_no = self._line_value(r.body_text(), "Report No")
                if report_no:
                    break
        note = {
            "title": f"Approval note: {tag} casing thickness survey",
            "subject": f"Return to service decision for {tag}",
            "reference": f"Based on inspection report {report_no}" if report_no else "Inspection report",
            "summary": summary or [{"text": "No inspection date was extracted."}],
            "findings": findings,
            "consistency_findings": cons,
            "recommendation": recommendation,
            "incomplete": [] if f else ["Findings were not extracted; the draft needs manual completion."],
        }
        return _json(note)

    def _summarise_map(self, req: LLMRequest) -> LLMResponse:
        recs = [r for r in self._records(req) if r.kind == "ocr_text"]
        sections: dict[str, dict[str, Any]] = {}
        for r in recs:
            text = r.body_text()
            current = None
            buffer: list[str] = []
            blocks: list[tuple[str, str]] = []
            for line in text.splitlines():
                m = re.match(r"^\s*(\d+)\.\s+([A-Z][^\n]+)$", line.strip())
                if m:
                    if current:
                        blocks.append((current, " ".join(buffer)))
                    current, buffer = m.group(2).strip(), []
                elif current:
                    buffer.append(line.strip())
            if current:
                blocks.append((current, " ".join(buffer)))
            for heading, body in blocks:
                base = re.sub(r"\s*\(continued.*\)$", "", heading)
                if not any(k in base.lower() for k in KEY_TERMS) and "definition" not in base.lower():
                    continue
                sents = [s for s in _sentences(body) if SIGNAL.search(s) and "clause" not in s.lower()]
                if not sents:
                    continue
                sec = sections.setdefault(base, {"heading": base, "pages": [], "points": []})
                page = r.anchor.page if r.anchor else None
                if page is not None and page not in sec["pages"]:
                    sec["pages"].append(page)
                for sent in sents[:2]:
                    if len(sec["points"]) < 3:
                        sec["points"].append({"text": _cite(sent, r.id)})
        return _json({"title": f"Summary of {req.meta.get('doc', 'the document')}",
                      "sections": [s for s in sections.values() if s["points"]]})

    def _summarise_reduce(self, req: LLMRequest) -> LLMResponse:
        partials = req.meta.get("partials") or []
        merged: dict[str, dict[str, Any]] = {}
        for p in partials:
            for sec in p.get("sections") or []:
                m = merged.setdefault(sec["heading"], {"heading": sec["heading"], "pages": [], "points": []})
                m["pages"] = sorted(set(m["pages"]) | set(sec.get("pages") or []))
                for pt in sec["points"]:
                    if pt not in m["points"] and len(m["points"]) < 3:
                        m["points"].append(pt)
        points = [pt for sec in merged.values() for pt in sec["points"]]
        scored = sorted(points, key=lambda pt: (-len(re.findall(r"\d", pt["text"].split("[")[0])), points.index(pt)))
        overall = [pt for pt in scored if re.search(r"\d", pt["text"].split("[")[0])][:6]
        return _json({"title": f"Summary of {req.meta.get('doc', 'the document')}",
                      "overall": overall, "sections": list(merged.values())})

    def _answer_question(self, req: LLMRequest) -> LLMResponse:
        question = str(req.meta.get("task_text") or last_user(req))
        recs = [r for r in self._records(req) if r.kind in {"kb_chunk", "ocr_text", "graph_fact"}]
        ranked = rank_statements(question, recs)
        best: list[tuple[float, str, LedgerRecord]] = []
        for x in ranked:
            if x[0] < 0.3 or (best and x[0] < best[0][0] - 0.15) or len(best) == 2:
                break
            if all(x[1] != b[1] and x[2].id != b[2].id for b in best):
                best.append(x)
        if not best:
            return _json({"answer": [{"text": "The retrieved passages do not answer this question."}],
                          "not_found": True})
        return _json({"answer": [{"text": _cite(sent, r.id)} for _s, sent, r in best], "not_found": False})

    def _deck_outline(self, req: LLMRequest) -> LLMResponse:
        recs = [r for r in self._records(req) if r.kind == "ocr_text"]
        text = "\n".join(r.body_text() for r in recs)
        title = next((ln.lstrip("# ").strip() for ln in text.splitlines() if ln.startswith("#")), "Briefing")
        slides = []
        for ln in text.splitlines():
            m = re.match(r"^\s*-\s*([^:]+):\s*(.+)$", ln)
            if m:
                bullets = [b.strip().rstrip(".") + "." for b in re.split(r";\s*", m.group(2)) if b.strip()]
                slides.append({"title": m.group(1).strip(), "bullets": bullets})
        return _json({"title": title, "slides": slides or [{"title": title, "bullets": ["No notes found."]}]})

    # -- calculation ----------------------------------------------------------------------------

    def _extract_calc_inputs(self, req: LLMRequest) -> LLMResponse:
        recs = [r for r in self._records(req) if r.kind == "ocr_text"]
        rec = recs[0] if recs else None
        text = rec.body_text() if rec else ""
        variables: dict[str, Any] = {}
        for ln in text.splitlines():
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if len(cells) != 3 or cells[0].lower() in {"parameter", ""} or set(cells[0]) <= {"-"}:
                continue
            desc, sym, raw = cells
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", sym):
                continue
            qs = find_quantities(raw)
            if qs:
                unit = raw[len(raw.split()[0]):].strip() or "dimensionless"
                variables[sym] = {"value": qs[0].magnitude, "unit": unit, "description": desc,
                                  "record": rec.id if rec else ""}
            else:
                try:
                    variables[sym] = {"value": float(raw), "unit": "dimensionless", "description": desc,
                                      "record": rec.id if rec else ""}
                except ValueError:
                    continue
        formulas = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([A-Za-z0-9_+\-*/(). ]+)", text)
        base = next(((n, e.strip()) for n, e in formulas if any(v in e for v in variables) and "+" in e
                     and n not in variables and "CA" not in e), None)
        total = next(((n, e.strip()) for n, e in formulas if base and base[0] in re.findall(r"\w+", e)), None)
        if base and total:
            expression = f"{total[0]} = " + re.sub(rf"\b{base[0]}\b", f"({base[1]})", total[1])
            name = total[0]
        elif base:
            expression, name = f"{base[0]} = {base[1]}", base[0]
        else:
            expression, name = "result = 0", "result"
        units = [v["unit"] for v in variables.values() if v["unit"] in {"mm", "m", "in"}]
        title = next((ln.lstrip("# ").strip() for ln in text.splitlines() if ln.startswith("#")), "Calculation")
        standard = next((ln.split(":", 1)[1].split(",")[0].strip() for ln in text.splitlines()
                         if ln.lower().startswith("design code")), "")
        return _json({"title": title, "standard": standard, "expression": expression, "result_name": name,
                      "result_unit": units[0] if units else "dimensionless", "variables": variables})

    # -- offers --------------------------------------------------------------------------------

    def _compare_offers(self, req: LLMRequest) -> LLMResponse:
        recs = [r for r in self._records(req) if r.kind == "ocr_text"]
        by_doc: dict[str, list[LedgerRecord]] = {}
        for r in recs:
            by_doc.setdefault(r.anchor.doc if r.anchor else "?", []).append(r)
        offers, criteria = [], []
        tender = None
        for doc, pages in by_doc.items():
            joined = "\n".join(p.body_text() for p in pages)
            if re.search(r"TENDER CONDITIONS", joined):
                tender = pages
                continue
            vendor = self._line_value(joined, "Bidder")
            if not vendor:
                continue
            values: dict[str, Any] = {}
            for p in pages:
                t = p.body_text()
                page = p.anchor.page if p.anchor else None
                for key, label in (("price", "Total price"), ("delivery_weeks", "Delivery period"),
                                   ("warranty_months", "Warranty")):
                    raw = self._line_value(t, label)
                    if raw and key not in values:
                        qs = find_quantities(raw)
                        if qs:
                            values[key] = {"value": qs[0].magnitude, "record": p.id, "page": page}
            devs = []
            for p in pages:
                lines = p.body_text().splitlines()
                for i, ln in enumerate(lines):
                    if ln.strip() == "Deviations":
                        for d in lines[i + 1:]:
                            if not d.strip() or d.strip().isupper() or "|" in d:
                                break
                            if not re.match(r"^\s*No deviation", d, re.I):
                                devs.append({"text": d.strip(), "record": p.id})
            offers.append({"vendor": vendor.split(" | ")[0], "document": doc, "values": values, "deviations": devs})
        weights = {"price": 0.6, "delivery_weeks": 0.25, "warranty_months": 0.15}
        limits: dict[str, tuple[float, str, str]] = {}
        if tender:
            for p in tender:
                t = " ".join(p.body_text().split())
                m = re.search(r"price \(weight ([\d.]+)\).*?delivery period \(weight ([\d.]+)\).*?warranty \(weight "
                              r"([\d.]+)\)", t, re.I)
                if m:
                    weights = {"price": float(m.group(1)), "delivery_weeks": float(m.group(2)),
                               "warranty_months": float(m.group(3))}
                for key, pat, kind in (("delivery_weeks", r"Delivery period shall not exceed (\d+) weeks", "max"),
                                       ("warranty_months", r"Warranty shall be at least (\d+) months", "min"),
                                       ("price", r"price shall not exceed the estimate of INR ([\d,]+)", "max")):
                    mm = re.search(pat, t, re.I)
                    if mm:
                        limits[key] = (float(mm.group(1).replace(",", "")), kind, p.id)
        for key, unit, better in (("price", "INR", "lower"), ("delivery_weeks", "weeks", "lower"),
                                  ("warranty_months", "months", "higher")):
            lim = limits.get(key)
            criteria.append({"name": key, "unit": unit, "better": better, "weight": weights[key],
                             "limit": lim[0] if lim else None, "limit_kind": lim[1] if lim else None,
                             "limit_record": lim[2] if lim else None})
        return _json({"criteria": criteria, "offers": offers, "recommendation": {"vendor": None, "rationale": []}})

    def _draft_recommendation(self, req: LLMRequest) -> LLMResponse:
        recs = self._records(req)
        comp_rec = next((r for r in recs if r.kind == "model_output" and isinstance(r.body, dict)
                         and "offers" in r.body), None)
        score_rec = next((r for r in recs if r.kind == "sandbox_result" and isinstance(r.body, dict)), None)
        comp: dict[str, Any] = comp_rec.body if comp_rec and isinstance(comp_rec.body, dict) else {}
        scores = (score_rec.body.get("result") if score_rec and isinstance(score_rec.body, dict) else None) or {}
        crit = {c["name"]: c for c in comp.get("criteria") or []}
        summary, findings, cons = [], [], []
        for o in comp.get("offers") or []:
            v = o["values"]
            parts, ids = [], []
            if "price" in v:
                parts.append(f"a total price of INR {_indian(v['price']['value'])}")
                ids.append(v["price"]["record"])
            if "delivery_weeks" in v:
                parts.append(f"delivery in {_fmt(v['delivery_weeks']['value'])} weeks")
                ids.append(v["delivery_weeks"]["record"])
            if "warranty_months" in v:
                parts.append(f"a warranty of {_fmt(v['warranty_months']['value'])} months")
                ids.append(v["warranty_months"]["record"])
            summary.append({"text": _cite(f"{o['vendor']} offers {', '.join(parts)}.", *ids)})
            for d in o.get("deviations") or []:
                findings.append({"text": _cite(f"{o['vendor']} deviation: {d['text'].rstrip('.')}.", d["record"])})
        ranking = {r["vendor"]: r for r in (scores.get("ranking") or [])}
        for o in comp.get("offers") or []:
            r = ranking.get(o["vendor"], {})
            fails = r.get("violations") or []
            status = "pass" if not fails else "mismatch"
            text = f"{o['vendor']}: " + ("meets every mandatory tender condition." if not fails else
                                         "violates " + ", ".join(fails) + ".")
            ids = [o["values"][k]["record"] for k in fails if k in o["values"]]
            ids += [crit[k]["limit_record"] for k in fails if crit.get(k, {}).get("limit_record")]
            cons.append({"rule": "tender_conditions", "status": status,
                         "text": _cite(text, *ids, score_rec.id if score_rec and not fails else None)})
        best = scores.get("recommended")
        recommendation = []
        if best and score_rec is not None:
            recommendation.append({"text": _cite(f"Recommend {best}, which has the highest weighted score among the "
                                                 "compliant offers.", score_rec.id)})
            weights = ", ".join(f"{c['name'].replace('_', ' ')} {c['weight']:g}" for c in comp.get("criteria") or [])
            limit_ids = [c.get("limit_record") for c in comp.get("criteria") or [] if c.get("limit_record")]
            recommendation.append({"text": _cite(f"The weights applied are {weights}, as stated in the tender "
                                                 "conditions.", *limit_ids[:1])})
        else:
            recommendation.append({"text": "No compliant offer could be recommended."})
        return _json({"title": "Recommendation note: cooling water booster pumps", "subject": "Evaluation of offers",
                      "summary": summary, "findings": findings, "consistency_findings": cons,
                      "recommendation": recommendation})

    # -- code ------------------------------------------------------------------------------------

    def _code_write(self, req: LLMRequest) -> LLMResponse:
        text = str(req.meta.get("task_text", ""))
        files = [f.rsplit("/", 1)[-1] for f in req.meta.get("files") or []]
        csv_name = next((f for f in files if f.endswith(".csv")), files[0] if files else "input.csv")
        m = re.search(r"\b([a-z]+)\s+(?:readings|values|data|measurements|series)\b", text, re.I)
        column = m.group(1).lower() if m else "value"
        return LLMResponse(text=f"```python\n{code_script(text, csv_name, column)}```")

    def _code_fix(self, req: LLMRequest) -> LLMResponse:
        text = str(req.meta.get("task_text", ""))
        feedback = req.meta.get("feedback") or {}
        previous = str(req.meta.get("previous_script", ""))
        files = [f.rsplit("/", 1)[-1] for f in req.meta.get("files") or []]
        csv_name = next((f for f in files if f.endswith(".csv")), files[0] if files else "input.csv")
        tb = "\n".join(feedback.get("traceback_head") or [])
        m = re.search(r"KeyError: '([^']+)'", tb)
        column_match = re.search(r"COLUMN = '([^']+)'", previous)
        column = column_match.group(1) if column_match else "value"
        if m:
            wanted = m.group(1)
            columns: list[str] = []
            for cols in (req.meta.get("columns") or {}).values():
                columns += cols
            numeric = [c for c in columns if c not in {"timestamp", "sensor_id"}]
            pick = [c for c in numeric if c.startswith(wanted)] or difflib.get_close_matches(wanted, numeric, 1, 0.3)
            if pick:
                column = pick[0]
        return LLMResponse(text=f"```python\n{code_script(text, csv_name, column)}```")


def _indian(value: float) -> str:
    n = round(value)
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups) + "," + tail


def code_script(task: str, csv_name: str, column: str) -> str:
    """A small, readable data-processing script for the request, using the standard library only."""
    low = task.lower()
    if re.search(r"\b(mean|average)\b", low):
        body = """
values = [float(r[COLUMN]) for r in rows]
result = {"rows": len(values), "column": COLUMN, "mean": round(sum(values) / len(values), 4)}
assert result["rows"] > 0, "no rows"
"""
    elif re.search(r"\b(max|maximum|peak)\b", low):
        body = """
values = [float(r[COLUMN]) for r in rows]
result = {"rows": len(values), "column": COLUMN, "max": max(values), "min": min(values)}
assert result["max"] >= result["min"]
"""
    elif re.search(r"\bcount\b", low):
        thr = re.search(r"(?:above|over|greater than)\s+([\d.]+)", low)
        limit = float(thr.group(1)) if thr else 0.0
        body = f"""
values = [float(r[COLUMN]) for r in rows]
result = {{"rows": len(values), "column": COLUMN, "threshold": {limit}, "count": sum(1 for v in values if v > {limit})}}
assert 0 <= result["count"] <= result["rows"]
"""
    else:
        body = """
values = [float(r[COLUMN]) for r in rows]
ordered = sorted(values)
median = statistics.median(ordered)
mad = statistics.median([abs(v - median) for v in ordered]) or 1e-9
flags = []
for i, (r, v) in enumerate(zip(rows, values)):
    score = 0.6745 * (v - median) / mad
    if abs(score) > 6.0:
        flags.append({"row": i + 2, "timestamp": r.get("timestamp", ""), "value": v, "robust_z": round(score, 2)})

with open("anomalies.csv", "w", newline="", encoding="utf-8") as fh:
    writer = csv.DictWriter(fh, fieldnames=["row", "timestamp", "value", "robust_z"])
    writer.writeheader()
    writer.writerows(flags)

width, height = 640, 200
lo, hi = min(values), max(values)
span = (hi - lo) or 1.0
points = " ".join(f"{i * width / max(1, len(values) - 1):.1f},{height - (v - lo) / span * height:.1f}"
                  for i, v in enumerate(values))
marks = "".join(f'<circle cx="{(f["row"] - 2) * width / max(1, len(values) - 1):.1f}" '
                f'cy="{height - (f["value"] - lo) / span * height:.1f}" r="4" fill="#c2410c"/>' for f in flags)
with open("chart.svg", "w", encoding="utf-8") as fh:
    fh.write(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
             f'viewBox="0 0 {width} {height}"><polyline fill="none" stroke="#0f766e" stroke-width="1" '
             f'points="{points}"/>{marks}</svg>')

result = {"rows": len(values), "column": COLUMN, "median": round(median, 3), "anomalies": len(flags),
          "anomaly_rows": [f["row"] for f in flags]}
assert result["anomalies"] > 0, "expected at least one anomaly"
assert result["anomalies"] < 0.05 * result["rows"], "too many rows flagged"
assert os.path.exists("anomalies.csv") and os.path.exists("chart.svg")
"""
    return f'''"""Generated for: {task.strip()[:120]}"""
import csv
import json
import os
import statistics

INPUT = {csv_name!r}
COLUMN = {column!r}

with open(INPUT, newline="", encoding="utf-8") as fh:
    rows = list(csv.DictReader(fh))
{body}
with open("summary.json", "w", encoding="utf-8") as fh:
    json.dump(result, fh, indent=2)
print(json.dumps(result))
'''


SCORING_SCRIPT = '''"""Score offers against the tender criteria (weighted, compliant offers only)."""
import json

with open("comparison.json", encoding="utf-8") as fh:
    comp = json.load(fh)

criteria = comp["criteria"]
offers = comp["offers"]
ranking = []
for offer in offers:
    values = {k: v["value"] for k, v in offer["values"].items()}
    violations = []
    for c in criteria:
        if c.get("limit") is None or c["name"] not in values:
            continue
        v = values[c["name"]]
        kind = c.get("limit_kind") or ("max" if c["better"] == "lower" else "min")
        if (kind == "max" and v > c["limit"]) or (kind == "min" and v < c["limit"]):
            violations.append(c["name"])
    ranking.append({"vendor": offer["vendor"], "values": values, "violations": violations})

for c in criteria:
    column = [r["values"][c["name"]] for r in ranking if c["name"] in r["values"]]
    best, worst = min(column), max(column)
    for r in ranking:
        v = r["values"].get(c["name"])
        if v is None:
            continue
        score = best / v if c["better"] == "lower" else v / worst
        r.setdefault("scores", {})[c["name"]] = round(score, 4)

for r in ranking:
    r["weighted"] = round(sum(r["scores"][c["name"]] * c["weight"] for c in criteria if c["name"] in r["scores"]), 4)
    r["compliant"] = not r["violations"]

compliant = sorted((r for r in ranking if r["compliant"]), key=lambda r: -r["weighted"])
for i, r in enumerate(compliant, start=1):
    r["rank"] = i
result = {"ranking": ranking, "recommended": compliant[0]["vendor"] if compliant else None}
assert all("weighted" in r for r in ranking)
print(json.dumps(result))
'''
