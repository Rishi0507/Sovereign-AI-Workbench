"""The plant graph endpoint and the drawing sheets behind it."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from workbench.api.app import create_app
from workbench.runtime import Runtime

ENG = {"X-User": "engineer1"}
DEV = {"X-User": "dev1"}


@pytest.fixture()
def client(make_runtime: Callable[..., Runtime]) -> Iterator[TestClient]:
    rt = make_runtime()
    app = create_app(rt, install_guard=False)
    with TestClient(app) as c:
        yield c


def test_the_whole_plant_is_returned_with_its_relations(client: TestClient) -> None:
    graph = client.get("/api/graph", headers=ENG).json()
    kinds = {n["kind"] for n in graph["nodes"]}
    assert {"tag", "document", "clause", "inspection", "vendor", "po", "class"} <= kinds
    tags = {n["key"] for n in graph["nodes"] if n["kind"] == "tag"}
    assert {"P-108A", "P-108B", "E-201", "V-301"} <= tags
    ids = {n["id"] for n in graph["nodes"]}
    assert all(e["source"] in ids and e["target"] in ids for e in graph["edges"])
    assert graph["ceiling"] == "Confidential"


def test_one_tag_returns_its_neighbourhood_only(client: TestClient) -> None:
    graph = client.get("/api/graph?tag=P-108B&depth=2", headers=ENG).json()
    names = {n["kind"]: [x["name"] for x in graph["nodes"] if x["kind"] == n["kind"]] for n in graph["nodes"]}
    assert next(n for n in graph["nodes"] if n["kind"] == "tag")["key"] == "P-108B"
    assert any("Narmada" in v for v in names.get("vendor", []))
    assert any("PID-CW-003" in v for v in names.get("document", []))
    # An inspection reads as a measurement, not as a database key.
    assert any("Wall thickness" in v and "mm" in v for v in names.get("inspection", []))
    assert client.get("/api/graph?tag=P-999X", headers=ENG).status_code == 404


def test_the_graph_stops_at_the_reading_ceiling(client: TestClient) -> None:
    mine = client.get("/api/graph", headers=ENG).json()
    theirs = client.get("/api/graph", headers=DEV).json()
    assert len(theirs["nodes"]) < len(mine["nodes"])
    assert theirs["ceiling"] == "Restricted"
    assert all(n["label"]["level"] in {"Unclassified", "Restricted"} for n in theirs["nodes"])


def test_drawing_sheets_are_workspace_files_with_clickable_tags(client: TestClient) -> None:
    files: list[dict[str, Any]] = client.get("/api/workspaces/plant-a/files?area=inputs", headers=ENG).json()
    sheets = [f for f in files if f["name"].startswith("PID") and f["name"].endswith(".svg")]
    assert {s["name"] for s in sheets} >= {"PID-CW-003.svg", "PID-PW-001.svg", "PID-AM-002.svg", "PID-FL-001.svg"}
    svg = client.get(f"/api/files/{sheets[0]['id']}/download", headers=ENG).text
    assert svg.startswith("<svg") and "<script" not in svg
    # Tags on the sheet are the tags in the graph, so a click can open the record behind them.
    drawn = {line.split('data-tag="')[1].split('"')[0] for line in svg.splitlines() if "data-tag=" in line}
    graph = client.get("/api/graph", headers=ENG).json()
    assert drawn <= {n["key"] for n in graph["nodes"] if n["kind"] == "tag"}
