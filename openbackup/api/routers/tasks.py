from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import session_scope
from ...db.models import Task, TaskKind, TaskLog, TaskState
from ..deps import Principal, audit, get_db, operator, viewer
from ..schemas import TaskLogOut, TaskOut

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("", response_model=list[TaskOut])
def list_tasks(
    job_id: int | None = None,
    kind: TaskKind | None = None,
    state: TaskState | None = None,
    active: bool = False,
    before_id: int | None = None,
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    _: Principal = Depends(viewer),
):
    q = select(Task).order_by(Task.id.desc()).limit(limit)
    if job_id:
        q = q.where(Task.job_id == job_id)
    if kind:
        q = q.where(Task.kind == kind)
    if state:
        q = q.where(Task.state == state)
    if active:
        q = q.where(Task.state.in_([TaskState.queued, TaskState.running]))
    if before_id:
        q = q.where(Task.id < before_id)
    return list(db.scalars(q))


def _get(db: Session, task_id: int) -> Task:
    t = db.get(Task, task_id)
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    return t


@router.get("/{task_id}", response_model=TaskOut)
def get_task(task_id: int, db: Session = Depends(get_db), _: Principal = Depends(viewer)):
    return _get(db, task_id)


@router.get("/{task_id}/logs", response_model=list[TaskLogOut])
def task_logs(task_id: int, after_id: int = 0, limit: int = Query(1000, ge=1, le=5000),
              db: Session = Depends(get_db), _: Principal = Depends(viewer)):
    _get(db, task_id)
    return list(db.scalars(select(TaskLog).where(TaskLog.task_id == task_id,
                                                 TaskLog.id > after_id)
                           .order_by(TaskLog.id).limit(limit)))


def _snapshot(task_id: int, after_id: int) -> tuple[dict | None, list[dict]]:
    with session_scope() as db:
        t = db.get(Task, task_id)
        if t is None:
            return None, []
        task = TaskOut.model_validate(t).model_dump(mode="json")
        logs = [TaskLogOut.model_validate(r).model_dump(mode="json") for r in db.scalars(
            select(TaskLog).where(TaskLog.task_id == task_id, TaskLog.id > after_id)
            .order_by(TaskLog.id).limit(500))]
    return task, logs


@router.get("/{task_id}/stream")
async def stream(task_id: int, request: Request, _: Principal = Depends(viewer)):
    """Server-sent events: the task state and new log lines, until it ends."""

    async def gen():
        after = 0
        last_task = None
        while True:
            if await request.is_disconnected():
                return
            task, logs = await asyncio.to_thread(_snapshot, task_id, after)
            if task is None:
                return
            if logs:
                after = logs[-1]["id"]
                yield f"event: logs\ndata: {json.dumps(logs)}\n\n"
            if task != last_task:
                last_task = task
                yield f"event: task\ndata: {json.dumps(task)}\n\n"
            if TaskState(task["state"]).finished and not logs:
                yield "event: end\ndata: {}\n\n"
                return
            await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/{task_id}/cancel", response_model=TaskOut)
def cancel(task_id: int, request: Request, db: Session = Depends(get_db),
           p: Principal = Depends(operator)):
    t = _get(db, task_id)
    if t.state.finished:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Task has already finished")
    t.cancel_requested = True
    audit(db, request, "task.cancel", principal=p, target=f"{t.id} {t.title}")
    return t
