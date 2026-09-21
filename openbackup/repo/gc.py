"""Reclaiming space after restore points are deleted.

Retention deletes metadata; this is what actually frees bytes. With one file
per chunk it would be an unlink. With pack files a deleted point leaves live
and dead chunks interleaved inside packs, so space comes back in two ways:

* a pack with nothing live left is deleted outright;
* a pack that is mostly dead is rewritten with only its live chunks, and the
  original deleted.

Rewriting is deliberately *not* done for every pack with a little dead space.
Repacking costs a full read and write of the data, so a pack that is 90% live
is left alone: the 10% is not worth rewriting 128 MB to recover.

Ordering is chosen so that an interruption can only ever waste space, never
lose it. The replacement pack is written and indexed before the original is
deleted, so a crash in between leaves both -- recoverable by the next run --
rather than leaving chunks that a restore point still references unreachable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .codec import ChunkCodec
from .packfile import PackWriter
from .restorepoint import PointStore
from .store import PACK_ID_BYTES, pack_path

log = logging.getLogger(__name__)

#: Rewrite a pack only when this fraction or less of it is still referenced.
DEFAULT_REPACK_BELOW = 0.5


@dataclass
class GcResult:
    live_chunks: int = 0
    packs_examined: int = 0
    packs_deleted: int = 0
    packs_repacked: int = 0
    packs_written: int = 0
    bytes_reclaimed: int = 0
    orphans_removed: int = 0
    dry_run: bool = False
    deleted: list[str] = field(default_factory=list)


def build_live_set(repo) -> int:
    """Record every chunk still referenced by any restore point."""
    index = repo.index
    points = PointStore(repo.backend)
    index.create_live_set()
    total = 0
    for vm_uuid in points.list_vms():
        for point_id in points.list_points(vm_uuid):
            point = points.load(vm_uuid, point_id)
            for disk in point.disks:
                try:
                    bm = points.load_blockmap(vm_uuid, point_id, disk.key)
                except Exception as exc:
                    # Refusing to continue is the safe choice: a block map we
                    # cannot read would make its chunks look unreferenced, and
                    # we would delete data a restore point still needs.
                    index.drop_live_set()
                    raise RuntimeError(
                        f"cannot read block map for {point_id} disk "
                        f"{disk.key} ({exc}); refusing to collect garbage "
                        "without a complete picture of what is referenced"
                    ) from exc
                digests = bm.distinct_hashes()
                index.add_live(digests)
                total += len(digests)
    return total


def collect_garbage(repo, *, repack_below: float = DEFAULT_REPACK_BELOW,
                    dry_run: bool = False) -> GcResult:
    """Free space no restore point references any more."""
    index = repo.index
    backend = repo.backend
    result = GcResult(dry_run=dry_run)

    build_live_set(repo)
    try:
        result.live_chunks = index._db.execute(
            "SELECT COUNT(*) FROM live").fetchone()[0]

        # A pack the index lists but the repository no longer holds is stale
        # bookkeeping, not data loss; drop it so it stops being considered.
        present = {
            relpath.rsplit("/", 1)[-1][:-5]
            for relpath in backend.list("packs") if relpath.endswith(".pack")
        }
        for pack_id in index.orphan_packs(present):
            log.warning("pack %s is indexed but missing from the repository",
                        pack_id)
            if not dry_run:
                index.forget_pack(pack_id)
            result.orphans_removed += 1

        codec = ChunkCodec(key=repo._key)
        pack_size = repo.config.pack_size
        writer = PackWriter(target_size=pack_size)
        to_delete: list[tuple[str, int]] = []
        #: Packs written during this run. A pack id is a content hash, so a
        #: rewrite that happens to reproduce an existing pack byte for byte
        #: gets the same id -- and deleting it afterwards would throw away the
        #: copy we just made.
        written_ids: set[str] = set()

        for pack_id, size, total_chunks, live in index.pack_liveness():
            if pack_id not in present:
                continue
            result.packs_examined += 1

            if live == 0:
                to_delete.append((pack_id, size))
                result.packs_deleted += 1
                continue

            if live == total_chunks:
                continue        # nothing dead in here; rewriting frees nothing

            if total_chunks and live / total_chunks > repack_below:
                continue        # enough of it is still in use to leave alone

            # Mostly dead: move the survivors into a fresh pack.
            if dry_run:
                result.packs_repacked += 1
                to_delete.append((pack_id, size))
                continue

            for digest, offset, length in index.live_chunks_in(pack_id):
                blob = backend.read_range(pack_path(pack_id), offset, length)
                # Verify on the way through: repacking is the one moment we
                # touch every surviving chunk, so a bad one should surface
                # here rather than be faithfully copied into the new pack.
                codec.decode(blob, digest)
                writer.add(digest, blob)
                if writer.is_full:
                    _flush(repo, writer, codec, result, written_ids)
            result.packs_repacked += 1
            to_delete.append((pack_id, size))

        if not dry_run and not writer.is_empty:
            _flush(repo, writer, codec, result, written_ids)

        # Only now, with every survivor durably stored and indexed elsewhere.
        for pack_id, size in to_delete:
            if pack_id in written_ids:
                # This run rewrote a pack into something byte-identical, so
                # the "old" and "new" packs are the same file.
                log.debug("pack %s was rewritten to itself; keeping it", pack_id)
                continue
            result.deleted.append(pack_id)
            result.bytes_reclaimed += size
            if not dry_run:
                backend.delete(pack_path(pack_id))
                index.forget_pack(pack_id)
    finally:
        index.drop_live_set()

    return result


def _flush(repo, writer: PackWriter, codec: ChunkCodec, result: GcResult,
           written_ids: set[str]) -> None:
    import hashlib

    blob, entries = writer.finish()
    pack_id = hashlib.blake2b(blob, digest_size=PACK_ID_BYTES).hexdigest()
    written_ids.add(pack_id)
    repo.backend.write(pack_path(pack_id), blob)
    # Rewriting the index entry moves each chunk's recorded home to the new
    # pack, which is what makes deleting the old one safe.
    repo.index.add_pack(pack_id, len(blob), entries)
    writer.reset()
    result.packs_written += 1
