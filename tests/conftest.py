"""Shared test fixtures. Every test runs offline with sockets disabled (see pyproject.toml)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import ROOT, settings_for
from workbench.agent.approvals import ApprovalGate, AutoApprove
from workbench.core.clock import FixedClock
from workbench.runtime import Runtime, setup_environment


@pytest.fixture(scope="session", autouse=True)
def _fixtures() -> None:
    marker = ROOT / "fixtures" / "ws" / "plant-a" / "inputs" / "inspection_P101A_injected.pdf"
    if not marker.is_file() or not (ROOT / "org_templates" / "approval_note.docx").is_file():
        subprocess.run([sys.executable, str(ROOT / "scripts" / "make_fixtures.py")], check=True)


@pytest.fixture(scope="session")
def seeded_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A fully set-up data directory, created once and copied for each test that needs it."""
    tmp = tmp_path_factory.mktemp("seed")
    setup_environment(settings_for(tmp))
    return tmp


@pytest.fixture()
def make_runtime(tmp_path: Path, seeded_template: Path) -> Iterator[Callable[..., Runtime]]:
    created: list[Runtime] = []

    def factory(approvals: ApprovalGate | None = None, clock: FixedClock | None = None, **overrides: Any) -> Runtime:
        root = tmp_path / f"rt{len(created)}"
        shutil.copytree(seeded_template, root)
        # In Go mode the seed already wrote into the workspace root that sandboxd guards.
        settings = settings_for(root, **overrides)
        rt = Runtime(settings, approvals=approvals or AutoApprove(), clock=clock)
        created.append(rt)
        return rt

    yield factory
    for rt in created:
        rt.close()


@pytest.fixture()
def rt(make_runtime: Callable[..., Runtime]) -> Runtime:
    return make_runtime()
