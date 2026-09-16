"""Dual-read reconciliation of critical fields (README section 4.5.1).

1. Blind VLM read of the region, without the OCR value as a hint.
2. Both values pass through the shared normaliser.
3. Agreement is stored with high confidence.
4. On disagreement the region is re-read zoomed; the VLM sees both candidates and answers A, B or
   neither. A choice that equals a candidate and is consistent with the OCR glyph shapes is
   accepted with medium confidence. Anything else is ``uncertain``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from workbench.core.models import Anchor, Confidence, TypedValue
from workbench.core.normalise import glyph_compatible, norm_date, norm_quantity, norm_tag
from workbench.documents.readers import Region
from workbench.llm import structured
from workbench.llm.base import ChatMessage, LLMBackend, LLMRequest
from workbench.llm.prompts import render_prompt
from workbench.llm.schemas import load_schema

CRITICAL_KINDS = frozenset({"tag", "quantity", "date", "po", "stamp", "signature"})
KIND_TO_TYPED = {"tag": "tag", "quantity": "quantity", "date": "date", "po": "po", "stamp": "text",
                 "signature": "text", "handwriting": "text"}


def normalise_field(kind: str, value: str | None, dayfirst: bool = True) -> str | None:
    if value is None:
        return None
    value = value.strip()
    try:
        if kind == "tag":
            return norm_tag(value) or None
        if kind == "quantity":
            mag, unit = norm_quantity(value)
            return f"{mag:g} {unit}"
        if kind == "date":
            return norm_date(value, dayfirst)
        if kind == "po":
            return value.upper().replace(" ", "") or None
        if kind in {"stamp", "signature"}:
            return "present" if value and value.lower() not in {"absent", "none", "no"} else "absent"
    except ValueError:
        return None
    return " ".join(value.lower().split()) or None


@dataclass
class DualReadOutcome:
    value: TypedValue
    status: str
    detail: dict[str, Any]


def _request(model: str, purpose: str, region: Region, prompt: str, images: list[bytes],
             task_id: str | None, extra: dict[str, Any]) -> LLMRequest:
    return LLMRequest(
        model=model, purpose=purpose, task_id=task_id, images=images, max_tokens=64,
        messages=[ChatMessage(role="system", content=render_prompt("vlm_system")),
                  ChatMessage(role="user", content=prompt)],
        meta={"region": region.model_dump(), **extra},
    )


def _typed(kind: str, raw: str, normalised: str, confidence: Confidence, anchor: Anchor | None) -> TypedValue:
    magnitude = unit = None
    if kind == "quantity":
        try:
            magnitude, unit = norm_quantity(raw)
        except ValueError:
            pass
    tkind = KIND_TO_TYPED.get(kind, "text")
    return TypedValue(kind=tkind, raw=raw, normalised=normalised, magnitude=magnitude, unit=unit,  # type: ignore[arg-type]
                      confidence=confidence, anchor=anchor)


def reconcile(region: Region, backend: LLMBackend, model: str, anchor: Anchor | None = None,
              crop: bytes | None = None, zoomed_crop: bytes | None = None, task_id: str | None = None,
              dayfirst: bool = True, max_retries: int = 2) -> DualReadOutcome:
    schema = load_schema("field_read")
    kind = region.field_kind
    blind = structured.call(backend, _request(
        model, "vlm.read_field", region,
        render_prompt("vlm_read_field", field_kind=kind), [crop] if crop else [], task_id, {"blind": True},
    ), schema, max_retries).value
    vlm_raw = str(blind.get("value", ""))
    ocr_norm = normalise_field(kind, region.ocr_value, dayfirst)
    vlm_norm = normalise_field(kind, vlm_raw, dayfirst)
    detail: dict[str, Any] = {"field_kind": kind, "ocr_value": region.ocr_value, "ocr_conf": region.ocr_conf,
                              "vlm_value": vlm_raw, "ocr_normalised": ocr_norm, "vlm_normalised": vlm_norm}
    if ocr_norm is not None and ocr_norm == vlm_norm:
        detail["status"] = "agree"
        return DualReadOutcome(_typed(kind, vlm_raw, ocr_norm, "high", anchor), "agree", detail)

    choice = structured.call(backend, _request(
        model, "vlm.choose_field", region,
        render_prompt("vlm_choose_field", field_kind=kind, a=region.ocr_value, b=vlm_raw),
        [zoomed_crop or crop] if (zoomed_crop or crop) else [], task_id, {"a": region.ocr_value, "b": vlm_raw},
    ), schema, max_retries).value
    picked = choice.get("choice", "neither")
    chosen_raw = {"A": region.ocr_value, "B": vlm_raw}.get(str(picked))
    detail["zoomed_choice"] = picked
    detail["zoomed_value"] = choice.get("value")
    if chosen_raw is not None:
        chosen_norm = normalise_field(kind, chosen_raw, dayfirst)
        shapes_ok = kind != "tag" and kind != "po" or glyph_compatible(chosen_raw, region.ocr_value)
        if kind in {"stamp", "signature"}:
            shapes_ok = True
        if chosen_norm is not None and shapes_ok:
            detail["status"] = "resolved"
            return DualReadOutcome(_typed(kind, chosen_raw, chosen_norm, "medium", anchor), "resolved", detail)
    detail["status"] = "uncertain"
    fallback = region.ocr_value or vlm_raw
    return DualReadOutcome(
        _typed(kind, fallback, normalise_field(kind, fallback, dayfirst) or fallback, "uncertain", anchor),
        "uncertain", detail,
    )


def read_non_critical(region: Region, backend: LLMBackend, model: str, page_text: str,
                      crop: bytes | None = None, task_id: str | None = None, max_retries: int = 2) -> str:
    """General VLM reading (handwriting, remarks) with the OCR text as a hint."""
    schema = load_schema("field_read")
    out = structured.call(backend, _request(
        model, "vlm.read_region", region,
        render_prompt("vlm_read_region", field_kind=region.field_kind, hint=region.ocr_value,
                      page_excerpt=page_text[:600]),
        [crop] if crop else [], task_id, {"hint": region.ocr_value},
    ), schema, max_retries).value
    return str(out.get("value", ""))
