"""Stage 3: capability score (README section 4.2.3).

Pick the lowest-cost candidate whose measured quality meets the complexity threshold. If none
does, pick the highest-quality candidate and record ``below_threshold``. A low-confidence profile
raises the threshold one level. Without any quality table, use ``routing.fallback``.
"""

from __future__ import annotations

from dataclasses import dataclass

from workbench.registry.models import ModelEntry, RoutingConfig
from workbench.router.constraints import WaitEstimator
from workbench.router.types import Candidate

LEVELS = ("low", "medium", "high")


def threshold_for(complexity: str, confidence: float, routing: RoutingConfig) -> float:
    idx = LEVELS.index(complexity) if complexity in LEVELS else 1
    if confidence < routing.low_confidence_below:
        idx = min(idx + 1, len(LEVELS) - 1)
    return float(routing.thresholds[LEVELS[idx]])


def cost_of(entry: ModelEntry, pool: WaitEstimator) -> tuple[float, float]:
    step = entry.latency.step_s / (entry.speculative_speedup or 1.0)
    wait = pool.expected_wait(entry.name)
    return step + wait, wait


@dataclass
class ScoreResult:
    chosen: str | None
    rows: list[Candidate]
    below_threshold: bool
    used_fallback: bool


def score(
    candidates: list[ModelEntry],
    route: str,
    threshold: float,
    languages: list[str],
    pool: WaitEstimator,
    routing: RoutingConfig,
) -> ScoreResult:
    rows: list[Candidate] = []
    scored: list[tuple[ModelEntry, float, float]] = []
    for entry in candidates:
        quality = entry.quality_for(route, languages)
        cost, wait = cost_of(entry, pool)
        meets = quality is not None and quality >= threshold
        rows.append(Candidate(model=entry.name, ok=True, quality=quality, cost_s=round(cost, 3),
                              wait_s=round(wait, 3), meets_threshold=meets,
                              reason=None if quality is not None else "no quality for route"))
        if quality is not None:
            scored.append((entry, quality, cost))
    if not candidates:
        return ScoreResult(None, rows, False, False)
    if not scored:
        fb = routing.fallback.get(route)
        names = [c.name for c in candidates]
        chosen = fb if fb in names else names[0]
        return ScoreResult(chosen, rows, False, True)
    eligible = [s for s in scored if s[1] >= threshold]
    if eligible:
        best = min(eligible, key=lambda s: (s[2], -s[1], s[0].name))
        return ScoreResult(best[0].name, rows, False, False)
    best = max(scored, key=lambda s: (s[1], -s[2], s[0].name))
    return ScoreResult(best[0].name, rows, True, False)
