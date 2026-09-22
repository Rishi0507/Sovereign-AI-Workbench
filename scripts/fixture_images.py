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

from PIL import Image, ImageDraw, ImageFilter, ImageFont

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


def scanned_sheet(target: Path) -> None:
    w, h = 1560, 960
    rng = random.Random(3)
    img = Image.new("RGB", (w, h), (238, 236, 228))
    d = ImageDraw.Draw(img)
    ink = (38, 42, 48)
    tag_font = _font(["arialbd.ttf", "DejaVuSans-Bold.ttf"], 22)
    small = _font(["arial.ttf", "DejaVuSans.ttf"], 17)
    d.rectangle([12, 12, w - 12, h - 12], outline=ink, width=2)
    tags: dict[str, tuple[int, int, int, int]] = {}
    for x, tag, note in ((450, "P-108A", "duty"), (840, "P-108B", "standby")):
        d.ellipse([x - 40, 455, x + 40, 535], outline=ink, width=3)
        d.polygon([(x - 15, 477), (x + 22, 495), (x - 15, 513)], outline=ink, width=3)
        d.text((x - 42, 548), tag, fill=ink, font=tag_font)
        d.text((x - 30, 578), note, fill=ink, font=small)
        tags[tag] = (x - 60, 440, x + 60, 605)
    d.line([(90, 495), (410, 495)], fill=ink, width=3)
    d.line([(210, 495), (210, 700), (800, 700), (800, 495)], fill=ink, width=3)
    d.line([(450, 455), (450, 290), (1140, 290)], fill=ink, width=3)
    d.line([(840, 455), (840, 290)], fill=ink, width=3)
    d.ellipse([1140, 256, 1208, 324], outline=ink, width=3)
    d.line([(1140, 290), (1208, 290)], fill=ink, width=2)
    d.text((1156, 262), "FT", fill=ink, font=small)
    d.text((1154, 294), "108", fill=ink, font=small)
    tags["FT-108"] = (1130, 246, 1218, 334)
    d.line([(1208, 290), (1480, 290)], fill=ink, width=3)
    d.text((95, 468), "CW-12 cooling water supply, 8 in", fill=ink, font=small)
    d.text((1230, 262), "CW-14 to cooling tower, 8 in", fill=ink, font=small)
    d.rectangle([1070, 780, 1540, 940], outline=ink, width=2)
    d.text((1086, 792), "PID-CW-003   Rev C", fill=ink, font=tag_font)
    d.text((1086, 832), "Cooling water booster pumps", fill=ink, font=small)
    d.text((1086, 868), "RESTRICTED", fill=(150, 30, 30), font=tag_font)
    px = img.load()
    for _ in range(22000):
        x, y = rng.randint(0, w - 1), rng.randint(0, h - 1)
        v = rng.randint(200, 235)
        px[x, y] = (v, v - 2, v - 8)
    img = img.rotate(0.6, fillcolor=(238, 236, 228)).filter(ImageFilter.GaussianBlur(0.7))
    img.save(target, quality=84)
    text = ("PID-CW-003 Rev C  Cooling water booster pumps\n"
            "P-108A cooling water booster pump A, duty\n"
            "P-108B cooling water booster pump B, standby\n"
            "FT-108 flow transmitter on common discharge\n"
            "CW-12 cooling water supply header, 8 in\n"
            "CW-14 to cooling tower, 8 in")
    regions = []
    for i, (tag, box) in enumerate(tags.items(), start=1):
        misread = tag.replace("108", "1O8") if i == 2 else tag
        regions.append({"id": f"p1-tag{i}", "field_kind": "tag", "bbox": _box(*box, w, h), "ocr_value": misread,
                        "ocr_conf": 0.5 if misread != tag else 0.83, "needs_vlm": misread != tag,
                        "vlm_value": tag, "vlm_value_zoomed": tag, "truth": tag})
    _sidecar(target, text, regions)
    _label(target, "Restricted")


def build_images(ws_inputs: Path) -> None:
    site_photo(ws_inputs / "photo_P108B_flange.jpg")
    shift_note(ws_inputs / "handwritten_shift_note.jpg")
    scanned_sheet(ws_inputs / "PID-CW-003_scan.jpg")
