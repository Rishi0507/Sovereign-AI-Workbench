"""Collision checking for generated P&ID sheets (used by ``scripts/make_fixtures.py``).

At the tag density a realistic sheet needs, hand-placed coordinates will eventually put a label
on top of another label or across a pipe. Rather than trust the coordinates by eye, every label
and every pipe segment drawn for a sheet is registered here as it is drawn, and ``check()`` raises
a ``LayoutError`` naming every collision it finds. A sheet that fails this is a bug in the
generator, not a rendering detail, so ``build_pid_sheets`` calls it on every sheet before writing
the SVG.

The same registrations double as the ground-truth manifest for the tag detector: every equipment,
valve, instrument, line-number and connector call also states the class it OUGHT to be, so the
detector's independent, regex-based classification of the tag text can be checked against it
without the check being circular.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LabelBox:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    on_line: bool = False


@dataclass
class Segment:
    x0: float
    y0: float
    x1: float
    y1: float


class LayoutError(Exception):
    """Raised when two labels overlap, or a label (other than a line-number flag, which is
    drawn on top of its own line by design) crosses a pipe segment."""


class Layout:
    """Collects one sheet's labels, pipe segments and ground-truth tags, and checks them."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.labels: list[LabelBox] = []
        self.segments: list[Segment] = []
        self.manifest: list[dict[str, str]] = []

    def label(self, x: float, y: float, text: str, size: float = 11, anchor: str = "middle",
             on_line: bool = False) -> None:
        if not text:
            return
        width = len(text) * size * 0.58
        height = size * 1.4
        x0 = x - width / 2 if anchor == "middle" else x
        x1 = x0 + width
        y0, y1 = y - height * 0.82, y + height * 0.32
        self.labels.append(LabelBox(x0, y0, x1, y1, text, on_line))

    def segment(self, x0: float, y0: float, x1: float, y1: float) -> None:
        pad = 2.0
        self.segments.append(Segment(min(x0, x1) - pad, min(y0, y1) - pad,
                                     max(x0, x1) + pad, max(y0, y1) + pad))

    def path(self, d: str) -> None:
        """Register every straight (M/H/V/L) run in an SVG ``d`` path as a segment. Curves
        (arcs, used only for vessel shells and pump casings) are not straight pipe runs and are
        skipped; nothing of interest to the text-crossing check happens inside them."""
        x = y = 0.0
        tokens = d.replace(",", " ").split()
        i = 0
        cmd = None
        while i < len(tokens):
            tok = tokens[i]
            if tok and (tok[0].isalpha()):
                cmd = tok[0]
                rest = tok[1:]
                tokens[i:i + 1] = ([rest] if rest else [])
                continue
            if cmd == "M":
                x, y = float(tokens[i]), float(tokens[i + 1])
                i += 2
            elif cmd == "H":
                nx = float(tokens[i])
                self.segment(x, y, nx, y)
                x = nx
                i += 1
            elif cmd == "V":
                ny = float(tokens[i])
                self.segment(x, y, x, ny)
                y = ny
                i += 1
            elif cmd == "L":
                nx, ny = float(tokens[i]), float(tokens[i + 1])
                self.segment(x, y, nx, ny)
                x, y = nx, ny
                i += 2
            else:  # an arc or an unrecognised command: skip its numbers, not a straight run
                i += 1

    def tag(self, tag: str, cls: str, description: str = "", subclass: str | None = None,
           bbox: tuple[float, float, float, float] | None = None) -> None:
        self.manifest.append({"tag": tag, "cls": cls, "description": description,
                              "subclass": subclass or cls, "bbox": bbox})

    @staticmethod
    def _overlap(a: LabelBox | Segment, b: LabelBox | Segment) -> bool:
        return a.x0 < b.x1 and a.x1 > b.x0 and a.y0 < b.y1 and a.y1 > b.y0

    def check(self) -> None:
        problems: list[str] = []
        for i, a in enumerate(self.labels):
            for b in self.labels[i + 1:]:
                if self._overlap(a, b):
                    problems.append(f'labels overlap: "{a.text}" and "{b.text}"')
            if not a.on_line:
                for s in self.segments:
                    if self._overlap(a, s):
                        problems.append(f'label crosses a line: "{a.text}"')
        if problems:
            shown = problems[:20]
            more = f" (+{len(problems) - 20} more)" if len(problems) > 20 else ""
            raise LayoutError(f"{self.name}: " + "; ".join(shown) + more)
