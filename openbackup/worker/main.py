"""The worker: runs queued tasks and fires job schedules.

API and worker share nothing but the database. Tasks against the same
repository run one at a time, which is what makes it safe for a backup to
run retention and garbage collection inline. Different repositories run in
parallel up to ``worker_concurrency``.
"""

from __future__ import annotations

import logging
import signal
import threading
import traceback
from datetime import UTC, datetime

from sqlalchemy import select, update

from ..config import get_settings
from ..db import init_db, session_scope
from ..db.models import Job, Task, TaskKind, TaskState
from ..engine.context import Cancelled
from ..services import next_run
from .context import DbTaskContext
from .tasks import EXECUTORS

log = logging.getLogger("openbackup.worker")


def enqueue_backup(db, job: Job, requested_by: str, params: dict | None = None) -> Task | None:
    """Queue a run of ``job`` unless one is already queued or running."""
    busy = db.scalar(select(Task.id).where(
        Task.job_id == job.id, Task.state.in_([TaskState.queued, TaskState.running])))
    if busy:
        return None
    t = Task(kind=TaskKind.backup, title=f"Backup job {job.name}", job_id=job.id,
             repository_id=job.repository_id, requested_by=requested_by, params=params or {})
    db.add(t)
    db.flush()
    return t


def schedule_tick(now: datetime | None = None) -> int:
    """Queue every scheduled job that is due. A job that was due several
    times while the worker was down runs once, not once per missed slot."""
    now = now or datetime.now(UTC)
    queued = 0
    with session_scope() as db:
        for job in db.scalars(select(Job).where(Job.enabled.is_(True),
                                                Job.schedule_cron.is_not(None))):
            nra = job.next_run_at
            if nra is None:
                job.next_run_at = next_run(job.schedule_cron, now)
                continue
            if nra <= now:
                if enqueue_backup(db, job, "scheduler"):
                    queued += 1
                job.next_run_at = next_run(job.schedule_cron, now)
    return queued


def recover_interrupted() -> None:
    with session_scope() as db:
        db.execute(update(Task).where(Task.state == TaskState.running).values(
            state=TaskState.failed, finished_at=datetime.now(UTC),
            summary="Interrupted: the worker stopped while this task was running"))


def claim_next(running_repos: set[int | None]) -> int | None:
    with session_scope() as db:
        for t in db.scalars(select(Task).where(Task.state == TaskState.queued)
                            .order_by(Task.id)):
            if t.repository_id is not None and t.repository_id in running_repos:
                continue
            if t.cancel_requested:
                t.state = TaskState.cancelled
                t.finished_at = datetime.now(UTC)
                continue
            res = db.execute(update(Task).where(Task.id == t.id,
                                                Task.state == TaskState.queued)
                             .values(state=TaskState.running, started_at=datetime.now(UTC)))
            if res.rowcount == 1:
                return t.id
    return None


def execute(task_id: int) -> None:
    ctx = DbTaskContext(task_id)
    with session_scope() as db:
        kind = db.get(Task, task_id).kind
    try:
        state, summary = EXECUTORS[kind](task_id, ctx)
    except Cancelled:
        state, summary = TaskState.cancelled, "Cancelled by user"
        ctx.log(summary, "warning")
    except Exception as e:
        log.error("task %s crashed:\n%s", task_id, traceback.format_exc())
        state, summary = TaskState.failed, str(e) or type(e).__name__
        ctx.log(f"Failed: {summary}", "error")
    if state == TaskState.success and ctx.warnings:
        state = TaskState.warning
    ctx.flush()
    with session_scope() as db:
        t = db.get(Task, task_id)
        t.state = state
        t.summary = summary
        t.finished_at = datetime.now(UTC)
        if state in (TaskState.success, TaskState.warning):
            t.progress = 1.0
    ctx.log(f"Finished: {state.value}. {summary}")


class Worker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.stop = threading.Event()
        self.threads: dict[int, tuple[threading.Thread, int | None]] = {}

    def _reap(self) -> None:
        for tid, (th, _repo) in list(self.threads.items()):
            if not th.is_alive():
                del self.threads[tid]

    def run_once(self) -> None:
        schedule_tick()
        self._reap()
        while len(self.threads) < self.settings.worker_concurrency:
            running = {repo for _th, repo in self.threads.values()}
            tid = claim_next(running)
            if tid is None:
                break
            with session_scope() as db:
                repo_id = db.get(Task, tid).repository_id
            th = threading.Thread(target=execute, args=(tid,), name=f"task-{tid}", daemon=True)
            self.threads[tid] = (th, repo_id)
            th.start()

    def run(self) -> None:
        recover_interrupted()
        log.info("worker started")
        while not self.stop.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("worker loop error")
            self.stop.wait(self.settings.worker_poll_seconds)
        log.info("worker stopping; waiting for running tasks")
        for th, _ in self.threads.values():
            th.join()


def run() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_db()
    w = Worker()

    def _stop(*_):
        w.stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    w.run()
