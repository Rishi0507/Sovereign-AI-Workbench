"""SQLite storage shared by the ledger, tasks, jobs, files, downgrades and the plant graph."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Column,
    Engine,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    event,
)
from sqlalchemy.engine import Connection

metadata = MetaData()

ledger_table = Table(
    "ledger_records", metadata,
    Column("id", String, primary_key=True),
    Column("task_id", String, index=True, nullable=False),
    Column("seq", Integer, nullable=False),
    Column("kind", String, nullable=False),
    Column("hash", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("data", Text, nullable=False),
)

tasks_table = Table(
    "tasks", metadata,
    Column("id", String, primary_key=True),
    Column("workspace", String, index=True, nullable=False),
    Column("user_id", String, nullable=False),
    Column("parent_id", String, nullable=True),
    Column("status", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("data", Text, nullable=False),
)

jobs_table = Table(
    "jobs", metadata,
    Column("id", String, primary_key=True),
    Column("task_id", String, index=True, nullable=False),
    Column("model", String, nullable=True),
    Column("kind", String, nullable=False),
    Column("state", String, nullable=False),
    Column("position", Integer, nullable=False, default=0),
    Column("created_at", String, nullable=False),
    Column("started_at", String, nullable=True),
    Column("finished_at", String, nullable=True),
    Column("eta_s", Float, nullable=True),
    Column("error", Text, nullable=True),
)

files_table = Table(
    "files", metadata,
    Column("id", String, primary_key=True),
    Column("workspace", String, index=True, nullable=False),
    Column("area", String, nullable=False),
    Column("relpath", String, nullable=False),
    Column("label", Text, nullable=False),
    Column("sha256", String, nullable=False),
    Column("size", Integer, nullable=False),
    Column("task_id", String, nullable=True),
    Column("created_by", String, nullable=True),
    Column("created_at", String, nullable=False),
)

downgrades_table = Table(
    "downgrades", metadata,
    Column("id", String, primary_key=True),
    Column("artifact", String, index=True, nullable=False),
    Column("status", String, nullable=False),
    Column("data", Text, nullable=False),
)

outcomes_table = Table(
    "outcomes", metadata,
    Column("task_id", String, primary_key=True),
    Column("route", String, nullable=False),
    Column("model", String, nullable=False),
    Column("language", String, nullable=False, default="en"),
    Column("approved", Integer, nullable=True),
    Column("data", Text, nullable=False),
)

nodes_table = Table(
    "graph_nodes", metadata,
    Column("id", String, primary_key=True),
    Column("kind", String, index=True, nullable=False),
    Column("key", String, index=True, nullable=False),
    Column("label", Text, nullable=False),
    Column("props", Text, nullable=False),
)

edges_table = Table(
    "graph_edges", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("src", String, index=True, nullable=False),
    Column("dst", String, index=True, nullable=False),
    Column("kind", String, nullable=False),
    Column("source_record", String, nullable=True),
    Column("valid_from", String, nullable=True),
    Column("valid_to", String, nullable=True),
)


class Database:
    def __init__(self, path: Path | str) -> None:
        url = "sqlite://" if str(path) == ":memory:" else f"sqlite:///{Path(path).as_posix()}"
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        from sqlalchemy.pool import StaticPool

        kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False, "timeout": 30}}
        if str(path) == ":memory:":
            kwargs["poolclass"] = StaticPool
        self.engine: Engine = create_engine(url, **kwargs)
        self.lock = threading.RLock()

        @event.listens_for(self.engine, "connect")
        def _pragmas(dbapi_conn: Any, _rec: Any) -> None:
            cur = dbapi_conn.cursor()
            if str(path) != ":memory:":
                cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

        metadata.create_all(self.engine)

    @contextmanager
    def tx(self) -> Iterator[Connection]:
        with self.lock, self.engine.begin() as conn:
            yield conn

    @contextmanager
    def read(self) -> Iterator[Connection]:
        with self.engine.connect() as conn:
            yield conn

    def dispose(self) -> None:
        self.engine.dispose()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def loads(value: str | None) -> Any:
    return json.loads(value) if value else None
