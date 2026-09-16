"""HTTP clients for the Go host services.

On POSIX the services listen on Unix domain sockets (``run/<name>.sock``, mode 0660). CPython on
Windows has no ``AF_UNIX``, so there the services listen on an ephemeral loopback port written to
``run/<name>.addr`` and require the bearer token in ``run/<name>.token``.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from workbench.core.errors import PolicyError, ServiceUnavailable
from workbench.llm.openai_compat import is_loopback_host


def service_client(run_dir: Path, name: str, transport: str, timeout_s: float = 5.0) -> httpx.Client:
    if transport == "unix":
        sock = run_dir / f"{name}.sock"
        return httpx.Client(transport=httpx.HTTPTransport(uds=str(sock)), base_url="http://local",
                            timeout=timeout_s, trust_env=False)
    addr_file = run_dir / f"{name}.addr"
    token_file = run_dir / f"{name}.token"
    if not addr_file.is_file():
        raise ServiceUnavailable(f"{name} is not running (no {addr_file.name})")
    addr = addr_file.read_text(encoding="utf-8").strip()
    host = addr.rsplit(":", 1)[0].strip("[]")
    if not is_loopback_host(host):
        raise PolicyError(f"{name} address {addr} is not loopback")
    headers = {}
    if token_file.is_file():
        headers["Authorization"] = f"Bearer {token_file.read_text(encoding='utf-8').strip()}"
    return httpx.Client(base_url=f"http://{addr}", headers=headers, timeout=timeout_s, trust_env=False)
