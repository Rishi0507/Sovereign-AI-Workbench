"""Helpers shared by the test modules."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from workbench.runtime import Runtime
from workbench.settings import Settings, load_settings

ROOT = Path(__file__).resolve().parent.parent
GO_MODE = os.environ.get("WB_GO_BINARIES") == "1"


def settings_for(tmp: Path, **overrides: Any) -> Settings:
    base = {
        "data_dir": str(tmp / "var"), "workspaces_root": str(tmp / "var" / "workspaces"),
        "db_path": str(tmp / "var" / "workbench.db"), "audit_path": str(tmp / "var" / "audit.jsonl"),
        "secret_key_path": str(tmp / "var" / "secret.key"), "kb_dir": str(tmp / "var" / "kb"),
        "run_dir": str(tmp / "run"), "reports_dir": str(tmp / "reports"),
        "asset_register": str(tmp / "var" / "reference" / "asset_register.csv"),
        "sandbox": "sandboxd" if GO_MODE else "fake", "egress": "egressd" if GO_MODE else "fake",
        "time_scale": 0.0, "llm_backend": "heuristic", "chat_model": None, "egress_guard": False,
    }
    if GO_MODE:
        base["run_dir"] = os.environ.get("WB_RUN_DIR", str(ROOT / "run"))
        base["workspaces_root"] = os.environ.get("WB_GO_WORKSPACES", base["workspaces_root"])
    root = Path(overrides.pop("root", ROOT))
    base.update(overrides)
    return load_settings(root, overrides=base)


TRACES = {
    "A": ("plant-a", "engineer1", "Draft an approval note for this inspection report", ["inputs/inspection_P108B.pdf"]),
    "B": ("plant-a", "engineer1", "Write a Python script to parse these pressure readings and flag anomalies",
          ["inputs/pressure_readings.csv"]),
    "C": ("plant-a", "engineer1", "Summarise this vendor contract", ["inputs/vendor_contract.pdf"]),
    "D": ("plant-a", "engineer1", "Compute the required wall thickness for this pipe per the attached data",
          ["inputs/pipe_data.md"]),
    "E": ("proc", "buyer1", "Compare these three vendor offers against the tender conditions and recommend one",
          ["inputs/offer_a.pdf", "inputs/offer_b.pdf", "inputs/offer_c.pdf", "inputs/tender_conditions.pdf"]),
}


def run_trace(rt: Runtime, key: str, **meta: Any) -> Any:
    ws, user, text, atts = TRACES[key]
    task = rt.orchestrator.create_task(ws, user, text, atts, meta=meta or None)
    return rt.jobs.run_inline(task.id)
