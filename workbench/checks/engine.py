"""Deterministic consistency checker (README section 4.6.1).

The model only extracts facts. Comparison is code: normalised matching for tags and document
numbers, pint conversion before comparing a measurement with a limit, date arithmetic for
validity windows and fuzzy matching with a fixed threshold for party names. Facts marked
``uncertain`` are never compared; they are reported as ``not_checked``.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from workbench.checks.rules import RuleSpec
from workbench.checks.trend import TrendPoint, project
from workbench.core.labels import Label, high_water
from workbench.core.ledger import Ledger
from workbench.core.models import Anchor, LedgerRecord
from workbench.core.normalise import (
    convert,
    find_quantities,
    norm_date,
    norm_quantity,
    norm_tag,
    parse_iso,
    party_similarity,
    units_compatible,
)

Status = Literal["pass", "mismatch", "not_found", "not_checked"]


class CheckResult(BaseModel):
    id: str
    rule: str
    description: str = ""
    status: Status
    left_value: str | None = None
    right_value: str | None = None
    left_anchor: Anchor | None = None
    right_anchor: Anchor | None = None
    left_record: str | None = None
    right_record: str | None = None
    note: str = ""
    record_id: str | None = None
    extra: dict[str, Any] = {}


@dataclass
class Item:
    value: Any
    record: str | None
    confidence: str = "high"
    context: str = ""
    unit: str | None = None


@dataclass
class CheckContext:
    task_id: str
    facts: dict[str, Any]
    ledger: Ledger
    records: list[LedgerRecord]
    produced_by: str
    document_type: str = "inspection_report"
    dayfirst: bool = True
    party_threshold: float = 0.85
    results: list[CheckResult] = field(default_factory=list)


def load_register(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _norm_generic(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


class CheckEngine:
    def __init__(self, rules: list[RuleSpec], register_path: Path) -> None:
        self.rules = rules
        self.register_path = register_path
        self.register = load_register(register_path)

    # -- resolution ------------------------------------------------------------------------------

    def _fact(self, ctx: CheckContext, name: str) -> Item | None:
        raw = ctx.facts.get(name)
        if raw is None:
            return None
        if isinstance(raw, dict) and "value" in raw:
            return Item(raw["value"], raw.get("record"), raw.get("confidence", "high"), name)
        return Item(raw, None, "high", name)

    def _left_items(self, ctx: CheckContext, expr: str) -> list[Item]:
        path = expr.removeprefix("fact.")
        m = re.fullmatch(r"(\w+)\[(\*|(\w+)=(\w+))\]\.(\w+)", path)
        if m:
            rows = ctx.facts.get(m.group(1)) or []
            key, want, attr = m.group(3), m.group(4), m.group(5)
            items = []
            for row in rows:
                if key and str(row.get(key)) != want:
                    continue
                items.append(Item(row.get(attr), row.get("record"), row.get("confidence", "high"),
                                  str(row.get("location", "")), row.get("unit")))
            return items
        item = self._fact(ctx, path)
        return [item] if item is not None else []

    def _template(self, ctx: CheckContext, text: str) -> str | None:
        m = re.fullmatch(r"\{fact\.(\w+)\}", text.strip())
        if not m:
            return text
        item = self._fact(ctx, m.group(1))
        return None if item is None else str(item.value)

    def _register_row(self, tag: str) -> tuple[int, dict[str, str]] | None:
        for i, row in enumerate(self.register):
            if norm_tag(row.get("tag", "")) == norm_tag(tag):
                return i + 2, row
        return None

    def _register_anchor(self, row_no: int) -> Anchor:
        return Anchor(doc=f"{self.register_path.name} row {row_no}")

    def _record_anchor(self, ctx: CheckContext, rid: str | None) -> Anchor | None:
        if not rid:
            return None
        rec = next((r for r in ctx.records if r.id == rid), None) or ctx.ledger.maybe(rid)
        return rec.anchor if rec else None

    def kb_limit(self, ctx: CheckContext, kind: str, tag: str | None) -> Item | None:
        words = {"minimum_thickness": ("minimum", "thickness"), "maximum_pressure": ("maximum", "pressure")}.get(
            kind, tuple(kind.split("_")))
        cls_words: list[str] = []
        if tag:
            found = self._register_row(tag)
            if found:
                cls_words = found[1].get("class", "").replace("_", " ").split()
        best: tuple[int, Item] | None = None
        for rec in ctx.records:
            if rec.kind != "kb_chunk":
                continue
            text = rec.body_text()
            low = text.lower()
            if not all(w in low for w in words):
                continue
            for sentence in re.split(r"(?<=[.;])\s+", text):
                s_low = sentence.lower()
                if not all(w in s_low for w in words):
                    continue
                qs = [q for q in find_quantities(sentence) if q.unit and units_compatible(q.unit, "millimeter")]
                if not qs:
                    continue
                score = sum(1 for w in cls_words if w.rstrip("s") in s_low)
                item = Item(qs[0].magnitude, rec.id, "high", sentence.strip(), qs[0].unit)
                if best is None or score > best[0]:
                    best = (score, item)
        return best[1] if best else None

    # -- comparison --------------------------------------------------------------------------------

    @staticmethod
    def _quantity(item: Item) -> tuple[float, str | None]:
        if isinstance(item.value, (int, float)):
            return float(item.value), item.unit
        mag, unit = norm_quantity(str(item.value))
        return mag, unit

    def _compare(self, rule: RuleSpec, ctx: CheckContext, left: Item, right: Item | None,
                 register_values: list[str] | None = None) -> tuple[Status, str]:
        op = rule.compare
        if op == "exists_normalised":
            values = {norm_tag(v) for v in (register_values or [])}
            ok = norm_tag(str(left.value)) in values
            return ("pass" if ok else "mismatch",
                    "found in register" if ok else "not present in the asset register")
        if right is None:
            return "not_found", "reference value not found"
        if right.confidence == "uncertain":
            return "not_checked", "reference value is uncertain"
        if op == "eq_normalised":
            ok = _norm_generic(left.value) == _norm_generic(right.value)
            return ("pass" if ok else "mismatch", "values match" if ok else "values differ after normalisation")
        if op == "fuzzy_party":
            sim = party_similarity(str(left.value), str(right.value))
            thr = rule.threshold if rule.threshold is not None else ctx.party_threshold
            return ("pass" if sim >= thr else "mismatch", f"similarity {sim:.2f} (threshold {thr:.2f})")
        if op in {"gte", "lte"}:
            lmag, lunit = self._quantity(left)
            rmag, runit = self._quantity(right)
            if lunit and runit:
                if not units_compatible(lunit, runit):
                    return "not_checked", f"incompatible units {lunit} and {runit}"
                lmag = convert((lmag, lunit), runit)
            ok = lmag >= rmag if op == "gte" else lmag <= rmag
            sym = ">=" if op == "gte" else "<="
            return ("pass" if ok else "mismatch", f"{lmag:g} {sym} {rmag:g} is {'true' if ok else 'false'}")
        if op in {"lte_date", "gte_date"}:
            try:
                ld = norm_date(str(left.value), ctx.dayfirst)
                rd = norm_date(str(right.value), ctx.dayfirst)
            except ValueError:
                return "not_checked", "date could not be normalised"
            ok = ld <= rd if op == "lte_date" else ld >= rd
            return ("pass" if ok else "mismatch", f"{ld} {'<=' if op == 'lte_date' else '>='} {rd} is "
                    f"{'true' if ok else 'false'}")
        return "not_checked", f"unknown comparator {op}"

    def _fmt(self, item: Item | None) -> str | None:
        if item is None:
            return None
        if isinstance(item.value, float):
            return f"{item.value:g} {self._unit_symbol(item.unit)}".strip()
        return str(item.value)

    @staticmethod
    def _unit_symbol(unit: str | None) -> str:
        return {"millimeter": "mm", "bar": "bar", "megapascal": "MPa"}.get(unit or "", unit or "")

    # -- run -------------------------------------------------------------------------------

    def _record(self, ctx: CheckContext, res: CheckResult, inputs: list[str]) -> CheckResult:
        labels: list[Label] = []
        for rid in inputs:
            rec = ctx.ledger.maybe(rid)
            if rec is not None:
                labels.append(rec.label)
        detail = f"{res.left_value} vs {res.right_value}" if res.right_value is not None else str(res.left_value)
        rec = ctx.ledger.add(
            ctx.task_id, "check_result",
            summary=f"{res.rule}: {res.status} ({detail}; {res.note})",
            body=res.model_dump(mode="json", exclude={"record_id"}),
            label=high_water(labels), produced_by=ctx.produced_by, inputs=[i for i in inputs if i],
            confidence="high",
        )
        res.record_id = rec.id
        return res

    def run(self, ctx: CheckContext) -> list[CheckResult]:
        results: list[CheckResult] = []
        counter = 0

        def next_id() -> str:
            nonlocal counter
            counter += 1
            return f"C{counter}"

        for rule in self.rules:
            if rule.applies_to and ctx.document_type not in rule.applies_to:
                continue
            if rule.type == "trend":
                results.append(self._run_trend(ctx, rule, next_id()))
                continue
            assert rule.left and rule.right
            lefts = self._left_items(ctx, rule.left)
            if not lefts:
                results.append(self._record(ctx, CheckResult(
                    id=next_id(), rule=rule.name, description=rule.description, status="not_found",
                    note=f"{rule.left} was not extracted"), []))
                continue
            if rule.aggregate in {"min", "max"}:
                numeric = [i for i in lefts if i.confidence != "uncertain"]
                pick = min if rule.aggregate == "min" else max
                lefts = [pick(numeric, key=lambda i: self._quantity(i)[0])] if numeric else lefts[:1]
            rows: list[CheckResult] = []
            for left in lefts:
                rid = next_id()
                src = rule.right.get("source")
                right: Item | None = None
                register_values: list[str] | None = None
                right_anchor: Anchor | None = None
                if left.confidence == "uncertain":
                    rows.append(CheckResult(id=rid, rule=rule.name, description=rule.description,
                                            status="not_checked", left_value=str(left.value),
                                            left_record=left.record, left_anchor=self._record_anchor(ctx, left.record),
                                            note="extracted value is uncertain (dual-read disagreement)"))
                    continue
                if src == "fact":
                    right = self._fact(ctx, str(rule.right["field"]))
                    right_anchor = self._record_anchor(ctx, right.record) if right else None
                elif src == "asset_register":
                    fieldname = str(rule.right["field"])
                    if "key" in rule.right:
                        key = self._template(ctx, str(rule.right["key"]))
                        found = self._register_row(key) if key else None
                        if found:
                            right = Item(found[1].get(fieldname, ""), None, "high", "asset register")
                            right_anchor = self._register_anchor(found[0])
                    else:
                        register_values = [r.get(fieldname, "") for r in self.register]
                        match = self._register_row(str(left.value)) if fieldname == "tag" else None
                        if match:
                            right = Item(match[1].get(fieldname), None)
                            right_anchor = self._register_anchor(match[0])
                elif src == "kb_limit":
                    tag = self._template(ctx, str(rule.right.get("tag", "")))
                    right = self.kb_limit(ctx, str(rule.right.get("kind")), tag)
                    right_anchor = self._record_anchor(ctx, right.record) if right else None
                status, note = self._compare(rule, ctx, left, right, register_values)
                per_row = "[" in rule.left
                if per_row and left.context:
                    note = f"{note} at {left.context}"
                left_fmt = self._fmt(left) if rule.compare in {"gte", "lte"} else str(left.value)
                if rule.compare in {"gte", "lte"} and left.unit:
                    left_fmt = f"{float(left.value):g} {self._unit_symbol(left.unit)}"
                right_fmt = self._fmt(right)
                rows.append(CheckResult(
                    id=rid, rule=rule.name, description=rule.description, status=status,
                    left_value=left_fmt, right_value=right_fmt if src != "asset_register" or right else (
                        "not in register" if rule.compare == "exists_normalised" else None),
                    left_anchor=self._record_anchor(ctx, left.record), right_anchor=right_anchor,
                    left_record=left.record, right_record=right.record if right else None, note=note,
                    extra={"location": left.context} if per_row and left.context else {},
                ))
            if rule.report == "failures_only":
                failures = [r for r in rows if r.status != "pass"]
                if failures:
                    rows = failures
                else:
                    first = rows[0]
                    rows = [first.model_copy(update={"left_value": f"{len(lefts)} row(s)",
                                                     "note": "all rows match"})]
            for r in rows:
                results.append(self._record(ctx, r, [r.left_record or "", r.right_record or ""]))
        ctx.results = results
        return results

    def _run_trend(self, ctx: CheckContext, rule: RuleSpec, rid: str) -> CheckResult:
        assert rule.limit and rule.horizon and rule.quantity
        tag = self._template(ctx, rule.tag or "{fact.equipment_tag}")
        tag_item = self._fact(ctx, "equipment_tag")
        base = CheckResult(id=rid, rule=rule.name, description=rule.description, status="not_found")
        if not tag or (tag_item and tag_item.confidence == "uncertain"):
            return self._record(ctx, base.model_copy(update={"status": "not_checked",
                                                             "note": "equipment tag uncertain"}), [])
        points: list[TrendPoint] = []
        inputs: list[str] = []
        for rec in ctx.records:
            if rec.kind != "graph_fact" or not isinstance(rec.body, dict):
                continue
            b = rec.body
            if norm_tag(str(b.get("tag", ""))) == norm_tag(tag) and b.get("quantity") == rule.quantity:
                points.append(TrendPoint(date=parse_iso(str(b["date"])), value=float(b["value"]), record=rec.id))
                inputs.append(rec.id)
        tag_rows = [m for m in (ctx.facts.get("measurements") or [])
                    if m.get("quantity") == rule.quantity and m.get("confidence") != "uncertain"
                    and norm_tag(str(m.get("tag"))) == norm_tag(tag)]
        date_item = self._fact(ctx, "inspection_date") or self._fact(ctx, "report_date")
        if tag_rows and date_item is not None:
            low = min(tag_rows, key=lambda m: float(m["value"]))
            points.append(TrendPoint(date=parse_iso(norm_date(str(date_item.value), ctx.dayfirst)),
                                     value=float(low["value"]), record=str(low["record"])))
            inputs.append(str(low["record"]))
        horizon_item = self._fact(ctx, str(rule.horizon.get("field")))
        limit = self.kb_limit(ctx, str(rule.limit.get("kind")), tag)
        if len(points) < 2 or horizon_item is None or limit is None:
            missing = "fewer than two dated readings" if len(points) < 2 else (
                "no next inspection date" if horizon_item is None else "no limit in the retrieved clauses")
            return self._record(ctx, base.model_copy(update={"note": missing}), inputs)
        horizon = parse_iso(norm_date(str(horizon_item.value), ctx.dayfirst))
        trend = project(points, horizon)
        calc = ctx.ledger.add(
            ctx.task_id, "calc_result",
            summary=(f"Trend of {rule.quantity} for {tag}: slope {trend.slope_per_year:+.2f} mm/yr, "
                     f"projected {trend.projected:.2f} mm on {horizon.isoformat()}"),
            body={"rule": rule.name, **trend.model_dump(mode="json"), "unit": "mm"},
            label=high_water([r.label for r in ctx.records if r.id in inputs]),
            produced_by=ctx.produced_by, inputs=[*inputs, limit.record or ""], confidence="high",
        )
        ok = trend.projected >= float(limit.value)
        res = base.model_copy(update={
            "status": "pass" if ok else "mismatch",
            "left_value": f"{trend.projected:.2f} mm projected on {horizon.isoformat()}",
            "right_value": f"{float(limit.value):g} mm",
            "left_record": calc.id, "right_record": limit.record,
            "left_anchor": None, "right_anchor": self._record_anchor(ctx, limit.record),
            "note": (f"{len(points)} readings, slope {trend.slope_per_year:+.2f} mm/yr; projection "
                     f"{'stays above' if ok else 'falls below'} the minimum before the next inspection"),
            "extra": {"calc_record": calc.id, "points": [p.model_dump(mode="json") for p in trend.points]},
        })
        return self._record(ctx, res, [calc.id, limit.record or ""])
