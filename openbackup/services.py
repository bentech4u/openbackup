"""Glue between database rows and the engines: opening repositories (and
mounting their NFS shares) and connecting to vCenters."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from croniter import croniter

from .auth.secrets import decrypt
from .config import get_settings
from .db.models import RepoKind, VCenter
from .db.models import Repository as RepoRow
from .engine.source import VSphereSource
from .repo import nfs
from .repo.repository import Repository


class ServiceError(Exception):
    pass


def repo_location(row: RepoRow) -> Path:
    """Where the repository lives on this host, mounting the NFS export if
    needed. NFS mounts are left in place between jobs."""
    if row.kind == RepoKind.local:
        return Path(row.path)
    mountpoint = get_settings().mount_root / f"repo-{row.id}"
    try:
        nfs.ensure_mounted(row.nfs_server, row.nfs_export, mountpoint, row.nfs_options)
    except nfs.NfsError as e:
        raise ServiceError(str(e)) from None
    sub = row.path.strip("/")
    return mountpoint / sub if sub else mountpoint


def repo_passphrase(row: RepoRow) -> str | None:
    return decrypt(row.passphrase_enc) if row.encrypted and row.passphrase_enc else None


def index_dir() -> Path:
    return get_settings().data_dir / "index"


@contextmanager
def open_repository(row: RepoRow) -> Iterator[Repository]:
    path = repo_location(row)
    repo = Repository.open(path, index_dir(), repo_passphrase(row))
    try:
        yield repo
    finally:
        repo.close()


def vsphere_source(vc: VCenter) -> VSphereSource:
    s = get_settings()
    return VSphereSource(vc.host, vc.username, decrypt(vc.password_enc), vc.thumbprint,
                         port=vc.port, vddk_libdir=s.vddk_libdir, nbdkit=s.nbdkit)


def next_run(cron: str, after: datetime | None = None) -> datetime:
    """Next time a cron expression fires, evaluated in the server's local
    time zone, returned in UTC."""
    base = (after or datetime.now(UTC)).astimezone()
    return croniter(cron, base).get_next(datetime).astimezone(UTC)


def valid_cron(cron: str) -> bool:
    return croniter.is_valid(cron)
