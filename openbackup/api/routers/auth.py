from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...auth import service
from ...auth.passwords import PasswordPolicyError, check_policy, hash_password, verify_password
from ...config import Settings, get_settings
from ...db.models import utcnow
from ..deps import (
    SESSION_COOKIE,
    Principal,
    audit,
    client_ip,
    current_principal_allow_pw_change,
    get_db,
)
from ..schemas import UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class MeOut(BaseModel):
    user: UserOut
    csrf_token: str


class PasswordChangeIn(BaseModel):
    current_password: str = Field(max_length=1024)
    new_password: str = Field(max_length=1024)


def _me(p: Principal) -> MeOut:
    return MeOut(user=UserOut.model_validate(p.user), csrf_token=p.session.csrf_token)


@router.post("/login", response_model=MeOut)
def login(
    body: LoginIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MeOut:
    ip = client_ip(request)
    try:
        user = service.authenticate(db, settings, body.username, body.password, ip)
    except service.LoginError as e:
        audit(db, request, "auth.login", username=body.username[:64], success=False,
              detail={"reason": str(e)})
        # Persist the failure counters before the exception rolls back.
        db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e)) from None

    new = service.create_session(db, user, ip, request.headers.get("user-agent", ""))
    service.prune(db, settings)
    p = Principal(user=user, session=new.session)
    audit(db, request, "auth.login", principal=p)
    response.set_cookie(
        SESSION_COOKIE,
        new.token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
        max_age=settings.session_absolute_hours * 3600,
    )
    return _me(p)


@router.post("/logout", status_code=204)
def logout(
    request: Request,
    response: Response,
    p: Principal = Depends(current_principal_allow_pw_change),
    db: Session = Depends(get_db),
) -> None:
    db.delete(p.session)
    audit(db, request, "auth.logout", principal=p)
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me", response_model=MeOut)
def me(p: Principal = Depends(current_principal_allow_pw_change)) -> MeOut:
    return _me(p)


@router.post("/password", status_code=204)
def change_password(
    body: PasswordChangeIn,
    request: Request,
    p: Principal = Depends(current_principal_allow_pw_change),
    db: Session = Depends(get_db),
) -> None:
    if not verify_password(p.user.password_hash, body.current_password):
        audit(db, request, "auth.password_change", principal=p, success=False)
        db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect")
    if body.new_password == body.current_password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "New password must differ")
    try:
        check_policy(body.new_password, p.user.username)
    except PasswordPolicyError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None
    p.user.password_hash = hash_password(body.new_password)
    p.user.must_change_password = False
    p.user.password_changed_at = utcnow()
    # Sign out every other browser; this one stays logged in.
    service.revoke_user_sessions(db, p.user.id, keep=p.session.id)
    audit(db, request, "auth.password_change", principal=p)
