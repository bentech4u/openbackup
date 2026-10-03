from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...auth.passwords import PasswordPolicyError, check_policy, hash_password
from ...auth.service import revoke_user_sessions
from ...db.models import Role, User, utcnow
from ..deps import Principal, admin, audit, get_db
from ..schemas import UserOut

router = APIRouter(prefix="/api/users", tags=["users"])

USERNAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$"


class UserCreate(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    full_name: str = Field(default="", max_length=128)
    password: str = Field(max_length=1024)
    role: Role = Role.viewer
    must_change_password: bool = True


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, max_length=128)
    role: Role | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, max_length=1024)
    unlock: bool = False


def _policy(password: str, username: str) -> None:
    try:
        check_policy(password, username)
    except PasswordPolicyError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None


def _get(db: Session, user_id: int) -> User:
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return u


def _other_active_admins(db: Session, user_id: int) -> int:
    return db.scalar(
        select(func.count())
        .select_from(User)
        .where(User.role == Role.admin, User.is_active.is_(True), User.id != user_id)
    )


@router.get("", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db), _: Principal = Depends(admin)) -> list[User]:
    return list(db.scalars(select(User).order_by(User.username)))


@router.post("", response_model=UserOut, status_code=201)
def create_user(
    body: UserCreate,
    request: Request,
    db: Session = Depends(get_db),
    p: Principal = Depends(admin),
) -> User:
    if db.scalar(select(User).where(func.lower(User.username) == body.username.lower())):
        raise HTTPException(status.HTTP_409_CONFLICT, "Username already exists")
    _policy(body.password, body.username)
    u = User(
        username=body.username,
        full_name=body.full_name,
        password_hash=hash_password(body.password),
        role=body.role,
        must_change_password=body.must_change_password,
    )
    db.add(u)
    db.flush()
    audit(db, request, "user.create", principal=p, target=u.username,
          detail={"role": u.role.value})
    return u


@router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    body: UserUpdate,
    request: Request,
    db: Session = Depends(get_db),
    p: Principal = Depends(admin),
) -> User:
    u = _get(db, user_id)
    changes: dict = {}
    losing_admin = (body.role is not None and body.role != Role.admin) or body.is_active is False
    if u.role == Role.admin and losing_admin and _other_active_admins(db, u.id) == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot remove the last active admin")
    if body.full_name is not None:
        u.full_name = body.full_name
    if body.role is not None and body.role != u.role:
        changes["role"] = [u.role.value, body.role.value]
        u.role = body.role
    if body.is_active is not None and body.is_active != u.is_active:
        changes["is_active"] = body.is_active
        u.is_active = body.is_active
        if not body.is_active:
            revoke_user_sessions(db, u.id)
    if body.password:
        _policy(body.password, u.username)
        u.password_hash = hash_password(body.password)
        u.must_change_password = True
        u.password_changed_at = utcnow()
        revoke_user_sessions(db, u.id)
        changes["password_reset"] = True
    if body.unlock:
        u.locked_until = None
        u.failed_logins = 0
        changes["unlocked"] = True
    audit(db, request, "user.update", principal=p, target=u.username, detail=changes)
    return u


@router.delete("/{user_id}", status_code=204)
def delete_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    p: Principal = Depends(admin),
) -> None:
    u = _get(db, user_id)
    if u.id == p.user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot delete your own account")
    if u.role == Role.admin and _other_active_admins(db, u.id) == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot remove the last active admin")
    audit(db, request, "user.delete", principal=p, target=u.username)
    db.delete(u)


@router.post("/{user_id}/revoke-sessions", status_code=204)
def revoke_sessions(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    p: Principal = Depends(admin),
) -> None:
    u = _get(db, user_id)
    revoke_user_sessions(db, u.id, keep=p.session.id if u.id == p.user.id else None)
    audit(db, request, "user.revoke_sessions", principal=p, target=u.username)
