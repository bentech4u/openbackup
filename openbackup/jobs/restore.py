"""Restoring a disk from a restore point.

Every restore point names every block it needs, so restoring never walks a
chain of increments -- it reads one block map and fetches the chunks it names.
An incremental is as cheap to restore as a full.

Two targets:

* :class:`RestoreToFile` rebuilds a flat VMDK plus its descriptor on any
  filesystem. It is the fast, safe path: nothing in vSphere is touched, and
  the result can be imported or compared against the original.
* :class:`RestoreToVm` creates a new VM and streams the disks into it. It
  never writes to the VM that was backed up; restoring over live data is a
  separate decision an operator should have to make explicitly.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from ..repo.blockmap import BlockMap
from ..repo.codec import ChunkMissing, chunk_hash
from ..repo.restorepoint import DiskPoint, PointStore, RestorePoint

log = logging.getLogger(__name__)


class RestoreError(Exception):
    pass


@dataclass
class DiskRestoreResult:
    label: str
    path: str
    size: int
    blocks: int = 0
    blocks_written: int = 0
    blocks_skipped_zero: int = 0
    bytes_written: int = 0
    seconds: float = 0.0


@dataclass
class RestoreResult:
    point_id: str
    vm_name: str
    target: str
    seconds: float = 0.0
    disks: list[DiskRestoreResult] = field(default_factory=list)

    @property
    def bytes_written(self) -> int:
        return sum(d.bytes_written for d in self.disks)


def disk_stream(store, bm: BlockMap) -> Iterator[bytes]:
    """Yield a disk's contents, block by block, from the chunk store."""
    for index in range(len(bm)):
        _offset, length = bm.block_range(index)
        digest = bm[index]
        try:
            data = store.get(digest)
        except ChunkMissing as exc:
            raise RestoreError(
                f"block {index} needs chunk {digest.hex()}, which is not in "
                f"the repository; the backup is incomplete"
            ) from exc
        if len(data) != length:
            raise RestoreError(
                f"block {index} should be {length} bytes but the stored chunk "
                f"is {len(data)}"
            )
        yield data


def descriptor_for(disk: DiskPoint, extent_name: str) -> bytes:
    """A minimal VMDK descriptor pointing at a restored flat extent."""
    sectors = disk.capacity // 512
    return (
        "# Disk DescriptorFile\n"
        "version=1\n"
        'encoding="UTF-8"\n'
        f"CID={os.urandom(4).hex()}\n"
        "parentCID=ffffffff\n"
        'createType="vmfs"\n'
        "\n"
        "# Extent description\n"
        f'RW {sectors} VMFS "{extent_name}"\n'
        "\n"
        "# The Disk Data Base\n"
        "#DDB\n"
        "\n"
        'ddb.adapterType = "lsilogic"\n'
        f'ddb.thinProvisioned = "{1 if disk.thin else 0}"\n'
    ).encode()


