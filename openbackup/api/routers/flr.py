"""File-level restore: browse a restore point's filesystems and download
files. Restoring into a VM goes through a task (see points.restore)."""

from __future__ import annotations

import os
import posixpath
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from ...auth.secrets import decrypt
from ...config import get_settings
from ...db.models import Repository as RepoRow
from ...db.models import RestorePoint, Role, VCenter
from ...flr.session import FlrError, FlrSession, registry
from ...vsphere.client import VSphereError
from ...vsphere.guestops import GuestError
from ..deps import Principal, audit, get_db, operator
from . import vcenters

router = APIRouter(tags=["file-level restore"])

PATH_RE = re.compile(r"^/v\d+(/[^\x00]*)?$")
MAX_ZIP_BYTES = 20 << 30


def check_browse_path(path: str) -> str:
    if not PATH_RE.match(path) or ".." in path.split("/"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid path")
    return path


def _session(sid: str, p: Principal) -> FlrSession:
    s = registry.get(sid)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "Browse session has ended; open the restore point again")
    if s.owner != p.user.username and p.role != Role.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your browse session")
    return s


def _flr_call(fn, *args):
    try:
        return fn(*args)
    except FlrError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None


@router.post("/api/points/{point_id}/browse")
async def open_browse(point_id: str, request: Request, db: Session = Depends(get_db),
                      p: Principal = Depends(operator)) -> dict:
    point = db.get(RestorePoint, point_id)
    if point is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Restore point not found")
    row = db.get(RepoRow, point.repository_id)
    db.expunge(row)
    s = FlrSession(row, point_id, owner=p.user.username)
    try:
        await run_in_threadpool(s.open)
    except (FlrError, OSError) as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"Could not open the restore point: {e}") from None
    registry.add(s)
    audit(db, request, "point.browse", principal=p, target=f"{point.vm_name} {point_id}")
    return {"session_id": s.id, "volumes": s.volumes, "os": s.os}


@router.get("/api/flr/{sid}/ls")
async def ls(sid: str, path: str = Query(max_length=4096),
             p: Principal = Depends(operator)) -> list[dict]:
    s = _session(sid, p)
    return await run_in_threadpool(_flr_call, s.ls, check_browse_path(path))


@router.delete("/api/flr/{sid}", status_code=204)
def close(sid: str, p: Principal = Depends(operator)) -> None:
    _session(sid, p)
    registry.remove(sid)


def _prepare_download(s: FlrSession, path: str) -> tuple[Path, str, Path]:
    tmpdir = Path(tempfile.mkdtemp(prefix="openbackup-dl-", dir=_staging_root()))
    try:
        st = s.stat(path)
        name = posixpath.basename(path) or "volume"
        if st["type"] == "file":
            dest = tmpdir / "file"
            s.download(path, dest)
            return dest, name, tmpdir
        if st["type"] != "dir":
            raise FlrError(f"{name} is a {st['type']}, not a file or folder")
        tree = s.walk(path)
        if sum(e["size"] for e in tree if e["type"] == "file") > MAX_ZIP_BYTES:
            raise FlrError("Folder is too large to download as a zip; restore it to a VM")
        zpath = tmpdir / "folder.zip"
        part = tmpdir / "part"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            for e in tree:
                arc = name + e["path"]
                if e["type"] == "dir":
                    z.writestr(arc + "/", b"")
                elif e["type"] == "file":
                    s.download(path + e["path"], part)
                    z.write(part, arc)
                    part.unlink()
        return zpath, name + ".zip", tmpdir
    except BaseException:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise


def _staging_root() -> Path:
    root = get_settings().data_dir / "flr-staging"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


@router.get("/api/flr/{sid}/download")
async def download(sid: str, request: Request, path: str = Query(max_length=4096),
                   db: Session = Depends(get_db), p: Principal = Depends(operator)):
    s = _session(sid, p)
    check_browse_path(path)
    file, name, tmpdir = await run_in_threadpool(_flr_call, _prepare_download, s, path)
    audit(db, request, "point.download", principal=p, target=f"{s.point_id}:{path}",
          detail={"bytes": file.stat().st_size})
    return FileResponse(file, filename=name, media_type="application/octet-stream",
                        background=BackgroundTask(shutil.rmtree, tmpdir, True))


class GuestCheckIn(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024)


def _guest_check(vc: VCenter, moref: str, body: GuestCheckIn) -> dict:
    from ...vsphere.guestops import GuestFiles

    try:
        vs = vcenters._connect(vc.host, vc.port, vc.username, decrypt(vc.password_enc),
                               vc.thumbprint)
    except VSphereError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from None
    try:
        info = GuestFiles(vs, moref, body.username, body.password).check()
    except (GuestError, VSphereError) as e:
        return {"ok": False, "message": str(e)}
    finally:
        vs.close()
    return {"ok": True, "family": info.family, "os_name": info.os_name,
            "hostname": info.hostname, "windows": info.windows}


@router.post("/api/vcenters/{vc_id}/vms/{moref}/guest-check")
async def guest_check(vc_id: int, moref: str, body: GuestCheckIn, db: Session = Depends(get_db),
                      _: Principal = Depends(operator)) -> dict:
    """Check that VMware Tools is running and the guest credentials work.
    Read-only: nothing in vCenter or the guest changes."""
    if not re.fullmatch(r"vm-\d+", moref):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid VM")
    vc = db.get(VCenter, vc_id)
    if vc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "vCenter not found")
    db.expunge(vc)
    return await run_in_threadpool(_guest_check, vc, moref, body)
