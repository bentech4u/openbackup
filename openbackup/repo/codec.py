"""Encoding of a single chunk: compress, optionally encrypt, frame, verify.

Separated from storage so that how a chunk is encoded and where it is put are
independent concerns. Chunks are addressed by the hash of their *plaintext*,
which is what makes deduplication work regardless of the layout underneath.
"""

from __future__ import annotations

import hashlib
import os
import struct

import zstandard

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover
    AESGCM = None
    InvalidTag = Exception

#: blake2b truncated to 256 bits. Chosen over SHA-256 for speed: this runs over
#: every byte of every backup, and blake2b is roughly twice as fast without
#: needing hardware acceleration to be competitive.
HASH_BYTES = 32


def chunk_hash(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=HASH_BYTES).digest()


_MAGIC = b"OBC1"
_HEADER = struct.Struct("<4sBBHI")  # magic, flags, reserved, reserved, orig len
_NONCE_BYTES = 12

FLAG_ZSTD = 1 << 0
FLAG_AESGCM = 1 << 1


class ChunkCorruption(Exception):
    """A chunk failed to decode or did not hash to the name it is stored under."""


class ChunkMissing(KeyError):
    """A chunk named by a block map is not present in the repository."""


class ChunkCodec:
    """Turns a block of disk data into a self-describing blob and back.

    Blobs carry their own header, so a pack file can be walked and recovered
    even without its index.
    """

    def __init__(self, *, level: int = 3, key: bytes | None = None):
        if key is not None:
            if AESGCM is None:  # pragma: no cover
                raise RuntimeError("encryption requires the cryptography package")
            if len(key) not in (16, 24, 32):
                raise ValueError("key must be 16, 24 or 32 bytes")
        self.level = level
        self._key = key
        self._aes = AESGCM(key) if key else None
        self._cctx = zstandard.ZstdCompressor(level=level)
        self._dctx = zstandard.ZstdDecompressor()

    @property
    def encrypted(self) -> bool:
        return self._aes is not None

    def encode(self, data: bytes) -> bytes:
        flags = 0
        body = data
        packed = self._cctx.compress(body)
        # Incompressible blocks (already-compressed files, encrypted volumes)
        # are common in VM disks; storing them raw avoids paying zstd's frame
        # overhead and the decompression cost on restore.
        if len(packed) < len(body):
            body = packed
            flags |= FLAG_ZSTD

        nonce = b""
        if self._aes is not None:
            nonce = os.urandom(_NONCE_BYTES)
            body = self._aes.encrypt(nonce, body, None)
            flags |= FLAG_AESGCM

        return _HEADER.pack(_MAGIC, flags, 0, 0, len(data)) + nonce + body

    def decode(self, blob: bytes, expect: bytes) -> bytes:
        if len(blob) < _HEADER.size:
            raise ChunkCorruption(f"chunk {expect.hex()} is truncated")
        magic, flags, _r1, _r2, orig_len = _HEADER.unpack(blob[:_HEADER.size])
        if magic != _MAGIC:
            raise ChunkCorruption(f"chunk {expect.hex()} has bad magic {magic!r}")
        body = blob[_HEADER.size:]

        if flags & FLAG_AESGCM:
            if self._aes is None:
                raise ChunkCorruption(
                    f"chunk {expect.hex()} is encrypted but no key was supplied")
            nonce, body = body[:_NONCE_BYTES], body[_NONCE_BYTES:]
            try:
                body = self._aes.decrypt(nonce, body, None)
            except InvalidTag as exc:
                raise ChunkCorruption(
                    f"chunk {expect.hex()} failed authentication") from exc
        elif self._aes is not None:
            raise ChunkCorruption(
                f"chunk {expect.hex()} is unencrypted in an encrypted repository")

        if flags & FLAG_ZSTD:
            try:
                body = self._dctx.decompress(body, max_output_size=orig_len)
            except zstandard.ZstdError as exc:
                raise ChunkCorruption(
                    f"chunk {expect.hex()} failed to decompress") from exc

        if len(body) != orig_len:
            raise ChunkCorruption(
                f"chunk {expect.hex()} decoded to {len(body)} bytes, "
                f"header claims {orig_len}")

        # Always re-check. A backup that silently restores altered blocks is
        # worse than one that fails, because the failure is at least visible.
        actual = chunk_hash(body)
        if actual != expect:
            raise ChunkCorruption(
                f"chunk {expect.hex()} content hashes to {actual.hex()}")
        return body
