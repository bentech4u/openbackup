from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ... import services
from ...auth.secrets import encrypt
from ...db.models import KubeCluster, RestorePoint, Role, Task, TaskKind, VCenter
from ...db.models import Repository as RepoRow
from ...repo.repository import RepositoryError
from ..deps import Principal, admin, audit, get_db, operator, viewer
from ..schemas import RestorePointOut, TaskOut
from .flr import check_browse_path

router = APIRouter(prefix="/api/points", tags=["restore points"])


@router.get("", response_model=list[RestorePointOut])
def list_points(
    vm_uuid: str | None = None,
    repository_id: int | None = None,
    job_id: int | None = None,
    q: str | None = Query(None, max_length=255),
    limit: int = Query(200, ge=1, le=2000),
    db: Session = Depends(get_db),
    _: Principal = Depends(viewer),
):
    stmt = select(RestorePoint).order_by(RestorePoint.created_at.desc()).limit(limit)
    if vm_uuid:
        stmt = stmt.where(RestorePoint.vm_uuid == vm_uuid)
    if repository_id:
        stmt = stmt.where(RestorePoint.repository_id == repository_id)
    if job_id:
        stmt = stmt.where(RestorePoint.job_id == job_id)
    if q:
        stmt = stmt.where(RestorePoint.vm_name.ilike(f"%{q}%"))
    return list(db.scalars(stmt))


@router.get("/vms")
def protected_vms(db: Session = Depends(get_db), _: Principal = Depends(viewer)) -> list[dict]:
    """One row per backed-up VM with its point count and latest point."""
    rows = db.execute(
        select(RestorePoint.vm_uuid, func.max(RestorePoint.vm_name), func.count(),
               func.max(RestorePoint.created_at), func.sum(RestorePoint.new_bytes),
               func.max(RestorePoint.subject_kind))
        .group_by(RestorePoint.vm_uuid)
        .order_by(func.max(RestorePoint.vm_name))).all()
    return [{"vm_uuid": u, "vm_name": n, "points": c, "latest": latest, "stored_bytes": s,
             "subject_kind": k}
            for u, n, c, latest, s, k in rows]


def _point(db: Session, point_id: str) -> RestorePoint:
    p = db.get(RestorePoint, point_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Restore point not found")
    return p


def _manifest(row: RepoRow, point_id: str) -> dict:
    with services.open_repository(row) as repo:
        return repo.load_manifest(point_id)


@router.get("/{point_id}")
async def get_point(point_id: str, db: Session = Depends(get_db),
                    _: Principal = Depends(viewer)) -> dict:
    p = _point(db, point_id)
    row = db.get(RepoRow, p.repository_id)
    db.expunge(row)
    try:
        m = await run_in_threadpool(_manifest, row, point_id)
    except (services.ServiceError, RepositoryError, OSError) as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Repository unavailable: {e}") \
            from None
    out = RestorePointOut.model_validate(p).model_dump(mode="json")
    out.update(config=m.get("config", {}), vcenter=m.get("vcenter", ""),
               warnings=m.get("warnings", []), duration_s=m.get("duration_s"),
               disk_details=m.get("disks", []))
    if p.subject_kind == "etcd":
        out.update(files=m.get("files", []), source_set=m.get("source_set", ""),
                   collected_at=m.get("collected_at"))
    if p.subject_kind == "namespace":
        out.update(secrets_included=bool(m.get("secrets_included")),
                   cluster=m.get("cluster", {}), namespace=m.get("namespace", ""),
                   resources=m.get("resources", {}), pvcs=m.get("pvcs", []),
                   skipped_types=m.get("skipped_types", []))
    return out


@router.get("/{point_id}/files/{key}")
async def download_file(point_id: str, key: str, request: Request, db: Session = Depends(get_db),
                        pr: Principal = Depends(operator)):
    """A file stored in a point (etcd snapshots and the like), streamed from
    the repository and verified block by block."""
    p = _point(db, point_id)
    if p.subject_kind != "etcd":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This point holds disks, not files")
    row = db.get(RepoRow, p.repository_id)
    db.expunge(row)
    try:
        manifest = await run_in_threadpool(_manifest, row, point_id)
    except (services.ServiceError, RepositoryError, OSError) as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Repository unavailable: {e}") \
            from None
    rec = next((f for f in manifest.get("files", []) if f["key"] == key), None)
    if rec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such file in this point")
    audit(db, request, "point.download", principal=pr, target=f"{point_id}:{rec['name']}",
          detail={"bytes": rec["capacity"]})
    db.commit()

    def chunks():
        with services.open_repository(row) as repo:
            m = repo.load_map(point_id, key)
            for i, cid in enumerate(m.ids):
                yield repo.read_chunk(cid, m.block_length(i))

    return StreamingResponse(chunks(), media_type="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="{rec["name"]}"',
        "Content-Length": str(rec["capacity"])})


