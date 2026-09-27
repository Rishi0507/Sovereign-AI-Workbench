"""Image inputs for the demo workspace: a site photograph, a handwritten shift note and a scanned sheet.

Each image comes with a page read in the same form as the scanned reports, so the pipeline treats
it like any other scanned source: page text, regions with a first reading and a second opinion,
and crops cut from the image itself.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from workbench.documents.pid_layout import Layout

FONT_DIRS = [Path("C:/Windows/Fonts"), Path("/usr/share/fonts"), Path("/Library/Fonts")]


def _font(names: list[str], size: int) -> Any:
    for folder in FONT_DIRS:
        for name in names:
            for path in folder.rglob(name) if folder.is_dir() else []:
                try:
                    return ImageFont.truetype(str(path), size)
                except OSError:
                    continue
    return ImageFont.load_default(size=size)


def _box(x0: float, y0: float, x1: float, y1: float, w: int, h: int) -> list[float]:
    return [round(x0 / w, 4), round(y0 / h, 4), round(x1 / w, 4), round(y1 / h, 4)]


def _sidecar(target: Path, text: str, regions: list[dict[str, Any]]) -> None:
    page = {"page": 1, "script": "latin", "scanned": True, "text": text, "tables": [], "regions": regions}
    data = {"doc": target.name, "engine": "fixture (stands in for PaddleOCR PP-Structure)", "pages": [page]}
    target.with_name(target.stem + ".ocr.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def _label(target: Path, level: str) -> None:
    Path(f"{target}.label.json").write_text(
        json.dumps({"label": {"level": level, "compartments": []}}, indent=2), encoding="utf-8")


def site_photo(target: Path) -> None:
    w, h = 1280, 960
    rng = random.Random(7)
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            base = 70 + int(40 * y / h)
            px[x, y] = (base + rng.randint(-6, 6), base + 4 + rng.randint(-6, 6), base + 10 + rng.randint(-6, 6))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 380, w, 600], fill=(118, 124, 130))
    d.rectangle([0, 392, w, 410], fill=(150, 156, 162))
    d.ellipse([430, 250, 850, 730], fill=(104, 110, 116), outline=(60, 64, 68), width=6)
    d.ellipse([520, 340, 760, 640], fill=(92, 96, 102), outline=(58, 60, 64), width=4)
    for i in range(12):
        import math
        a = i * math.tau / 12
        cx, cy = 640 + 180 * math.cos(a), 490 + 205 * math.sin(a)
        d.ellipse([cx - 16, cy - 16, cx + 16, cy + 16], fill=(80, 82, 86), outline=(40, 40, 42), width=3)
    for _ in range(260):
        cx, cy = rng.randint(440, 840), rng.randint(270, 720)
        r = rng.randint(4, 22)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(150 + rng.randint(0, 40), 82 + rng.randint(0, 30), 40))
    plate = [880, 170, 1200, 290]
    d.rectangle(plate, fill=(232, 228, 214), outline=(40, 40, 40), width=4)
    d.text((905, 190), "P-108B", fill=(24, 24, 24), font=_font(["arialbd.ttf", "DejaVuSans-Bold.ttf"], 72))
    gauge = [110, 640, 400, 860]
    d.rounded_rectangle(gauge, radius=18, fill=(250, 196, 40), outline=(30, 30, 30), width=4)
    d.rectangle([140, 670, 370, 760], fill=(186, 206, 170), outline=(30, 30, 30), width=3)
    d.text((160, 680), "5.6 mm", fill=(20, 30, 20), font=_font(["consola.ttf", "DejaVuSansMono.ttf"], 58))
    d.text((150, 790), "UT GAUGE  CAL 30/06/26", fill=(30, 30, 30), font=_font(["arial.ttf", "DejaVuSans.ttf"], 22))
    img = img.filter(ImageFilter.GaussianBlur(0.8))
    img.save(target, quality=86)
    text = ("Site photograph, cooling water booster pump B, casing volute bottom.\n"
            "Tag plate: P-108B\n"
            "Ultrasonic gauge reading: 5.6 mm\n"
            "External corrosion visible on the flange face and bolt heads.")
    _sidecar(target, text, [
        {"id": "p1-tag", "field_kind": "tag", "bbox": _box(*plate, w, h), "ocr_value": "P-1O8B", "ocr_conf": 0.54,
         "needs_vlm": True, "vlm_value": "P-108B", "vlm_value_zoomed": "P-108B", "truth": "P-108B"},
        {"id": "p1-gauge", "field_kind": "thickness", "bbox": _box(*gauge, w, h), "ocr_value": "5.6 mm",
         "ocr_conf": 0.71, "needs_vlm": True, "vlm_value": "5.6 mm", "vlm_value_zoomed": "5.6 mm", "truth": "5.6 mm"},
    ])
    _label(target, "Restricted")


def shift_note(target: Path) -> None:
    w, h = 1200, 900
    rng = random.Random(11)
    img = Image.new("RGB", (w, h), (246, 241, 226))
    d = ImageDraw.Draw(img)
    for y in range(150, h, 62):
        d.line([(60, y), (w - 60, y)], fill=(176, 196, 222), width=2)
    d.line([(140, 60), (140, h - 40)], fill=(222, 150, 150), width=2)
    hand = _font(["Inkfree.ttf", "segoesc.ttf", "comic.ttf", "DejaVuSans-Oblique.ttf"], 40)
    lines = ["Shift log 14/03/2026  night",
             "P-108B slight vibration on DE bearing,",
             "casing temp 62 C. UT at volute 5.6 mm.",
             "Informed J. Menon (maint). Recheck AM.",
             "R. Sharma"]
    boxes = []
    for i, line in enumerate(lines):
        x, y = 170 + rng.randint(-6, 6), 100 + i * 124 + rng.randint(-4, 4)
        d.text((x, y), line, fill=(28, 42, 110), font=hand)
        right = x + int(d.textlength(line, font=hand))
        boxes.append((x - 6, y - 4, right + 6, y + 54))
    img = img.rotate(-0.8, fillcolor=(246, 241, 226)).filter(ImageFilter.GaussianBlur(0.6))
    img.save(target, quality=86)
    _sidecar(target, "\n".join(lines), [
        {"id": "p1-tag", "field_kind": "tag", "bbox": _box(*boxes[1], w, h), "ocr_value": "P-1O8B", "ocr_conf": 0.47,
         "needs_vlm": True, "vlm_value": "P-108B", "vlm_value_zoomed": "P-108B", "truth": "P-108B"},
        {"id": "p1-reading", "field_kind": "handwriting", "bbox": _box(*boxes[2], w, h),
         "ocr_value": "casing temp 62 C. UT at volute 5.b mm.", "ocr_conf": 0.44, "needs_vlm": True,
         "vlm_value": lines[2], "vlm_value_zoomed": lines[2], "truth": lines[2]},
        {"id": "p1-signed", "field_kind": "handwriting", "bbox": _box(*boxes[4], w, h), "ocr_value": "R. Shanna",
         "ocr_conf": 0.41, "needs_vlm": True, "vlm_value": "R. Sharma", "vlm_value_zoomed": "R. Sharma",
         "truth": "R. Sharma"},
    ])
    _label(target, "Restricted")


def _stamp(img: Image.Image, cx: int, cy: int, line1: str, line2: str, angle: float = -12) -> tuple[int, int, int, int]:
    """A rubber stamp: rotated circular text pasted onto ``img``. Returns its bounding box."""
    size = 260
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    stamp_ink = (170, 30, 40, 215)
    ld.ellipse([10, 10, size - 10, size - 10], outline=stamp_ink, width=5)
    ld.ellipse([26, 26, size - 26, size - 26], outline=stamp_ink, width=2)
    f1 = _font(["arialbd.ttf", "DejaVuSans-Bold.ttf"], 24)
    f2 = _font(["arialbd.ttf", "DejaVuSans-Bold.ttf"], 19)
    ld.text((size / 2, size / 2 - 26), line1, fill=stamp_ink, font=f1, anchor="mm")
    ld.text((size / 2, size / 2 + 12), line2, fill=stamp_ink, font=f2, anchor="mm")
    layer = layer.rotate(angle, resample=Image.BICUBIC, expand=True)
    img.paste(layer, (cx - layer.width // 2, cy - layer.height // 2), layer)
    return (cx - layer.width // 2, cy - layer.height // 2, cx + layer.width // 2, cy + layer.height // 2)


def _revision_cloud(d: ImageDraw.ImageDraw, cx: int, cy: int, rx: int, ry: int, n: int = 14) -> None:
    """A hand-drafted revision cloud: a ring of overlapping bumps, the usual markup convention."""
    import math
    color = (140, 30, 140)
    for i in range(n):
        a = i * math.tau / n
        bx, by = cx + rx * math.cos(a), cy + ry * math.sin(a)
        r = 13 + (i % 3)
        d.arc([bx - r, by - r, bx + r, by + r], 200, 520, fill=color, width=3)


def _rasterize_svg(svg_text: str, w: int, h: int) -> Image.Image:
    """Render a generated SVG sheet to a raster at its own pixel size, with the PyMuPDF SVG
    renderer already used elsewhere in this script for PDFs, so the scan shows the real drawing
    rather than a simplified stand-in for it."""
    doc = fitz.open(stream=svg_text.encode("utf-8"), filetype="svg")
    page = doc[0]
    scale = w / page.rect.width
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    doc.close()
    return img if (pix.width, pix.height) == (w, h) else img.resize((w, h))


def _garble_confusable(tag: str) -> str:
    """The first 0 or 1 in a tag misread as its look-alike letter, e.g. ``"P-108B"`` ->
    ``"P-1O8B"``; a tag with neither digit comes back unchanged."""
    for i, c in enumerate(tag):
        if c == "0":
            return tag[:i] + "O" + tag[i + 1:]
        if c == "1":
            return tag[:i] + "I" + tag[i + 1:]
    return tag


def _ocr_region(rid: str, value: str, bbox_px: tuple[float, float, float, float], conf: float,
               w: int = 1600, h: int = 1000, needs_vlm: bool = False, vlm_value: str | None = None) -> dict[str, Any]:
    truth = vlm_value or value
    return {"id": rid, "field_kind": "tag", "bbox": _box(*bbox_px, w, h), "ocr_value": value,
           "ocr_conf": round(conf, 2), "needs_vlm": needs_vlm, "vlm_value": truth,
           "vlm_value_zoomed": truth, "truth": truth}


def scan_from_sheet(target: Path, svg_path: Path, lc: Layout, seed: int, rotate: float,
                    stamp_lines: tuple[str, str], markup_lines: tuple[str, str]) -> None:
    """A scanned raster of a generated sheet: the SVG is rendered as-is, then degraded the way a
    photocopied, years-old drawing would be (skewed, speckled, blurred), stamped, and marked up
    with a hand-drawn revision cloud.

    The OCR sidecar lists every tag the sheet declares (``lc.manifest``), each with the kind of
    noise a real OCR pass leaves: most tags read cleanly at a plausible confidence; some come
    back as two separate words (a hyphen misread as a space, or an instrument bubble's two lines
    read apart, e.g. "FIC" and "108"); some have a digit misread as a look-alike letter, corrected
    by a second, vision read the same way the site photo and shift note fixtures already are.
    """
    w, h = 1600, 1000
    rng = random.Random(seed)
    img = _rasterize_svg(svg_path.read_text(encoding="utf-8"), w, h).convert("RGB")
    tint = Image.new("RGB", (w, h), (238, 236, 228))
    img = Image.blend(img, tint, 0.12)
    px = img.load()
    for _ in range(w * h // 90):
        x, y = rng.randint(0, w - 1), rng.randint(0, h - 1)
        v = rng.randint(150, 235)
        px[x, y] = (v, v - 2, v - 8)
    d = ImageDraw.Draw(img)
    hand = _font(["Inkfree.ttf", "segoesc.ttf", "comic.ttf", "DejaVuSans-Oblique.ttf"], 24)
    cloud_x, cloud_y = w * 0.60, h * 0.16
    _revision_cloud(d, cloud_x, cloud_y, 100, 70)
    d.text((cloud_x + 80, cloud_y - 55), markup_lines[0], fill=(120, 20, 120), font=hand)
    d.text((cloud_x + 80, cloud_y - 25), markup_lines[1], fill=(120, 20, 120), font=hand)
    stamp_box = _stamp(img, int(w * 0.85), int(h * 0.72), *stamp_lines)
    img = img.rotate(rotate, fillcolor=(238, 236, 228), resample=Image.BICUBIC).filter(ImageFilter.GaussianBlur(0.7))
    img.save(target, quality=84)

    regions: list[dict[str, Any]] = []
    text_lines: list[str] = []
    for i, entry in enumerate(lc.manifest):
        tag, cls, bbox = entry["tag"], entry["cls"], entry.get("bbox")
        if not bbox or cls in {"line", "connector"}:
            continue  # the OCR sidecar carries equipment-ish tags; drawing furniture is skipped
        x0, y0, x1, y1 = bbox
        text_lines.append(tag)
        if cls == "instrument" and "-" in tag:
            kind, loop = tag.split("-", 1)
            my = y0 + (y1 - y0) * 0.5
            regions.append(_ocr_region(f"r{i}a", kind, (x0, y0 + 6, x1, my), 0.82))
            regions.append(_ocr_region(f"r{i}b", loop, (x0, my, x1, y1 - 6), 0.82))
        elif i % 5 == 1 and "-" in tag:
            prefix, rest = tag.split("-", 1)
            midx = x0 + (x1 - x0) * 0.45
            regions.append(_ocr_region(f"r{i}a", prefix, (x0, y0, midx, y1), 0.77))
            regions.append(_ocr_region(f"r{i}b", rest, (midx, y0, x1, y1), 0.77))
        elif i % 5 == 3:
            garbled = _garble_confusable(tag)
            regions.append(_ocr_region(f"r{i}", garbled, (x0, y0, x1, y1), 0.52,
                                       needs_vlm=garbled != tag, vlm_value=tag))
        else:
            regions.append(_ocr_region(f"r{i}", tag, (x0, y0, x1, y1), round(rng.uniform(0.78, 0.94), 2)))
    stamp_text = " ".join(stamp_lines)
    regions.append({"id": "stamp", "field_kind": "stamp", "bbox": _box(*stamp_box, w, h),
                    "ocr_value": stamp_text, "ocr_conf": 0.6, "needs_vlm": True, "vlm_value": stamp_text,
                    "vlm_value_zoomed": stamp_text, "truth": stamp_text})
    markup_text = " ".join(markup_lines)
    markup_box = (cloud_x - 20, cloud_y - 65, cloud_x + 300, cloud_y + 85)
    regions.append({"id": "markup", "field_kind": "markup", "bbox": _box(*markup_box, w, h),
                    "ocr_value": markup_text, "ocr_conf": 0.4, "needs_vlm": True, "vlm_value": markup_text,
                    "vlm_value_zoomed": markup_text, "truth": markup_text})
    _sidecar(target, "\n".join(text_lines), regions)
    _label(target, "Restricted")


def build_images(ws_inputs: Path, layouts: dict[str, Layout]) -> None:
    site_photo(ws_inputs / "photo_P108B_flange.jpg")
    shift_note(ws_inputs / "handwritten_shift_note.jpg")
    scan_from_sheet(ws_inputs / "PID-CW-003_scan.jpg", ws_inputs / "PID-CW-003.svg", layouts["PID-CW-003"],
                   seed=3, rotate=0.6, stamp_lines=("APPROVED FOR", "CONSTRUCTION"),
                   markup_lines=("TIE-IN FOR V-302 PLANNED", "SEE RFI-098"))
    scan_from_sheet(ws_inputs / "PID-AM-002_scan.jpg", ws_inputs / "PID-AM-002.svg", layouts["PID-AM-002"],
                   seed=5, rotate=-0.9, stamp_lines=("AS BUILT", "REV B"),
                   markup_lines=("FIELD ROUTING CHANGED", "SEE NOTE 4"))
