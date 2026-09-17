"""LLM backends, registry, router and tidal pool (PRD phase 2)."""

from __future__ import annotations

import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import ROOT
from workbench.core.audit import AuditLog
from workbench.core.clock import FixedClock
from workbench.core.errors import ConfigError, InvalidModelOutput, PolicyError
from workbench.core.labels import Label, Level
from workbench.eval.datasets import load
from workbench.eval.routing import evaluate_routing
from workbench.llm import structured
from workbench.llm.base import ChatMessage, LLMRequest, LLMResponse
from workbench.llm.heuristic import HeuristicBackend
from workbench.llm.openai_compat import OpenAICompatBackend, check_endpoint
from workbench.llm.schemas import load_schema
from workbench.llm.scripted import ScriptedBackend
from workbench.pool.control import FakeVLLMControl
from workbench.pool.manager import PoolManager, PoolPolicy, TidePolicy
from workbench.registry import serve_cmd
from workbench.registry.models import load_registry, set_field_in_yaml
from workbench.router.constraints import ProvenancePolicy
from workbench.router.router import Router
from workbench.router.rules import apply_rules
from workbench.router.types import AttachmentInfo, TaskInput
from workbench.settings import ProfileSettings


@pytest.fixture()
def registry() -> Any:
    return load_registry(ROOT / "config" / "models.yaml", "S")


def _pool(registry: Any, clock: FixedClock | None = None, scale: float = 1.0, **tide: Any) -> PoolManager:
    policy = PoolPolicy(swap_slot="gpt-oss-20b", tide=TidePolicy(**tide))
    pool = PoolManager(registry, policy, FakeVLLMControl(registry, 0.0), clock or FixedClock(), scale)
    pool.startup()
    return pool


def _router(registry: Any, pool: PoolManager | None = None) -> Router:
    return Router(registry, HeuristicBackend(), pool or _pool(registry, scale=0.0),
                  ProvenancePolicy.load(ROOT / "config" / "provenance.yaml"), "router")


# -- structured calls and backends ---------------------------------------------------------


def _req(purpose: str = "route.classify") -> LLMRequest:
    return LLMRequest(model="m", purpose=purpose, messages=[ChatMessage(role="user", content="Task: hi")])


def test_structured_retries_then_succeeds() -> None:
    good = {"task_type": "general", "needs_tools": False, "complexity": "low",
            "needs_visual_after_extract": False, "confidence": 0.9}
    backend = ScriptedBackend([
        {"match": {"purpose": "route.classify"}, "times": 1, "response": {"text": "nope"}},
        {"match": {"purpose": "route.classify"}, "times": 1, "response": {"text": "```json\n" + json.dumps(good) + "\n```"}},
    ])
    result = structured.call(backend, _req(), load_schema("task_profile"))
    assert result.value == good and result.attempts == 2 and "not valid JSON" in result.errors[0]


def test_structured_raises_after_retries() -> None:
    backend = ScriptedBackend([{"match": {}, "times": 3, "response": {"json": {"task_type": "poetry"}}}])
    with pytest.raises(InvalidModelOutput) as exc:
        structured.call(backend, _req(), load_schema("task_profile"), max_retries=2)
    assert len(exc.value.attempts) == 3
    assert backend.remaining() == 0


def test_scripted_backend_falls_back_and_logs() -> None:
    backend = ScriptedBackend([], fallback=HeuristicBackend())
    resp = backend.chat(_req())
    assert resp.parsed is not None and backend.log == [{"purpose": "route.classify", "model": "m", "scripted": False}]
    with pytest.raises(RuntimeError):
        ScriptedBackend([]).chat(_req())


def test_every_scripted_fixture_loads() -> None:
    files = sorted((ROOT / "fixtures" / "scripts").glob("*.yaml"))
    assert len(files) >= 5
    for f in files:
        assert ScriptedBackend.from_file(f).remaining() > 0


def test_endpoint_policy() -> None:
    check_endpoint("http://127.0.0.1:8001/v1")
    check_endpoint("http://localhost:8001/v1")
    check_endpoint("http://10.20.0.10:636", {("10.20.0.10", 636)})
    with pytest.raises(PolicyError):
        check_endpoint("https://api.example.com/v1")