@router.post("/{point_id}/verify", response_model=TaskOut, status_code=202)
def verify(point_id: str, request: Request, db: Session = Depends(get_db),
           pr: Principal = Depends(operator)):
    p = _point(db, point_id)
    t = Task(kind=TaskKind.verify, title=f"Verify {p.vm_name} {point_id}",
             repository_id=p.repository_id, requested_by=pr.user.username,
             params={"point_id": point_id})
    db.add(t)
    db.flush()
    audit(db, request, "point.verify", principal=pr, target=point_id)
    return t


class BulkDeleteIn(BaseModel):
    """Either explicit points, or every point of one VM / namespace / etcd set."""

    point_ids: list[str] = Field(default_factory=list, max_length=5000)
    vm_uuid: str | None = Field(None, max_length=255)


@router.post("/delete", response_model=list[TaskOut], status_code=202)
def delete_points(body: BulkDeleteIn, request: Request, db: Session = Depends(get_db),
                  pr: Principal = Depends(admin)):
    """Delete many restore points. Jobs are left alone; their next run starts
    a new chain with a full backup if nothing is left to build on."""
    q = select(RestorePoint)
    if body.vm_uuid:
        q = q.where(RestorePoint.vm_uuid == body.vm_uuid)
    elif body.point_ids:
        q = q.where(RestorePoint.id.in_(body.point_ids))
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose restore points to delete")
    points = list(db.scalars(q))
    if not points:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No matching restore points")
    if body.point_ids and not body.vm_uuid and len(points) != len(set(body.point_ids)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Some restore points no longer exist")
    by_repo: dict[int, list[RestorePoint]] = {}
    for p in points:
        by_repo.setdefault(p.repository_id, []).append(p)
    tasks = []
    for repo_id, pts in by_repo.items():
        names = sorted({p.vm_name for p in pts})
        t = Task(kind=TaskKind.gc, repository_id=repo_id, requested_by=pr.user.username,
                 title=f"Delete {len(pts)} restore point(s) of {', '.join(names)[:150]}",
                 params={"delete_points": [p.id for p in pts]})
        db.add(t)
        tasks.append(t)
    db.flush()
    audit(db, request, "point.delete", principal=pr,
          target=", ".join(sorted({p.vm_name for p in points}))[:255],
          detail={"points": len(points), "all_of_subject": bool(body.vm_uuid)})
    return tasks


@router.delete("/{point_id}", response_model=TaskOut, status_code=202)
def delete_point(point_id: str, request: Request, db: Session = Depends(get_db),
                 pr: Principal = Depends(admin)):
    p = _point(db, point_id)
    t = Task(kind=TaskKind.gc, title=f"Delete {p.vm_name} {point_id}",
             repository_id=p.repository_id, requested_by=pr.user.username,
             params={"delete_points": [point_id]})
    db.add(t)
    db.flush()
    audit(db, request, "point.delete", principal=pr, target=f"{p.vm_name} {point_id}")
    return t


class NewVmIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    folder: str
    resource_pool: str
    datastore: str = Field(min_length=1, max_length=255)
    host: str | None = None
    network_map: dict[str, str] = {}
    power_on: bool = False


class FilesIn(BaseModel):
    """Restore selected files/folders into a running VM through VMware Tools."""

    items: list[str] = Field(min_length=1, max_length=1000)
    vm_moref: str = Field(pattern=r"^vm-\d+$")
    guest_user: str = Field(min_length=1, max_length=255)
    guest_password: str = Field(min_length=1, max_length=1024)
    conflict: Literal["overwrite", "rename", "skip"] = "rename"
    target_dir: str | None = Field(None, max_length=1024)


class NamespaceIn(BaseModel):
    """Restore an OpenShift namespace point into a cluster."""

    cluster_id: int
    target_namespace: str = Field(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63)
    storage_class_map: dict[str, str] = {}
    route_host_map: dict[str, str] = {}
    keep_uid_range: bool = True
    merge: bool = False
    include_data: bool = True
    restore_secrets: bool = True
    # Used for this restore only, when the cluster has no stored restore token.
    restore_token: str | None = Field(None, max_length=16384)


class RestoreIn(BaseModel):
    mode: Literal["new_vm", "in_place", "export", "files", "namespace"]
    vcenter_id: int | None = None
    target: NewVmIn | None = None
    format: Literal["raw", "vmdk", "qcow2"] = "raw"
    power_on: bool = False
    files: FilesIn | None = None
    namespace: NamespaceIn | None = None


@router.post("/{point_id}/restore", response_model=TaskOut, status_code=202)
def restore(point_id: str, body: RestoreIn, request: Request, db: Session = Depends(get_db),
            pr: Principal = Depends(operator)):
    p = _point(db, point_id)
    params: dict = {"point_id": point_id, "mode": body.mode}
    if body.mode == "in_place" and pr.role.rank < Role.admin.rank:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Overwriting a production VM requires the admin role")
    if body.mode == "files":
        f = body.files
        if f is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose files and a target VM")
        if f.conflict == "overwrite" and pr.role.rank < Role.admin.rank:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "Overwriting files in a running VM requires the admin role; "
                                "use rename instead")
        for item in f.items:
            check_browse_path(item)
        if f.target_dir is not None and ("\x00" in f.target_dir or not f.target_dir.strip()):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid target folder")
    if body.mode == "namespace":
        n = body.namespace
        if n is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose the target cluster")
        if p.subject_kind != "namespace":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Not a namespace backup")
        if n.merge and pr.role.rank < Role.admin.rank:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "Restoring into an existing namespace requires the admin role")
        cluster = db.get(KubeCluster, n.cluster_id)
        if cluster is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown cluster")
        if not (n.restore_token or cluster.restore_token_enc):
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "This cluster has no stored restore token; paste one")
        opts = n.model_dump(exclude={"restore_token", "cluster_id"})
        params.update(target_cluster_id=n.cluster_id, options=opts)
        if n.restore_token:
            params["restore_token_enc"] = encrypt(n.restore_token)
        title = f"Restore {p.vm_name} into {cluster.name}/{n.target_namespace}"
    if body.mode in ("new_vm", "in_place", "files"):
        if body.vcenter_id is None or db.get(VCenter, body.vcenter_id) is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose the target vCenter")
        params["vcenter_id"] = body.vcenter_id
    if body.mode == "namespace":
        pass  # parameters set above
    elif body.mode == "files":
        f = body.files
        params.update(items=f.items, vm_moref=f.vm_moref, guest_user=f.guest_user,
                      guest_password_enc=encrypt(f.guest_password), conflict=f.conflict,
                      target_dir=(f.target_dir or "").strip() or None)
        n = len(f.items)
        title = (f"Restore {n} item{'s' if n > 1 else ''} from {p.vm_name} into "
                 f"{f.vm_moref} ({f.conflict})")
    elif body.mode == "new_vm":
        if body.target is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Target placement is required")
        params["target"] = body.target.model_dump()
        title = f"Restore {p.vm_name} as {body.target.name}"
    elif body.mode == "in_place":
        params["power_on"] = body.power_on
        title = f"Restore {p.vm_name} over the original VM"
    else:
        row = db.get(RepoRow, p.repository_id)
        db.expunge(row)
        try:
            loc = services.repo_location(row)
        except services.ServiceError as e:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from None
        # Exports always land inside the repository, never at a caller-chosen path.
        params["path"] = str(loc / "exports" / point_id)
        params["format"] = body.format
        title = f"Export disks of {p.vm_name} ({body.format})"
    t = Task(kind=TaskKind.restore, title=title, repository_id=p.repository_id,
             requested_by=pr.user.username, params=params)
    db.add(t)
    db.flush()
    audit(db, request, f"point.restore.{body.mode}", principal=pr,
          target=f"{p.vm_name} {point_id}", detail={k: v for k, v in params.items()
                                                     if k not in ("point_id",
                                                                  "guest_password_enc",
                                                                  "restore_token_enc")})
    return t
