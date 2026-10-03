from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db.models import Job, Repository, RestorePoint, Task, TaskKind, TaskState
from ..deps import Principal, get_db, viewer
from ..schemas import RepositoryOut, TaskOut

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("")
def dashboard(db: Session = Depends(get_db), _: Principal = Depends(viewer)) -> dict:
    since = datetime.now(UTC) - timedelta(hours=24)
    states = dict(db.execute(
        select(Task.state, func.count()).where(Task.kind == TaskKind.backup,
                                               Task.created_at >= since)
        .group_by(Task.state)).all())
    finished = sum(v for k, v in states.items() if TaskState(k).finished)
    good = states.get(TaskState.success, 0) + states.get(TaskState.warning, 0)
    latest_per_vm = (select(RestorePoint.vm_uuid, func.max(RestorePoint.created_at).label("t"))
                     .group_by(RestorePoint.vm_uuid).subquery())
    stale_cutoff = datetime.now(UTC) - timedelta(hours=48)
    stale = db.scalar(select(func.count()).select_from(latest_per_vm)
                      .where(latest_per_vm.c.t < stale_cutoff))
    return {
        "backups_24h": {k.value if hasattr(k, "value") else k: v for k, v in states.items()},
        "success_rate_24h": round(good / finished, 3) if finished else None,
        "protected_vms": db.scalar(select(func.count(func.distinct(RestorePoint.vm_uuid)))),
        "stale_vms": stale,
        "restore_points": db.scalar(select(func.count()).select_from(RestorePoint)),
        "jobs": db.scalar(select(func.count()).select_from(Job)),
        "jobs_enabled": db.scalar(select(func.count()).select_from(Job)
                                  .where(Job.enabled.is_(True))),
        "running": [TaskOut.model_validate(t).model_dump(mode="json") for t in db.scalars(
            select(Task).where(Task.state.in_([TaskState.running, TaskState.queued]))
            .order_by(Task.id))],
        "recent": [TaskOut.model_validate(t).model_dump(mode="json") for t in db.scalars(
            select(Task).where(Task.state.not_in([TaskState.running, TaskState.queued]))
            .order_by(Task.id.desc()).limit(10))],
        "repositories": [RepositoryOut.model_validate(r).model_dump(mode="json")
                         for r in db.scalars(select(Repository).order_by(Repository.name))],
        "upcoming": [{"job_id": j.id, "name": j.name, "next_run_at": j.next_run_at}
                     for j in db.scalars(select(Job).where(Job.enabled.is_(True),
                                                           Job.next_run_at.is_not(None))
                                         .order_by(Job.next_run_at).limit(5))],
    }
