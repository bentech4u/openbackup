"""Chunk encoding: compression, optional encryption, and chunk identity.

An unencrypted repository names chunks by SHA-256 of their content. An
encrypted one uses HMAC-SHA-256 under a repository key, so chunk names do not
reveal whether a known piece of data is present.

The master key is random and stored in the repository header wrapped under a
key derived from the passphrase (scrypt). The data on the share stays
recoverable from the passphrase alone, without this server's database.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

import zstandard
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

ZERO_ID = bytes(32)

_FLAG_RAW = 0
_FLAG_ZSTD = 1

_cctx = zstandard.ZstdCompressor(level=3)
_dctx = zstandard.ZstdDecompressor()


class IntegrityError(Exception):
    """Stored data does not match what it claims to be."""


class WrongPassphrase(Exception):
    pass


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _unb64(s: str) -> bytes:
    return base64.b64decode(s)


def _kdf(passphrase: str, salt: bytes, n: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=8, p=1).derive(passphrase.encode())


def new_encryption_header(passphrase: str) -> tuple[dict, bytes, bytes]:
    """Return (header, enc_key, id_key) for a new encrypted repository."""
    enc_key, id_key = os.urandom(32), os.urandom(32)
    salt = os.urandom(16)
    n = 2**15
    kek = _kdf(passphrase, salt, n)
    nonce = os.urandom(12)
    wrapped = AESGCM(kek).encrypt(nonce, enc_key + id_key, b"openbackup-master-key")
    header = {
        "kdf": "scrypt",
        "n": n,
        "salt": _b64(salt),
        "nonce": _b64(nonce),
        "wrapped": _b64(wrapped),
    }
    return header, enc_key, id_key


def unwrap_keys(header: dict, passphrase: str) -> tuple[bytes, bytes]:
    kek = _kdf(passphrase, _unb64(header["salt"]), header["n"])
    try:
        keys = AESGCM(kek).decrypt(
            _unb64(header["nonce"]), _unb64(header["wrapped"]), b"openbackup-master-key"
        )
    except Exception as e:
        raise WrongPassphrase("Repository passphrase is incorrect") from e
    return keys[:32], keys[32:]


class Codec:
    def __init__(self, enc_key: bytes | None = None, id_key: bytes | None = None):
        self._aead = AESGCM(enc_key) if enc_key else None
        self._id_key = id_key

    @property
    def encrypted(self) -> bool:
        return self._aead is not None

    def chunk_id(self, data: bytes) -> bytes:
        if self._id_key:
            return hmac.new(self._id_key, data, hashlib.sha256).digest()
        return hashlib.sha256(data).digest()

    def encode(self, chunk_id: bytes, data: bytes) -> bytes:
        compressed = _cctx.compress(data)
        if len(compressed) < len(data):
            body = bytes([_FLAG_ZSTD]) + compressed
        else:
            body = bytes([_FLAG_RAW]) + data
        if self._aead:
            nonce = os.urandom(12)
            # The chunk id is bound as associated data, so a blob cannot be
            # swapped for another chunk's blob without detection.
            return nonce + self._aead.encrypt(nonce, body, chunk_id)
        return body

    def decode(self, chunk_id: bytes, blob: bytes) -> bytes:
        if self._aead:
            try:
                body = self._aead.decrypt(blob[:12], blob[12:], chunk_id)
            except Exception as e:
                raise IntegrityError(f"chunk {chunk_id.hex()} failed authentication") from e
        else:
            body = blob
        flag, payload = body[0], body[1:]
        if flag == _FLAG_ZSTD:
            data = _dctx.decompress(payload)
        elif flag == _FLAG_RAW:
            data = payload
        else:
            raise IntegrityError(f"chunk {chunk_id.hex()} has unknown encoding {flag}")
        if not hmac.compare_digest(self.chunk_id(data), chunk_id):
            raise IntegrityError(f"chunk {chunk_id.hex()} content does not match its id")
        return data

    def seal_metadata(self, data: bytes) -> bytes:
        if not self._aead:
            return data
        nonce = os.urandom(12)
        return b"OBENC1" + nonce + self._aead.encrypt(nonce, data, b"metadata")

    def open_metadata(self, blob: bytes) -> bytes:
        if not blob.startswith(b"OBENC1"):
            if self._aead:
                raise IntegrityError("expected encrypted metadata")
            return blob
        if not self._aead:
            raise IntegrityError("metadata is encrypted but no key is loaded")
        try:
            return self._aead.decrypt(blob[6:18], blob[18:], b"metadata")
        except Exception as e:
            raise IntegrityError("metadata failed authentication") from e
