from __future__ import annotations

import secrets
import shutil
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import services
from ...auth.passwords import MIN_LENGTH
from ...auth.secrets import encrypt
from ...config import get_settings
from ...db.models import DatastoreAccess, Job, RepoKind, Task, TaskKind, TaskState
from ...db.models import Repository as RepoRow
from ...repo import nfs
from ...repo.crypto import WrongPassphrase
from ...repo.repository import Repository, RepositoryError
from ..deps import Principal, admin, audit, get_db, operator, viewer
from ..schemas import RepositoryOut, TaskOut

router = APIRouter(prefix="/api/repositories", tags=["repositories"])

FORBIDDEN_LOCAL = {"/", "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot", "/proc",
                   "/sys", "/dev", "/root", "/var", "/var/lib", "/home", "/opt", "/tmp"}


class RepoIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    kind: RepoKind
    path: str = Field("", max_length=1024)
    nfs_server: str = Field("", max_length=255)
    nfs_export: str = Field("", max_length=1024)
    nfs_options: str = Field(nfs.DEFAULT_OPTIONS, max_length=255)
    passphrase: str | None = Field(None, max_length=1024)
    # create: initialise a new repository; import: attach an existing one
    mode: Literal["create", "import"] = "create"


class RepoTestIn(BaseModel):
    kind: RepoKind
    path: str = ""
    nfs_server: str = ""
    nfs_export: str = ""
    nfs_options: str = nfs.DEFAULT_OPTIONS


class RepoUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    nfs_options: str | None = Field(None, max_length=255)


def _validate(body: RepoIn | RepoTestIn) -> None:
    if body.kind == RepoKind.local:
        p = Path(body.path)
        if not p.is_absolute() or ".." in p.parts:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Path must be absolute")
        if str(p).rstrip("/") in FORBIDDEN_LOCAL or str(p) == "/":
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "Choose a dedicated directory for backups")
    else:
        try:
            nfs.validate(body.nfs_server, body.nfs_export, body.nfs_options)
        except nfs.NfsError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None
        if ".." in Path(body.path).parts:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid subdirectory")


def _row_for(body) -> RepoRow:
    return RepoRow(id=0, name="", kind=body.kind, path=body.path,
                   nfs_server=body.nfs_server, nfs_export=body.nfs_export,
                   nfs_options=body.nfs_options)


def _location(row: RepoRow) -> Path:
    try:
        return services.repo_location(row)
    except services.ServiceError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None


def _check(row: RepoRow) -> dict:
    loc = _location(row)
    res = nfs.check_path(loc)
    return {"ok": res.ok, "message": res.message, "capacity_bytes": res.capacity_bytes,
            "free_bytes": res.free_bytes, "write_mib_s": res.write_mib_s,
            "existing_repository": Repository.exists(loc)}


@router.get("", response_model=list[RepositoryOut])
def list_repos(db: Session = Depends(get_db), _: Principal = Depends(viewer)):
    exports = {(a.nfs_server, a.nfs_export.rstrip("/")): a.datastore
               for a in db.scalars(select(DatastoreAccess))}
    out = []
    for r in db.scalars(select(RepoRow).order_by(RepoRow.name)):
        o = RepositoryOut.model_validate(r)
        if r.kind == RepoKind.nfs:
            o.shares_datastore = exports.get((r.nfs_server, r.nfs_export.rstrip("/")))
        out.append(o)
    return out


def _test(body: RepoTestIn) -> dict:
    if body.kind == RepoKind.local:
        return _check(_row_for(body))
    # A scratch mountpoint, unmounted again afterwards.
    mp = get_settings().mount_root / f"test-{secrets.token_hex(4)}"
    try:
        mounted = nfs.ensure_mounted(body.nfs_server, body.nfs_export, mp, body.nfs_options)
    except nfs.NfsError as e:
        return {"ok": False, "message": str(e)}
    try:
        loc = mp / body.path.strip("/") if body.path.strip("/") else mp
        res = nfs.check_path(loc)
        return {"ok": res.ok, "message": res.message, "capacity_bytes": res.capacity_bytes,
                "free_bytes": res.free_bytes, "write_mib_s": res.write_mib_s,
                "existing_repository": Repository.exists(loc)}
    finally:
        if mounted:
            try:
                nfs.unmount(mp)
                mp.rmdir()
            except (nfs.NfsError, OSError):
                pass


@router.post("/test")
async def test_location(body: RepoTestIn, _: Principal = Depends(admin)) -> dict:
    _validate(body)
    return await run_in_threadpool(_test, body)


def _create(row: RepoRow, body: RepoIn) -> None:
    loc = _location(row)
    if body.mode == "create":
        try:
            Repository.create(loc, services.index_dir(), body.passphrase or None).close()
        except RepositoryError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None
    else:
        try:
            Repository.open(loc, services.index_dir(), body.passphrase or None).close()
        except WrongPassphrase:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Wrong passphrase") from None
        except RepositoryError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None
    st = shutil.disk_usage(loc)
    row.capacity_bytes, row.free_bytes = st.total, st.free


