"""Shadow onboarding (README section 4.2.5).

A ``status: shadow`` entry receives no live traffic (the router rejects it with reason
``status``). ``run_shadow`` measures its quality table offline and writes a proposed diff;
``promote`` flips ``status`` to ``active`` after an explicit confirmation. Both are audited.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from workbench.core.errors import PolicyError
from workbench.registry.models import load_registry, set_field_in_yaml

if TYPE_CHECKING:
    from workbench.runtime import Runtime


def run_shadow(rt: Runtime, name: str, by: str) -> dict[str, Any]:
    from workbench.eval.metrics import model_quality
    from workbench.eval.write_registry import propose

    entry = rt.registry.get(name)
    if entry.status != "shadow":
        raise PolicyError(f"{name} is {entry.status}; only shadow entries are evaluated this way")
    quality = model_quality(rt, name, rt.settings.root)
    path = rt.settings.config_dir / "models.proposed.yaml"
    doc = propose(rt.registry, {name: quality}, path, rt.settings.llm_backend,
                  note=f"Shadow evaluation of {name}. Promote with an explicit confirmation after review.")
    rt.audit.append({"type": "registry.shadow_eval", "model": name, "quality": quality, "by": by,
                     "registry_version": rt.registry.version})
    return {"model": name, "quality": quality, "proposed_file": path.name, "proposed": doc}


def promote(rt: Runtime, name: str, confirm: bool, by: str) -> dict[str, Any]:
    entry = rt.registry.get(name)
    if entry.status != "shadow":
        raise PolicyError(f"{name} is {entry.status}, not shadow")
    if not confirm:
        raise PolicyError("promotion needs an explicit confirmation")
    path = rt.settings.config_dir / "models.yaml"
    set_field_in_yaml(path, name, "status", "active")
    reloaded = load_registry(path, rt.settings.profile)
    rt.registry.models = reloaded.models
    rt.registry.version = reloaded.version
    rt.audit.append({"type": "registry.promote", "model": name, "by": by, "registry_version": reloaded.version})
    return {"model": name, "status": "active", "registry_version": reloaded.version}
