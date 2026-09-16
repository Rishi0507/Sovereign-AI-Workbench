"""Document readers (PRD section 4.10).

``FixtureReader`` loads an ``<stem>.ocr.json`` sidecar, which is how scanned-document behaviour
is reproduced without OCR models. ``TextPdfReader`` reads digital PDFs with PyMuPDF.
``TextFileReader`` covers Markdown, text and CSV. ``CompositeReader`` picks the right one.
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Literal, Protocol

import pymupdf
from pydantic import BaseModel, Field

from workbench.core.normalise import DATE_RE, QUANTITY_RE, TAG_RE, detect_script

Script = Literal["latin", "devanagari", "mixed"]
TEXT_EXTS = frozenset({".md", ".txt", ".csv", ".json", ".py", ".sql", ".sh", ".js", ".ts", ".yaml", ".yml"})
PO_RE = re.compile(r"\b(?:PO|WO)-\d{5,12}\b")


class Block(BaseModel):
    text: str
    bbox: tuple[float, float, float, float] | None = None


class Table(BaseModel):
    id: str
    title: str = ""
    header: list[str]
    rows: list[list[str]]
    row_bboxes: list[tuple[float, float, float, float]] = Field(default_factory=list)

    def markdown(self) -> str:
        lines = ["| " + " | ".join(self.header) + " |", "|" + "---|" * len(self.header)]
        lines += ["| " + " | ".join(r) + " |" for r in self.rows]
        return "\n".join(lines)


class Region(BaseModel):
    id: str
    bbox: tuple[float, float, float, float]
    field_kind: str
    ocr_value: str
    ocr_conf: float
    needs_vlm: bool
    vlm_value: str | None = None
    vlm_value_zoomed: str | None = None
    truth: str | None = None


class PageRead(BaseModel):
    page: int
    script: Script = "latin"
    scanned: bool = False
    text: str
    blocks: list[Block] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    regions: list[Region] = Field(default_factory=list)


class DocumentReader(Protocol):
    def read(self, path: Path) -> list[PageRead]: ...


def sidecar_for(path: Path) -> Path | None:
    if path.name.endswith(".ocr.json"):
        return path
    candidate = path.with_name(path.stem + ".ocr.json")
    return candidate if candidate.is_file() else None


class FixtureReader:
    engine = "fixture"

    def can_read(self, path: Path) -> bool:
        return sidecar_for(path) is not None

    def read(self, path: Path) -> list[PageRead]:
        side = sidecar_for(path)
        if side is None:
            raise FileNotFoundError(f"no OCR sidecar for {path.name}")
        data = json.loads(side.read_text(encoding="utf-8"))
        return [PageRead.model_validate(p) for p in data["pages"]]


def _norm_bbox(rect: pymupdf.Rect, page: pymupdf.Page) -> tuple[float, float, float, float]:
    w, h = page.rect.width, page.rect.height
    return (round(rect.x0 / w, 4), round(rect.y0 / h, 4), round(rect.x1 / w, 4), round(rect.y1 / h, 4))


class TextPdfReader:
    engine = "pymupdf"

    def read(self, path: Path) -> list[PageRead]:
        pages: list[PageRead] = []
        with pymupdf.open(path) as doc:
            for index, page in enumerate(doc):
                text = page.get_text("text")
                scanned = not text.strip() and bool(page.get_images())
                blocks = [Block(text=b[4].strip(), bbox=_norm_bbox(pymupdf.Rect(b[:4]), page))
                          for b in page.get_text("blocks") if b[4].strip()]
                tables = self._tables(text, index + 1)
                regions = [] if scanned else self._regions(page, text, index + 1)
                pages.append(PageRead(page=index + 1, script=detect_script(text), scanned=scanned, text=text,
                                      blocks=blocks, tables=tables, regions=regions))
        return pages

    @staticmethod
    def _tables(text: str, page_no: int) -> list[Table]:
        tables: list[Table] = []
        rows: list[list[str]] = []
        kv: list[list[str]] = []

        def flush() -> None:
            nonlocal rows
            if len(rows) >= 2:
                tables.append(Table(id=f"p{page_no}-t{len(tables) + 1}", header=rows[0], rows=rows[1:]))
            rows = []

        for line in text.splitlines():
            if line.count(" | ") >= 1:
                rows.append([c.strip() for c in line.split("|")])
                continue
            flush()
            m = re.match(r"^\s*([A-Z][A-Za-z /()-]{2,40}):\s+(.+?)\s*$", line)
            if m and not m.group(1).lower().startswith("page"):
                kv.append([m.group(1).strip(), m.group(2).strip()])
        flush()
        if kv:
            tables.append(Table(id=f"p{page_no}-kv", title="Key facts", header=["Field", "Value"], rows=kv))
        return tables

    @staticmethod
    def _regions(page: pymupdf.Page, text: str, page_no: int) -> list[Region]:
        found: list[tuple[str, str]] = []
        for kind, pattern in (("tag", TAG_RE), ("po", PO_RE), ("date", DATE_RE)):
            for m in pattern.finditer(text):
                found.append((kind, m.group(0)))
        for m in QUANTITY_RE.finditer(text):
            if m.group("unit") or m.group("prefix"):
                found.append(("quantity", m.group(0).strip()))
        regions: list[Region] = []
        seen: set[tuple[str, str]] = set()
        for kind, value in found:
            if (kind, value) in seen:
                continue
            seen.add((kind, value))
            hits = page.search_for(value)
            if not hits:
                continue
            regions.append(Region(id=f"p{page_no}-r{len(regions) + 1}", bbox=_norm_bbox(hits[0], page),
                                  field_kind=kind, ocr_value=value, ocr_conf=1.0, needs_vlm=False))
        return regions


class TextFileReader:
    engine = "text"

    def read(self, path: Path) -> list[PageRead]:
        raw = path.read_text(encoding="utf-8", errors="replace")
        tables: list[Table] = []
        text = raw
        if path.suffix.lower() == ".csv":
            reader = list(csv.reader(io.StringIO(raw)))
            if reader:
                head, body = reader[0], reader[1:]
                preview = body[:20]
                tables.append(Table(id="csv", title=f"{path.name} ({len(body)} rows)", header=head, rows=preview))
                text = (f"CSV file {path.name} with {len(body)} rows and columns: {', '.join(head)}\n"
                        + "\n".join(",".join(r) for r in [head, *preview]))
        else:
            rows = [ln for ln in raw.splitlines() if ln.strip().startswith("|")]
            parsed = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in rows]
            parsed = [r for r in parsed if not all(set(c) <= {"-", ":"} for c in r)]
            if len(parsed) >= 2:
                tables.append(Table(id="md-t1", header=parsed[0], rows=parsed[1:]))
        return [PageRead(page=1, script=detect_script(raw), text=text, tables=tables)]


class CompositeReader:
    def __init__(self) -> None:
        self.fixture = FixtureReader()
        self.pdf = TextPdfReader()
        self.text = TextFileReader()

    def engine_for(self, path: Path) -> str:
        if self.fixture.can_read(path):
            return self.fixture.engine
        if path.suffix.lower() == ".pdf":
            return self.pdf.engine
        return self.text.engine

    def read(self, path: Path) -> list[PageRead]:
        if self.fixture.can_read(path):
            return self.fixture.read(path)
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            return self.pdf.read(path)
        if suffix in TEXT_EXTS:
            return self.text.read(path)
        raise ValueError(f"no reader for {path.name}; install the OCR adapter for images")


def display_name(path: Path) -> str:
    return path.name[: -len(".ocr.json")] if path.name.endswith(".ocr.json") else path.name
