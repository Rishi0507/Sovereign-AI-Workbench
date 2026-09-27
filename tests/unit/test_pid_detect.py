"""The deterministic tag detector behind the drawings viewer's overlay."""

from __future__ import annotations

from workbench.documents.pid_detect import classify_tag, detect_ocr, detect_sheet, detect_svg, link_to_register
from workbench.documents.readers import PageRead, Region

SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50">
  <g class="pid-item" data-tag="P-108A"><title>P-108A pump</title>
    <rect class="pid-hit" x="0" y="0" width="20" height="20" fill="transparent" stroke="none"/>
  </g>
  <g class="pid-item" data-tag="FIC-108"><title>FIC-108 controller</title>
    <rect class="pid-hit" x="40" y="0" width="10" height="10" fill="transparent" stroke="none"/>
  </g>
  <g class="pid-line" data-line="8&quot;-CW-1024-A1A">
    <rect class="pid-hit" x="0" y="30" width="30" height="5" fill="transparent" stroke="none"/>
  </g>
  <g class="pid-ref" data-ref="P-108C">
    <rect class="pid-hit" x="60" y="30" width="20" height="5" fill="transparent" stroke="none"/>
  </g>
</svg>'''


def test_classify_tag_reads_isa_prefixes_not_a_lookup_table() -> None:
    assert classify_tag("P-108A").cls == "pump"
    assert classify_tag("V-301").cls == "vessel"
    assert classify_tag("E-201").cls == "exchanger"
    assert classify_tag("GV-1081").cls == "valve"
    assert classify_tag("CHK-1083").cls == "valve"
    assert classify_tag("FCV-108").cls == "control_valve"
    assert classify_tag("PSV-108").cls == "relief_valve"
    ft = classify_tag("FT-108")
    assert ft.cls == "instrument" and ft.meta["measured"] == "flow" and ft.meta["function"] == "transmitter"
    fic = classify_tag("FIC-108")
    assert fic.cls == "instrument" and fic.meta["function"] == "indicator controller"
    assert classify_tag("random text").cls == "unknown"


def test_detect_svg_reads_boxes_from_the_click_target_geometry() -> None:
    found = {d.tag: d for d in detect_svg(SVG)}
    assert found["P-108A"].cls == "pump"
    assert found["P-108A"].bbox == (0.0, 0.0, 0.2, 0.4)
    assert found["FIC-108"].cls == "instrument"
    assert found['8"-CW-1024-A1A'].cls == "line"
    # A tag mentioned only in the notes is still detected, just from a different source.
    assert found["P-108C"].cls == "pump" and found["P-108C"].source == "note"


def test_unregistered_tags_are_flagged_missing_from_the_asset_register() -> None:
    result = detect_sheet("PID-CW-003.svg", SVG, None, known_tags={"P-108A", "FIC-108"})
    assert result.summary.total == 4
    assert result.summary.unknown_tags == ["P-108C"]
    by_tag = {d.tag: d for d in result.detections}
    assert by_tag["P-108A"].known is True
    assert by_tag["P-108C"].known is False
    # Line numbers are drawing furniture, not equipment, so they are never checked.
    assert by_tag['8"-CW-1024-A1A'].known is None


def test_detect_ocr_classifies_a_tag_region_and_a_handwritten_mention() -> None:
    page = PageRead(page=1, scanned=True, text="scan", regions=[
        Region(id="r1", bbox=(0.1, 0.1, 0.2, 0.2), field_kind="tag", ocr_value="P-1O8B", ocr_conf=0.5,
              needs_vlm=True, vlm_value="P-108B"),
        Region(id="r2", bbox=(0.5, 0.5, 0.9, 0.6), field_kind="markup", ocr_value="TIE-IN FOR V-3O2",
              ocr_conf=0.4, needs_vlm=True, vlm_value="TIE-IN FOR V-302 PLANNED"),
    ])
    detections = detect_ocr([page])
    by_tag = {d.tag: d for d in detections}
    assert by_tag["P-108B"].cls == "pump" and by_tag["P-108B"].source == "ocr"
    assert by_tag["P-108B"].confidence_bucket in {"medium", "high"}
    assert by_tag["V-302"].cls == "vessel" and by_tag["V-302"].source == "handwritten-annotation"
    assert by_tag["V-302"].confidence < by_tag["P-108B"].confidence

    linked = link_to_register(detections, known_tags={"P-108B"})
    assert linked.summary.unknown_tags == ["V-302"]


def test_detect_ocr_merges_a_two_line_instrument_bubble_and_a_space_split_tag() -> None:
    page = PageRead(page=1, scanned=True, text="scan", regions=[
        Region(id="a", bbox=(0.60, 0.10, 0.66, 0.14), field_kind="tag", ocr_value="FT", ocr_conf=0.8,
              needs_vlm=False),
        Region(id="b", bbox=(0.60, 0.145, 0.66, 0.185), field_kind="tag", ocr_value="108", ocr_conf=0.8,
              needs_vlm=False),
        Region(id="c", bbox=(0.30, 0.30, 0.33, 0.34), field_kind="tag", ocr_value="GV", ocr_conf=0.75,
              needs_vlm=False),
        Region(id="d", bbox=(0.335, 0.30, 0.38, 0.34), field_kind="tag", ocr_value="1081", ocr_conf=0.75,
              needs_vlm=False),
    ])
    by_tag = {d.tag: d for d in detect_ocr([page])}
    assert by_tag["FT-108"].cls == "instrument"
    assert by_tag["GV-1081"].cls == "valve"


def test_normalise_confusions_fixes_a_digit_run_without_touching_a_real_suffix() -> None:
    assert classify_tag("P-1O8B").cls == "unknown"  # the raw OCR text is not yet a valid tag
    page = PageRead(page=1, scanned=True, text="scan", regions=[
        Region(id="a", bbox=(0.1, 0.1, 0.2, 0.2), field_kind="tag", ocr_value="P-1O8B", ocr_conf=0.7,
              needs_vlm=False),
    ])
    by_tag = {d.tag: d for d in detect_ocr([page])}
    assert by_tag["P-108B"].cls == "pump"
