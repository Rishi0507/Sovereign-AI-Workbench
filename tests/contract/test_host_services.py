"""Contract tests: the same assertions hold for the in-process fakes and the real Go daemons.

Run against the binaries with ``make go-integration`` (sets WB_GO_BINARIES=1).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.helpers import GO_MODE, ROOT
from workbench.runtime import Runtime
from workbench.tools.sandbox import SandboxResult


def job_dir(rt: Runtime, name: str) -> Path:
    path = rt.settings.path(rt.settings.workspaces_root) / "_jobs" / "contract" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_sandbox_result_mirrors_the_go_struct() -> None:
    go_src = (ROOT / "go" / "internal" / "sandbox" / "api.go").read_text(encoding="utf-8")
    block = go_src[go_src.index("type RunResult struct"):]
    block = block[: block.index("}")]
    go_fields = re.findall(r'json:"([a-z_]+)"', block)
    assert go_fields == list(SandboxResult.model_fields)


def test_health(make_runtime: Callable[..., Runtime]) -> None:
    rt = make_runtime()
    sb, eg = rt.sandbox.health(), rt.egress.health()
    assert sb["status"] == "ok" and eg["status"] == "ok"
    if GO_MODE:
        assert sb["backend"] in {"docker", "dev"} and "uptime_s" in sb
        assert eg["mode"] == "dev"


def test_run_failure_success_and_files(make_runtime: Callable[..., Runtime]) -> None:
    rt = make_runtime()
    job = job_dir(rt, "run1")
    (job / "script.py").write_text("rows = {}\nprint(rows['pressure'])\n", encoding="utf-8")
    res = rt.sandbox.run(job, "script.py", 30, run_id="contract-1")
    assert res.exit_code == 1 and res.traceback_head[0].startswith("Traceback")
    assert "KeyError" in res.stderr_tail and not res.timed_out
    (job / "script.py").write_text("open('out.txt', 'w').write('ok')\nprint('{\"done\": true}')\n", encoding="utf-8")
    ok = rt.sandbox.run(job, "script.py", 30, run_id="contract-2")
    assert ok.exit_code == 0 and ok.stdout_tail.strip().endswith('{"done": true}')
    assert [f.path for f in ok.new_files] == ["out.txt"] and len(ok.new_files[0].sha256) == 64
    assert ok.run_id == "contract-2" and ok.argv


def test_timeout(make_runtime: Callable[..., Runtime]) -> None:
    rt = make_runtime()
    job = job_dir(rt, "slow")
    (job / "script.py").write_text("import time\ntime.sleep(20)\n", encoding="utf-8")
    start = time.time()
    res = rt.sandbox.run(job, "script.py", 1, run_id="contract-slow")
    assert res.timed_out and res.exit_code != 0 and time.time() - start < 15


def test_network_attempt_is_blocked_and_counted(make_runtime: Callable[..., Runtime]) -> None:
    rt = make_runtime()
    before = rt.egress.snapshot()["blocked_connect_sandbox"]
    job = job_dir(rt, "net")
    (job / "script.py").write_text(
        "import socket\ntry:\n    socket.create_connection(('1.1.1.1', 443), 5)\n    print('connected')\n"
        "except OSError as exc:\n    print('blocked')\n", encoding="utf-8")
    res = rt.sandbox.run(job, "script.py", 30, run_id="contract-net")
    assert res.stdout_tail.strip() == "blocked"
    assert [a.addr for a in res.net_attempts] == ["1.1.1.1:443"]
    deadline = time.time() + 5
    while rt.egress.snapshot()["blocked_connect_sandbox"] < before + 1 and time.time() < deadline:
        time.sleep(0.05)
    assert rt.egress.snapshot()["blocked_connect_sandbox"] == before + 1
    events = rt.egress.events(0, 1000)
    assert any(e["origin"] == "sandbox" and e["addr"] == "1.1.1.1:443" for e in events)


def test_report_and_egress_test(make_runtime: Callable[..., Runtime]) -> None:
    rt = make_runtime()
    snap = rt.egress.snapshot()
    rt.egress.report({"origin": "host", "addr": "203.0.113.9:443", "pid": 4242, "source": "contract"})
    after = rt.egress.snapshot()
    assert after["blocked_connect_host"] == snap["blocked_connect_host"] + 1
    result = rt.egress.run_test()
    assert result["pass"], result
    assert [c["name"] for c in result["checks"]] == ["host_raw_ip", "host_dns", "sandbox"]
    final = rt.egress.snapshot()
    assert final["blocked_connect_sandbox"] == after["blocked_connect_sandbox"] + 1
    assert final["external_connections"] == 0 and not final["breach"]
    assert final["blocked_packets"] is None


@pytest.mark.skipif(not GO_MODE, reason="validation happens in sandboxd")
def test_sandboxd_rejects_bad_requests(make_runtime: Callable[..., Runtime]) -> None:
    from workbench.core.errors import ToolError

    rt = make_runtime()
    outside = rt.settings.path(rt.settings.data_dir)
    (outside / "script.py").write_text("print(1)", encoding="utf-8")
    with pytest.raises(ToolError, match="400"):
        rt.sandbox.run(outside, "script.py", 5, run_id="escape")
    job = job_dir(rt, "bad")
    with pytest.raises(ToolError, match="400"):
        rt.sandbox.run(job, "../script.py", 5, run_id="bad-name")
