from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db.models import AuditEntry
from ..deps import Principal, admin, get_db
from ..schemas import AuditOut

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("", response_model=list[AuditOut])
def list_audit(
    before_id: int | None = None,
    action: str | None = None,
    username: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: Principal = Depends(admin),
) -> list[AuditEntry]:
    q = select(AuditEntry).order_by(AuditEntry.id.desc()).limit(limit)
    if before_id:
        q = q.where(AuditEntry.id < before_id)
    if action:
        q = q.where(AuditEntry.action.startswith(action))
    if username:
        q = q.where(AuditEntry.username == username)
    return list(db.scalars(q))
