from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db.models import Job, Repository, Task, TaskState, VCenter
from ...services import next_run, valid_cron
from ...worker.main import enqueue_backup
from ..deps import Principal, admin, audit, get_db, operator, viewer
from ..schemas import JobOut, TaskOut

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class VmRef(BaseModel):
    moref: str = Field(pattern=r"^vm-\d+$")
    name: str = Field(max_length=255)


class JobIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field("", max_length=2000)
    vcenter_id: int
    repository_id: int
    vms: list[VmRef] = Field(min_length=1, max_length=500)
    schedule_cron: str | None = Field(None, max_length=128)
    enabled: bool = True
    retention_points: int = Field(14, ge=0, le=10000)
    retention_days: int = Field(0, ge=0, le=36500)
    quiesce: bool = True
    active_full_days: int = Field(0, ge=0, le=3650)

    @field_validator("schedule_cron")
    @classmethod
    def _cron(cls, v: str | None) -> str | None:
        v = (v or "").strip() or None
        if v is not None and not valid_cron(v):
            raise ValueError("Invalid cron expression")
        return v


def _check_refs(db: Session, body: JobIn) -> None:
    if db.get(VCenter, body.vcenter_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown vCenter")
    if db.get(Repository, body.repository_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown repository")
    if body.retention_points == 0 and body.retention_days == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Set a retention by number of points or by days")


def _out(db: Session, job: Job) -> JobOut:
    out = JobOut.model_validate(job)
    last = db.scalar(select(Task.state).where(Task.job_id == job.id)
                     .order_by(Task.id.desc()).limit(1))
    out.last_state = last
    return out


def _get(db: Session, job_id: int) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return job


@router.get("", response_model=list[JobOut])
def list_jobs(db: Session = Depends(get_db), _: Principal = Depends(viewer)):
    return [_out(db, j) for j in db.scalars(select(Job).order_by(Job.name))]


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: int, db: Session = Depends(get_db), _: Principal = Depends(viewer)):
    return _out(db, _get(db, job_id))


@router.post("", response_model=JobOut, status_code=201)
def create_job(body: JobIn, request: Request, db: Session = Depends(get_db),
               p: Principal = Depends(admin)):
    _check_refs(db, body)
    if db.scalar(select(Job).where(func.lower(Job.name) == body.name.lower())):
        raise HTTPException(status.HTTP_409_CONFLICT, "A job with that name exists")
    data = body.model_dump()
    data["vms"] = [v.model_dump() for v in body.vms]
    job = Job(**data)
    if job.schedule_cron:
        job.next_run_at = next_run(job.schedule_cron)
    db.add(job)
    db.flush()
    audit(db, request, "job.create", principal=p, target=job.name,
          detail={"vms": len(job.vms), "schedule": job.schedule_cron})
    return _out(db, job)


@router.put("/{job_id}", response_model=JobOut)
def update_job(job_id: int, body: JobIn, request: Request, db: Session = Depends(get_db),
               p: Principal = Depends(admin)):
    job = _get(db, job_id)
    _check_refs(db, body)
    clash = db.scalar(select(Job).where(func.lower(Job.name) == body.name.lower(),
                                        Job.id != job_id))
    if clash:
        raise HTTPException(status.HTTP_409_CONFLICT, "A job with that name exists")
    if body.repository_id != job.repository_id and db.scalar(
            select(Task.id).where(Task.job_id == job_id,
                                  Task.state.in_([TaskState.queued, TaskState.running]))):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Cannot change the repository while the job is running")
    for k, v in body.model_dump().items():
        setattr(job, k, v)
    job.vms = [v.model_dump() for v in body.vms]
    job.next_run_at = next_run(job.schedule_cron) if job.schedule_cron else None
    audit(db, request, "job.update", principal=p, target=job.name)
    return _out(db, job)


@router.delete("/{job_id}", status_code=204)
def delete_job(job_id: int, request: Request, db: Session = Depends(get_db),
               p: Principal = Depends(admin)):
    """Delete the job definition. Its restore points stay in the repository
    and remain restorable."""
    job = _get(db, job_id)
    if db.scalar(select(Task.id).where(Task.job_id == job_id,
                                       Task.state.in_([TaskState.queued, TaskState.running]))):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Stop the running task first")
    audit(db, request, "job.delete", principal=p, target=job.name)
    db.delete(job)


class RunIn(BaseModel):
    active_full: bool = False
    vms: list[str] | None = None


@router.post("/{job_id}/run", response_model=TaskOut, status_code=202)
def run_job(job_id: int, request: Request, body: RunIn | None = None,
            db: Session = Depends(get_db), p: Principal = Depends(operator)):
    job = _get(db, job_id)
    body = body or RunIn()
    params = {"active_full": body.active_full}
    if body.vms:
        params["vms"] = body.vms
    t = enqueue_backup(db, job, p.user.username, params)
    if t is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This job is already queued or running")
    audit(db, request, "job.run", principal=p, target=job.name, detail=params)
    return t


class EnableIn(BaseModel):
    enabled: bool


@router.post("/{job_id}/enabled", response_model=JobOut)
def set_enabled(job_id: int, body: EnableIn, request: Request, db: Session = Depends(get_db),
                p: Principal = Depends(operator)):
    job = _get(db, job_id)
    job.enabled = body.enabled
    if body.enabled and job.schedule_cron:
        job.next_run_at = next_run(job.schedule_cron)
    audit(db, request, "job.enable" if body.enabled else "job.disable", principal=p,
          target=job.name)
    return _out(db, job)
