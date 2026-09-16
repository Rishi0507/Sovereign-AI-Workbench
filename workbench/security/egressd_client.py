"""Client for ``egressd`` plus an in-process fake with the same interface.

Counting, collectors and the egress test live in Go. Python only reports and displays.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from workbench.core.errors import ServiceUnavailable
from workbench.security.transport import service_client


class Egress(Protocol):
    def snapshot(self) -> dict[str, Any]: ...

    def events(self, since: int = 0, limit: int = 100) -> list[dict[str, Any]]: ...

    def run_test(self) -> dict[str, Any]: ...

    def report(self, event: dict[str, Any]) -> None: ...

    def health(self) -> dict[str, Any]: ...


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class EgressdClient:
    def __init__(self, run_dir: Path, transport: str) -> None:
        self.run_dir = run_dir
        self.transport = transport
        self.spool = run_dir / "egress_spool.jsonl"
        self._lock = threading.Lock()

    def _client(self, timeout: float = 5.0) -> httpx.Client:
        return service_client(self.run_dir, "egressd", self.transport, timeout)

    def _get(self, path: str, timeout: float = 5.0) -> Any:
        try:
            with self._client(timeout) as c:
                resp = c.get(path)
                resp.raise_for_status()
                return resp.json()
        except (httpx.HTTPError, ServiceUnavailable) as exc:
            raise ServiceUnavailable(f"egressd unavailable: {exc}") from exc

    def health(self) -> dict[str, Any]:
        try:
            data: dict[str, Any] = self._get("/v1/health")
            return data
        except ServiceUnavailable as exc:
            return {"status": "down", "error": str(exc)}

    def snapshot(self) -> dict[str, Any]:
        data: dict[str, Any] = self._get("/v1/snapshot")
        return data

    def events(self, since: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        data = self._get(f"/v1/events?since={since}&limit={limit}")
        return list(data.get("events", data) if isinstance(data, dict) else data)

    def run_test(self) -> dict[str, Any]:
        try:
            with self._client(60.0) as c:
                resp = c.post("/v1/test")
                resp.raise_for_status()
                result: dict[str, Any] = resp.json()
                return result
        except (httpx.HTTPError, ServiceUnavailable) as exc:
            raise ServiceUnavailable(f"egressd unavailable: {exc}") from exc

    def report(self, event: dict[str, Any]) -> None:
        payload = {"ts": _now(), **event}
        try:
            with self._client() as c:
                self.flush(c)
                c.post("/v1/report", json=payload).raise_for_status()
        except (httpx.HTTPError, ServiceUnavailable):
            with self._lock:
                self.run_dir.mkdir(parents=True, exist_ok=True)
                with self.spool.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(payload) + "\n")

    def flush(self, client: httpx.Client) -> int:
        with self._lock:
            if not self.spool.is_file():
                return 0
            lines = [ln for ln in self.spool.read_text(encoding="utf-8").splitlines() if ln.strip()]
            sent = 0
            for line in lines:
                client.post("/v1/report", json=json.loads(line)).raise_for_status()
                sent += 1
            self.spool.unlink()
            return sent


class FakeEgressd:
    """In-process implementation of the egressd interface for tests and the fake mode.

    It never dials anything. The host checks are counted as refused by the guarded dialer and the
    DNS check is simulated, exactly as egressd does in ``dev`` mode.
    """

    def __init__(self, sandbox: Any = None, probe_dir: Path | None = None) -> None:
        self.sandbox = sandbox
        self.probe_dir = probe_dir
        self.since = _now()
        self.counters = {"blocked_connect_host": 0, "blocked_connect_sandbox": 0}
        self._events: deque[dict[str, Any]] = deque(maxlen=1000)
        self._seq = 0
        self._lock = threading.Lock()

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "mode": "fake", "version": "in-process"}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"mode": "fake", "external_connections": 0, "blocked_packets": None, **self.counters,
                    "collectors": {"nft": "unavailable(fake mode)", "conntrack": "unavailable(fake mode)",
                                   "auditlog": "unavailable(fake mode)", "reports": "ok"},
                    "since": self.since, "breach": False}

    def events(self, since: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self._events if e["seq"] > since][:limit]

    def report(self, event: dict[str, Any]) -> None:
        origin = event.get("origin")
        if origin not in {"host", "sandbox"}:
            raise ValueError("origin must be host or sandbox")
        with self._lock:
            self._seq += 1
            self.counters[f"blocked_connect_{origin}"] += 1
            self._events.append({"seq": self._seq, "ts": event.get("ts", _now()), "origin": origin,
                                 "addr": event.get("addr", ""), "port": event.get("port"),
                                 "pid": event.get("pid"), "run_id": event.get("run_id"),
                                 "source": event.get("source", "report")})

    def run_test(self) -> dict[str, Any]:
        checks = []
        before = dict(self.counters)
        self.report({"origin": "host", "addr": "1.1.1.1:443", "source": "selftest-guard"})
        after = dict(self.counters)
        checks.append({"name": "host_raw_ip", "expected": "refused and counted", "observed": "refused by guarded dialer",
                       "counters_before": before, "counters_after": after,
                       "pass": after["blocked_connect_host"] == before["blocked_connect_host"] + 1})
        checks.append({"name": "host_dns", "expected": "resolution fails", "observed": "no nameserver configured",
                       "simulated": True, "counters_before": after, "counters_after": dict(self.counters),
                       "pass": True})
        if self.sandbox is None:
            checks.append({"name": "sandbox", "expected": "attempt counted", "observed": "no sandbox configured",
                           "pass": False})
        else:
            import tempfile

            before = dict(self.counters)
            with tempfile.TemporaryDirectory() as tmp:
                job = Path(tmp)
                (job / "probe.py").write_text(
                    "import socket\ntry:\n    socket.create_connection(('1.1.1.1', 443), 5)\n"
                    "except OSError as exc:\n    print('blocked:', exc)\n", encoding="utf-8")
                result = self.sandbox.run(job, "probe.py", 30, run_id="egress-selftest")
            after = dict(self.counters)
            checks.append({"name": "sandbox", "expected": "one net attempt, sandbox counter +1",
                           "observed": result.stdout_tail.strip(), "net_attempts": len(result.net_attempts),
                           "counters_before": before, "counters_after": after,
                           "pass": len(result.net_attempts) == 1 and
                           after["blocked_connect_sandbox"] == before["blocked_connect_sandbox"] + 1})
        return {"mode": "fake", "checks": checks, "pass": all(c["pass"] for c in checks)}
