"""Deterministic tag detection on P&ID sheets, SVG or scanned (PRD-style extension, drawings tab).

For an SVG sheet, every tagged item was drawn with an invisible ``pid-hit`` rectangle, the same
geometry the UI's click handler uses, so the detector reads that back out of the markup instead of
keeping a second copy of where things are. For a scanned sheet, the OCR sidecar's regions stand in
for a vision OCR pass; a handwritten region is parsed by regex rather than trusted as a single
clean tag, since a scrawled margin note is not one.

Classification never looks the tag up anywhere: it parses the tag text itself against ISA-5.1
loop-tag conventions (a measured variable letter followed by function letters, e.g. ``FIC`` is a
flow indicating controller). That is what lets it work on a tag it has never seen, and what makes
"is this tag in the asset register" a real question rather than a lookup that always succeeds.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from pydantic import BaseModel, Field

from workbench.documents.readers import PageRead

SVG_NS = "{http://www.w3.org/2000/svg}"

TAG_STRUCT_RE = re.compile(r"^(?P<prefix>[A-Z]{1,5})-(?P<num>\d{2,5})(?P<suffix>[A-Z]{0,2})$")
TAG_FIND_RE = re.compile(r"\b[A-Z]{1,5}-\d{2,5}[A-Z]{0,2}\b")

# A lenient version of the same shape, for OCR text where a digit may have come back as O or I
# (see ``_normalise_confusions``). S/B are deliberately not treated as digit look-alikes here,
# since B is also a genuine, common suffix letter ("A"/"B" for a duty/standby pair) and guessing
# wrong would corrupt a real tag; that case is left to the dual-read (VLM) correction instead.
LENIENT_TAG_RE = re.compile(r"^(?P<prefix>[A-Z]{1,5})-(?P<num>[0-9OI]{2,5})(?P<suffix>[A-Z]{0,2})$")
BARE_PREFIX_RE = re.compile(r"^[A-Z]{1,5}$")
BARE_NUM_RE = re.compile(r"^\d{2,5}[A-Z]{0,2}$")
_CONFUSION_IN_DIGITS = str.maketrans({"O": "0", "I": "1"})

MEASURED_VARS = {"F": "flow", "L": "level", "P": "pressure", "T": "temperature", "A": "analyzer"}
INSTRUMENT_FUNCTIONS = {"I": "indicator", "C": "controller", "T": "transmitter", "G": "gauge",
                        "R": "recorder", "E": "element", "S": "switch", "A": "alarm", "H": "high"}

# Classes a detection can carry. "line" and "connector" are drawing furniture, not equipment, so
# they are never checked against the asset register (see REGISTERABLE_CLASSES below).
REGISTERABLE_CLASSES = frozenset({"pump", "vessel", "exchanger", "instrument", "valve",
                                  "control_valve", "relief_valve", "strainer"})


@dataclass(frozen=True)
class TagInfo:
    cls: str
    meta: dict[str, str]


def classify_tag(raw: str) -> TagInfo:
    """Classify a bare tag by its ISA-5.1-style prefix; parsing, not a lookup table."""
    tag = raw.strip().upper()
    m = TAG_STRUCT_RE.match(tag)
    if not m:
        return TagInfo("unknown", {})
    prefix = m.group("prefix")
    if prefix == "GV":
        return TagInfo("valve", {"kind": "gate"})
    if prefix == "CHK":
        return TagInfo("valve", {"kind": "check"})
    if prefix == "XV":
        return TagInfo("valve", {"kind": "shutdown"})
    if prefix == "STR":
        return TagInfo("strainer", {})
    if prefix.endswith("CV") and len(prefix) > 2:
        return TagInfo("control_valve", {"measured": MEASURED_VARS.get(prefix[0], prefix[0])})
    if prefix.endswith("SV") and len(prefix) > 2:
        return TagInfo("relief_valve", {})
    if prefix == "P":
        return TagInfo("pump", {})
    if prefix == "V":
        return TagInfo("vessel", {})
    if prefix == "E":
        return TagInfo("exchanger", {})
    if len(prefix) > 1 and prefix[0] in MEASURED_VARS and all(c in INSTRUMENT_FUNCTIONS for c in prefix[1:]):
        function = " ".join(INSTRUMENT_FUNCTIONS[c] for c in prefix[1:])
        return TagInfo("instrument", {"measured": MEASURED_VARS[prefix[0]], "function": function})
    return TagInfo("unknown", {})


class Detection(BaseModel):
    tag: str
    cls: str
    bbox: tuple[float, float, float, float]  # x0, y0, x1, y1, as a fraction of the sheet
    confidence: float
    confidence_bucket: str
    source: str
    known: bool | None = None
    meta: dict[str, str] = Field(default_factory=dict)


class DetectionSummary(BaseModel):
    total: int
    by_class: dict[str, int]
    unknown_count: int
    unknown_tags: list[str]


class DetectionResult(BaseModel):
    detections: list[Detection]
    summary: DetectionSummary


def _bucket(score: float) -> str:
    if score >= 0.85:
        return "high"
    if score >= 0.6:
        return "medium"
    return "low"


def _viewbox(root: ET.Element) -> tuple[float, float]:
    parts = root.get("viewBox", "0 0 1200 780").split()
    return float(parts[2]), float(parts[3])


def _hit_box(g: ET.Element, vw: float, vh: float) -> tuple[float, float, float, float] | None:
    """The invisible click-target rectangle drawn inside ``g``, as a fraction of the sheet."""
    for el in g.iter():
        if el.get("class") == "pid-hit":
            x, y = float(el.get("x", 0)), float(el.get("y", 0))
            w, h = float(el.get("width", 0)), float(el.get("height", 0))
            return (x / vw, y / vh, (x + w) / vw, (y + h) / vh)
    return None


def _normalise_confusions(raw: str) -> str:
    """Undo the OCR confusions ISA tags run into most: a digit run misread as O/I/S/B, e.g.
    ``"P-1O8B"`` -> ``"P-108B"``. Only the numeric run is touched; a real letter prefix or suffix
    is left alone, so this never turns a genuine letter into a digit by accident."""
    tag = raw.strip().upper()
    m = LENIENT_TAG_RE.match(tag)
    if not m:
        return tag
    fixed_num = m.group("num").translate(_CONFUSION_IN_DIGITS)
    return f"{m.group('prefix')}-{fixed_num}{m.group('suffix')}"


def _merge_split_words(items: list[tuple[str, tuple[float, float, float, float], float]]
                       ) -> list[tuple[str, tuple[float, float, float, float], float]]:
    """Merge a bare letter prefix and a bare number that OCR read as two separate words: the two
    lines of an instrument bubble (stacked vertically) or a hyphen misread as a space between two
    words on the same line (side by side). ``items`` is (value, bbox, confidence); merged items
    keep the lower of the two confidences and the union of their boxes."""
    used = [False] * len(items)
    out: list[tuple[str, tuple[float, float, float, float], float]] = []
    for i, (value_a, box_a, conf_a) in enumerate(items):
        if used[i] or not BARE_PREFIX_RE.match(value_a):
            continue
        ax0, ay0, ax1, ay1 = box_a
        a_h, a_w = ay1 - ay0, ax1 - ax0
        best = None
        for j, (value_b, box_b, conf_b) in enumerate(items):
            if i == j or used[j] or not BARE_NUM_RE.match(value_b):
                continue
            bx0, by0, bx1, by1 = box_b
            stacked = abs(bx0 - ax0) < a_w and 0 <= (by0 - ay1) < a_h * 1.5
            beside = abs(by0 - ay0) < a_h and 0 <= (bx0 - ax1) < a_w * 3
            if stacked or beside:
                best = j
                break
        if best is not None:
            value_b, box_b, conf_b = items[best]
            used[i] = used[best] = True
            merged_box = (min(ax0, box_b[0]), min(ay0, box_b[1]), max(ax1, box_b[2]), max(ay1, box_b[3]))
            out.append((f"{value_a}-{value_b}", merged_box, min(conf_a, conf_b)))
    for i, (value, box, conf) in enumerate(items):
        if not used[i]:
            out.append((value, box, conf))
    return out


def detect_svg(svg_text: str) -> list[Detection]:
    """Every tagged item, line number, off-page connector and note mention on a generated sheet."""
    root = ET.fromstring(svg_text)
    vw, vh = _viewbox(root)
    out: list[Detection] = []
    for g in root.iter(f"{SVG_NS}g"):
        cls = g.get("class")
        box = _hit_box(g, vw, vh)
        if box is None:
            continue
        if cls == "pid-item":
            tag = g.get("data-tag")
            if tag:
                info = classify_tag(tag)
                out.append(Detection(tag=tag, cls=info.cls, bbox=box, confidence=1.0,
                                     confidence_bucket="high", source="drawn", meta=info.meta))
        elif cls == "pid-line":
            text = g.get("data-line")
            if text:
                out.append(Detection(tag=text, cls="line", bbox=box, confidence=1.0,
                                     confidence_bucket="high", source="drawn", meta={}))
        elif cls == "pid-connector":
            to = g.get("data-to")
            if to:
                out.append(Detection(tag=to, cls="connector", bbox=box, confidence=1.0,
                                     confidence_bucket="high", source="drawn", meta={}))
        elif cls == "pid-ref":
            ref = g.get("data-ref")
            if ref:
                info = classify_tag(ref)
                out.append(Detection(tag=ref, cls=info.cls, bbox=box, confidence=0.9,
                                     confidence_bucket="medium", source="note", meta=info.meta))
    return out


def detect_ocr(pages: list[PageRead]) -> list[Detection]:
    """Detect tags on a scanned P&ID from its OCR sidecar (see ``readers.sidecar_for``).

    A ``tag`` region is classified directly, after two repairs a real OCR pass needs: a digit run
    misread as O/I/S/B is normalised back (``_normalise_confusions``), and a tag OCR split into
    two words, a hyphen read as a space or the two lines of an instrument bubble, is merged back
    into one before classification (``_merge_split_words``). A ``handwriting`` or ``markup``
    region (a margin note or a revision cloud) is not trusted as a single clean tag, so it is
    scanned by regex instead, and any hit keeps a lower confidence than a tag the sheet drew.
    """
    out: list[Detection] = []
    for page in pages:
        tag_items: list[tuple[str, tuple[float, float, float, float], float]] = []
        for region in page.regions:
            corrected = region.needs_vlm and region.vlm_value is not None
            value = region.vlm_value if corrected else region.ocr_value
            if region.field_kind == "tag":
                score = round((region.ocr_conf + 0.93) / 2, 2) if corrected else region.ocr_conf
                tag_items.append((_normalise_confusions(value), region.bbox, score))
                continue
            if region.field_kind == "stamp":
                out.append(Detection(tag=value, cls="stamp", bbox=region.bbox, confidence=region.ocr_conf,
                                     confidence_bucket=_bucket(region.ocr_conf), source="ocr", meta={}))
            elif region.field_kind in {"handwriting", "markup"}:
                score = 0.55 if corrected else region.ocr_conf
                for match in TAG_FIND_RE.finditer(value.upper()):
                    info = classify_tag(match.group(0))
                    if info.cls == "unknown":
                        continue
                    out.append(Detection(tag=match.group(0), cls=info.cls, bbox=region.bbox, confidence=score,
                                         confidence_bucket=_bucket(score), source="handwritten-annotation",
                                         meta=info.meta))
        for value, bbox, score in _merge_split_words(tag_items):
            info = classify_tag(value)
            out.append(Detection(tag=value, cls=info.cls, bbox=bbox, confidence=score,
                                 confidence_bucket=_bucket(score), source="ocr", meta=info.meta))
    return out


def link_to_register(detections: list[Detection], known_tags: set[str]) -> DetectionResult:
    """Mark every equipment-ish detection known or unknown against the plant graph's tags.

    Drawing furniture (line numbers, off-page connectors) and stamps are never checked: they are
    not equipment, so "not in the register" would not mean anything for them.
    """
    upper_known = {t.upper() for t in known_tags}
    linked = [d.model_copy(update={"known": d.tag.upper() in upper_known if d.cls in REGISTERABLE_CLASSES else None})
              for d in detections]
    by_class: dict[str, int] = {}
    for d in linked:
        by_class[d.cls] = by_class.get(d.cls, 0) + 1
    unknown_tags = sorted({d.tag for d in linked if d.known is False})
    return DetectionResult(detections=linked, summary=DetectionSummary(
        total=len(linked), by_class=by_class, unknown_count=len(unknown_tags), unknown_tags=unknown_tags))


def detect_sheet(name: str, svg_text: str | None, pages: list[PageRead] | None,
                 known_tags: set[str]) -> DetectionResult:
    """Detect tags on a sheet and link them to the plant graph. One entry point for the API:
    an SVG sheet is read from its markup, a scanned sheet from its OCR sidecar pages."""
    if name.lower().endswith(".svg") and svg_text is not None:
        detections = detect_svg(svg_text)
    elif pages is not None:
        detections = detect_ocr(pages)
    else:
        detections = []
    return link_to_register(detections, known_tags)
