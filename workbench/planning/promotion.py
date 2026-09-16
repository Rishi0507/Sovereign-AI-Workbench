"""Template promotion: an approved compiled plan becomes a versioned template draft."""

from __future__ import annotations

import re
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from workbench.core.errors import NotFound, PolicyError
from workbench.core.normalise import tokens
from workbench.planning.plan_schema import Plan
from workbench.planning.templates import attachment_kind


def _slug(text: str) -> str:
    words = [w for w in tokens(text) if len(w) > 2][:4]
    return re.sub(r"[^a-z0-9_]", "", "_".join(words)) or "promoted_plan"


def save_as_template(plan: Plan, task_text: str, route: str, attachments: list[str], drafts_dir: Path,
                     author: str, name: str | None = None) -> Path:
    if plan.source == "template":
        raise PolicyError("this run already used a template")
    if not plan.valid:
        raise PolicyError("only a valid, compiled plan can become a template")
    name = name or _slug(task_text)
    keywords = [w for w, _ in Counter(tokens(task_text)).most_common(3)]
    steps: list[dict[str, Any]] = []
    for s in plan.steps:
        entry: dict[str, Any] = {"id": s.id}
        if s.title:
            entry["title"] = s.title
        if s.tool:
            entry["tool"] = s.tool
            entry["default_args"] = s.default_args or s.args
        else:
            entry["model_task"] = s.model_task
        refs = s.step_refs()
        if refs:
            entry["inputs"] = refs
        entry["output_type"] = s.output_type
        if s.side_effect:
            entry["side_effect"] = True
        steps.append(entry)
    data = {
        "name": name,
        "version": 1,
        "description": f"Promoted from an approved run by {author} on {datetime.now().date().isoformat()}.",
        "match": {"route": [route], "attachments": sorted({attachment_kind(a) for a in attachments}),
                  "intent_keywords": keywords},
        "deliverables": [d.model_dump(exclude_none=True) for d in plan.deliverables],
        "steps": steps,
    }
    drafts_dir.mkdir(parents=True, exist_ok=True)
    path = drafts_dir / f"{name}.v1.yaml"
    path.write_text("# Template draft awaiting reviewer approval.\n" + yaml.safe_dump(data, sort_keys=False),
                    encoding="utf-8")
    return path


def approve_template(draft: Path, templates_dir: Path, reviewer: str, author: str | None = None) -> Path:
    if not draft.is_file():
        raise NotFound(f"template draft {draft.name} not found")
    if author and author == reviewer:
        raise PolicyError("the author cannot approve their own template")
    data = yaml.safe_load(draft.read_text(encoding="utf-8"))
    target = templates_dir / f"{data['name']}.yaml"
    if target.exists():
        existing = yaml.safe_load(target.read_text(encoding="utf-8"))
        data["version"] = int(existing.get("version", 1)) + 1
    data["description"] = f"{data.get('description', '')} Approved by {reviewer}.".strip()
    target.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    shutil.move(str(draft), str(draft.with_suffix(".approved")))
    return target
