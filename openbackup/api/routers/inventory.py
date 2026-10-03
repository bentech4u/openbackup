"""Live vCenter inventory for the job and restore wizards. Results are
cached briefly: listing VMs on a large vCenter takes seconds."""

from __future__ import annotations

import threading
import time
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from ...auth.secrets import decrypt
from ...db.models import VCenter
from ...vsphere.client import VSphereError
from ..deps import Principal, get_db, operator, viewer
from . import vcenters

router = APIRouter(prefix="/api/vcenters/{vc_id}", tags=["inventory"])

_cache: dict[tuple[int, str], tuple[float, object]] = {}
_lock = threading.Lock()
TTL = 60


def _fetch(vc: VCenter, what: str, refresh: bool):
    key = (vc.id, what)
    with _lock:
        hit = _cache.get(key)
    if hit and not refresh and time.monotonic() - hit[0] < TTL:
        return hit[1]
    try:
        vs = vcenters._connect(vc.host, vc.port, vc.username, decrypt(vc.password_enc),
                               vc.thumbprint)
    except VSphereError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from None
    try:
        if what == "vms":
            data = [v.to_dict() for v in vs.list_vms()]
        elif what == "placement":
            data = asdict(vs.placement_options())
        else:
            data = vs.about()
    except VSphereError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from None
    finally:
        vs.close()
    with _lock:
        _cache[key] = (time.monotonic(), data)
    return data


def _vc(db: Session, vc_id: int) -> VCenter:
    vc = db.get(VCenter, vc_id)
    if vc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "vCenter not found")
    db.expunge(vc)
    return vc


@router.get("/vms")
async def list_vms(vc_id: int, refresh: bool = False, db: Session = Depends(get_db),
                   _: Principal = Depends(viewer)):
    return await run_in_threadpool(_fetch, _vc(db, vc_id), "vms", refresh)


@router.get("/placement")
async def placement(vc_id: int, refresh: bool = False, db: Session = Depends(get_db),
                    _: Principal = Depends(operator)):
    return await run_in_threadpool(_fetch, _vc(db, vc_id), "placement", refresh)


@router.get("/about")
async def about(vc_id: int, db: Session = Depends(get_db), _: Principal = Depends(viewer)):
    return await run_in_threadpool(_fetch, _vc(db, vc_id), "about", True)
