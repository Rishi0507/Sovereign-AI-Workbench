"""Sandbox interface. Python never calls Docker directly; ``sandboxd`` (Go) does.

``SandboxResult`` mirrors ``go/internal/sandbox/api.go`` field by field (contract-tested).
``FakeSandbox`` is the in-repo stand-in for tests and machines without the Go daemon: it runs
the script as a subprocess through the same probe entry point and returns the same shape.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field

from workbench.core.errors import ServiceUnavailable, ToolError
from workbench.security.transport import service_client

PROBE_DIR = Path(__file__).resolve().parent.parent / "security" / "probe"
RUN_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
SKIP_NAMES = {".net_attempts.jsonl"}
MAX_HASH_BYTES = 50 * 1024 * 1024


class FileInfo(BaseModel):
    path: str
    size: int
    sha256: str


class NetAttempt(BaseModel):
    ts: str
    addr: str


class SandboxResult(BaseModel):
    run_id: str
    backend: str
    exit_code: int
    timed_out: bool
    oom_killed: bool
    duration_ms: int
    stdout_tail: str
    stderr_tail: str
    traceback_head: list[str] = Field(default_factory=list)
    new_files: list[FileInfo] = Field(default_factory=list)
    changed_files: list[FileInfo] = Field(default_factory=list)
    net_attempts: list[NetAttempt] = Field(default_factory=list)
    argv: list[str] = Field(default_factory=list)


class Sandbox(Protocol):
    def run(self, job_dir: Path, script: str, timeout_s: int = 60, run_id: str | None = None,
            label: str = "") -> SandboxResult: ...

    def health(self) -> dict[str, Any]: ...


def snapshot(job_dir: Path) -> dict[str, tuple[int, float, str]]:
    out: dict[str, tuple[int, float, str]] = {}
    for p in sorted(job_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(job_dir).as_posix()
        if rel in SKIP_NAMES or rel.startswith(".home/"):
            continue
        st = p.stat()
        digest = "" if st.st_size > MAX_HASH_BYTES else hashlib.sha256(p.read_bytes()).hexdigest()
        out[rel] = (st.st_size, st.st_mtime, digest)
    return out


def diff_files(before: dict[str, tuple[int, float, str]],
               after: dict[str, tuple[int, float, str]]) -> tuple[list[FileInfo], list[FileInfo]]:
    new, changed = [], []
    for rel, (size, _mtime, digest) in after.items():
        if rel not in before:
            new.append(FileInfo(path=rel, size=size, sha256=digest))
        elif before[rel][2] != digest or before[rel][0] != size:
            changed.append(FileInfo(path=rel, size=size, sha256=digest))
    return new, changed


def traceback_head(stderr: str, lines: int = 15) -> list[str]:
    idx = stderr.rfind("Traceback (most recent call last):")
    if idx == -1:
        return []
    return stderr[idx:].splitlines()[:lines]


def tail(text: str, limit: int) -> str:
    data = text.encode("utf-8", errors="replace")
    return data[-limit:].decode("utf-8", errors="replace") if len(data) > limit else text


def read_net_attempts(job_dir: Path) -> list[NetAttempt]:
    path = job_dir / ".net_attempts.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            data = json.loads(line)
            out.append(NetAttempt(ts=str(data.get("ts", "")), addr=str(data.get("addr", ""))))
        except ValueError:
            continue
    return out


class FakeSandbox:
    """Subprocess runner with the same probe and result shape. Isolation is probe-only."""

    backend = "fake"

    def __init__(self, python: str | None = None, reporter: Any = None, stdout_tail_bytes: int = 4000,
                 traceback_lines: int = 15) -> None:
        self.python = python or sys.executable
        self.reporter = reporter
        self.stdout_tail_bytes = stdout_tail_bytes
        self.traceback_lines = traceback_lines
        self.runs: dict[str, SandboxResult] = {}

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "backend": self.backend, "isolation": "probe-only", "version": "in-process"}

    def run(self, job_dir: Path, script: str, timeout_s: int = 60, run_id: str | None = None,
            label: str = "") -> SandboxResult:
        run_id = run_id or f"run-{int(time.time() * 1000)}"
        if not RUN_ID_RE.match(run_id):
            raise ToolError(f"invalid run id {run_id!r}")
        if "/" in script or "\\" in script or ".." in script or not script.endswith(".py"):
            raise ToolError("script must be a .py file name inside the job directory")
        if not (job_dir / script).is_file():
            raise ToolError(f"{script} not found in job directory")
        (job_dir / ".net_attempts.jsonl").unlink(missing_ok=True)
        before = snapshot(job_dir)
        home = job_dir / ".home"
        home.mkdir(exist_ok=True)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "PYTHONIOENCODING": "utf-8"}
        for key in ("SYSTEMROOT", "SystemRoot", "TEMP", "TMP"):
            if key in os.environ:
                env[key] = os.environ[key]
        argv = [self.python, "-I", str(PROBE_DIR / "run.py"), script]
        started = time.perf_counter()
        timed_out = False
        try:
            proc = subprocess.run(argv, cwd=job_dir, env=env, capture_output=True, timeout=timeout_s,
                                  stdin=subprocess.DEVNULL, check=False)
            code, out, err = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            code, out, err = -9, exc.stdout or b"", (exc.stderr or b"") + b"\n[sandbox] timed out"
        duration = int((time.perf_counter() - started) * 1000)
        stderr = err.decode("utf-8", errors="replace")
        new, changed = diff_files(before, snapshot(job_dir))
        attempts = read_net_attempts(job_dir)
        result = SandboxResult(
            run_id=run_id, backend=self.backend, exit_code=code, timed_out=timed_out, oom_killed=False,
            duration_ms=duration, stdout_tail=tail(out.decode("utf-8", errors="replace"), self.stdout_tail_bytes),
            stderr_tail=tail(stderr, self.stdout_tail_bytes), traceback_head=traceback_head(stderr, self.traceback_lines),
            new_files=[f for f in new if not f.path.startswith(".home")], changed_files=changed,
            net_attempts=attempts, argv=[Path(a).name if i == 0 else a for i, a in enumerate(argv)],
        )
        if self.reporter is not None:
            for a in attempts:
                self.reporter.report({"origin": "sandbox", "addr": a.addr, "run_id": run_id, "source": "probe"})
        self.runs[run_id] = result
        return result


class SandboxdClient:
    """HTTP client for ``sandboxd`` over its Unix socket (or loopback on Windows)."""

    backend = "sandboxd"

    def __init__(self, run_dir: Path, transport: str, image: str, workspace_root: Path) -> None:
        self.run_dir = run_dir
        self.transport = transport
        self.image = image
        self.workspace_root = workspace_root

    def _client(self, timeout: float) -> httpx.Client:
        return service_client(self.run_dir, "sandboxd", self.transport, timeout)

    def health(self) -> dict[str, Any]:
        try:
            with self._client(5.0) as c:
                data: dict[str, Any] = c.get("/v1/health").json()
                return data
        except (httpx.HTTPError, ServiceUnavailable) as exc:
            return {"status": "down", "error": str(exc)}

    def run(self, job_dir: Path, script: str, timeout_s: int = 60, run_id: str | None = None,
            label: str = "") -> SandboxResult:
        body = {"run_id": run_id or f"run-{int(time.time() * 1000)}", "job_dir": str(job_dir.resolve()),
                "script": script, "image": self.image, "runtime": "", "timeout_s": timeout_s,
                "cpus": 0, "memory_mb": 0, "pids": 0, "label": label}
        try:
            with self._client(timeout_s + 15.0) as c:
                resp = c.post("/v1/run", json=body)
        except (httpx.HTTPError, ServiceUnavailable) as exc:
            raise ToolError(f"sandboxd unavailable: {exc}") from exc
        if resp.status_code != 200:
            try:
                err = resp.json().get("error", {})
                msg = f"{err.get('code')}: {err.get('message')}"
            except ValueError:
                msg = resp.text[:200]
            raise ToolError(f"sandboxd refused the run ({resp.status_code}) {msg}")
        return SandboxResult.model_validate(resp.json())
