"""Grounded question answering: attachments are read, the graph answers structural questions."""

from __future__ import annotations

from typing import Any

import pytest

from workbench.runtime import Runtime


def ask(rt: Runtime, text: str, attachments: list[str]) -> tuple[Any, list[str]]:
    task = rt.orchestrator.create_task("plant-a", "engineer1", text, attachments)
    state = rt.jobs.run_inline(task.id)
    outputs = [r for r in rt.ledger.for_task(task.id) if r.kind == "model_output"]
    return state, [a["text"] for a in outputs[-1].body["answer"]]


def test_question_about_an_attachment_reads_it_and_keeps_its_label(rt: Runtime) -> None:
    state, answer = ask(rt, "What is the design pressure in the data sheet?", ["inputs/pipe_data.md"])
    assert state.status == "completed"
    assert "4.5 MPa" in answer[0]
    assert state.label.display() == "Restricted"


def test_which_clause_prefers_the_titled_section(rt: Runtime) -> None:
    _state, answer = ask(rt, "Which clause of this contract covers warranty?", ["inputs/vendor_contract.pdf"])
    assert answer[0].startswith("7. Warranty:")
    assert "18 months" in answer[0]


def test_question_naming_a_tag_is_answered_about_that_tag(rt: Runtime) -> None:
    _state, answer = ask(rt, "What does the approval note history say about P-108B?", [])
    assert all("P-108B" in a for a in answer)


@pytest.mark.parametrize("doc", ["SOP-MECH-014"])
def test_governed_equipment_comes_from_the_plant_graph(rt: Runtime, doc: str) -> None:
    state, answer = ask(rt, f"Which pumps are governed by {doc}?", [])
    assert state.plan.steps[0].tool == "graph_lookup"
    assert "P-101A, P-101B, P-108A, P-108B" in answer[0]
    assert f"{doc} Rev 5" in answer[0]


def test_every_save_changes_the_revision_even_within_a_second(rt: Runtime) -> None:
    # Timestamps have one-second resolution, so clients watch revision_no instead.
    task = rt.orchestrator.create_task("plant-a", "engineer1", "Which pumps are governed by SOP-MECH-014?", [])
    first = rt.tasks.get(task.id)
    stale = rt.tasks.get(task.id)
    rt.tasks.save(first)
    rt.tasks.save(stale)  # an older copy saved later must still move the counter forward
    seen = rt.tasks.get(task.id)
    assert seen.revision_no > first.revision_no
