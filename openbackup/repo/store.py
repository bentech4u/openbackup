"""The chunk store: content-addressed blocks, grouped into pack files.

Callers see `put`/`get`/`exists` and need not know that chunks are batched into
packs underneath. What they do need to know is that :meth:`flush` is what makes
a chunk durable -- a backup job must flush before it records a restore point
that references those chunks.

Ordering is deliberate. A pack is written to the repository first and indexed
only afterwards, so the index can lag the repository but never point at
something absent. The opposite ordering would produce an index that promises
chunks a restore cannot find. An unindexed pack is merely an orphan, and
reindexing recovers it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .backend import FilesystemBackend
from .codec import ChunkCodec, ChunkMissing, chunk_hash
from .index import ChunkIndex
from .packfile import DEFAULT_PACK_SIZE, PackWriter

#: 128 bits of pack identifier: collision-free in practice for any repository
#: size, and short enough to keep paths readable.
PACK_ID_BYTES = 16


@dataclass(frozen=True)
class PutResult:
    hash: bytes
    stored_size: int
    is_new: bool


@dataclass
class StoreStats:
    chunks_written: int = 0
    chunks_deduped: int = 0
    bytes_in: int = 0
    bytes_stored: int = 0
    packs_written: int = 0

    @property
    def dedup_ratio(self) -> float:
        total = self.chunks_written + self.chunks_deduped
        return self.chunks_deduped / total if total else 0.0


def pack_path(pack_id: str) -> str:
    return f"packs/{pack_id[:2]}/{pack_id}.pack"


class PackedChunkStore:
    def __init__(self, backend: FilesystemBackend, index: ChunkIndex,
                 codec: ChunkCodec | None = None, *,
                 pack_size: int = DEFAULT_PACK_SIZE):
        self.backend = backend
        self.index = index
        self.codec = codec or ChunkCodec()
        self._writer = PackWriter(target_size=pack_size)
        self.stats = StoreStats()

    # -- writing ------------------------------------------------------------

    def put(self, data: bytes) -> PutResult:
        digest = chunk_hash(data)

        # Check the open pack first: it is in memory, and a block repeated
        # within one disk is common enough that this is the hot path.
        if self._writer.contains(digest):
            self.stats.chunks_deduped += 1
            self.stats.bytes_in += len(data)
            return PutResult(digest, 0, is_new=False)

        existing = self.index.locate(digest)
        if existing is not None:
            self.stats.chunks_deduped += 1
            self.stats.bytes_in += len(data)
            return PutResult(digest, existing.length, is_new=False)

        blob = self.codec.encode(data)
        self._writer.add(digest, blob)
        self.stats.chunks_written += 1
        self.stats.bytes_in += len(data)
        self.stats.bytes_stored += len(blob)

        if self._writer.is_full:
            self.flush()
        return PutResult(digest, len(blob), is_new=True)

    def flush(self) -> str | None:
        """Write the open pack to the repository. Returns its id, if any."""
        if self._writer.is_empty:
            return None
        blob, entries = self._writer.finish()
        pack_id = hashlib.blake2b(blob, digest_size=PACK_ID_BYTES).hexdigest()

        # Durable in the repository first, indexed second.
        self.backend.write(pack_path(pack_id), blob)
        self.index.add_pack(pack_id, len(blob), entries)

        self._writer.reset()
        self.stats.packs_written += 1
        return pack_id

    # -- reading ------------------------------------------------------------

    def get(self, digest: bytes) -> bytes:
        pending = self._writer.blob(digest)
        if pending is not None:
            return self.codec.decode(pending, digest)

        location = self.index.locate(digest)
        if location is None:
            raise ChunkMissing(digest.hex())
        blob = self.backend.read_range(
            pack_path(location.pack_id), location.offset, location.length)
        # decode() re-verifies the hash, so bitrot inside a pack surfaces here
        # rather than as silently wrong data in a restored disk.
        return self.codec.decode(blob, digest)

    def exists(self, digest: bytes) -> bool:
        return self._writer.contains(digest) or self.index.has(digest)

    def missing(self, digests) -> set[bytes]:
        """Which of these chunks we do not have, asked in one round."""
        wanted = set(digests)
        have = self.index.have_any(wanted)
        have.update(d for d in wanted if self._writer.contains(d))
        return wanted - have

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self.flush()

    def __enter__(self) -> "PackedChunkStore":
        return self

    def __exit__(self, exc_type, *rest) -> None:
        # On a failed job leave the partial pack unwritten: the restore point
        # will not be recorded, so those chunks would be unreferenced anyway.
        if exc_type is None:
            self.flush()
