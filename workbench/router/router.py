"""Capability-scored router (README section 4.2)."""

from __future__ import annotations

from workbench.core.clock import Clock, SystemClock
from workbench.core.normalise import detect_script
from workbench.llm.base import LLMBackend
from workbench.registry.models import ModelEntry, Registry
from workbench.router import explain
from workbench.router.classifier import classify
from workbench.router.constraints import (
    ProvenancePolicy,
    WaitEstimator,
    check_entry,
    filter_candidates,
)
from workbench.router.rules import DOC_EXTENSIONS, IMAGE_EXTENSIONS, apply_rules
from workbench.router.scorer import score, threshold_for
from workbench.router.types import Candidate, RouteDecision, TaskInput, TaskProfile

SCRIPT_LANGS = {"latin": ["en"], "devanagari": ["hi"], "mixed": ["hi", "en"]}


def estimate_tokens(task: TaskInput, tokens_per_scanned_page: int, kb_budget: int) -> int:
    total = len(task.text) // 4
    for a in task.attachments:
        if a.scanned or a.ext.lower() in IMAGE_EXTENSIONS:
            total += a.pages * tokens_per_scanned_page
        else:
            total += a.chars // 4
    return total + kb_budget


def detect_languages(task: TaskInput) -> list[str]:
    langs: list[str] = []
    for script in [detect_script(task.text), *(a.script for a in task.attachments)]:
        for lang in SCRIPT_LANGS.get(script, ["en"]):
            if lang not in langs:
                langs.append(lang)
    order = {"hi": 0, "en": 1}
    return sorted(langs, key=lambda x: order.get(x, 9))


class Router:
    def __init__(
        self,
        registry: Registry,
        backend: LLMBackend,
        pool: WaitEstimator,
        provenance: ProvenancePolicy,
        router_model: str,
        tokens_per_scanned_page: int = 1100,
        kb_token_budget: int = 4000,
        output_reserve: int = 4096,
        clock: Clock | None = None,
        max_retries: int = 2,
    ) -> None:
        self.registry = registry
        self.backend = backend
        self.pool = pool
        self.provenance = provenance
        self.router_model = router_model
        self.tokens_per_scanned_page = tokens_per_scanned_page
        self.kb_token_budget = kb_token_budget
        self.output_reserve = output_reserve
        self.clock = clock or SystemClock()
        self.max_retries = max_retries

    def profile(self, task: TaskInput) -> TaskProfile:
        rule = apply_rules(task.text, task.attachments)
        cls = classify(self.backend, self.router_model, task, self.max_retries)
        task_type = rule.route if rule else str(cls["task_type"])
        modalities = ["text"]
        if any(a.ext.lower() in DOC_EXTENSIONS | IMAGE_EXTENSIONS for a in task.attachments):
            modalities.append("image")
        needs_visual = bool(cls["needs_visual_after_extract"])
        decoupled = False
        if "image" in modalities and not needs_visual and task_type != "vision":
            modalities = ["text"]
            decoupled = True
        return TaskProfile(
            task_type=task_type,
            rule=rule.rule if rule else None,
            modalities=modalities,
            needs_tools=bool(cls["needs_tools"]),
            complexity=str(cls["complexity"]),
            needs_visual_after_extract=needs_visual,
            est_input_tokens=estimate_tokens(task, self.tokens_per_scanned_page, self.kb_token_budget),
            languages=detect_languages(task),
            label=task.label,
            confidence=1.0 if rule else float(cls["confidence"]),
            classifier_confidence=float(cls["confidence"]),
            decoupled=decoupled,
        )

    def route(self, task: TaskInput, profile: TaskProfile | None = None) -> RouteDecision:
        profile = profile or self.profile(task)
        route = profile.task_type
        candidates, rejections = filter_candidates(
            self.registry, route, profile, task.workspace, self.pool, self.provenance,
            self.output_reserve, task.interactive_limit_s,
        )
        threshold = threshold_for(profile.complexity, profile.confidence, self.registry.routing)
        result = score(candidates, route, threshold, profile.languages, self.pool, self.registry.routing)
        rows: list[Candidate] = list(result.rows)
        rows += [Candidate(model=name, ok=False, reason=reason) for name, reason in rejections]
        order = {m.name: i for i, m in enumerate(self.registry.models)}
        rows.sort(key=lambda c: (0 if c.ok else 1, order.get(c.model, 99)))
        chosen = result.chosen
        used_fallback = result.used_fallback
        if chosen is None:
            chosen = self.registry.routing.fallback.get(route) or self.registry.active()[0].name
            used_fallback = True
        decision = RouteDecision(
            task_id=task.id, ts=self.clock.now(), profile=profile, threshold=threshold, candidates=rows,
            chosen=chosen, below_threshold=result.below_threshold, used_fallback=used_fallback,
            queued_for_tide=self.pool.pool_of(chosen) == "swap" and self.pool.expected_wait(chosen) > 0,
            registry_version=self.registry.version,
        )
        decision.log_line = explain.log_line(decision)
        return decision

    def escalate(self, decision: RouteDecision, current: str, workspace: str) -> ModelEntry | None:
        """The strongest model whose ``escalation_for`` includes the route and that passes stage 2."""
        route = decision.profile.task_type
        options: list[tuple[float, ModelEntry]] = []
        for entry in self.registry.active():
            if entry.name == current or route not in entry.escalation_for:
                continue
            reason = check_entry(entry, route, decision.profile, workspace, self.pool, self.provenance,
                                 self.output_reserve, None, check_pool=True)
            if reason is not None:
                continue
            q = entry.quality_for(route, decision.profile.languages)
            if q is None:
                q = max(entry.quality.values(), default=0.0)
            options.append((q, entry))
        if not options:
            return None
        return max(options, key=lambda o: (o[0], o[1].name))[1]
