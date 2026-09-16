"""Sleep and wake control for vLLM servers (README section 3.0.2)."""

from __future__ import annotations

import threading
import time
from typing import Protocol
from urllib.parse import urlparse

import httpx

from workbench.core.errors import PolicyError, ServiceUnavailable
from workbench.llm.openai_compat import is_loopback_host
from workbench.registry.models import Registry


class VLLMControl(Protocol):
    def sleep(self, model: str, level: int = 1) -> None: ...

    def wake(self, model: str) -> None: ...

    def is_awake(self, model: str) -> bool: ...


class FakeVLLMControl:
    """Simulates sleep and wake with the registry's ``latency.wake_s`` times ``time_scale``."""

    def __init__(self, registry: Registry, time_scale: float = 0.0) -> None:
        self.registry = registry
        self.time_scale = time_scale
        self._awake: dict[str, bool] = {m.name: False for m in registry.models}
        self._lock = threading.Lock()
        self.calls: list[tuple[str, str]] = []

    def _delay(self, model: str, factor: float) -> None:
        entry = self.registry.maybe(model)
        wake_s = (entry.latency.wake_s if entry and entry.latency.wake_s else 0.0) * factor
        if wake_s and self.time_scale:
            time.sleep(wake_s * self.time_scale)

    def sleep(self, model: str, level: int = 1) -> None:
        self._delay(model, 0.25)
        with self._lock:
            self._awake[model] = False
            self.calls.append(("sleep", model))

    def wake(self, model: str) -> None:
        self._delay(model, 1.0)
        with self._lock:
            self._awake[model] = True
            self.calls.append(("wake", model))

    def is_awake(self, model: str) -> bool:
        with self._lock:
            return self._awake.get(model, False)


class HttpVLLMControl:
    """Calls vLLM's ``/sleep`` and ``/wake_up`` endpoints on the loopback port."""

    def __init__(self, registry: Registry, timeout_s: float = 120.0) -> None:
        self.registry = registry
        self.client = httpx.Client(timeout=timeout_s, trust_env=False)

    def _root(self, model: str) -> str:
        endpoint = self.registry.get(model).endpoint
        parsed = urlparse(endpoint)
        if not is_loopback_host(parsed.hostname or ""):
            raise PolicyError(f"{model}: control endpoint must be loopback")
        return f"{parsed.scheme}://{parsed.netloc}"

    def _post(self, url: str) -> None:
        try:
            resp = self.client.post(url)
        except httpx.HTTPError as exc:
            raise ServiceUnavailable(str(exc)) from exc
        if resp.status_code >= 400:
            raise ServiceUnavailable(f"{url} returned {resp.status_code}")

    def sleep(self, model: str, level: int = 1) -> None:
        self._post(f"{self._root(model)}/sleep?level={level}")

    def wake(self, model: str) -> None:
        self._post(f"{self._root(model)}/wake_up")

    def is_awake(self, model: str) -> bool:
        try:
            resp = self.client.get(f"{self._root(model)}/is_sleeping")
            return not bool(resp.json().get("is_sleeping", False))
        except (httpx.HTTPError, ValueError):
            return False
