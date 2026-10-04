"""OpenShift / Kubernetes clusters."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...auth.secrets import decrypt, encrypt
from ...db.models import Job, KubeCluster, VCenter
from ...kube import bootstrap, inventory
from ...kube.client import KubeClient, KubeError, fetch_chain, validate_ca_bundle
from ..deps import Principal, admin, audit, get_db, viewer

router = APIRouter(prefix="/api/clusters", tags=["clusters"])

URL_PATTERN = r"^https://[A-Za-z0-9.\-\[\]:]+(:\d+)?/?$"

# Tests replace these to talk to fake servers.
client_factory = KubeClient
oauth_transport = None


class ClusterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    api_url: str
    vcenter_id: int | None
    vcenter_name: str | None = None
    has_restore_token: bool = False
    ca_subjects: list[str] = []
    created_at: datetime


def _out(c: KubeCluster) -> ClusterOut:
    o = ClusterOut.model_validate(c)
    o.vcenter_name = c.vcenter.name if c.vcenter_id and c.vcenter else None
    o.has_restore_token = bool(c.restore_token_enc)
    try:
        o.ca_subjects = [x.subject for x in validate_ca_bundle(c.ca_pem)]
    except KubeError:
        pass
    return o


class FetchCaIn(BaseModel):
    api_url: str = Field(pattern=URL_PATTERN)


class ClusterIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    api_url: str = Field(pattern=URL_PATTERN)
    ca_pem: str = Field(min_length=1, max_length=65536)
    backup_token: str = Field(min_length=1, max_length=16384)
    restore_token: str | None = Field(None, max_length=16384)
    vcenter_id: int | None = None


class ClusterUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    ca_pem: str | None = Field(None, max_length=65536)
    backup_token: str | None = Field(None, max_length=16384)
    restore_token: str | None = Field(None, max_length=16384)
    clear_restore_token: bool = False
    vcenter_id: int | None = None


def _detect_vcenter(db: Session, api_url: str, ca_pem: str, token: str) -> VCenter | None:
    """The registered vCenter this cluster runs on, if any."""
    try:
        with client_factory(api_url, token, ca_pem) as c:
            servers = {s.lower() for s in inventory.vsphere_servers(c)}
    except KubeError:
        return None
    for vc in db.scalars(select(VCenter)):
        if vc.host.lower() in servers:
            return vc
    return None


def _probe(api_url: str, ca_pem: str, token: str, need_read: bool = True) -> dict:
    try:
        validate_ca_bundle(ca_pem)
        with client_factory(api_url, token, ca_pem) as c:
            ver = c.version()
            info = {"version": ver.get("gitVersion", ""), "user": c.whoami(),
                    "permissions": inventory.permissions(c),
                    "kubevirt": c.has_group("kubevirt.io"),
                    "openshift": c.has_group("route.openshift.io")}
    except KubeError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None
    if need_read and not all(info["permissions"].get(k) for k in
                              ("list_namespaces", "list_pvcs", "list_pvs", "list_deployments")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "The backup token cannot read the cluster (it needs cluster-reader; "
                            "see deploy/openshift/backup-serviceaccount.yaml)")
    return info


@router.post("/fetch-ca")
async def fetch_ca(body: FetchCaIn, _: Principal = Depends(admin)) -> list[dict]:
    """The certificates the API server presents, for the admin to confirm
    which CA to pin."""
    try:
        chain = await run_in_threadpool(fetch_chain, body.api_url)
    except KubeError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None
    return [{"pem": c.pem, "subject": c.subject, "issuer": c.issuer, "sha256": c.sha256,
             "is_ca": c.is_ca} for c in chain]


class OAuthInfoIn(BaseModel):
    api_url: str = Field(pattern=URL_PATTERN)
    ca_pem: str = Field(min_length=1, max_length=65536)


@router.post("/oauth-info")
async def oauth_info(body: OAuthInfoIn, _: Principal = Depends(admin)) -> dict:
    """Where password login happens, and the certificates it presents (the
    OAuth server sits on the ingress, usually with a different CA)."""
    def work():
        return _oauth_info(body.api_url, body.ca_pem)
    try:
        return await run_in_threadpool(work)
    except KubeError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None


def _oauth_info(api_url: str, ca_pem: str) -> dict:
    validate_ca_bundle(ca_pem)
    with client_factory(api_url.rstrip("/"), "", ca_pem) as c:
        url = bootstrap.oauth_endpoint(c)
    u = urlsplit(url)
    chain = [] if oauth_transport is not None else fetch_chain(f"https://{u.netloc}")
    return {"authorize_url": url, "chain": [
        {"pem": x.pem, "subject": x.subject, "issuer": x.issuer, "sha256": x.sha256,
         "is_ca": x.is_ca} for x in chain]}


@router.get("/{cid}/oauth-info")
async def cluster_oauth_info(cid: int, db: Session = Depends(get_db),
                             _: Principal = Depends(admin)) -> dict:
    """OAuth server certificates of a registered cluster, using its pinned API CA."""
    c = _get(db, cid)
    try:
        return await run_in_threadpool(_oauth_info, c.api_url, c.ca_pem)
    except KubeError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None


class AdminAuth(BaseModel):
    kind: Literal["password", "token"]
    username: str = Field("", max_length=255)
    password: str = Field("", max_length=1024)
    token: str = Field("", max_length=16384)
    oauth_ca_pem: str = Field("", max_length=65536)


class SetupIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    api_url: str = Field(pattern=URL_PATTERN)
    ca_pem: str = Field(min_length=1, max_length=65536)
    vcenter_id: int | None = None
    admin: AdminAuth
    # Create the restore account too, and keep its token in OpenBackup.
    restore_account: bool = True


def _setup(body: SetupIn) -> tuple[bootstrap.SetupResult, dict, dict | None]:
    return _run_setup(body.api_url.rstrip("/"), body.ca_pem, body.admin, body.restore_account)


def _run_setup(api_url: str, ca_pem: str, a: AdminAuth, restore_account: bool
               ) -> tuple[bootstrap.SetupResult, dict, dict | None]:
    validate_ca_bundle(ca_pem)
    obtained = False
    if a.kind == "password":
        if not a.username or not a.password:
            raise KubeError("Enter the admin username and password")
        with client_factory(api_url, "", ca_pem) as anon:
            url = bootstrap.oauth_endpoint(anon)
        if oauth_transport is None:
            validate_ca_bundle(a.oauth_ca_pem)
        admin_token = bootstrap.password_login(url, a.username, a.password, a.oauth_ca_pem,
                                               transport=oauth_transport)
        obtained = True
    else:
        if not a.token:
            raise KubeError("Paste an admin token")
        admin_token = a.token.strip()
    with client_factory(api_url, admin_token, ca_pem) as admin_client:
        try:
            result = bootstrap.setup_accounts(admin_client, restore_account)
        finally:
            if obtained:  # only the session we created; a pasted token stays the user's
                try:
                    bootstrap.revoke_session_token(admin_client, admin_token)
                except KubeError:
                    pass
    backup = _probe(api_url, ca_pem, result.backup_token)
    restore = (_probe(api_url, ca_pem, result.restore_token, False)
               if result.restore_token else None)
    return result, backup, restore


class PermissionsIn(BaseModel):
    admin: AdminAuth
    restore_account: bool | None = None  # default: keep what the cluster has


@router.post("/{cid}/permissions")
async def update_permissions(cid: int, body: PermissionsIn, request: Request,
                             db: Session = Depends(get_db), p: Principal = Depends(admin)) -> dict:
    """Re-apply OpenBackup's ServiceAccounts and roles on an existing cluster
    (after an upgrade added permissions), with admin credentials used once."""
    c = _get(db, cid)
    with_restore = bool(c.restore_token_enc) if body.restore_account is None \
        else body.restore_account
    try:
        result, backup, restore = await run_in_threadpool(_run_setup, c.api_url, c.ca_pem,
                                                          body.admin, with_restore)
    except KubeError as e:
        audit(db, request, "cluster.permissions", principal=p, target=c.name, success=False,
              detail={"error": str(e)[:300]})
        db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None
    c.backup_token_enc = encrypt(result.backup_token)
    if result.restore_token:
        c.restore_token_enc = encrypt(result.restore_token)
    audit(db, request, "cluster.permissions", principal=p, target=c.name,
          detail={"admin_user": result.admin_user, "login": body.admin.kind,
                  "applied": result.created})
    return {"cluster": _out(c).model_dump(mode="json"), "backup": backup, "restore": restore,
            "admin_user": result.admin_user, "created": result.created,
            "skipped": result.skipped}


@router.post("/setup", status_code=201)
async def setup(body: SetupIn, request: Request, db: Session = Depends(get_db),
                p: Principal = Depends(admin)) -> dict:
    """Create OpenBackup's ServiceAccounts with admin credentials used once
    and never stored, then register the cluster with those accounts."""
    if db.scalar(select(KubeCluster).where(KubeCluster.name == body.name)):
        raise HTTPException(status.HTTP_409_CONFLICT, "A cluster with that name exists")
    if body.vcenter_id is not None and db.get(VCenter, body.vcenter_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown vCenter")
    try:
        result, backup, restore = await run_in_threadpool(_setup, body)
    except KubeError as e:
        audit(db, request, "cluster.setup", principal=p, target=body.name, success=False,
              detail={"api_url": body.api_url, "error": str(e)[:300]})
        db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None
    vcenter_id = body.vcenter_id
    if vcenter_id is None:
        vc = await run_in_threadpool(_detect_vcenter, db, body.api_url.rstrip("/"),
                                     body.ca_pem, result.backup_token)
        vcenter_id = vc.id if vc else None
    c = KubeCluster(name=body.name, api_url=body.api_url.rstrip("/"), ca_pem=body.ca_pem,
                    backup_token_enc=encrypt(result.backup_token),
                    restore_token_enc=encrypt(result.restore_token) if result.restore_token
                    else None, vcenter_id=vcenter_id)
    db.add(c)
    db.flush()
    db.refresh(c)
    audit(db, request, "cluster.setup", principal=p, target=c.name,
          detail={"api_url": c.api_url, "admin_user": result.admin_user,
                  "login": body.admin.kind, "created": result.created})
    return {"cluster": _out(c).model_dump(mode="json"), "backup": backup, "restore": restore,
            "admin_user": result.admin_user, "created": result.created,
            "skipped": result.skipped}


@router.get("", response_model=list[ClusterOut])
def list_clusters(db: Session = Depends(get_db), _: Principal = Depends(viewer)):
    return [_out(c) for c in db.scalars(select(KubeCluster).order_by(KubeCluster.name))]


@router.post("", status_code=201)
async def add_cluster(body: ClusterIn, request: Request, db: Session = Depends(get_db),
                      p: Principal = Depends(admin)) -> dict:
    if db.scalar(select(KubeCluster).where(KubeCluster.name == body.name)):
        raise HTTPException(status.HTTP_409_CONFLICT, "A cluster with that name exists")
    if body.vcenter_id is not None and db.get(VCenter, body.vcenter_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown vCenter")
    info = await run_in_threadpool(_probe, body.api_url.rstrip("/"), body.ca_pem,
                                   body.backup_token)
    restore_info = None
    if body.restore_token:
        restore_info = await run_in_threadpool(_probe, body.api_url.rstrip("/"), body.ca_pem,
                                               body.restore_token, False)
    vcenter_id = body.vcenter_id
    if vcenter_id is None:
        vc = await run_in_threadpool(_detect_vcenter, db, body.api_url.rstrip("/"),
                                     body.ca_pem, body.backup_token)
        vcenter_id = vc.id if vc else None
    c = KubeCluster(name=body.name, api_url=body.api_url.rstrip("/"), ca_pem=body.ca_pem,
                    backup_token_enc=encrypt(body.backup_token),
                    restore_token_enc=encrypt(body.restore_token) if body.restore_token
                    else None, vcenter_id=vcenter_id)
    db.add(c)
    db.flush()
    db.refresh(c)
    audit(db, request, "cluster.create", principal=p, target=c.name,
          detail={"api_url": c.api_url, "user": info["user"], "version": info["version"]})
    return {"cluster": _out(c).model_dump(mode="json"), "backup": info, "restore": restore_info}


def _get(db: Session, cid: int) -> KubeCluster:
    c = db.get(KubeCluster, cid)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cluster not found")
    return c


@router.patch("/{cid}", response_model=ClusterOut)
async def update_cluster(cid: int, body: ClusterUpdate, request: Request,
                         db: Session = Depends(get_db), p: Principal = Depends(admin)):
    c = _get(db, cid)
    ca = body.ca_pem or c.ca_pem
    token = body.backup_token or decrypt(c.backup_token_enc)
    if body.ca_pem or body.backup_token:
        await run_in_threadpool(_probe, c.api_url, ca, token)
    if body.restore_token:
        await run_in_threadpool(_probe, c.api_url, ca, body.restore_token, False)
        c.restore_token_enc = encrypt(body.restore_token)
    if body.clear_restore_token:
        c.restore_token_enc = None
    if body.name:
        c.name = body.name
    c.ca_pem = ca
    if body.backup_token:
        c.backup_token_enc = encrypt(body.backup_token)
    if "vcenter_id" in body.model_fields_set:
        c.vcenter_id = body.vcenter_id
    audit(db, request, "cluster.update", principal=p, target=c.name,
          detail={k: True for k, v in body.model_dump().items() if v})
    return _out(c)


@router.delete("/{cid}", status_code=204)
def delete_cluster(cid: int, request: Request, db: Session = Depends(get_db),
                   p: Principal = Depends(admin)) -> None:
    c = _get(db, cid)
    if db.scalar(select(Job.id).where(Job.cluster_id == cid)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cluster is used by backup jobs")
    audit(db, request, "cluster.delete", principal=p, target=c.name)
    db.delete(c)


def cluster_client(c: KubeCluster, restore_token: str | None = None) -> KubeClient:
    token = restore_token or decrypt(c.backup_token_enc)
    return client_factory(c.api_url, token, c.ca_pem)


def _namespaces(c: KubeCluster, include_system: bool) -> list[dict]:
    try:
        with cluster_client(c) as k:
            return inventory.namespaces(k, include_system)
    except KubeError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from None


@router.get("/{cid}/namespaces")
async def list_namespaces(cid: int, include_system: bool = False, db: Session = Depends(get_db),
                          _: Principal = Depends(viewer)) -> list[dict]:
    c = _get(db, cid)
    db.expunge(c)
    return await run_in_threadpool(_namespaces, c, include_system)


@router.get("/{cid}/vsphere")
async def vsphere_info(cid: int, db: Session = Depends(get_db),
                       _: Principal = Depends(viewer)) -> dict:
    """The vCenter the cluster reports running on, and whether it is linked."""
    c = _get(db, cid)
    db.expunge(c)

    def work():
        with cluster_client(c) as k:
            return inventory.vsphere_servers(k)
    try:
        servers = await run_in_threadpool(work)
    except KubeError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from None
    match = next((vc for vc in db.scalars(select(VCenter))
                  if vc.host.lower() in {s.lower() for s in servers}), None)
    return {"reported": servers, "suggested_vcenter_id": match.id if match else None,
            "linked_vcenter_id": c.vcenter_id}


@router.get("/{cid}/check")
async def check(cid: int, db: Session = Depends(get_db), _: Principal = Depends(admin)) -> dict:
    c = _get(db, cid)
    db.expunge(c)
    backup = await run_in_threadpool(_probe, c.api_url, c.ca_pem, decrypt(c.backup_token_enc))
    restore = None
    if c.restore_token_enc:
        restore = await run_in_threadpool(_probe, c.api_url, c.ca_pem,
                                          decrypt(c.restore_token_enc), False)
    return {"backup": backup, "restore": restore}
