"""Shared normalisers for tags, quantities, dates and party names.

The same functions are used by extraction, dual-read reconciliation, the consistency checker,
the plant graph and number provenance, so a value compares the same way everywhere.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from typing import Any

# ---------------------------------------------------------------------------------------------
# Tags

TAG_RE = re.compile(r"\b[A-Z]{1,3}-?\d{2,4}-?[A-Z]?\b")
_CONFUSABLE: list[set[str]] = [{"0", "O", "D", "Q"}, {"1", "I", "L"}, {"8", "B"}, {"5", "S"}, {"2", "Z"}, {"6", "G"}]


def norm_tag(tag: str) -> str:
    """``norm_tag("P101-A") == norm_tag("P-101A") == "P101A"``."""
    return re.sub(r"[^A-Z0-9]", "", tag.upper())


def _glyph_class(ch: str) -> str:
    for group in _CONFUSABLE:
        if ch in group:
            return min(group)
    return ch


def glyph_compatible(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` differ only by characters OCR commonly confuses."""
    na, nb = norm_tag(a), norm_tag(b)
    if len(na) != len(nb):
        return False
    return all(x == y or _glyph_class(x) == _glyph_class(y) for x, y in zip(na, nb, strict=True))


def find_tags(text: str) -> list[str]:
    return [m.group(0) for m in TAG_RE.finditer(text)]


# ---------------------------------------------------------------------------------------------
# Numbers and quantities

NUMBER_RE = re.compile(r"(?<![\w.])-?(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d+)?(?![\w])")

_UNIT_ALIASES: dict[str, str] = {
    "mm": "millimeter", "millimetre": "millimeter", "millimeter": "millimeter",
    "cm": "centimeter", "m": "meter", "in": "inch", "inch": "inch",
    "mm/yr": "millimeter / year", "mm/year": "millimeter / year",
    "bar": "bar", "barg": "bar", "kpa": "kilopascal", "mpa": "megapascal", "psi": "psi",
    "kg/cm2": "kilogram_force / centimeter ** 2", "kg/cm²": "kilogram_force / centimeter ** 2",
    "°c": "degree_Celsius", "degc": "degree_Celsius", "c": "degree_Celsius",
    "%": "percent", "percent": "percent",
    "week": "week", "weeks": "week", "wk": "week", "month": "month", "months": "month",
    "day": "day", "days": "day", "year": "year", "years": "year",
    "kg": "kilogram", "t": "metric_ton",
    "inr": "INR", "rs": "INR", "rs.": "INR", "₹": "INR", "lakh": "INR", "usd": "USD",
}
UNIT_PATTERN = (r"mm/yr|mm/year|kg/cm2|kg/cm²|°C|degC|MPa|kPa|barg|bar|psi|mm|cm|%|"
                r"weeks?|months?|days?|years?|kg|INR|Rs\.?|₹")
QUANTITY_RE = re.compile(
    r"(?P<prefix>INR|Rs\.?|₹)?\s?(?P<num>-?(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d+)?)\s?(?P<unit>" + UNIT_PATTERN + r")?(?![A-Za-z])"
)


@lru_cache(maxsize=1)
def ureg() -> Any:
    import pint

    reg = pint.UnitRegistry(autoconvert_offset_to_baseunit=True)
    reg.define("INR = [currency]")
    reg.define("USD = [currency_usd]")
    return reg


def parse_number(raw: str) -> float:
    return float(raw.replace(",", ""))


def canonical_unit(unit: str | None) -> str | None:
    if unit is None or not unit.strip():
        return None
    key = unit.strip().lower()
    if key not in _UNIT_ALIASES:
        raise ValueError(f"unknown unit {unit!r}")
    return _UNIT_ALIASES[key]


def norm_quantity(text: str) -> tuple[float, str]:
    """``norm_quantity("6.2 mm") == (6.2, "millimeter")``."""
    m = QUANTITY_RE.fullmatch(text.strip())
    if not m or not (m.group("unit") or m.group("prefix")):
        raise ValueError(f"not a quantity: {text!r}")
    unit = canonical_unit(m.group("unit") or m.group("prefix"))
    assert unit is not None
    return parse_number(m.group("num")), unit


def convert(q: tuple[float, str], unit: str) -> float:
    reg = ureg()
    target = canonical_unit(unit) or unit
    return float(reg.Quantity(q[0], q[1]).to(target).magnitude)


