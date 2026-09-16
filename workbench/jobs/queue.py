"""SQLite-backed job queue with an in-process scheduler (README section 4.8).

Agent jobs run in worker threads, at most ``per_model_concurrency`` per active model in total.
Swap-slot work is additionally batched by the pool manager. Document ingestion runs in a
separate thread pool so large scans do not block interactive work.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from sqlalchemy import select

from workbench.core.db import jobs_table
from workbench.core.errors import NotFound, PolicyError
from workbench.core.ids import new_id

if TYPE_CHECKING:
    from workbench.runtime import Runtime

ACTIVE = ("queued", "running")


class Job(BaseModel):
    id: str
    task_id: str
    model: str | None
    kind: str
    state: str
    position: int
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    eta_s: float | None = None
    error: str | None = None


class JobQueue:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self.max_workers = max(1, rt.settings.per_model_concurrency * max(1, len(rt.pool.residents())))
        self._cancel: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._scheduler: threading.Thread | None = None
        self.ingest_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ingest")

    # -- persistence ---------------------------------------------------------------------------

    def _row(self, row: Any) -> Job:
        return Job(id=row.id, task_id=row.task_id, model=row.model, kind=row.kind, state=row.state,
                   position=row.position, created_at=row.created_at, started_at=row.started_at,
                   finished_at=row.finished_at, eta_s=row.eta_s, error=row.error)

    def get(self, job_id: str) -> Job:
        with self.rt.db.read() as conn:
            row = conn.execute(select(jobs_table).where(jobs_table.c.id == job_id)).first()
        if row is None:
            raise NotFound(f"job {job_id} not found")
        return self._row(row)

    def for_task(self, task_id: str) -> Job | None:
        with self.rt.db.read() as conn:
            row = conn.execute(select(jobs_table).where(jobs_table.c.task_id == task_id)
                               .order_by(jobs_table.c.created_at.desc())).first()
        return self._row(row) if row else None

    def _update(self, job_id: str, **values: Any) -> None:
        with self.rt.db.tx() as conn:
            conn.execute(jobs_table.update().where(jobs_table.c.id == job_id).values(**values))

    def list(self, include_done: bool = True, limit: int = 100) -> list[Job]:
        stmt = select(jobs_table).order_by(jobs_table.c.created_at.desc()).limit(limit)
        if not include_done:
            stmt = stmt.where(jobs_table.c.state.in_(ACTIVE))
        with self.rt.db.read() as conn:
            rows = conn.execute(stmt).all()
        jobs = [self._row(r) for r in rows]
        queued = sorted((j for j in jobs if j.state == "queued"), key=lambda j: j.created_at)
        running = [j for j in jobs if j.state == "running"]
        budget = self.rt.settings.budget("model_task") * 6
        ahead = sum(budget for _ in running)
        for i, j in enumerate(queued, start=1):
            j.position = i
            j.eta_s = round(ahead / self.max_workers, 1)
            ahead += budget
        return jobs

    # -- submission ----------------------------------------------------------------------------

    def submit(self, task_id: str, kind: str = "agent") -> Job:
        job_id = new_id("J")
        now = self.rt.clock.now().isoformat()
        with self.rt.db.tx() as conn:
            conn.execute(jobs_table.insert().values(id=job_id, task_id=task_id, model=None, kind=kind,
                                                    state="queued", position=0, created_at=now))
        self._cancel[job_id] = threading.Event()
        self._wake.set()
        return self.get(job_id)

    def cancel(self, job_id: str, user: str) -> Job:
        job = self.get(job_id)
        task = self.rt.tasks.get(job.task_id)
        user_obj = self.rt.policy.user(user)
        if task.user != user and "admin" not in user_obj.roles:
            raise PolicyError("only the task owner or an administrator can cancel a job")
        if job.state not in ACTIVE:
            return job
        self._cancel.setdefault(job_id, threading.Event()).set()
        if job.state == "queued":
            self._update(job_id, state="cancelled", finished_at=self.rt.clock.now().isoformat())
            task.status = "cancelled"
            self.rt.tasks.save(task)
        self.rt.audit.append({"type": "job.cancel", "job": job_id, "task": job.task_id, "by": user})
        return self.get(job_id)

    def run_inline(self, task_id: str) -> Any:
        job = self.submit(task_id)
        self._run(job)
        return self.rt.tasks.get(task_id)

    def ingest(self, fn: Any, *args: Any) -> Future[Any]:
        return self.ingest_pool.submit(fn, *args)

    # -- scheduling ----------------------------------------------------------------------------

    def _run(self, job: Job) -> None:
        cancel = self._cancel.setdefault(job.id, threading.Event())
        self._update(job.id, state="running", started_at=self.rt.clock.now().isoformat())
        try:
            state = self.rt.orchestrator.run(job.task_id, cancel.is_set)
            final = "cancelled" if state.status == "cancelled" else ("failed" if state.status == "failed" else "done")
            self._update(job.id, state=final, model=state.model, finished_at=self.rt.clock.now().isoformat(),
                         error=state.error)
        except Exception as exc:  # the orchestrator records its own failures; this is a last resort
            self._update(job.id, state="failed", finished_at=self.rt.clock.now().isoformat(), error=str(exc))
        finally:
            with self._lock:
                self._threads.pop(job.id, None)
            self._wake.set()

    def _tick(self) -> None:
        with self._lock:
            running = sum(1 for t in self._threads.values() if t.is_alive())
            free = self.max_workers - running
        if free <= 0:
            return
        queued = sorted((j for j in self.list(include_done=False) if j.state == "queued"),
                        key=lambda j: j.created_at)
        for job in queued[:free]:
            thread = threading.Thread(target=self._run, args=(job,), name=f"job-{job.id}", daemon=True)
            with self._lock:
                self._threads[job.id] = thread
            thread.start()

    def start(self) -> None:
        if self._scheduler is not None:
            return
        with self.rt.db.tx() as conn:
            conn.execute(jobs_table.update().where(jobs_table.c.state == "running").values(
                state="failed", error="server restarted while the job was running"))

        def loop() -> None:
            while not self._stop.is_set():
                self._tick()
                self._wake.wait(0.5)
                self._wake.clear()

        self._scheduler = threading.Thread(target=loop, name="job-scheduler", daemon=True)
        self._scheduler.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for ev in self._cancel.values():
            ev.set()
        self.ingest_pool.shutdown(wait=False, cancel_futures=True)

    def wait(self, task_id: str, timeout: float = 60.0) -> None:
        job = self.for_task(task_id)
        if job is None:
            return
        with self._lock:
            thread = self._threads.get(job.id)
        if thread is not None:
            thread.join(timeout)
