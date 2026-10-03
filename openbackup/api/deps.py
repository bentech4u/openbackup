from __future__ import annotations

import secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..auth.service import resolve_session
from ..config import Settings, get_settings
from ..db import get_sessionmaker
from ..db.models import AuditEntry, AuthSession, Role, User

SESSION_COOKIE = "ob_session"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def get_db() -> Iterator[Session]:
    db = get_sessionmaker()()
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


@dataclass
class Principal:
    user: User
    session: AuthSession

    @property
    def role(self) -> Role:
        return self.user.role


def _authenticate(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Principal:
    s = resolve_session(db, settings, request.cookies.get(SESSION_COOKIE))
    if s is None:
        # Commit the deletion of an expired session before raising.
        db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    if request.method not in SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        if not secrets.compare_digest(sent, s.csrf_token):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token missing or invalid")
    return Principal(user=s.user, session=s)


def current_principal_allow_pw_change(p: Principal = Depends(_authenticate)) -> Principal:
    """For the few endpoints a user may reach before changing a temporary
    password: who am I, change password, log out."""
    return p


def current_principal(p: Principal = Depends(_authenticate)) -> Principal:
    if p.user.must_change_password:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Password change required")
    return p


def require_role(minimum: Role) -> Callable[..., Principal]:
    def dep(p: Principal = Depends(current_principal)) -> Principal:
        if p.role.rank < minimum.rank:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires {minimum.value} role")
        return p

    return dep


viewer = require_role(Role.viewer)
operator = require_role(Role.operator)
admin = require_role(Role.admin)


def audit(
    db: Session,
    request: Request,
    action: str,
    *,
    principal: Principal | None = None,
    username: str = "",
    target: str = "",
    success: bool = True,
    detail: dict | None = None,
) -> None:
    db.add(
        AuditEntry(
            user_id=principal.user.id if principal else None,
            username=principal.user.username if principal else username,
            ip=client_ip(request),
            action=action,
            target=target,
            success=success,
            detail=detail or {},
        )
    )