class _Stub(BaseHTTPRequestHandler):
    seen: list[dict[str, Any]] = []

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Stub.seen.append(body)
        if body.get("tools"):
            message = {"content": None, "tool_calls": [{"function": {"name": "search_kb",
                                                                     "arguments": "{\"queries\": [\"x\"]}"}}]}
        else:
            message = {"content": "{\"value\": \"P-108B\"}"}
        payload = {"choices": [{"message": message}],
                   "usage": {"prompt_tokens": 10, "completion_tokens": 3, "prompt_tokens_details": {"cached_tokens": 8}}}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        return


def test_openai_compat_against_loopback_stub() -> None:
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        backend = OpenAICompatBackend(lambda m: url, cache_salt_mode="request")
        req = _req("vlm.read_field").model_copy(update={"cache_salt": "abc", "images": [b"png"],
                                                        "json_schema": load_schema("field_read")})
        resp = backend.chat(req)
        assert resp.text == '{"value": "P-108B"}' and resp.usage["cached_tokens"] == 8
        body = _Stub.seen[-1]
        assert body["cache_salt"] == "abc"
        assert body["response_format"]["type"] == "json_schema"
        assert body["messages"][-1]["content"][1]["type"] == "image_url"
        tools_resp = backend.chat(_req("step.decide").model_copy(update={"tools": [{"type": "function"}]}))
        assert tools_resp.parsed == {"tool": "search_kb", "args": {"queries": ["x"]}}
        prefix = OpenAICompatBackend(lambda m: url, cache_salt_mode="prefix")
        prefix.chat(LLMRequest(model="m", purpose="p", cache_salt="s1",
                               messages=[ChatMessage(role="system", content="sys"),
                                         ChatMessage(role="user", content="u")]))
        assert _Stub.seen[-1]["messages"][0]["content"].startswith("[salt:s1]")
        assert "cache_salt" not in _Stub.seen[-1]
    finally:
        server.shutdown()


def test_openai_backend_refuses_remote_endpoint() -> None:
    backend = OpenAICompatBackend(lambda m: "https://models.example.com/v1")
    with pytest.raises(PolicyError):
        backend.chat(_req())


# -- registry --------------------------------------------------------------------------------


def test_registry_validation(tmp_path: Path, registry: Any) -> None:
    assert {m.name for m in registry.active()} == {"qwen3-vl-8b", "qwen2.5-coder-7b", "gpt-oss-20b"}
    assert registry.get("granite-3.3-8b").status == "shadow"
    bad = (ROOT / "config" / "models.yaml").read_text(encoding="utf-8")
    cases = {
        "remote endpoint": bad.replace("http://127.0.0.1:8001/v1", "http://10.1.1.1:8001/v1"),
        "eagle without head": bad.replace('speculative: {method: "off"}', "speculative: {method: eagle3, "
                                                                          "num_speculative_tokens: 3}"),
        "k out of range": bad.replace("num_speculative_tokens: 4", "num_speculative_tokens: 9"),
        "swap slot speculating": bad.replace('speculative: {method: "off"}',
                                             "speculative: {method: ngram, num_speculative_tokens: 3}"),
    }
    for name, text in cases.items():
        path = tmp_path / f"{name.replace(' ', '_')}.yaml"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(ConfigError):
            load_registry(path, "S")
    path = tmp_path / "m.yaml"
    path.write_text(cases["swap slot speculating"], encoding="utf-8")
    assert load_registry(path, "M").validate_profile("M")


def test_serve_command_argv(registry: Any) -> None:
    ps = ProfileSettings(kv_cache_fp8=True)
    coder = serve_cmd.build(registry.get("qwen2.5-coder-7b"), "S", ps)
    assert coder == [
        "vllm", "serve", "Qwen/Qwen2.5-Coder-7B-Instruct-AWQ", "--served-model-name", "qwen2.5-coder-7b",
        "--host", "127.0.0.1", "--port", "8002", "--gpu-memory-utilization", "0.38", "--max-model-len", "16384",
        "--enable-prefix-caching", "--kv-cache-dtype", "fp8", "--speculative-config",
        '{"method": "ngram", "num_speculative_tokens": 4, "prompt_lookup_min": 2, "prompt_lookup_max": 5}',
    ]
    vl = serve_cmd.build(registry.get("qwen3-vl-8b"), "S", ps)
    assert vl[vl.index("--tool-call-parser") + 1] == "hermes" and "--enable-auto-tool-choice" in vl
    swap = serve_cmd.build(registry.get("gpt-oss-20b"), "S", ps)
    assert "--enable-sleep-mode" in swap and "--speculative-config" not in swap
    resident_on_m = serve_cmd.build(registry.get("gpt-oss-20b"), "M", ProfileSettings(all_resident=True))
    assert "--enable-sleep-mode" not in resident_on_m
    script = serve_cmd.render_script(registry, "S", ps)
    assert script.index("gpt-oss-20b (swap") < script.index("qwen3-vl-8b (resident")
    assert script.count("--speculative-config") == 2 and "sleep?level=1" in script


