"""Application database schema.

This database holds configuration, users, tasks and a mirror of restore
points for fast listing. The repositories on NFS remain authoritative for
backup data: a lost database can be rebuilt by importing a repository.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Stores UTC and always hands back timezone-aware values. SQLite keeps
    no zone, so without this, times would come back naive and be
    misread as local time by API clients."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(UTC)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime()}


class Role(enum.StrEnum):
    viewer = "viewer"
    operator = "operator"
    admin = "admin"

    @property
    def rank(self) -> int:
        return {"viewer": 0, "operator": 1, "admin": 2}[self.value]


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    full_name: Mapped[str] = mapped_column(String(128), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False), default=Role.viewer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(nullable=True)
    password_changed_at: Mapped[datetime] = mapped_column(default=utcnow)


class AuthSession(Base):
    """A browser login. The primary key is a SHA-256 of the cookie token, so a
    leaked database does not hand out live sessions."""

    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(255), default="")

    user: Mapped[User] = relationship()


class LoginFailure(Base):
    """Failed logins by source address, for per-IP throttling."""

    __tablename__ = "login_failures"

    id: Mapped[int] = mapped_column(primary_key=True)
    ip: Mapped[str] = mapped_column(String(64), index=True)
    at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class AuditEntry(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    user_id: Mapped[int | None] = mapped_column(nullable=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str] = mapped_column(String(255), default="")
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class VCenter(Base):
    __tablename__ = "vcenters"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=443)
    username: Mapped[str] = mapped_column(String(255))
    password_enc: Mapped[str] = mapped_column(Text)
    # SHA-1 colon-hex as VDDK expects; pinned when the vCenter is added.
    thumbprint: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class DatastoreAccess(Base):
    """How this server reads an NFS datastore directly. The address vSphere
    uses (often a storage VLAN) may not be routable from here, so the
    administrator supplies one that is. Always mounted read-only."""

    __tablename__ = "datastore_access"
    __table_args__ = (UniqueConstraint("vcenter_id", "datastore"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    vcenter_id: Mapped[int] = mapped_column(ForeignKey("vcenters.id", ondelete="CASCADE"))
    datastore: Mapped[str] = mapped_column(String(255))
    nfs_server: Mapped[str] = mapped_column(String(255))
    nfs_export: Mapped[str] = mapped_column(String(1024))
    nfs_options: Mapped[str] = mapped_column(String(255), default="nfsvers=4,hard")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class KubeCluster(Base):
    """An OpenShift / Kubernetes cluster. The API server is trusted through a
    pinned CA bundle, never the system trust store."""

    __tablename__ = "kube_clusters"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    api_url: Mapped[str] = mapped_column(String(512))
    ca_pem: Mapped[str] = mapped_column(Text)
    # Read-only ServiceAccount token used for backups.
    backup_token_enc: Mapped[str] = mapped_column(Text)
    # Optional stored credential for restores; otherwise entered per restore.
    restore_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The vCenter backing vSphere CSI volumes, for persistent-volume data.
    vcenter_id: Mapped[int | None] = mapped_column(
        ForeignKey("vcenters.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    vcenter: Mapped[VCenter | None] = relationship()


class JobKind(enum.StrEnum):
    vsphere = "vsphere"
    openshift = "openshift"
    etcd = "etcd"


class RepoKind(enum.StrEnum):
    local = "local"
    nfs = "nfs"


class Repository(Base):
    __tablename__ = "repositories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[RepoKind] = mapped_column(Enum(RepoKind, native_enum=False))
    # local: absolute path. nfs: subdirectory inside the export.
    path: Mapped[str] = mapped_column(String(1024), default="")
    nfs_server: Mapped[str] = mapped_column(String(255), default="")
    nfs_export: Mapped[str] = mapped_column(String(1024), default="")
    nfs_options: Mapped[str] = mapped_column(String(255), default="nfsvers=4.2,hard")
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    passphrase_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    capacity_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    free_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[JobKind] = mapped_column(Enum(JobKind, native_enum=False),
                                          default=JobKind.vsphere,
                                          server_default=JobKind.vsphere.value)
    vcenter_id: Mapped[int | None] = mapped_column(ForeignKey("vcenters.id"), nullable=True)
    cluster_id: Mapped[int | None] = mapped_column(
        ForeignKey("kube_clusters.id"), nullable=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    # vsphere: [{"moref": "vm-123", "name": "web01"}]
    vms: Mapped[list] = mapped_column(JSON, default=list)
    # openshift: {"namespaces": [...], "freeze_vms": bool}
    # etcd:      {"source": {...}}
    selection: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    schedule_cron: Mapped[str | None] = mapped_column(String(128), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    retention_points: Mapped[int] = mapped_column(Integer, default=14)
    retention_days: Mapped[int] = mapped_column(Integer, default=0)
    quiesce: Mapped[bool] = mapped_column(Boolean, default=True)
    # Ignore CBT and read everything once every N days (0 = never).
    active_full_days: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    next_run_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(nullable=True)

    vcenter: Mapped[VCenter | None] = relationship()
    cluster: Mapped[KubeCluster | None] = relationship()
    repository: Mapped[Repository] = relationship()


class TaskKind(enum.StrEnum):
    backup = "backup"
    restore = "restore"
    verify = "verify"
    gc = "gc"


class TaskState(enum.StrEnum):
    queued = "queued"
    running = "running"
    success = "success"
    warning = "warning"
    failed = "failed"
    cancelled = "cancelled"

    @property
    def finished(self) -> bool:
        return self not in (TaskState.queued, TaskState.running)


class Task(Base):
    """One execution: a backup run of a job, a restore, a verify or a GC."""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[TaskKind] = mapped_column(Enum(TaskKind, native_enum=False))
    state: Mapped[TaskState] = mapped_column(
        Enum(TaskState, native_enum=False), default=TaskState.queued, index=True
    )
    title: Mapped[str] = mapped_column(String(255), default="")
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    repository_id: Mapped[int | None] = mapped_column(
        ForeignKey("repositories.id", ondelete="SET NULL"), nullable=True
    )
    requested_by: Mapped[str] = mapped_column(String(64), default="scheduler")
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    bytes_total: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_read: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_written: Mapped[int] = mapped_column(BigInteger, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    # Per-item status shown in the UI, e.g. one entry per VM.
    items: Mapped[list] = mapped_column(JSON, default=list)

    job: Mapped[Job | None] = relationship()


class TaskLog(Base):
    __tablename__ = "task_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    at: Mapped[datetime] = mapped_column(default=utcnow)
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text)

    __table_args__ = (Index("ix_task_logs_task_id_id", "task_id", "id"),)


class RestorePoint(Base):
    """Mirror of a restore point stored in a repository."""

    __tablename__ = "restore_points"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # What was backed up: a VM, an OpenShift namespace or an etcd backup set.
    # vm_uuid/vm_name hold that subject's stable id and display name.
    subject_kind: Mapped[str] = mapped_column(String(16), default="vm", server_default="vm")
    vm_uuid: Mapped[str] = mapped_column(String(64), index=True)
    vm_name: Mapped[str] = mapped_column(String(255))
    vm_moref: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(index=True)
    kind: Mapped[str] = mapped_column(String(16))  # full | incremental
    logical_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    read_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    new_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    disks: Mapped[list] = mapped_column(JSON, default=list)

    repository: Mapped[Repository] = relationship()
