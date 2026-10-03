"""Login, sessions and lockout."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db.models import AuthSession, LoginFailure, User, utcnow
from .passwords import hash_password, needs_rehash, verify_password

# Per-address throttle, independent of any one account, so guessing across
# many usernames from one source is also slowed down.
IP_MAX_FAILURES = 20
IP_WINDOW = timedelta(minutes=15)


class LoginError(Exception):
    """Raised for any failed login. The message is safe to show to the user
    and deliberately does not distinguish unknown users from bad passwords."""


def token_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class NewSession:
    token: str
    session: AuthSession


def authenticate(
    db: Session, settings: Settings, username: str, password: str, ip: str
) -> User:
    now = utcnow()
    recent = db.scalar(
        select(func.count())
        .select_from(LoginFailure)
        .where(LoginFailure.ip == ip, LoginFailure.at > now - IP_WINDOW)
    )
    if recent >= IP_MAX_FAILURES:
        raise LoginError("Too many failed logins from this address. Try again later.")

    user = db.scalar(select(User).where(func.lower(User.username) == username.lower()))
    ok = verify_password(user.password_hash if user else None, password)

    if user is not None and user.locked_until and user.locked_until > now:
        # Checked after hashing so a locked account costs the same time.
        db.add(LoginFailure(ip=ip, at=now))
        raise LoginError("Account is temporarily locked. Try again later.")

    if not ok or user is None or not user.is_active:
        db.add(LoginFailure(ip=ip, at=now))
        if user is not None and not ok:
            user.failed_logins += 1
            if user.failed_logins >= settings.login_max_failures:
                user.locked_until = now + timedelta(minutes=settings.login_lockout_minutes)
                user.failed_logins = 0
        raise LoginError("Invalid username or password.")

    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    return user


def create_session(db: Session, user: User, ip: str, user_agent: str) -> NewSession:
    token = secrets.token_urlsafe(32)
    s = AuthSession(
        id=token_id(token),
        user_id=user.id,
        csrf_token=secrets.token_urlsafe(32),
        ip=ip,
        user_agent=user_agent[:255],
    )
    db.add(s)
    db.flush()
    return NewSession(token=token, session=s)


def resolve_session(db: Session, settings: Settings, token: str | None) -> AuthSession | None:
    if not token:
        return None
    s = db.get(AuthSession, token_id(token))
    if s is None:
        return None
    now = utcnow()
    idle = now - s.last_seen_at > timedelta(minutes=settings.session_idle_minutes)
    old = now - s.created_at > timedelta(hours=settings.session_absolute_hours)
    if idle or old or not s.user.is_active:
        db.delete(s)
        return None
    # Avoid a write per request; a minute of slack on the idle timer is fine.
    if now - s.last_seen_at > timedelta(minutes=1):
        s.last_seen_at = now
    return s


def revoke_user_sessions(db: Session, user_id: int, keep: str | None = None) -> None:
    q = delete(AuthSession).where(AuthSession.user_id == user_id)
    if keep:
        q = q.where(AuthSession.id != keep)
    db.execute(q)


def prune(db: Session, settings: Settings) -> None:
    now = utcnow()
    db.execute(delete(LoginFailure).where(LoginFailure.at < now - IP_WINDOW))
    db.execute(
        delete(AuthSession).where(
            AuthSession.last_seen_at < now - timedelta(minutes=settings.session_idle_minutes)
        )
    )
