from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_LENGTH = 12

_hasher = PasswordHasher()
# Verified against when the username does not exist, so a login for an
# unknown user costs the same as one for a real user.
_DUMMY_HASH = _hasher.hash("openbackup-timing-equaliser")


class PasswordPolicyError(ValueError):
    pass


def check_policy(password: str, username: str = "") -> None:
    if len(password) < MIN_LENGTH:
        raise PasswordPolicyError(f"Password must be at least {MIN_LENGTH} characters")
    if len(password) > 1024:
        raise PasswordPolicyError("Password is too long")
    if username and username.lower() in password.lower():
        raise PasswordPolicyError("Password must not contain the username")
    if len(set(password)) < 5:
        raise PasswordPolicyError("Password is too repetitive")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
