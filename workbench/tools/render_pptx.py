"""``make_pptx``: one idea per slide, marking on every slide footer."""

from __future__ import annotations

import io
from typing import Any

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Emu, Pt

from workbench.tools.registry import ToolContext, ToolResult, ToolSpec, obj
from workbench.tools.render_common import label_properties, marking_text

SCHEMA = obj({
    "data": {"type": "object"},
    "name": {"type": "string", "pattern": r"^[A-Za-z0-9_.-]+\.pptx$"},
}, ["data"])


def _marking(slide: Any, prs: Any, text: str) -> None:
    box = slide.shapes.add_textbox(Emu(int(prs.slide_width * 0.05)), Emu(int(prs.slide_height * 0.93)),
                                   Emu(int(prs.slide_width * 0.9)), Emu(int(prs.slide_height * 0.05)))
    box.name = "WB Marking"
    box.text_frame.text = text
    run = box.text_frame.paragraphs[0].runs[0]
    run.font.size = Pt(10)
    run.font.bold = True
    run.font.color.rgb = RGBColor(0x9B, 0x1C, 0x1C)


def make_pptx(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    data = dict(args["data"])
    if isinstance(data.get("value"), dict):
        data = data["value"]
    label = ctx.label()
    marking = marking_text(label)
    template = ctx.rt.settings.root / "org_templates" / "deck.pptx"
    prs = Presentation(str(template)) if template.is_file() else Presentation()
    # drop template sample slides
    for sld_id in list(prs.slides._sldIdLst):
        prs.part.drop_rel(sld_id.rId)
        prs.slides._sldIdLst.remove(sld_id)
    title_slide = prs.slides.add_slide(prs.slide_layouts[0])
    title_slide.shapes.title.text = str(data.get("title", "Briefing"))
    if len(title_slide.placeholders) > 1:
        title_slide.placeholders[1].text = label.display()
    _marking(title_slide, prs, marking)
    for s in data.get("slides") or []:
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = str(s.get("title", ""))
        body = slide.placeholders[1].text_frame
        bullets = [str(b) for b in s.get("bullets") or []][:5]
        body.text = bullets[0] if bullets else ""
        for b in bullets[1:]:
            body.add_paragraph().text = b
        _marking(slide, prs, marking)
    cp = prs.core_properties
    props = label_properties(label)
    cp.title = str(data.get("title", "Briefing"))
    cp.keywords, cp.category, cp.comments = props["keywords"], props["category"], props["comments"]
    buf = io.BytesIO()
    prs.save(buf)
    name = str(args.get("name") or "deck.pptx")
    rec = ctx.rt.files.write_draft(ctx.workspace, name, buf.getvalue(), label, ctx.task_id,
                                   overwrite_ok=bool(ctx.meta.get("action_approved", True)))
    return ToolResult(ok=True, summary=f"rendered drafts/{name} with {len(prs.slides)} slide(s)",
                      files=[rec.id], body={"file": rec.relpath, "file_id": rec.id, "label": label.display()})


SPECS = [ToolSpec(name="make_pptx", description="Render a slide deck from the org slide master into drafts/.",
                  input_schema=SCHEMA, handler=make_pptx, side_effect=True, output_type="file",
                  budget_key="make_pptx")]
