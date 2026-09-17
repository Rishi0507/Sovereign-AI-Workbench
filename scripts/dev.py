"""Cross-platform developer tasks (the Makefile delegates here).

Usage: python scripts/dev.py <task>

Tasks: setup, fixtures, test, lint, demo, serve, chat-model, eval, render-serve,
       go-build, go-test, go-lint, go-integration, clean
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GO_DIR = ROOT / "go"
BIN = ROOT / "bin"
EXE = ".exe" if os.name == "nt" else ""
GO_ENV = {"GOTOOLCHAIN": "local", "GOPROXY": "off", "GOFLAGS": "-trimpath", "CGO_ENABLED": "0",
          "GOWORK": "off"}


def run(cmd: list[str], env: dict[str, str] | None = None, cwd: Path = ROOT, check: bool = True) -> int:
    print("+", " ".join(cmd), flush=True)
    merged = {**os.environ, **(env or {})}
    code = subprocess.call(cmd, cwd=cwd, env=merged)
    if check and code != 0:
        sys.exit(code)
    return code


def py(*args: str, env: dict[str, str] | None = None, check: bool = True) -> int:
    return run([sys.executable, *args], env=env, check=check)


def workbench(*args: str) -> int:
    return py("-m", "workbench.cli", *args)


def go(*args: str, env: dict[str, str] | None = None, check: bool = True) -> int:
    return run(["go", *args], env={**GO_ENV, **(env or {})}, cwd=GO_DIR, check=check)


def version() -> str:
    try:
        return subprocess.check_output(["git", "describe", "--tags", "--always", "--dirty"], cwd=ROOT,
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "dev"


def task_setup() -> None:
    py("-m", "pip", "install", "-e", ".[dev]")
    task_fixtures()


def task_fixtures() -> None:
    py("scripts/make_fixtures.py")
    workbench("setup")


def task_test() -> None:
    py("-m", "pytest", *sys.argv[2:])


def task_lint() -> None:
    py("-m", "ruff", "check", "workbench", "tests", "scripts")
    py("-m", "mypy", "workbench/core", "workbench/planning", "workbench/checks", "workbench/router")


def task_demo() -> None:
    workbench("demo")


def task_serve() -> None:
    workbench("serve")


LLAMA_BUILD = "b11026"
CHAT_MODEL_URL = ("https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/"
                  "qwen2.5-1.5b-instruct-q4_k_m.gguf")


def llama_asset() -> str:
    machine = platform.machine().lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "x64"
    if os.name == "nt":
        return f"llama-{LLAMA_BUILD}-bin-win-cpu-{arch}.zip"
    system = "macos" if sys.platform == "darwin" else "ubuntu"
    return f"llama-{LLAMA_BUILD}-bin-{system}-{arch}.tar.gz"


def download(url: str, dest: Path) -> None:
    print(f"downloading {url}", flush=True)
    part = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url) as resp, part.open("wb") as out:
        shutil.copyfileobj(resp, out, 1 << 20)
    part.replace(dest)


def task_chat_model() -> None:
    """Fetch llama.cpp and Qwen2.5-1.5B-Instruct once, then serve it on 127.0.0.1:8010."""
    home = Path(os.environ.get("WB_MODELS_DIR", ROOT / "models"))
    server_dir = home / f"llama.cpp-{LLAMA_BUILD}"
    model = home / "qwen2.5-1.5b-instruct-q4_k_m.gguf"
    home.mkdir(parents=True, exist_ok=True)
    found = list(server_dir.rglob(f"llama-server{EXE}")) if server_dir.is_dir() else []
    if not found:
        archive = home / llama_asset()
        download(f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_BUILD}/{archive.name}", archive)
        shutil.unpack_archive(archive, server_dir)
        archive.unlink()
        found = list(server_dir.rglob(f"llama-server{EXE}"))
    if not model.is_file():
        download(CHAT_MODEL_URL, model)
    server = found[0]
    if os.name != "nt":
        server.chmod(0o755)
    threads = str(max(1, os.cpu_count() or 1))
    run([str(server), "--model", str(model), "--alias", "qwen2.5-1.5b-instruct", "--host", "127.0.0.1",
         "--port", "8010", "--ctx-size", "4096", "--threads", threads, "--parallel", "1"], cwd=server.parent)


def task_eval() -> None:
    workbench("eval", "--write-registry")


def task_render_serve() -> None:
    workbench("registry", "render-serve", "--profile", os.environ.get("WB_PROFILE", "S"))


def task_go_build() -> None:
    BIN.mkdir(exist_ok=True)
    flags = f"-s -w -X main.version={version()}"
    for name in ("sandboxd", "egressd"):
        go("build", "-ldflags", flags, "-o", str(BIN / f"{name}{EXE}"), f"./cmd/{name}")


def race_supported() -> bool:
    return os.name != "nt" and shutil.which("gcc") is not None


def task_go_test() -> None:
    args = ["test", "-count=1", "-coverprofile=coverage.out", "./..."]
    env = {}
    if race_supported():
        args.insert(1, "-race")
        env["CGO_ENABLED"] = "1"
    else:
        print("note: the race detector needs cgo and a C toolchain; running without -race", flush=True)
    go(*args, env=env)
    go("tool", "cover", "-func=coverage.out")


def task_go_lint() -> None:
    out = subprocess.check_output(["gofmt", "-l", "."], cwd=GO_DIR, text=True).strip()
    if out:
        print("gofmt would change:\n" + out)
        sys.exit(1)
    go("vet", "./...")
    mod = (GO_DIR / "go.mod").read_text(encoding="utf-8")
    if "require" in mod:
        print("go.mod must not contain require lines")
        sys.exit(1)


def wait_health(run_dir: Path, name: str, timeout: float = 20.0) -> None:
    sys.path.insert(0, str(ROOT))
    from workbench.security.transport import service_client

    transport = "tcp" if os.name == "nt" else "unix"
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with service_client(run_dir, name, transport, 2.0) as c:
                if c.get("/v1/health").status_code == 200:
                    return
        except Exception as exc:  # the daemon is still starting
            last = str(exc)
        time.sleep(0.3)
    raise SystemExit(f"{name} did not become healthy: {last}")


def task_go_integration() -> None:
    task_go_build()
    tmp = Path(tempfile.mkdtemp(prefix="wb-go-"))
    run_dir, ws_root = tmp / "run", tmp / "workspaces"
    run_dir.mkdir()
    ws_root.mkdir()
    cfg_dir = tmp / "config"
    workbench("render-go-config", "--out", str(cfg_dir), "--run-dir", str(run_dir),
              "--workspace-root", str(ws_root), "--backend", "dev", "--mode", "dev")
    procs = []
    logs = []
    try:
        for name in ("sandboxd", "egressd"):
            log = (tmp / f"{name}.log").open("w")
            logs.append(log)
            procs.append(subprocess.Popen([str(BIN / f"{name}{EXE}"), "-config", str(cfg_dir / f"{name}.json")],
                                          cwd=ROOT, stdout=log, stderr=subprocess.STDOUT))
        for name in ("sandboxd", "egressd"):
            wait_health(run_dir, name)
        env = {"WB_GO_BINARIES": "1", "WB_RUN_DIR": str(run_dir), "WB_GO_WORKSPACES": str(ws_root)}
        py("-m", "pytest", "tests/contract", "tests/acceptance/test_traces.py", "-k",
           "contract or trace_b or egress", "-p", "no:cacheprovider", env=env)
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        for log in logs:
            log.close()
        for name in ("sandboxd", "egressd"):
            text = (tmp / f"{name}.log").read_text(encoding="utf-8", errors="replace")
            print(f"--- {name} log (last lines) ---\n" + "\n".join(text.splitlines()[-15:]))
        state = run_dir / "egressd_state.json"
        if state.is_file():
            print("egressd state:", json.dumps(json.loads(state.read_text(encoding="utf-8"))))
        shutil.rmtree(tmp, ignore_errors=True)


def task_clean() -> None:
    for d in ("var", "run", "bin", "reports", ".pytest_cache", ".ruff_cache", ".mypy_cache"):
        shutil.rmtree(ROOT / d, ignore_errors=True)


TASKS = {name[5:].replace("_", "-"): fn for name, fn in globals().items() if name.startswith("task_")}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in TASKS:
        print(__doc__)
        sys.exit(2)
    TASKS[sys.argv[1]]()


if __name__ == "__main__":
    main()