@router.post("", response_model=RepositoryOut, status_code=201)
async def create_repo(body: RepoIn, request: Request, db: Session = Depends(get_db),
                      p: Principal = Depends(admin)):
    _validate(body)
    if body.passphrase is not None and body.passphrase and len(body.passphrase) < MIN_LENGTH:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"Passphrase must be at least {MIN_LENGTH} characters")
    if db.scalar(select(RepoRow).where(RepoRow.name == body.name)):
        raise HTTPException(status.HTTP_409_CONFLICT, "A repository with that name exists")
    row = RepoRow(name=body.name, kind=body.kind, path=body.path, nfs_server=body.nfs_server,
                  nfs_export=body.nfs_export, nfs_options=body.nfs_options,
                  encrypted=bool(body.passphrase),
                  passphrase_enc=encrypt(body.passphrase) if body.passphrase else None)
    db.add(row)
    db.flush()  # assigns the id used for the mountpoint
    await run_in_threadpool(_create, row, body)
    audit(db, request, f"repository.{body.mode}", principal=p, target=row.name,
          detail={"kind": row.kind.value, "encrypted": row.encrypted})
    if body.mode == "import":
        # Pull the existing restore points into the catalogue.
        db.add(Task(kind=TaskKind.gc, title=f"Rescan repository {row.name}",
                    repository_id=row.id, requested_by=p.user.username,
                    params={"rescan_only": True}))
    return row


@router.patch("/{repo_id}", response_model=RepositoryOut)
def update_repo(repo_id: int, body: RepoUpdate, request: Request,
                db: Session = Depends(get_db), p: Principal = Depends(admin)):
    row = db.get(RepoRow, repo_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Repository not found")
    if body.nfs_options is not None:
        try:
            nfs.validate(row.nfs_server or "x", row.nfs_export or "/", body.nfs_options)
        except nfs.NfsError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None
        row.nfs_options = body.nfs_options
    if body.name:
        row.name = body.name
    audit(db, request, "repository.update", principal=p, target=row.name)
    return row


@router.delete("/{repo_id}", status_code=204)
def delete_repo(repo_id: int, request: Request, db: Session = Depends(get_db),
                p: Principal = Depends(admin)):
    """Detach the repository. Backup data on the share is left untouched and
    can be imported again."""
    row = db.get(RepoRow, repo_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Repository not found")
    if db.scalar(select(Job.id).where(Job.repository_id == repo_id)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Repository is used by backup jobs")
    if db.scalar(select(Task.id).where(Task.repository_id == repo_id,
                                       Task.state.in_([TaskState.queued, TaskState.running]))):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Repository has active tasks")
    audit(db, request, "repository.detach", principal=p, target=row.name)
    db.delete(row)


def _stats(row: RepoRow) -> dict:
    with services.open_repository(row) as repo:
        s = repo.stats()
        st = shutil.disk_usage(repo.root)
    return {"packs": s.packs, "chunks": s.chunks, "stored_bytes": s.stored_bytes,
            "points": s.points, "capacity_bytes": st.total, "free_bytes": st.free}


@router.get("/{repo_id}/stats")
async def repo_stats(repo_id: int, db: Session = Depends(get_db),
                     _: Principal = Depends(viewer)) -> dict:
    row = db.get(RepoRow, repo_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Repository not found")
    db.expunge(row)
    try:
        out = await run_in_threadpool(_stats, row)
    except (services.ServiceError, RepositoryError, OSError) as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Repository unavailable: {e}") \
            from None
    live = db.get(RepoRow, repo_id)
    live.capacity_bytes, live.free_bytes = out["capacity_bytes"], out["free_bytes"]
    return out


def _queue(db: Session, row: RepoRow, kind: TaskKind, title: str, user: str,
           params: dict | None = None) -> Task:
    t = Task(kind=kind, title=title, repository_id=row.id, requested_by=user,
             params=params or {})
    db.add(t)
    db.flush()
    return t


@router.post("/{repo_id}/verify", response_model=TaskOut, status_code=202)
def verify_repo(repo_id: int, request: Request, db: Session = Depends(get_db),
                p: Principal = Depends(operator)):
    row = db.get(RepoRow, repo_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Repository not found")
    audit(db, request, "repository.verify", principal=p, target=row.name)
    return _queue(db, row, TaskKind.verify, f"Verify repository {row.name}", p.user.username)


@router.post("/{repo_id}/maintenance", response_model=TaskOut, status_code=202)
def maintain_repo(repo_id: int, request: Request, db: Session = Depends(get_db),
                  p: Principal = Depends(operator)):
    row = db.get(RepoRow, repo_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Repository not found")
    audit(db, request, "repository.maintenance", principal=p, target=row.name)
    return _queue(db, row, TaskKind.gc, f"Rescan and clean up {row.name}", p.user.username)