def dimensionality(unit: str | None) -> str:
    if not unit:
        return "dimensionless"
    try:
        return str(ureg().Unit(unit).dimensionality)
    except Exception:
        return unit


def units_compatible(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return True
    return dimensionality(a) == dimensionality(b)


def to_base(magnitude: float, unit: str | None) -> float:
    if not unit:
        return magnitude
    try:
        return float(ureg().Quantity(magnitude, unit).to_base_units().magnitude)
    except Exception:
        return magnitude


@dataclass(frozen=True)
class FoundQuantity:
    raw: str
    magnitude: float
    unit: str | None
    start: int
    end: int


def find_quantities(text: str, require_unit: bool = True) -> list[FoundQuantity]:
    out: list[FoundQuantity] = []
    for m in QUANTITY_RE.finditer(text):
        unit_raw = m.group("unit") or m.group("prefix")
        if require_unit and not unit_raw:
            continue
        # skip digits embedded in words or tags such as P-108B or SOP-MECH-014
        start = m.start("num")
        if start > 0 and (text[start - 1].isalnum() or text[start - 1] in "-/_"):
            continue
        try:
            unit = canonical_unit(unit_raw) if unit_raw else None
        except ValueError:
            continue
        out.append(FoundQuantity(m.group(0).strip(), parse_number(m.group("num")), unit, m.start(), m.end()))
    return out


# ---------------------------------------------------------------------------------------------
# Dates

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
DATE_RE = re.compile(
    r"\b(?:(?P<y1>\d{4})-(?P<m1>\d{1,2})-(?P<d1>\d{1,2})"
    r"|(?P<a>\d{1,2})[/.-](?P<b>\d{1,2})[/.-](?P<y2>\d{4})"
    r"|(?P<d3>\d{1,2})\s+(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?,?\s+(?P<y3>\d{4}))\b",
    re.IGNORECASE,
)


def norm_date(text: str, dayfirst: bool = True) -> str:
    """Return an ISO date. Accepts dd/mm/yyyy, dd-mm-yyyy, dd.mm.yyyy, yyyy-mm-dd and 12 Mar 2024."""
    m = DATE_RE.fullmatch(text.strip())
    if not m:
        raise ValueError(f"not a date: {text!r}")
    return _date_from_match(m, dayfirst).isoformat()


def _date_from_match(m: re.Match[str], dayfirst: bool) -> date:
    if m.group("y1"):
        return date(int(m.group("y1")), int(m.group("m1")), int(m.group("d1")))
    if m.group("y2"):
        a, b = int(m.group("a")), int(m.group("b"))
        d, mo = (a, b) if dayfirst else (b, a)
        return date(int(m.group("y2")), mo, d)
    return date(int(m.group("y3")), _MONTHS[m.group("mon")[:3].lower()], int(m.group("d3")))


@dataclass(frozen=True)
class FoundDate:
    raw: str
    iso: str
    start: int
    end: int


def find_dates(text: str, dayfirst: bool = True) -> list[FoundDate]:
    out = []
    for m in DATE_RE.finditer(text):
        try:
            out.append(FoundDate(m.group(0), _date_from_match(m, dayfirst).isoformat(), m.start(), m.end()))
        except ValueError:
            continue
    return out


def parse_iso(value: str) -> date:
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


# ---------------------------------------------------------------------------------------------
# Parties

_PARTY_NOISE = re.compile(r"\b(pvt|private|ltd|limited|llp|inc|co|corp|corporation|the)\b")


def norm_party(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = _PARTY_NOISE.sub(" ", s)
    return " ".join(s.split())


def party_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm_party(a), norm_party(b)).ratio()


# ---------------------------------------------------------------------------------------------
# Tokens

WORD_RE = re.compile(r"[A-Za-zऀ-ॿ][A-Za-zऀ-ॿ0-9]+")
STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the this to was were will with "
    "shall per not no all any each which these those than then there their been being into over under".split()
)


def tokens(text: str) -> list[str]:
    return [t.lower() for t in WORD_RE.findall(text) if t.lower() not in STOPWORDS]


def detect_script(text: str) -> str:
    deva = sum(1 for ch in text if "ऀ" <= ch <= "ॿ")
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    if deva and latin:
        share = deva / (deva + latin)
        if share > 0.9:
            return "devanagari"
        return "mixed" if share > 0.05 else "latin"
    return "devanagari" if deva else "latin"