class RestoreToFile:
    """Rebuild disks as flat VMDK files on a filesystem.

    Unwritten regions are skipped rather than written as zeros, so the result
    is a sparse file: a 350 GiB disk holding 13 GiB of data occupies 13 GiB.
    That also makes the restore far faster than its logical size suggests.
    """

    def __init__(self, repo, point: RestorePoint, target_dir: Path | str, *,
                 write_descriptors: bool = True,
                 progress: Callable[[str, int, int], None] | None = None):
        self.repo = repo
        self.point = point
        self.target_dir = Path(target_dir)
        self.write_descriptors = write_descriptors
        self.progress = progress

    def run(self) -> RestoreResult:
        started = time.monotonic()
        points = PointStore(self.repo.backend)
        self.target_dir.mkdir(parents=True, exist_ok=True)
        result = RestoreResult(point_id=self.point.id, vm_name=self.point.vm_name,
                               target=str(self.target_dir))

        for disk in self.point.disks:
            bm = points.load_blockmap(
                self.point.vm_instance_uuid, self.point.id, disk.key)
            if bm.disk_size != disk.capacity:
                raise RestoreError(
                    f"{disk.label}: block map covers {bm.disk_size} bytes but "
                    f"the restore point says {disk.capacity}")
            base = f"{self.point.vm_name}-disk{disk.key}"
            flat_name = f"{base}-flat.vmdk"
            flat_path = self.target_dir / flat_name
            result.disks.append(
                self._restore_disk(disk, bm, flat_path, base, flat_name))

        result.seconds = time.monotonic() - started
        return result

    def _restore_disk(self, disk: DiskPoint, bm: BlockMap, flat_path: Path,
                      base: str, flat_name: str) -> DiskRestoreResult:
        started = time.monotonic()
        store = self.repo.store
        dr = DiskRestoreResult(label=disk.label, path=str(flat_path),
                               size=disk.capacity, blocks=len(bm))

        # Precompute the zero-block hashes so unwritten regions can be skipped
        # without fetching and comparing them.
        zero_hashes = set()
        sizes = {bm.chunk_size}
        if bm.count:
            sizes.add(bm.block_range(bm.count - 1)[1])
        for size in sizes:
            zero_hashes.add(chunk_hash(b"\0" * size))

        fd = os.open(flat_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        try:
            # Set the full length up front; holes stay holes until written.
            os.ftruncate(fd, disk.capacity)
            for index in range(len(bm)):
                offset, length = bm.block_range(index)
                digest = bm[index]
                if digest in zero_hashes:
                    dr.blocks_skipped_zero += 1
                else:
                    try:
                        data = store.get(digest)
                    except ChunkMissing as exc:
                        raise RestoreError(
                            f"{disk.label}: block {index} needs chunk "
                            f"{digest.hex()}, missing from the repository"
                        ) from exc
                    if len(data) != length:
                        raise RestoreError(
                            f"{disk.label}: block {index} should be {length} "
                            f"bytes, stored chunk is {len(data)}")
                    written = 0
                    while written < len(data):
                        written += os.pwrite(fd, data[written:], offset + written)
                    dr.blocks_written += 1
                    dr.bytes_written += len(data)
                if self.progress and index % 1024 == 0:
                    self.progress(disk.label, index, len(bm))
            os.fsync(fd)
        finally:
            os.close(fd)

        if self.write_descriptors:
            (flat_path.parent / f"{base}.vmdk").write_bytes(
                descriptor_for(disk, flat_name))

        if self.progress:
            self.progress(disk.label, len(bm), len(bm))
        dr.seconds = time.monotonic() - started
        return dr


class VerifyResult:
    def __init__(self, point_id: str):
        self.point_id = point_id
        self.blocks = 0
        self.chunks_checked = 0
        self.bytes_checked = 0
        self.missing: list[str] = []
        self.corrupt: list[str] = []
        self.seconds = 0.0

    @property
    def ok(self) -> bool:
        return not self.missing and not self.corrupt


def verify_point(repo, point: RestorePoint, *,
                 progress: Callable[[str, int, int], None] | None = None
                 ) -> VerifyResult:
    """Check every chunk a restore point needs is present and intact.

    Reading a chunk re-hashes it, so this catches bitrot in the repository
    before someone discovers it during an actual restore. Distinct chunks are
    checked once, which matters because a mostly-empty disk references the
    same zero chunk many thousands of times.
    """
    started = time.monotonic()
    points = PointStore(repo.backend)
    result = VerifyResult(point.id)
    store = repo.store

    for disk in point.disks:
        bm = points.load_blockmap(
            point.vm_instance_uuid, point.id, disk.key)
        result.blocks += len(bm)
        unique = bm.distinct_hashes()
        for n, digest in enumerate(unique):
            try:
                data = store.get(digest)
                result.chunks_checked += 1
                result.bytes_checked += len(data)
            except ChunkMissing:
                result.missing.append(digest.hex())
            except Exception as exc:
                result.corrupt.append(f"{digest.hex()}: {exc}")
            if progress and n % 512 == 0:
                progress(disk.label, n, len(unique))
        if progress:
            progress(disk.label, len(unique), len(unique))

    result.seconds = time.monotonic() - started
    return result
