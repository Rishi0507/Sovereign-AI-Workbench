"""Settings loader.

Settings come from ``config/settings.yaml`` under the repository root. Any top-level key can be
overridden with an environment variable ``WB_<KEY>`` (for example ``WB_LLM_BACKEND=openai``), and
a ``.env`` file at the root is read for such overrides. The root itself is ``WB_ROOT`` or the
directory that contains this package.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

OFFLINE_ENV = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "VLLM_NO_USAGE_STATS": "1",
    "DO_NOT_TRACK": "1",
}
for _k, _v in OFFLINE_ENV.items():
    os.environ.setdefault(_k, _v)


def default_root() -> Path:
    env = os.environ.get("WB_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


class ProfileSettings(BaseModel):
    kv_cache_fp8: bool = True
    all_resident: bool = False


class Settings(BaseModel):
    root: Path = Field(default_factory=default_root)
    profile: str = "S"

    llm_backend: Literal["heuristic", "scripted", "openai"] = "heuristic"
    scripted_script: str | None = None
    router_model: str = "qwen2.5-1.5b-instruct"
    router_endpoint: str = "http://127.0.0.1:8010/v1"
    cache_salt_mode: Literal["request", "prefix"] = "request"
    openai_timeout_s: float = 120.0

    sandbox: Literal["sandboxd", "fake"] = "sandboxd"
    egress: Literal["egressd", "fake"] = "egressd"
    service_transport: Literal["auto", "unix", "tcp"] = "auto"
    sandbox_image: str = "workbench-sandbox:py311"
    sandbox_timeout_s: int = 60

    data_dir: str = "var"
    workspaces_root: str = "var/workspaces"
    db_path: str = "var/workbench.db"
    audit_path: str = "var/audit.jsonl"
    secret_key_path: str = "var/secret.key"
    kb_dir: str = "var/kb"
    run_dir: str = "run"
    reports_dir: str = "reports"
    asset_register: str = "var/reference/asset_register.csv"
    kb_source: str = "fixtures/kb"
    egress_guard: bool = True

    max_steps: int = 20
    max_retries: int = 2
    max_plan_steps: int = 12
    max_code_iters: int = 4
    max_repairs: int = 2
    auto_start_read_only: bool = True
    plan_approval_covers_drafts: bool = True
    record_inline_chars: int = 1200
    observation_max_chars: int = 2000
    tokens_per_scanned_page: int = 1100
    kb_token_budget: int = 4000
    output_token_reserve: int = 4096
    per_model_concurrency: int = 2
    interactive_latency_limit_s: float | None = None

    number_tolerance: float = 0.005
    citation_min_score: float = 0.12
    party_match_threshold: float = 0.85
    date_dayfirst: bool = True

    time_scale: float = 0.05
    step_budgets_s: dict[str, float] = Field(default_factory=dict)
    profiles: dict[str, ProfileSettings] = Field(default_factory=dict)

    def path(self, value: str | Path) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (self.root / p)

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def transport(self) -> Literal["unix", "tcp"]:
        if self.service_transport != "auto":
            return self.service_transport
        return "tcp" if sys.platform == "win32" else "unix"

    def profile_settings(self) -> ProfileSettings:
        return self.profiles.get(self.profile, ProfileSettings())

    def budget(self, key: str) -> float:
        return float(self.step_budgets_s.get(key, self.step_budgets_s.get("model_task", 4.0)))


def _read_dotenv(root: Path) -> dict[str, str]:
    env_file = root / ".env"
    out: dict[str, str] = {}
    if not env_file.is_file():
        return out
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _coerce(value: str) -> Any:
    loaded = yaml.safe_load(value)
    return loaded


def load_settings(root: Path | None = None, overrides: dict[str, Any] | None = None) -> Settings:
    root = (root or default_root()).resolve()
    data: dict[str, Any] = {}
    cfg = root / "config" / "settings.yaml"
    if cfg.is_file():
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    env = {**_read_dotenv(root), **os.environ}
    for name in Settings.model_fields:
        key = f"WB_{name.upper()}"
        if key in env and name != "root":
            data[name] = _coerce(env[key])
    if overrides:
        data.update(overrides)
    data["root"] = root
    return Settings.model_validate(data)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
