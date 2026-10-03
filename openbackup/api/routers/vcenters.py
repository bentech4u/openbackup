from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...auth.secrets import encrypt
from ...db.models import Job, VCenter
from ...vsphere.client import VSphereError, fetch_thumbprints
from ..deps import Principal, admin, audit, get_db, viewer
from ..schemas import VCenterOut

router = APIRouter(prefix="/api/vcenters", tags=["vcenters"])

HOST_PATTERN = r"^[A-Za-z0-9.\-:\[\]]{1,255}$"
THUMB_RE = re.compile(r"^([0-9A-F]{2}:){19}[0-9A-F]{2}$")

# Tests replace this with a fake.
connector = None


def _connect(host: str, port: int, user: str, password: str, thumbprint: str):
    if connector is not None:
        return connector(host, port, user, password, thumbprint)
    from ...vsphere.client import VSphere

    return VSphere(host, user, password, thumbprint, port=port, timeout=30).connect()


class ThumbprintIn(BaseModel):
    host: str = Field(pattern=HOST_PATTERN)
    port: int = Field(443, ge=1, le=65535)


class VCenterIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    host: str = Field(pattern=HOST_PATTERN)
    port: int = Field(443, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024)
    thumbprint: str = Field(description="SHA-1 thumbprint the administrator confirmed")


class VCenterUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    username: str | None = Field(None, min_length=1, max_length=255)
    password: str | None = Field(None, min_length=1, max_length=1024)
    thumbprint: str | None = None


def _check_thumb(t: str) -> str:
    t = t.strip().upper()
    if not THUMB_RE.match(t):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Thumbprint must be a SHA-1 fingerprint like AB:CD:...")
    return t


def _test_login(host, port, user, password, thumb) -> dict:
    try:
        vs = _connect(host, port, user, password, thumb)
    except VSphereError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None
    try:
        return vs.about()
    finally:
        vs.close()


@router.post("/thumbprint")
async def get_thumbprint(body: ThumbprintIn, _: Principal = Depends(admin)) -> dict:
    """Fetch the certificate fingerprint for the admin to confirm (trust on
    first use, made explicit)."""
    try:
        return await run_in_threadpool(fetch_thumbprints, body.host, body.port)
    except VSphereError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None


@router.get("", response_model=list[VCenterOut])
def list_vcenters(db: Session = Depends(get_db), _: Principal = Depends(viewer)):
    return list(db.scalars(select(VCenter).order_by(VCenter.name)))


@router.post("", response_model=VCenterOut, status_code=201)
async def add_vcenter(body: VCenterIn, request: Request, db: Session = Depends(get_db),
                      p: Principal = Depends(admin)):
    thumb = _check_thumb(body.thumbprint)
    if db.scalar(select(VCenter).where(VCenter.name == body.name)):
        raise HTTPException(status.HTTP_409_CONFLICT, "A vCenter with that name exists")
    about = await run_in_threadpool(_test_login, body.host, body.port, body.username,
                                    body.password, thumb)
    vc = VCenter(name=body.name, host=body.host, port=body.port, username=body.username,
                 password_enc=encrypt(body.password), thumbprint=thumb)
    db.add(vc)
    db.flush()
    audit(db, request, "vcenter.create", principal=p, target=vc.name,
          detail={"host": vc.host, "version": about.get("version")})
    return vc


@router.patch("/{vc_id}", response_model=VCenterOut)
async def update_vcenter(vc_id: int, body: VCenterUpdate, request: Request,
                         db: Session = Depends(get_db), p: Principal = Depends(admin)):
    vc = db.get(VCenter, vc_id)
    if vc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "vCenter not found")
    from ...auth.secrets import decrypt

    username = body.username or vc.username
    password = body.password or decrypt(vc.password_enc)
    thumb = _check_thumb(body.thumbprint) if body.thumbprint else vc.thumbprint
    if body.username or body.password or body.thumbprint:
        await run_in_threadpool(_test_login, vc.host, vc.port, username, password, thumb)
    if body.name:
        vc.name = body.name
    vc.username = username
    vc.thumbprint = thumb
    if body.password:
        vc.password_enc = encrypt(password)
    audit(db, request, "vcenter.update", principal=p, target=vc.name,
          detail={k: True for k, v in body.model_dump().items() if v is not None})
    return vc


@router.delete("/{vc_id}", status_code=204)
def delete_vcenter(vc_id: int, request: Request, db: Session = Depends(get_db),
                   p: Principal = Depends(admin)):
    vc = db.get(VCenter, vc_id)
    if vc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "vCenter not found")
    if db.scalar(select(Job.id).where(Job.vcenter_id == vc_id)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "vCenter is used by backup jobs")
    audit(db, request, "vcenter.delete", principal=p, target=vc.name)
    db.delete(vc)
