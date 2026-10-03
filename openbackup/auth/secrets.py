"""Encryption at rest for credentials stored in the database (vCenter
passwords, repository passphrases), keyed by a local key file."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from ..config import get_settings


class SecretKeyError(RuntimeError):
    pass


def ensure_key_file(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(Fernet.generate_key())


@lru_cache
def _fernet() -> Fernet:
    path = get_settings().secret_key_file
    try:
        return Fernet(path.read_bytes().strip())
    except FileNotFoundError as e:
        raise SecretKeyError(
            f"Secret key {path} is missing; run 'openbackup init' to create it"
        ) from e


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise SecretKeyError("Stored secret cannot be decrypted with the current key") from e
