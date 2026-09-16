"""Normalisers, labels, ledger and audit log (PRD phase 1)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.helpers import ROOT
from workbench.core.audit import AuditLog
from workbench.core.clock import FixedClock
from workbench.core.db import Database
from workbench.core.errors import PolicyError
from workbench.core.labels import Label, Level, PolicyEngine, high_water
from workbench.core.ledger import Ledger
from workbench.core.models import Anchor
from workbench.core.normalise import (
    convert,
    find_dates,
    find_quantities,
    glyph_compatible,
    norm_date,
    norm_party,
    norm_quantity,
    norm_tag,
    party_similarity,
)

# -- normalisers ---------------------------------------------------------------------------


def test_norm_tag_examples() -> None:
    assert norm_tag("P101-A") == norm_tag("P-101A") == norm_tag("p 101 a") == "P101A"


@given(st.from_regex(r"[A-Z]{1,3}-?[0-9]{2,4}[A-Z]?", fullmatch=True))
def test_norm_tag_idempotent_and_separator_insensitive(tag: str) -> None:
    n = norm_tag(tag)
    assert norm_tag(n) == n
    assert norm_tag(tag.replace("-", " - ")) == n
    assert norm_tag(tag.lower()) == n


def test_glyph_compatible() -> None:
    assert glyph_compatible("P-1O8B", "P-108B")
    assert glyph_compatible("P-108B", "P-1088")
    assert not glyph_compatible("WO-77231", "WO-77281")
    assert not glyph_compatible("P-108", "P-1080")


@given(st.from_regex(r"[A-Z0-9]{3,8}", fullmatch=True))
def test_glyph_compatible_is_reflexive_and_symmetric(value: str) -> None:
    other = value.replace("0", "O").replace("8", "B")
    assert glyph_compatible(value, value)
    assert glyph_compatible(value, other) == glyph_compatible(other, value)


def test_quantities_and_conversion() -> None:
    assert norm_quantity("6.2 mm") == (6.2, "millimeter")
    assert norm_quantity("4.5 MPa") == (4.5, "megapascal")
    assert convert((6.2, "millimeter"), "cm") == pytest.approx(0.62)
    assert convert((10.0, "kilogram_force / centimeter ** 2"), "bar") == pytest.approx(9.80665)
    assert norm_quantity("INR 48,50,000") == (4850000.0, "INR")
    with pytest.raises(ValueError):
        norm_quantity("6.2")


@given(st.floats(min_value=0.1, max_value=10_000, allow_nan=False).map(lambda x: round(x, 2)))
def test_quantity_round_trip(value: float) -> None:
    mag, unit = norm_quantity(f"{value} mm")
    assert mag == pytest.approx(value)
    assert convert((mag, unit), "mm") == pytest.approx(value)


def test_find_quantities_skips_tag_digits() -> None:
    found = find_quantities("P-108B casing at 5.6 mm; SOP-MECH-014 limit 6.0 mm")
    assert [(q.magnitude, q.unit) for q in found] == [(5.6, "millimeter"), (6.0, "millimeter")]


@pytest.mark.parametrize("raw", ["12/03/2024", "12-03-2024", "12.03.2024", "2024-03-12", "12 Mar 2024"])
def test_norm_date_formats(raw: str) -> None:
    assert norm_date(raw) == "2024-03-12"


def test_norm_date_dayfirst_is_configurable() -> None:
    assert norm_date("03/12/2024", dayfirst=False) == "2024-03-12"
    with pytest.raises(ValueError):
        norm_date("31/31/2024")


@given(st.dates(min_value=date(1990, 1, 1), max_value=date(2090, 12, 31)))
def test_norm_date_round_trip(d: date) -> None:
    assert norm_date(d.strftime("%d/%m/%Y")) == d.isoformat()
    assert norm_date(d.isoformat()) == d.isoformat()
    assert [x.iso for x in find_dates(f"on {d.strftime('%d.%m.%Y')} done")] == [d.isoformat()]


def test_party_normalisation() -> None:
    assert norm_party("Narmada Pumps Pvt. Ltd.") == "narmada pumps"
    assert party_similarity("Narmada Pumps Private Limited", "NARMADA PUMPS LTD") == 1.0
    assert party_similarity("Narmada Pumps", "Konkan Rotating Equipment") < 0.6


# -- labels --------------------------------------------------------------------------------


def test_label_join_and_dominates() -> None:
    a = Label(level=Level.CONFIDENTIAL)
    b = Label(level=Level.RESTRICTED, compartments=frozenset({"PROJECT-X"}))
    j = a.join(b)
    assert j.level == Level.CONFIDENTIAL and j.compartments == {"PROJECT-X"}
    assert j.dominates(a) and j.dominates(b)
    assert not a.dominates(b)
    assert Label.parse("Secret+VENDOR-COMMERCIAL").display() == "Secret · VENDOR-COMMERCIAL"


labels = st.builds(Label, level=st.sampled_from(list(Level)),
                   compartments=st.frozensets(st.sampled_from(["PROJECT-X", "VENDOR-COMMERCIAL"])))


@given(st.lists(labels, min_size=1, max_size=12))
def test_high_water_only_goes_up(seq: list[Label]) -> None:
    current = Label.lowest()
    for lab in seq:
        nxt = current.join(lab)
        assert nxt.dominates(current)
        assert nxt.dominates(lab)
        current = nxt
    assert high_water(seq) == current


def test_label_serialisation_is_stable() -> None:
    lab = Label(level=Level.SECRET, compartments=frozenset({"VENDOR-COMMERCIAL", "PROJECT-X"}))
    assert json.loads(lab.model_dump_json()) == {"level": "Secret", "compartments": ["PROJECT-X", "VENDOR-COMMERCIAL"]}


@pytest.fixture()
def policy() -> PolicyEngine:
    return PolicyEngine.from_config(ROOT / "config")


def test_policy_ceilings_and_reads(policy: PolicyEngine) -> None:
    eng = policy.user("engineer1")
    plant = policy.workspace("plant-a")
    general = policy.workspace("plant-a-general")
    conf = Label(level=Level.CONFIDENTIAL)
    assert policy.can_read(eng, plant, conf)
    assert not policy.can_read(eng, plant, Label(level=Level.SECRET))
    assert policy.can_place(conf, plant)
    assert not policy.can_place(conf, general)
    buyer = policy.user("buyer1")
    assert not policy.can_read(buyer, plant, Label(level=Level.RESTRICTED))
    proc = policy.workspace("proc")
    secret_vc = Label.parse("Secret+VENDOR-COMMERCIAL")
    assert policy.can_read(buyer, proc, secret_vc)
    assert policy.retrieval_ceiling(eng, plant) == conf


def test_marking_detection(policy: PolicyEngine) -> None:
    assert policy.policy.detect("CONFIDENTIAL\nreport") == Label(level=Level.CONFIDENTIAL)
    assert policy.policy.detect("SECRET // VENDOR-COMMERCIAL") == Label.parse("Secret+VENDOR-COMMERCIAL")
    assert policy.policy.detect("गोपनीय / text") == Label(level=Level.CONFIDENTIAL)
    assert policy.policy.detect("Unrestricted access road") is None
    assert policy.policy.detect("nothing here") is None


def test_downgrade_needs_authority_reason_and_second_person(policy: PolicyEngine) -> None:
    before, after = Label(level=Level.CONFIDENTIAL), Label(level=Level.RESTRICTED)
    with pytest.raises(PolicyError):
        policy.request_downgrade("D1", "F1", before, after, policy.user("engineer1"), "tidy")
    with pytest.raises(PolicyError):
        policy.request_downgrade("D1", "F1", before, after, policy.user("officer1"), "  ")
    with pytest.raises(PolicyError):
        policy.request_downgrade("D1", "F1", after, before, policy.user("officer1"), "raise")
    req = policy.request_downgrade("D1", "F1", before, after, policy.user("officer1"), "figures removed")
    with pytest.raises(PolicyError):
        policy.decide_downgrade(req, policy.user("officer1"), True, FixedClock().now())
    with pytest.raises(PolicyError):
        policy.decide_downgrade(req, policy.user("engineer1"), True, FixedClock().now())
    done = policy.decide_downgrade(req, policy.user("owner1"), True, FixedClock().now())
    assert done.status == "approved" and done.approver == "owner1"


# -- ledger and audit ----------------------------------------------------------------------


def _ledger(tmp_path: Path) -> Ledger:
    return Ledger(Database(tmp_path / "l.db"), FixedClock())


def test_ledger_append_only_hash_and_walk(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    low = Label(level=Level.RESTRICTED)
    a = led.add("T1", "user_input", summary="task", body="draft a note", label=low)
    b = led.add("T1", "ocr_text", summary="page", body="5.6 mm", label=Label(level=Level.CONFIDENTIAL),
                anchor=Anchor(doc="r.pdf", page=2))
    c = led.add("T1", "calc_result", summary="calc", body={"x": 1}, label=low, inputs=[b.id])
    assert (a.id, b.id, c.id) == ("R-T1-1", "R-T1-2", "R-T1-3")
    assert a.trust == "control" and b.trust == "data"
    assert led.verify("T1")
    assert [r.id for r in led.walk_inputs(c.id)] == [c.id, b.id]
    assert led.high_water("T1").level == Level.CONFIDENTIAL
    assert not hasattr(led, "update") and not hasattr(led, "delete")
    assert len(led.export("T1").strip().splitlines()) == 3
    assert [r.id for r in led.replay_source("T1")] == [a.id]


def test_ledger_hash_is_deterministic(tmp_path: Path) -> None:
    hashes = []
    for i in range(2):
        led = Ledger(Database(tmp_path / f"d{i}.db"), FixedClock())
        rec = led.add("T9", "ocr_text", summary="s", body={"b": [1, 2]}, label=Label.lowest())
        hashes.append(rec.hash)
    assert hashes[0] == hashes[1]


def test_audit_chain_detects_tampering(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl", FixedClock(tick=1))
    for i in range(5):
        log.append({"type": "step", "n": i})
    assert log.verify()
    assert log.verify_detail()["entries"] == 5
    reopened = AuditLog(tmp_path / "audit.jsonl", FixedClock())
    assert reopened.latest_hash() == log.latest_hash()
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    lines[2] = lines[2].replace('"n":2', '"n":7')
    (tmp_path / "audit.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    detail = log.verify_detail()
    assert not detail["ok"] and detail["broken_at"] == 3


def test_audit_daily_summary_has_signing_hook(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "a.jsonl", FixedClock(), signer=lambda s: "signed")
    log.append({"type": "route"})
    summary = log.daily_summary(date(2026, 9, 15))
    assert summary["entries"] == 1 and summary["by_type"] == {"route": 1} and summary["signature"] == "signed"
