"""Knowledge base: ingestion and hybrid, graph-expanded, revision-aware, label-filtered search."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from workbench.core.db import Database
from workbench.core.labels import Label, PolicyEngine, User, Workspace
from workbench.core.normalise import find_tags, norm_tag
from workbench.kb.bm25 import BM25Index
from workbench.kb.chunking import Chunk, chunk_markdown, chunk_pages
from workbench.kb.clause_diff import ClauseDiff, diff_revisions
from workbench.kb.embed import Embedder, HashingEmbedder
from workbench.kb.graph import Node, PlantGraph, node_id
from workbench.kb.rerank import LexicalReranker, Reranker
from workbench.kb.revisions import RevisionIndex, RevisionInfo
from workbench.kb.store import SimpleVectorStore

RRF_K = 60
CANDIDATES = 30


@dataclass
class Hit:
    chunk: Chunk
    score: float
    via: list[str] = field(default_factory=list)
    currency_warning: str | None = None


@dataclass
class SearchResult:
    hits: list[Hit]
    graph_facts: list[Node]
    expanded_tags: list[str]
    filtered_out: dict[str, int]
    as_of: date


class KnowledgeBase:
    def __init__(self, kb_dir: Path, db: Database, embedder: Embedder | None = None,
                 reranker: Reranker | None = None) -> None:
        self.dir = kb_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db = db
        self.embedder = embedder or HashingEmbedder()
        self.reranker = reranker or LexicalReranker()
        self.graph = PlantGraph(db)
        self.store = SimpleVectorStore(self.dir / "vectors.npz", self.embedder.dim)
        self.bm25 = BM25Index()
        self.revisions = RevisionIndex()
        self.chunks: dict[str, Chunk] = {}
        self._lock = threading.RLock()
        self._load()

    # -- persistence -------------------------------------------------------------------------

    @property
    def _chunks_path(self) -> Path:
        return self.dir / "chunks.jsonl"

    def _load(self) -> None:
        if self._chunks_path.is_file():
            with self._chunks_path.open(encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        c = Chunk.model_validate_json(line)
                        self.chunks[c.id] = c
        self._rebuild()

    def _save(self) -> None:
        tmp = self._chunks_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for c in self.chunks.values():
                fh.write(c.model_dump_json() + "\n")
        tmp.replace(self._chunks_path)
        self.store.save()

    def _rebuild(self) -> None:
        self.bm25.build([(c.id, f"{c.title}\n{c.section}\n{c.text}") for c in self.chunks.values()])
        infos: dict[tuple[str, str], RevisionInfo] = {}
        for c in self.chunks.values():
            if c.revision and c.effective_from:
                infos[(c.doc_number, c.revision)] = RevisionInfo(
                    doc_number=c.doc_number, revision=c.revision, effective_from=c.effective_from, title=c.title)
        self.revisions.build(list(infos.values()))
        for c in self.chunks.values():
            if c.revision:
                info = self.revisions.info(c.doc_number, c.revision)
                c.superseded_by = info.superseded_by if info else None

    def _add(self, chunks: list[Chunk]) -> None:
        if not chunks:
            return
        vectors = self.embedder.embed([f"{c.title}\n{c.section}\n{c.text}" for c in chunks])
        self.store.upsert([c.id for c in chunks], vectors)
        for c in chunks:
            self.chunks[c.id] = c

    # -- ingestion ---------------------------------------------------------------------------

    def ingest_dir(self, path: Path, asset_register: Path | None = None) -> dict[str, int]:
        with self._lock:
            new: list[Chunk] = []
            for md in sorted(path.rglob("*.md")):
                new += chunk_markdown(md.read_text(encoding="utf-8"),
                                      {"doc_number": md.stem, "source": md.relative_to(path).as_posix()})
            self._add(new)
            self._rebuild()
            stats = {"chunks": len(new), "documents": len({c.doc_id for c in new})}
            if asset_register and asset_register.is_file():
                stats["assets"] = self.graph.load_asset_register(asset_register)
            self.graph.load_chunks(list(self.chunks.values()))
            history = path / "inspections_history.csv"
            if history.is_file():
                stats["inspections"] = self.graph.load_inspections(history)
            self._save()
            return stats

    def index_document(self, doc_number: str, title: str, pages: list[tuple[int, str]], label: Label,
                       workspace: str, acl_groups: list[str], source: str) -> int:
        """Index a workspace document (for follow-up questions about an attachment)."""
        with self._lock:
            key = f"{workspace}/{doc_number}"
            for cid in [cid for cid, c in self.chunks.items() if c.workspace == workspace and c.doc_number == key]:
                del self.chunks[cid]
            chunks = chunk_pages(pages, {"doc_number": key, "title": title, "label": label,
                                         "workspace": workspace, "acl_groups": acl_groups, "source": source})
            self._add(chunks)
            self._rebuild()
            self._save()
            return len(chunks)

    def has_document(self, workspace: str, doc_number: str) -> bool:
        key = f"{workspace}/{doc_number}"
        return any(c.doc_number == key for c in self.chunks.values())

    # -- queries -----------------------------------------------------------------------------

    def chunks_of(self, doc_number: str, revision: str | None) -> list[Chunk]:
        return sorted((c for c in self.chunks.values() if c.doc_number == doc_number and c.revision == revision),
                      key=lambda c: c.id)

    def clause_diff(self, doc_number: str, revision: str) -> list[ClauseDiff]:
        prev = self.revisions.previous(doc_number, revision)
        if prev is None:
            return []
        return diff_revisions(self.chunks_of(doc_number, prev.revision), self.chunks_of(doc_number, revision))

    def governed_equipment(self, doc_number: str, as_of: date, user: User, workspace: Workspace,
                           policy: PolicyEngine) -> tuple[Chunk, dict[str, list[str]]] | None:
        """Equipment classes and tags governed by the revision of ``doc_number`` in force on ``as_of``.

        Returns the first chunk of that revision (for its anchor and label) and ``{class: [tags]}``,
        or ``None`` when the document is unknown or the user may not see it."""
        with self._lock:
            ceiling = policy.retrieval_ceiling(user, workspace)
            revision = self.revisions.in_force(doc_number, as_of)
            chunks = [c for c in self.chunks_of(doc_number, revision)
                      if self._allowed(c, user, workspace, ceiling, as_of, policy) is None]
            if not chunks:
                return None
            out: dict[str, list[str]] = {}
            for cls in sorted({cls for c in chunks for cls in c.applies_to_classes}):
                tags = [t for t in self.graph.tags_of_class(cls)
                        if (n := self.graph.node(node_id("tag", t))) is not None and ceiling.dominates(n.label)]
                out[cls] = tags
            return chunks[0], out

    def _allowed(self, c: Chunk, user: User, workspace: Workspace, ceiling: Label, as_of: date,
                 policy: PolicyEngine) -> str | None:
        if c.workspace is not None:
            if c.workspace != workspace.id:
                return "workspace"
        elif not (set(c.acl_groups) & set(user.groups)):
            return "acl"
        if not ceiling.dominates(c.label):
            return "label"
        if c.revision is not None:
            if c.effective_from and c.effective_from > as_of:
                return "revision"
            if self.revisions.in_force(c.doc_number, as_of) != c.revision:
                return "revision"
        return None

    def search(self, queries: list[str], top_k: int, as_of: date, user: User, workspace: Workspace,
               policy: PolicyEngine, extra_tags: list[str] | None = None, today: date | None = None,
               doc_filter: str | None = None, expand_graph: bool = True) -> SearchResult:
        with self._lock:
            ceiling = policy.retrieval_ceiling(user, workspace)
            fused: dict[str, float] = {}
            via: dict[str, set[str]] = {}
            best_q: dict[str, str] = {}
            for q in queries:
                qv = self.embedder.embed([q])[0]
                for rank, (cid, _s) in enumerate(self.store.search(qv, CANDIDATES)):
                    fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
                    via.setdefault(cid, set()).add("dense")
                    best_q.setdefault(cid, q)
                for rank, (cid, _s) in enumerate(self.bm25.search(q, CANDIDATES)):
                    fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
                    via.setdefault(cid, set()).add("sparse")
                    best_q.setdefault(cid, q)
            tags = list(dict.fromkeys([t for q in queries for t in find_tags(q)] + list(extra_tags or [])))
            if not expand_graph:
                tags = []
            graph_facts: list[Node] = []
            graph_docs: set[str] = set()
            for tag in tags:
                for node, _edge in self.graph.neighbours(tag, as_of=as_of, ceiling=ceiling):
                    if node.kind == "inspection":
                        if str(node.props.get("date", "")) <= as_of.isoformat():
                            graph_facts.append(node)
                    elif node.kind == "document":
                        graph_docs.add(str(node.props.get("doc_number") or node.key.split("@")[0]))
            if graph_docs:
                boost = 1.0 / (RRF_K + 1)
                for cid, c in self.chunks.items():
                    if c.doc_number in graph_docs:
                        fused[cid] = fused.get(cid, 0.0) + boost * 0.5
                        via.setdefault(cid, set()).add("graph")
                        best_q.setdefault(cid, " ".join(queries))
            filtered: dict[str, int] = {}
            scored: list[Hit] = []
            top_rrf = max(fused.values(), default=1.0) or 1.0
            for cid, rrf in fused.items():
                c = self.chunks.get(cid)
                if c is None:
                    continue
                if doc_filter and c.doc_number != doc_filter:
                    continue
                reason = self._allowed(c, user, workspace, ceiling, as_of, policy)
                if reason:
                    filtered[reason] = filtered.get(reason, 0) + 1
                    continue
                if not c.text.replace("#", "").strip() or len(c.text.split()) < 4:
                    continue
                lex = max(self.reranker.score(q, f"{c.section}\n{c.text}") for q in queries)
                score = round(0.6 * lex + 0.4 * rrf / top_rrf, 5)
                warning = None
                if c.revision and today and self.revisions.is_superseded(c.doc_number, c.revision, today):
                    info = self.revisions.info(c.doc_number, c.revision)
                    warning = (f"{c.doc_number} Rev {c.revision} is superseded by Rev {info.superseded_by} "
                               f"from {info.superseded_on}" if info else "superseded")
                scored.append(Hit(chunk=c, score=score, via=sorted(via.get(cid, set())), currency_warning=warning))
            scored.sort(key=lambda h: (-h.score, h.chunk.id))
            facts = sorted({n.id: n for n in graph_facts}.values(), key=lambda n: str(n.props.get("date")))
            return SearchResult(hits=scored[:top_k], graph_facts=facts, expanded_tags=[norm_tag(t) for t in tags],
                                filtered_out=filtered, as_of=as_of)

    def stats(self) -> dict[str, Any]:
        docs = {(c.doc_number, c.revision) for c in self.chunks.values() if c.workspace is None}
        return {"chunks": len(self.chunks), "documents": len(docs),
                "workspace_chunks": sum(1 for c in self.chunks.values() if c.workspace),
                "graph_nodes": len(self.graph.nodes()),
                "revision_chains": {k: [r.revision for r in v] for k, v in self.revisions.chains.items()}}

    def dump(self) -> str:
        return json.dumps(self.stats(), indent=2)
