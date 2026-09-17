"""Runtime: builds and wires every component from the settings."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from workbench.agent.approvals import ApprovalGate, QueueApprove
from workbench.agent.state import TaskStore
from workbench.checks.engine import CheckEngine
from workbench.checks.rules import load_rules
from workbench.context.cache_salt import CacheSimulator, load_or_create_key
from workbench.core.audit import AuditLog
from workbench.core.clock import Clock, SystemClock
from workbench.core.db import Database
from workbench.core.labels import PolicyEngine
from workbench.core.ledger import Ledger
from workbench.documents.readers import CompositeReader
from workbench.kb.retrieve import KnowledgeBase
from workbench.llm.base import LLMBackend
from workbench.planning.templates import TemplateLibrary
from workbench.pool.control import FakeVLLMControl, HttpVLLMControl, VLLMControl
from workbench.pool.manager import PoolManager, PoolPolicy
from workbench.registry.models import Registry, load_registry
from workbench.router.constraints import ProvenancePolicy
from workbench.router.router import Router
from workbench.security.egress_guard import load_allowlist
from workbench.security.egressd_client import Egress, EgressdClient, FakeEgressd
from workbench.settings import Settings, load_settings
from workbench.tools import build_registry
from workbench.tools.sandbox import FakeSandbox, Sandbox, SandboxdClient
from workbench.workspace import FileStore


def make_backend(settings: Settings, ledger: Ledger, registry: Registry) -> LLMBackend:
    from workbench.llm.heuristic import HeuristicBackend

    heuristic = HeuristicBackend(ledger)
    if settings.llm_backend in {"heuristic", "scripted"}:
        base: LLMBackend = heuristic
        if settings.llm_backend == "scripted":
            from workbench.llm.scripted import ScriptedBackend

            name = settings.scripted_script or "default"
            base = ScriptedBackend.from_file(settings.root / "fixtures" / "scripts" / f"{name}.yaml", heuristic)
        if not settings.chat_model:
            return base
        from workbench.llm.chat_model import ConversationalBackend

        return ConversationalBackend(base, settings.chat_model, settings.chat_endpoint, settings.chat_timeout_s)
    from workbench.llm.openai_compat import OpenAICompatBackend

    def resolve(model: str) -> str:
        if model == settings.router_model:
            return settings.router_endpoint
        return registry.get(model).endpoint

    allow = load_allowlist(settings.config_dir / "egress_allowlist.yaml")
    return OpenAICompatBackend(resolve, settings.cache_salt_mode, settings.openai_timeout_s, allow)


class Runtime:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        clock: Clock | None = None,
        backend: LLMBackend | None = None,
        approvals: ApprovalGate | None = None,
        sandbox: Sandbox | None = None,
        egress: Egress | None = None,
        control: VLLMControl | None = None,
        start_threads: bool = False,
    ) -> None:
        self.settings = s = settings or load_settings()
        self.clock = clock or SystemClock()
        for d in (s.data_dir, s.workspaces_root, s.kb_dir, s.run_dir):
            s.path(d).mkdir(parents=True, exist_ok=True)
        self.db = Database(s.path(s.db_path))
        self.ledger = Ledger(self.db, self.clock)
        self.audit = AuditLog(s.path(s.audit_path), self.clock)
        self.policy = PolicyEngine.from_config(s.config_dir)
        self.registry = load_registry(s.config_dir / "models.yaml", s.profile)
        self.secret_key = load_or_create_key(s.path(s.secret_key_path))
        self.cache = CacheSimulator()
        if control is None:
            control = HttpVLLMControl(self.registry) if s.llm_backend == "openai" else \
                FakeVLLMControl(self.registry, s.time_scale)
        self.control = control
        self.pool = PoolManager(self.registry, PoolPolicy.load(s.config_dir / "pool.yaml"), control, self.clock,
                                s.time_scale, s.profile_settings().all_resident, self.audit)
        self.pool.startup()
        self.backend = backend or make_backend(s, self.ledger, self.registry)
        self.provenance = ProvenancePolicy.load(s.config_dir / "provenance.yaml")
        self.router = Router(self.registry, self.backend, self.pool, self.provenance, s.router_model,
                             s.tokens_per_scanned_page, s.kb_token_budget, s.output_token_reserve, self.clock,
                             s.max_retries)
        self.tools = build_registry(s.observation_max_chars)
        self.kb = KnowledgeBase(s.path(s.kb_dir), self.db)
        self.checks = CheckEngine(load_rules(s.root / "rules" / "consistency"), s.path(s.asset_register))
        self.files = FileStore(s.path(s.workspaces_root), self.db, self.policy, self.clock)
        self.templates = TemplateLibrary(s.root / "templates", self.approved_templates_dir)
        self.tasks = TaskStore(self.db, self.clock)
        self.reader = CompositeReader()
        if egress is None:
            egress = FakeEgressd() if s.egress == "fake" else EgressdClient(s.path(s.run_dir), s.transport)
        self.egress = egress
        if sandbox is None:
            if s.sandbox == "fake":
                sandbox = FakeSandbox(reporter=egress)
            else:
                sandbox = SandboxdClient(s.path(s.run_dir), s.transport, s.sandbox_image, s.path(s.workspaces_root))
        self.sandbox = sandbox
        if isinstance(egress, FakeEgressd) and egress.sandbox is None:
            egress.sandbox = sandbox
        self.approvals = approvals or QueueApprove()
        from workbench.agent.loop import Orchestrator
        from workbench.jobs.queue import JobQueue

        self.orchestrator = Orchestrator(self, self.approvals)
        self.jobs = JobQueue(self)
        if start_threads:
            self.pool.start_ticker()
            self.jobs.start()

    @property
    def approved_templates_dir(self) -> Path:
        return self.settings.path(self.settings.data_dir) / "templates"

    @property
    def template_drafts_dir(self) -> Path:
        return self.approved_templates_dir / "drafts"

    def evidence_dir(self, task_id: str) -> Path:
        return self.settings.path(self.settings.data_dir) / "evidence" / task_id

    def health(self) -> dict[str, Any]:
        return {"sandboxd": self.sandbox.health(), "egressd": self.egress.health(),
                "backend": self.settings.llm_backend, "chat_model": self.settings.chat_model,
                "profile": self.settings.profile,
                "registry_version": self.registry.version}

    def close(self) -> None:
        self.jobs.stop()
        self.pool.stop()
        self.db.dispose()


def setup_environment(settings: Settings, reset: bool = False) -> dict[str, Any]:
    """Create runtime folders, seed demo workspaces and reference data, and ingest the KB."""
    root = settings.root
    if reset:
        for d in (settings.data_dir,):
            shutil.rmtree(settings.path(d), ignore_errors=True)
    fixtures = root / "fixtures"
    if not (fixtures / "ws").is_dir():
        raise FileNotFoundError("fixtures are missing; run `python scripts/make_fixtures.py` first")
    ws_root = settings.path(settings.workspaces_root)
    copied = 0
    for ws_dir in sorted((fixtures / "ws").iterdir()):
        for src in ws_dir.rglob("*"):
            if src.is_file():
                dst = ws_root / ws_dir.name / src.relative_to(ws_dir)
                if not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    copied += 1
    register = settings.path(settings.asset_register)
    register.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(fixtures / "asset_register.csv", register)
    rt = Runtime(settings)
    try:
        stats = rt.kb.ingest_dir(settings.path(settings.kb_source), register)
        synced = {ws: rt.files.sync(ws) for ws in rt.policy.workspaces}
        rt.audit.append({"type": "setup", "copied": copied, "kb": stats, "synced": synced})
    finally:
        rt.close()
    return {"copied": copied, "kb": stats, "synced": synced}
