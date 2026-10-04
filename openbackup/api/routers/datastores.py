"""Direct NFS access to datastores: the read-only path backups use to read
VM disks straight from the NAS, keeping vCenter and ESXi out of the data
path."""

from __future__ import annotations

import os
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import get_settings
from ...db.models import DatastoreAccess, VCenter
from ...repo import nfs
from ..deps import Principal, admin, audit, get_db, viewer
from .inventory import _fetch, _vc

router = APIRouter(prefix="/api/vcenters/{vc_id}/datastores", tags=["datastores"])


class AccessIn(BaseModel):
    nfs_server: str = Field(min_length=1, max_length=255)
    nfs_export: str = Field(min_length=1, max_length=1024)
    nfs_options: str = Field("nfsvers=4,hard", max_length=255)
    enabled: bool = True


def _validate(body: AccessIn) -> None:
    try:
        nfs.validate(body.nfs_server, body.nfs_export, body.nfs_options)
    except nfs.NfsError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None
    if "rw" in body.nfs_options.split(","):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Datastores are always mounted read-only")


def _test(body: AccessIn) -> dict:
    mp = get_settings().mount_root / f"test-ds-{secrets.token_hex(4)}"
    try:
        nfs.ensure_mounted(body.nfs_server, body.nfs_export, mp, body.nfs_options, read_only=True)
    except nfs.NfsError as e:
        return {"ok": False, "message": str(e)}
    try:
        info = nfs.find_mount(mp)
        entries = sorted(e.name for e in os.scandir(mp) if not e.name.startswith("."))
        vers = info.nfs_version if info else ""
        msg = f"Mounted read-only; {len(entries)} folders visible."
        return {"ok": True, "message": msg, "read_only": bool(info and info.read_only),
                "folders": entries[:50], "nfs_version": vers}
    except OSError as e:
        return {"ok": False, "message": f"Mounted, but the export cannot be listed: {e}"}
    finally:
        try:
            nfs.unmount(mp)
            mp.rmdir()
        except (nfs.NfsError, OSError):
            pass


@router.get("")
async def list_datastores(vc_id: int, refresh: bool = False, db: Session = Depends(get_db),
                          _: Principal = Depends(viewer)) -> list[dict]:
    vc = _vc(db, vc_id)
    live = await run_in_threadpool(_fetch, vc, "datastores", refresh)
    access = {a.datastore: a for a in db.scalars(
        select(DatastoreAccess).where(DatastoreAccess.vcenter_id == vc_id))}
    out = []
    for d in live:
        a = access.get(d["name"])
        out.append({**d, "direct_nfs": None if a is None else {
            "nfs_server": a.nfs_server, "nfs_export": a.nfs_export,
            "nfs_options": a.nfs_options, "enabled": a.enabled}})
    return out


@router.post("/{name}/direct-nfs/test")
async def test_access(vc_id: int, name: str, body: AccessIn, db: Session = Depends(get_db),
                      _: Principal = Depends(admin)) -> dict:
    _vc(db, vc_id)
    _validate(body)
    return await run_in_threadpool(_test, body)


@router.put("/{name}/direct-nfs")
async def set_access(vc_id: int, name: str, body: AccessIn, request: Request,
                     db: Session = Depends(get_db), p: Principal = Depends(admin)) -> dict:
    if db.get(VCenter, vc_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "vCenter not found")
    _validate(body)
    if body.enabled:
        res = await run_in_threadpool(_test, body)
        if not res["ok"]:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, res["message"])
    row = db.scalar(select(DatastoreAccess).where(DatastoreAccess.vcenter_id == vc_id,
                                                  DatastoreAccess.datastore == name))
    if row is None:
        row = DatastoreAccess(vcenter_id=vc_id, datastore=name)
        db.add(row)
    row.nfs_server, row.nfs_export = body.nfs_server, body.nfs_export
    row.nfs_options, row.enabled = body.nfs_options, body.enabled
    audit(db, request, "datastore.direct_nfs", principal=p, target=name,
          detail={"server": body.nfs_server, "export": body.nfs_export,
                  "enabled": body.enabled})
    return {"ok": True}


@router.delete("/{name}/direct-nfs", status_code=204)
def remove_access(vc_id: int, name: str, request: Request, db: Session = Depends(get_db),
                  p: Principal = Depends(admin)) -> None:
    row = db.scalar(select(DatastoreAccess).where(DatastoreAccess.vcenter_id == vc_id,
                                                  DatastoreAccess.datastore == name))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No direct NFS access configured")
    audit(db, request, "datastore.direct_nfs.remove", principal=p, target=name)
    db.delete(row)
