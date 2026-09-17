"""Structure-aware chunking of Markdown, text and PDF pages (PRD section 4.11)."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import yaml
from pydantic import BaseModel, Field

from workbench.core.labels import Label

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
PAGE_RE = re.compile(r"<!--\s*page\s+(\d+)\s*-->")
NUMBERED_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(.+)$")
PDF_HEADING_RE = re.compile(r"^(\d+(?:\.\d+)*)\.\s+([A-Z][^\n]{2,80})$")
TARGET_TOKENS = 500


class Chunk(BaseModel):
    id: str
    doc_id: str
    doc_number: str
    revision: str | None = None
    effective_from: dt.date | None = None
    superseded_by: str | None = None
    title: str = ""
    doc_type: str = "document"
    page: int | None = None
    section: str = ""
    clause_ids: list[str] = Field(default_factory=list)
    date: dt.date | None = None
    label: Label
    acl_groups: list[str] = Field(default_factory=list)
    workspace: str | None = None
    applies_to_classes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    source: str = ""
    text: str

    def cite(self) -> str:
        parts = [self.doc_number]
        if self.revision:
            parts[0] += f" Rev {self.revision}"
        if self.page is not None:
            parts.append(f"p. {self.page}")
        return ", ".join(parts)


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            meta = yaml.safe_load(text[3:end]) or {}
            return meta, text[end + 4 :].lstrip("\n")
    return {}, text


def _tokens(text: str) -> int:
    return len(text) // 4


def _split_long(text: str) -> list[str]:
    if _tokens(text) <= TARGET_TOKENS:
        return [text]
    parts, cur = [], ""
    for para in text.split("\n\n"):
        if cur and _tokens(cur + para) > TARGET_TOKENS:
            parts.append(cur.strip())
            cur = ""
        cur += para + "\n\n"
    if cur.strip():
        parts.append(cur.strip())
    return parts


def chunk_markdown(text: str, base: dict[str, Any]) -> list[Chunk]:
    """Split on headings; every chunk keeps its section path, clause id and page."""
    meta, body = split_front_matter(text)
    info = {**base, **meta}
    doc_number = str(info.get("doc_number") or base.get("doc_number"))
    revision = str(info["revision"]) if info.get("revision") is not None else None
    doc_id = f"{doc_number}@{revision}" if revision else doc_number
    label = Label.parse({"level": info.get("label", "Restricted"), "compartments": info.get("compartments") or []})
    sections: list[tuple[list[str], list[str], int | None, list[str]]] = []
    path: list[str] = []
    clause_path: list[str] = []
    page: int | None = None
    buf: list[str] = []

    def flush() -> None:
        content = "\n".join(buf).strip()
        if content:
            sections.append((list(path), list(clause_path), page, [content]))
        buf.clear()

    for line in body.splitlines():
        pm = PAGE_RE.search(line)
        if pm:
            flush()
            page = int(pm.group(1))
            continue
        hm = HEADING_RE.match(line)
        if hm:
            flush()
            level = len(hm.group(1))
            title = hm.group(2).strip()
            path[:] = [*path[:level - 1], title]
            nm = NUMBERED_RE.match(title)
            clause_path[:] = [*clause_path[:level - 1], nm.group(1) if nm else ""]
            buf.append(line)
            continue
        buf.append(line)
    flush()

    chunks: list[Chunk] = []
    for idx, (spath, cpath, pg, contents) in enumerate(sections):
        for part_no, part in enumerate(_split_long(contents[0])):
            clause_ids = [c for c in cpath if c]
            chunks.append(Chunk(
                id=f"{doc_id}#{idx}.{part_no}", doc_id=doc_id, doc_number=doc_number, revision=revision,
                effective_from=info.get("effective_from"), title=str(info.get("title", doc_number)),
                doc_type=str(info.get("doc_type", "document")), page=pg if pg is not None else 1,
                section=" > ".join(spath), clause_ids=clause_ids[-1:] if clause_ids else [],
                date=info.get("effective_from"), label=label,
                acl_groups=list(info.get("acl_groups") or []), workspace=info.get("workspace"),
                applies_to_classes=list(info.get("applies_to_classes") or []),
                tags=[str(t) for t in (info.get("tags") or [])], source=str(info.get("source", "")), text=part,
            ))
    return chunks


def chunk_pages(pages: list[tuple[int, str]], base: dict[str, Any]) -> list[Chunk]:
    """Chunk PDF page text, splitting on numbered clause headings such as ``7. Warranty``."""
    doc_number = str(base["doc_number"])
    label = Label.parse(base["label"])
    chunks: list[Chunk] = []
    for page_no, text in pages:
        blocks: list[tuple[str, list[str]]] = [("", [])]
        for line in text.splitlines():
            m = PDF_HEADING_RE.match(line.strip())
            if m:
                blocks.append((f"{m.group(1)} {m.group(2).strip()}", [line]))
            else:
                blocks[-1][1].append(line)
        for i, (heading, lines) in enumerate(blocks):
            content = "\n".join(lines).strip()
            if len(content) < 20:
                continue
            clause = heading.split(" ", 1)[0] if heading else ""
            chunks.append(Chunk(
                id=f"{doc_number}#p{page_no}.{i}", doc_id=doc_number, doc_number=doc_number,
                title=str(base.get("title", doc_number)), doc_type=str(base.get("doc_type", "workspace_document")),
                page=page_no, section=heading, clause_ids=[clause] if clause else [], label=label,
                acl_groups=list(base.get("acl_groups") or []), workspace=base.get("workspace"),
                source=str(base.get("source", "")), text=content,
            ))
    return chunks
