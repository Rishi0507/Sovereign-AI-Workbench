"""The P&ID layout checker: it must catch the overlaps it is meant to catch, and stay quiet
when a sheet is clean, including the one case (a line-number flag) that is meant to sit on a
pipe rather than beside it."""

from __future__ import annotations

import pytest

from workbench.documents.pid_layout import Layout, LayoutError


def test_clean_layout_passes() -> None:
    lc = Layout("clean")
    lc.label(100, 100, "FIC-108", 12)
    lc.label(100, 200, "well clear of the first label", 11)
    lc.path("M0 300 H400")
    lc.check()  # no exception


def test_overlapping_labels_are_caught() -> None:
    lc = Layout("overlap")
    lc.label(200, 200, "FIC-108 flow indicating controller", 11)
    lc.label(205, 202, "PT-2101 discharge pressure transmitter", 11)
    with pytest.raises(LayoutError, match="overlap"):
        lc.check()


def test_a_label_crossing_a_line_is_caught() -> None:
    lc = Layout("crossing")
    lc.path("M0 100 H500")  # a horizontal signal line at y=100
    lc.label(200, 100, "FIC-108 flow indicating controller", 11)  # sits right on top of it
    with pytest.raises(LayoutError, match="crosses a line"):
        lc.check()


def test_a_line_number_flag_is_allowed_to_sit_on_its_own_line() -> None:
    lc = Layout("line-flag")
    lc.path('M0 300 H500')
    lc.label(200, 300, '8"-CW-1024-A1A', 10.5, on_line=True)
    lc.check()  # a line-number flag is drawn on top of the line by design


def test_manifest_collects_declared_tags() -> None:
    lc = Layout("manifest")
    lc.tag("P-108A", "pump", "booster pump A")
    lc.tag("GV-1081", "valve", "suction gate valve", subclass="gate_valve")
    assert [m["tag"] for m in lc.manifest] == ["P-108A", "GV-1081"]
    assert lc.manifest[0]["cls"] == "pump" and lc.manifest[0]["subclass"] == "pump"
    assert lc.manifest[1]["subclass"] == "gate_valve"
