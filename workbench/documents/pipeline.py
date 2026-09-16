"""Document pipeline: pages become ``ocr_text`` records, VLM regions become ``vlm_read`` records."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf

from workbench.core.labels import Label, LabelPolicy
from workbench.core.ledger import Ledger
from workbench.core.models import Anchor, Confidence, LedgerRecord, TypedValue
from workbench.core.normalise import find_dates, find_quantities, find_tags, norm_tag
from workbench.documents.dual_read import CRITICAL_KINDS, read_non_critical, reconcile
from workbench.documents.readers import CompositeReader, PageRead, Region, display_name
from workbench.llm.base import LLMBackend

MAX_FIELDS_PER_PAGE = 40


@dataclass
class ReadContext:
    task_id: str
    ledger: Ledger
    backend: LLMBackend
    vlm_model: str
    policy: LabelPolicy
    evidence_dir: Path
    produced_by: str
    reader: CompositeReader = field(default_factory=CompositeReader)
    dayfirst: bool = True
    max_retries: int = 2


@dataclass
class ReadResult:
    doc: str
    pages: list[PageRead]
    records: list[LedgerRecord]
    label: Label
    engine: str
    uncertain: list[dict[str, Any]] = field(default_factory=list)
    dual_read: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        n_ocr = sum(1 for r in self.records if r.kind == "ocr_text")
        n_vlm = sum(1 for r in self.records if r.kind == "vlm_read")
        tables = sum(len(p.tables) for p in self.pages)
        s = f"{self.doc}: {len(self.pages)} page(s), {tables} table(s), {n_ocr} text record(s), {n_vlm} VLM read(s)"
        if self.uncertain:
            s += f", {len(self.uncertain)} uncertain field(s)"
        return s


def page_confidence(page: PageRead) -> Confidence:
    if not page.scanned:
        return "high"
    confs = [r.ocr_conf for r in page.regions] or [0.8]
    avg = sum(confs) / len(confs)
    return "high" if avg >= 0.9 else "medium" if avg >= 0.6 else "low"


def text_fields(text: str, anchor: Anchor, dayfirst: bool) -> dict[str, TypedValue]:
    out: dict[str, TypedValue] = {}
    for i, tag in enumerate(dict.fromkeys(find_tags(text))):
        out[f"tag[{i}]"] = TypedValue(kind="tag", raw=tag, normalised=norm_tag(tag), anchor=anchor)
    for i, q in enumerate(find_quantities(text)):
        if q.unit is None:
            continue
        out[f"quantity[{i}]"] = TypedValue(kind="quantity", raw=q.raw, normalised=f"{q.magnitude:g} {q.unit}",
                                           magnitude=q.magnitude, unit=q.unit, anchor=anchor)
    for i, d in enumerate(find_dates(text, dayfirst)):
        out[f"date[{i}]"] = TypedValue(kind="date", raw=d.raw, normalised=d.iso, anchor=anchor)
    return dict(list(out.items())[:MAX_FIELDS_PER_PAGE])


def page_body(page: PageRead) -> str:
    parts = [page.text.strip()]
    for t in page.tables:
        title = f"Table {t.id}" + (f": {t.title}" if t.title else "")
        parts.append(f"{title}\n{t.markdown()}")
    return "\n\n".join(p for p in parts if p)


class Cropper:
    """Renders region crops from a rasterisable PDF (normal and zoomed)."""

    def __init__(self, path: Path) -> None:
        self.doc = pymupdf.open(path) if path.suffix.lower() == ".pdf" else None

    def crop(self, page_no: int, bbox: tuple[float, float, float, float], zoom: float) -> bytes | None:
        if self.doc is None or page_no > len(self.doc):
            return None
        page = self.doc[page_no - 1]
        w, h = page.rect.width, page.rect.height
        pad = 4
        clip = pymupdf.Rect(bbox[0] * w - pad, bbox[1] * h - pad, bbox[2] * w + pad, bbox[3] * h + pad) & page.rect
        if clip.is_empty:
            return None
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip)
        data: bytes = pix.tobytes("png")
        return data

    def close(self) -> None:
        if self.doc is not None:
            self.doc.close()


def _save_crop(ctx: ReadContext, doc: str, region: Region, data: bytes | None, suffix: str) -> str | None:
    if data is None:
        return None
    ctx.evidence_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in f"{doc}-{region.id}{suffix}")
    path = ctx.evidence_dir / f"{safe}.png"
    path.write_bytes(data)
    return path.name


def read_document(path: Path, file_label: Label, ctx: ReadContext, mode: str = "full") -> ReadResult:
    doc = display_name(path)
    pages = ctx.reader.read(path)
    engine = ctx.reader.engine_for(path)
    cropper = Cropper(path)
    records: list[LedgerRecord] = []
    uncertain: list[dict[str, Any]] = []
    dual: list[dict[str, Any]] = []
    doc_label = file_label
    try:
        for page in pages:
            detected = ctx.policy.detect(page.text)
            page_label = file_label.join(detected) if detected else file_label
            doc_label = doc_label.join(page_label)
            anchor = Anchor(doc=doc, page=page.page)
            body = page_body(page)
            first = next((ln.strip() for ln in page.text.splitlines()
                          if ln.strip() and not ctx.policy.detect(ln)), "")
            ocr_rec = ctx.ledger.add(
                ctx.task_id, "ocr_text",
                summary=f"{doc} p.{page.page} ({'scan' if page.scanned else 'text'}, {engine}): {first[:120]}",
                body=body, label=page_label, anchor=anchor, confidence=page_confidence(page),
                produced_by=ctx.produced_by, fields=text_fields(page.text, anchor, ctx.dayfirst),
            )
            records.append(ocr_rec)
            if mode == "tables" and not page.regions:
                continue
            for region in page.regions:
                if not region.needs_vlm:
                    continue
                ranchor = Anchor(doc=doc, page=page.page, region=region.bbox)
                crop = cropper.crop(page.page, region.bbox, 2.0)
                zoomed = cropper.crop(page.page, region.bbox, 4.0)
                crop_name = _save_crop(ctx, doc, region, crop, "")
                zoom_name = _save_crop(ctx, doc, region, zoomed, "-zoom")
                if region.field_kind in CRITICAL_KINDS:
                    outcome = reconcile(region, ctx.backend, ctx.vlm_model, ranchor, crop, zoomed, ctx.task_id,
                                        ctx.dayfirst, ctx.max_retries)
                    tv = outcome.value
                    detail = {**outcome.detail, "region": region.id, "page": page.page, "crop": crop_name,
                              "zoomed_crop": zoom_name, "value": tv.raw, "normalised": tv.normalised}
                    rec = ctx.ledger.add(
                        ctx.task_id, "vlm_read",
                        summary=(f"{doc} p.{page.page} {region.field_kind} {region.id}: {tv.raw} "
                                 f"({outcome.status}, {tv.confidence}; OCR '{region.ocr_value}')"),
                        body=detail, label=page_label, anchor=ranchor, confidence=tv.confidence,
                        produced_by=ctx.produced_by, inputs=[ocr_rec.id],
                        fields={region.field_kind: tv.model_copy(update={"anchor": ranchor})},
                    )
                    dual.append({"record": rec.id, **detail})
                    if outcome.status == "uncertain":
                        uncertain.append({"record": rec.id, **detail})
                else:
                    value = read_non_critical(region, ctx.backend, ctx.vlm_model, page.text, crop,
                                              ctx.task_id, ctx.max_retries)
                    detail = {"field_kind": region.field_kind, "region": region.id, "page": page.page,
                              "ocr_value": region.ocr_value, "vlm_value": value, "value": value,
                              "status": "vlm_read", "crop": crop_name}
                    rec = ctx.ledger.add(
                        ctx.task_id, "vlm_read",
                        summary=f"{doc} p.{page.page} {region.field_kind} {region.id}: {value[:160]}",
                        body=detail, label=page_label, anchor=ranchor, confidence="medium",
                        produced_by=ctx.produced_by, inputs=[ocr_rec.id],
                        fields={region.field_kind: TypedValue(kind="text", raw=value,
                                                              normalised=" ".join(value.lower().split()),
                                                              confidence="medium", anchor=ranchor)},
                    )
                records.append(rec)
    finally:
        cropper.close()
    return ReadResult(doc=doc, pages=pages, records=records, label=doc_label, engine=engine,
                      uncertain=uncertain, dual_read=dual)