def test_set_field_keeps_comments(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    shutil.copy(ROOT / "config" / "models.yaml", path)
    set_field_in_yaml(path, "granite-3.3-8b", "status", "active")
    text = path.read_text(encoding="utf-8")
    assert "# Model registry." in text
    assert load_registry(path).get("granite-3.3-8b").status == "active"
    assert load_registry(path).get("qwen3-vl-8b").status == "active"


# -- router --------------------------------------------------------------------------------


def _att(name: str, ext: str, **kw: Any) -> AttachmentInfo:
    return AttachmentInfo(name=name, path=name, ext=ext, label=Label(level=Level.RESTRICTED), **kw)


def test_rules_order_and_weak_words() -> None:
    assert apply_rules("write a script to parse this PDF table", [_att("t.pdf", ".pdf")]).route == "code"
    assert apply_rules("What is the function of this valve?", []) is None
    assert apply_rules("Draft a note", [_att("r.pdf", ".pdf")]).rule == "attachment:pdf"
    assert apply_rules("x", [_att("a.pdf", ".pdf"), _att("b.pdf", ".pdf")]).route == "agentic"
    assert apply_rules("fix it", [_att("etl.py", ".py")]).rule == "code_attachment"
    assert apply_rules('Traceback (most recent call last):\n  File "a.py", line 1', []).rule == "stack_trace"
    assert apply_rules("look at this", [_att("p.jpg", ".jpg")]).route == "vision"


def test_routing_set_accuracy(registry: Any) -> None:
    report = evaluate_routing(_router(registry), load(ROOT, "routing"))
    misses = [r for r in report["rows"] if not r["ok"]]
    assert len(load(ROOT, "routing")) == 50
    assert report["accuracy"] >= 0.9, misses


def test_tricky_routes_and_explanation(registry: Any) -> None:
    router = _router(registry)
    scans = [_att(f"{x}.pdf", ".pdf", scanned=True, needs_visual=True, pages=4) for x in "abc"]
    d = router.route(TaskInput(id="T47", text="Compare these scanned offers and recommend the best value",
                               workspace="proc", user="u", attachments=scans, label=Label(level=Level.SECRET)))
    assert d.profile.decoupled and d.profile.modalities == ["text"] and d.chosen == "gpt-oss-20b"
    assert d.threshold == 0.85
    vl = next(c for c in d.candidates if c.model == "qwen3-vl-8b")
    assert vl.ok and not vl.meets_threshold
    assert "(decoupled)" in d.log_line and "✗ below threshold" in d.log_line and "+tide" in d.log_line
    doc = router.route(TaskInput(id="T42", text="Draft an approval note", workspace="plant-a", user="u",
                                 attachments=[_att("r.pdf", ".pdf", scanned=True, needs_visual=True, script="mixed")],
                                 label=Label(level=Level.CONFIDENTIAL)))
    assert doc.chosen == "qwen3-vl-8b" and doc.profile.languages == ["hi", "en"]
    assert "gpt-oss-20b ✗ modality" in doc.log_line and doc.profile.confidence == 1.0


def test_provenance_policy_and_fallback(registry: Any) -> None:
    router = _router(registry)
    d = router.route(TaskInput(id="T1", text="Reason about the trade-off between repair and replacement",
                               workspace="defence-cell", user="u", label=Label(level=Level.SECRET)))
    reasons = {c.model: c.reason for c in d.candidates if not c.ok}
    assert reasons["qwen3-vl-8b"] in {"provenance", "route"}
    assert d.chosen == "gpt-oss-20b"
    no_quality = registry.model_copy(deep=True)
    for m in no_quality.models:
        m.quality = {}
    d2 = _router(no_quality).route(TaskInput(id="T2", text="Hello", workspace="plant-a", user="u",
                                             label=Label.lowest()))
    assert d2.used_fallback and d2.chosen == "qwen3-vl-8b"


def test_escalation_and_interactive_limit(registry: Any) -> None:
    router = _router(registry)
    d = router.route(TaskInput(id="T3", text="Write a Python script to sum a column", workspace="plant-a", user="u",
                               attachments=[_att("x.csv", ".csv")], label=Label.lowest()))
    assert d.chosen == "qwen2.5-coder-7b"
    assert router.escalate(d, "qwen2.5-coder-7b", "plant-a").name == "gpt-oss-20b"
    assert router.escalate(d, "gpt-oss-20b", "plant-a") is None
    limited = router.route(TaskInput(id="T4", text="Reason about the trade-off between two pumps", workspace="plant-a",
                                     user="u", label=Label.lowest(), interactive_limit_s=5))
    assert next(c for c in limited.candidates if c.model == "gpt-oss-20b").reason == "pool"


# -- pool --------------------------------------------------------------------------------


def test_startup_order_and_state(registry: Any, tmp_path: Path) -> None:
    pool = PoolManager(registry, PoolPolicy(), FakeVLLMControl(registry), FixedClock(), 1.0,
                       audit=AuditLog(tmp_path / "a.jsonl"))
    order = pool.startup()
    assert order[:2] == ["start:gpt-oss-20b", "sleep:gpt-oss-20b"]
    assert pool.state["gpt-oss-20b"] == "asleep" and pool.state["qwen3-vl-8b"] == "awake"
    assert pool.state["granite-3.3-8b"] == "cold"


def test_no_thrashing_and_batching(registry: Any) -> None:
    clock = FixedClock()
    pool = _pool(registry, clock, 1.0, max_wait_s=90, max_queue=3, min_resident_s=120)
    tickets = [pool.submit_swap_job(f"J{i}", "gpt-oss-20b") for i in range(4)]
    assert not pool.maybe_turn_tide()
    clock.advance(100)
    assert not pool.maybe_turn_tide(), "min_resident_s not yet reached"
    clock.advance(30)
    assert pool.maybe_turn_tide()
    assert pool.tide == "swap" and pool.state["qwen3-vl-8b"] == "asleep"
    assert [t.released.is_set() for t in tickets] == [True, True, True, False]
    for t in tickets[:3]:
        pool.finish_swap_job(t)
    assert pool.tide == "resident" and pool.tide_count == 1
    assert not tickets[3].released.is_set()
    clock.advance(119)
    assert not pool.maybe_turn_tide()
    clock.advance(2)
    assert pool.maybe_turn_tide() and tickets[3].released.is_set() and pool.tide_count == 2
    assert pool.expected_wait("qwen3-vl-8b") == 0.0


def test_resident_steps_finish_before_tide(registry: Any) -> None:
    pool = _pool(registry, FixedClock(), 0.0, max_queue=1)
    entered = threading.Event()
    release = threading.Event()

    def resident_work() -> None:
        with pool.resident_step("qwen3-vl-8b"):
            entered.set()
            release.wait(5)

    worker = threading.Thread(target=resident_work)
    worker.start()
    entered.wait(5)
    ticket = pool.submit_swap_job("J1", "gpt-oss-20b")
    turner = threading.Thread(target=pool.maybe_turn_tide)
    turner.start()
    turner.join(0.3)
    assert turner.is_alive() and pool.state["qwen3-vl-8b"] == "awake"
    release.set()
    turner.join(5)
    worker.join(5)
    assert ticket.released.is_set() and pool.state["qwen3-vl-8b"] == "asleep"
    pool.finish_swap_job(ticket)
    assert pool.tide == "resident"


def test_all_resident_profile_has_no_tides(registry: Any) -> None:
    pool = PoolManager(registry, PoolPolicy(), FakeVLLMControl(registry), FixedClock(), 1.0, all_resident=True)
    pool.startup()
    assert not pool.is_swap("gpt-oss-20b") and pool.expected_wait("gpt-oss-20b") == 0.0
    with pool.resident_step("gpt-oss-20b"):
        pass


def test_heuristic_backend_unknown_purpose_is_empty() -> None:
    assert HeuristicBackend().chat(_req("nothing.here")) == LLMResponse(text="{}", parsed={})
