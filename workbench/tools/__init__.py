"""Agent tools. No tool performs network I/O; no tool can change a classification label."""

from __future__ import annotations

from workbench.tools.registry import ToolRegistry, ToolSpec


def builtin_specs() -> list[ToolSpec]:
    from workbench.tools import (
        calculate,
        check_consistency,
        document_stats,
        files,
        misc,
        read_document,
        render_docx,
        render_pptx,
        render_xlsx,
        run_python,
        search_kb,
    )

    specs: list[ToolSpec] = []
    for module in (files, run_python, read_document, document_stats, search_kb, check_consistency, calculate,
                   render_docx, render_xlsx, render_pptx, misc):
        specs.extend(module.SPECS)
    return specs


def build_registry(observation_limit: int = 2000) -> ToolRegistry:
    from workbench.agent.delegate import SPEC as DELEGATE_SPEC

    reg = ToolRegistry(observation_limit)
    for spec in builtin_specs():
        reg.register(spec)
    reg.register(DELEGATE_SPEC)
    return reg
