"""Response models shared between routers."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from ..db.models import RepoKind, Role, TaskKind, TaskState


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserOut(ORM):
    id: int
    username: str
    full_name: str
    role: Role
    is_active: bool
    must_change_password: bool
    created_at: datetime
    last_login_at: datetime | None
    locked_until: datetime | None


class VCenterOut(ORM):
    id: int
    name: str
    host: str
    port: int
    username: str
    thumbprint: str
    created_at: datetime


class RepositoryOut(ORM):
    id: int
    name: str
    kind: RepoKind
    path: str
    nfs_server: str
    nfs_export: str
    nfs_options: str
    encrypted: bool
    capacity_bytes: int | None
    free_bytes: int | None
    created_at: datetime


class JobOut(ORM):
    id: int
    name: str
    description: str
    vcenter_id: int
    repository_id: int
    vms: list
    schedule_cron: str | None
    enabled: bool
    retention_points: int
    retention_days: int
    quiesce: bool
    active_full_days: int
    created_at: datetime
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_state: TaskState | None = None


class TaskOut(ORM):
    id: int
    kind: TaskKind
    state: TaskState
    title: str
    job_id: int | None
    repository_id: int | None
    requested_by: str
    params: dict
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    progress: float
    bytes_total: int
    bytes_read: int
    bytes_written: int
    cancel_requested: bool
    summary: str
    items: list


class TaskLogOut(ORM):
    id: int
    at: datetime
    level: str
    message: str


class RestorePointOut(ORM):
    id: str
    repository_id: int
    job_id: int | None
    vm_uuid: str
    vm_name: str
    vm_moref: str
    created_at: datetime
    kind: str
    logical_bytes: int
    read_bytes: int
    new_bytes: int
    disks: list


class AuditOut(ORM):
    id: int
    at: datetime
    username: str
    ip: str
    action: str
    target: str
    success: bool
    detail: dict
