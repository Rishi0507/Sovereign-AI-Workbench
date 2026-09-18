"""In-process egress guard for the application and the CLI.

Patches ``socket.socket.connect``, ``connect_ex`` and ``socket.create_connection``. Loopback,
Unix sockets and allowlisted ``(host, port)`` pairs pass; everything else raises
``PermissionError`` before any system call and is reported to egressd as a host event.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

_state: dict[str, Any] = {"installed": False, "blocked": 0}
_lock = threading.Lock()
_reporting = threading.local()


def load_allowlist(path: Path) -> set[tuple[str, int]]:
    if not path.is_file():
        return set()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {(str(a["host"]), int(a["port"])) for a in data.get("allow") or []}


_names: dict[tuple[str, int], tuple[float, set[str]]] = {}
NAME_TTL_S = 300.0


def _addresses_of(host: str, port: int) -> set[str]:
    """Addresses an allowlisted name currently resolves to, remembered for a few minutes."""
    now = time.monotonic()
    cached = _names.get((host, port))
    if cached and now - cached[0] < NAME_TTL_S:
        return cached[1]
    try:
        found = {str(info[4][0]) for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)}
    except OSError:
        found = set()
    _names[(host, port)] = (now, found)
    return found


def _is_allowed(address: Any, family: int, allow: set[tuple[str, int]]) -> bool:
    if family == getattr(socket, "AF_UNIX", -1):
        return True
    if not isinstance(address, tuple) or len(address) < 2:
        return False
    host, port = str(address[0]), int(address[1])
    if host in {"localhost"}:
        return True
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
        if ip.is_loopback:
            return True
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped and ip.ipv4_mapped.is_loopback:
            return True
    except ValueError:
        return (host, port) in allow
    if (host, port) in allow:
        return True
    # An allowlisted name is matched by the addresses it resolves to, since that is what a socket sees.
    return any(host in _addresses_of(name, allowed_port)
               for name, allowed_port in allow if allowed_port == port and not name.replace(".", "").isdigit())


def install(allowlist: set[tuple[str, int]], reporter: Callable[[dict[str, Any]], None] | None = None) -> None:
    with _lock:
        if _state["installed"]:
            _state["allow"] = allowlist
            _state["reporter"] = reporter
            return
        _state.update(installed=True, allow=allowlist, reporter=reporter)
        original_connect = socket.socket.connect
        original_connect_ex = socket.socket.connect_ex
        original_create = socket.create_connection
        _state["originals"] = (original_connect, original_connect_ex, original_create)

    def _block(address: Any) -> None:
        with _lock:
            _state["blocked"] += 1
        rep = _state.get("reporter")
        if rep is not None and not getattr(_reporting, "active", False):
            _reporting.active = True
            try:
                stack = "".join(traceback.format_stack(limit=6)[:-2])[-400:]
                rep({"origin": "host", "addr": f"{address[0]}:{address[1]}" if isinstance(address, tuple) else str(address),
                     "pid": os.getpid(), "source": "egress_guard", "stack_summary": stack})
            except Exception:  # reporting must never break the caller's error path
                pass
            finally:
                _reporting.active = False
        raise PermissionError(f"outbound connection to {address} blocked by the sovereignty egress guard")

    def connect(self: socket.socket, address: Any) -> None:
        if not _is_allowed(address, self.family, _state["allow"]):
            _block(address)
        return original_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        if not _is_allowed(address, self.family, _state["allow"]):
            try:
                _block(address)
            except PermissionError:
                return 13
        return original_connect_ex(self, address)

    def create_connection(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
        if not _is_allowed(address, socket.AF_INET, _state["allow"]):
            _block(address)
        return original_create(address, *args, **kwargs)

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.create_connection = create_connection  # type: ignore[assignment]


def uninstall() -> None:
    with _lock:
        if not _state["installed"]:
            return
        socket.socket.connect, socket.socket.connect_ex, socket.create_connection = _state["originals"]  # type: ignore[method-assign,assignment]
        _state["installed"] = False


def blocked_count() -> int:
    return int(_state["blocked"])


def installed() -> bool:
    return bool(_state["installed"])
