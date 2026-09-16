"""Least-squares trend projection for the ``trend`` consistency rule."""

from __future__ import annotations

from datetime import date

import numpy as np
from pydantic import BaseModel


class TrendPoint(BaseModel):
    date: date
    value: float
    record: str


class TrendResult(BaseModel):
    method: str = "least_squares (in-process NumPy)"
    points: list[TrendPoint]
    slope_per_year: float
    intercept: float
    horizon: date
    projected: float


def _years(d: date, origin: date) -> float:
    return (d - origin).days / 365.25


def project(points: list[TrendPoint], horizon: date) -> TrendResult:
    if len(points) < 2:
        raise ValueError("a trend needs at least two dated readings")
    pts = sorted(points, key=lambda p: p.date)
    origin = pts[0].date
    x = np.array([_years(p.date, origin) for p in pts], dtype=float)
    y = np.array([p.value for p in pts], dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    projected = float(slope * _years(horizon, origin) + intercept)
    return TrendResult(points=pts, slope_per_year=round(float(slope), 4), intercept=round(float(intercept), 4),
                       horizon=horizon, projected=round(projected, 2))
