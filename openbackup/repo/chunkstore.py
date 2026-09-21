"""Content-addressed chunk storage.

A restore point is not a file: it is an ordered list of chunk hashes per disk
(see :mod:`openbackup.repo.blockmap`). Storing blocks by the hash of their
contents buys several things a Veeam-style VBK/VIB chain has to work for:

* deduplication within a disk, across a VM's restore points, and across VMs;
* synthetic fulls for free, because every restore point already names every
  block it needs and none of them depend on a chain of increments;
* retention that is a refcount decrement rather than a merge of increments.

The cost is that deleting data requires refcounting, which the catalog owns.

An all-zero block needs no special case: unallocated space in a thin disk
hashes to the same value every time, so it is stored once and deduplicated
thereafter. (We still avoid *reading* it from VMware, via CBT allocated
extents -- that is a separate saving.)
"""

from __future__ import annotations

import hashlib
import os
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path

import zstandard

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover
    AESGCM = None
    InvalidTag = Exception

#: blake2b truncated to 256 bits. Chosen over SHA-256 for speed: this runs over
#: every byte of every backup, and blake2b is roughly twice as fast without
#: needing a hardware-accelerated SHA implementation to be competitive.
HASH_BYTES = 32


def chunk_hash(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=HASH_BYTES).digest()


# -- on-disk chunk framing --------------------------------------------------

_MAGIC = b"OBC1"
_HEADER = struct.Struct("<4sBBHI")  # magic, flags, reserved, reserved, orig len
_NONCE_BYTES = 12

FLAG_ZSTD = 1 << 0
FLAG_AESGCM = 1 << 1


class ChunkCorruption(Exception):
    """A chunk failed to decode or did not hash to the name it was stored under."""


class ChunkMissing(KeyError):
    """A chunk named by a block map is not present in the store."""


@dataclass(frozen=True)
class PutResult:
    hash: bytes
    stored_size: int
    is_new: bool


class ChunkStore:
    """Chunks on a local filesystem, named by the hash of their plaintext.

    Writes are atomic: a chunk is written to a temporary file, fsynced and
    renamed into place, so a crash mid-backup can leave garbage in the temp
    directory but never a half-written chunk that something else will later
    trust because its name says the contents are good.
    """

    def __init__(self, root: Path | str, *, level: int = 3,
                 key: bytes | None = None, fsync: bool = True):
        self.root = Path(root)
        self.level = level
        self.fsync = fsync
        if key is not None:
            if AESGCM is None:  # pragma: no cover
                raise RuntimeError("encryption requires the cryptography package")
            if len(key) not in (16, 24, 32):
                raise ValueError("key must be 16, 24 or 32 bytes")
        self._key = key
        self._aes = AESGCM(key) if key else None
        self._cctx = zstandard.ZstdCompressor(level=level)
        self._dctx = zstandard.ZstdDecompressor()
        self._tmp = self.root / "tmp"

    # -- layout -------------------------------------------------------------

    def path_for(self, digest: bytes) -> Path:
        """Fan out two levels so no directory holds millions of entries.

        At 1 MiB chunks a 100 TiB repository is ~100M chunks; 65536 leaf
        directories keeps that to a few thousand files each, which every
        filesystem we care about handles without degrading.
        """
        hexed = digest.hex()
        return self.root / hexed[:2] / hexed[2:4] / hexed

    def init(self) -> None:
        self._tmp.mkdir(parents=True, exist_ok=True)

    # -- encode / decode ----------------------------------------------------

    def _encode(self, data: bytes) -> bytes:
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

    def _decode(self, blob: bytes, expect: bytes) -> bytes:
        if len(blob) < _HEADER.size:
            raise ChunkCorruption(f"chunk {expect.hex()} is truncated")
        magic, flags, _r1, _r2, orig_len = _HEADER.unpack(blob[:_HEADER.size])
        if magic != _MAGIC:
            raise ChunkCorruption(
                f"chunk {expect.hex()} has bad magic {magic!r}")
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
        return body

    # -- operations ---------------------------------------------------------

    def put(self, data: bytes) -> PutResult:
        """Store a block, returning its hash. Storing an existing block is a
        no-op, which is what makes deduplication essentially free."""
        digest = chunk_hash(data)
        dest = self.path_for(digest)
        if dest.exists():
            return PutResult(digest, dest.stat().st_size, is_new=False)

        blob = self._encode(data)
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._tmp.mkdir(parents=True, exist_ok=True)

        fd, tmp_name = tempfile.mkstemp(dir=self._tmp, prefix="chunk-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
                fh.flush()
                if self.fsync:
                    os.fsync(fh.fileno())
            # os.replace is atomic within a filesystem, so a concurrent writer
            # storing the same chunk is harmless: identical contents either way.
            os.replace(tmp_name, dest)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return PutResult(digest, len(blob), is_new=True)

    def get(self, digest: bytes) -> bytes:
        """Fetch and verify a block.

        The hash is always re-checked. A backup that silently restores altered
        blocks is worse than one that fails, because the failure is at least
        visible, so bitrot and truncation are turned into loud errors here.
        """
        path = self.path_for(digest)
        try:
            blob = path.read_bytes()
        except FileNotFoundError as exc:
            raise ChunkMissing(digest.hex()) from exc

        data = self._decode(blob, digest)
        actual = chunk_hash(data)
        if actual != digest:
            raise ChunkCorruption(
                f"chunk {digest.hex()} content hashes to {actual.hex()}")
        return data

    def exists(self, digest: bytes) -> bool:
        return self.path_for(digest).exists()

    def delete(self, digest: bytes) -> bool:
        try:
            self.path_for(digest).unlink()
            return True
        except FileNotFoundError:
            return False

    def iter_hashes(self):
        """Every chunk actually present, for garbage collection and audit."""
        for d1 in self.root.iterdir():
            if not d1.is_dir() or len(d1.name) != 2 or d1.name == "tm":
                continue
            for d2 in d1.iterdir():
                if not d2.is_dir():
                    continue
                for f in d2.iterdir():
                    if f.is_file() and len(f.name) == HASH_BYTES * 2:
                        try:
                            yield bytes.fromhex(f.name)
                        except ValueError:
                            continue
