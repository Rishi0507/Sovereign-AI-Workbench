"""Vector stores. ``SimpleVectorStore`` persists to ``.npz``; ``QdrantStore`` is optional."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

import numpy as np


class VectorStore(Protocol):
    def upsert(self, ids: list[str], vectors: np.ndarray) -> None: ...

    def search(self, vector: np.ndarray, k: int) -> list[tuple[str, float]]: ...

    def save(self) -> None: ...


class SimpleVectorStore:
    def __init__(self, path: Path, dim: int) -> None:
        self.path = path
        self.dim = dim
        self.ids: list[str] = []
        self.matrix = np.zeros((0, dim), dtype=np.float32)
        if path.is_file():
            data = np.load(path, allow_pickle=False)
            self.matrix = data["matrix"].astype(np.float32)
            self.ids = json.loads(str(data["ids"]))

    def upsert(self, ids: list[str], vectors: np.ndarray) -> None:
        index = {cid: i for i, cid in enumerate(self.ids)}
        new_rows = []
        new_ids = []
        for cid, vec in zip(ids, vectors, strict=True):
            if cid in index:
                self.matrix[index[cid]] = vec
            else:
                new_ids.append(cid)
                new_rows.append(vec)
        if new_rows:
            self.matrix = np.vstack([self.matrix, np.asarray(new_rows, dtype=np.float32)])
            self.ids.extend(new_ids)

    def search(self, vector: np.ndarray, k: int) -> list[tuple[str, float]]:
        if not self.ids:
            return []
        scores = self.matrix @ vector.astype(np.float32)
        k = min(k, len(self.ids))
        top = np.argpartition(-scores, k - 1)[:k]
        ordered = sorted(top.tolist(), key=lambda i: (-float(scores[i]), self.ids[i]))
        return [(self.ids[i], float(scores[i])) for i in ordered]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp.npz")
        np.savez_compressed(tmp, matrix=self.matrix, ids=np.array(json.dumps(self.ids)))
        tmp.replace(self.path)


class QdrantStore:
    """Qdrant local mode (on-disk). Optional: requires ``qdrant-client``; imported lazily."""

    def __init__(self, path: Path, dim: int, collection: str = "kb") -> None:
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams

        self.client: Any = QdrantClient(path=str(path))
        self.collection = collection
        self._point_ids: dict[str, int] = {}
        if not self.client.collection_exists(collection):
            self.client.create_collection(collection, vectors_config=VectorParams(size=dim, distance=Distance.COSINE))

    def upsert(self, ids: list[str], vectors: np.ndarray) -> None:
        from qdrant_client.models import PointStruct

        points = []
        for cid, vec in zip(ids, vectors, strict=True):
            pid = self._point_ids.setdefault(cid, len(self._point_ids) + 1)
            points.append(PointStruct(id=pid, vector=vec.tolist(), payload={"chunk_id": cid}))
        self.client.upsert(self.collection, points=points)

    def search(self, vector: np.ndarray, k: int) -> list[tuple[str, float]]:
        hits = self.client.query_points(self.collection, query=vector.tolist(), limit=k).points
        return [(h.payload["chunk_id"], float(h.score)) for h in hits]

    def save(self) -> None:
        return None
