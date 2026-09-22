"""Backing up a VM: snapshot, work out what to read, read it, record a point."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from ..repo.blockmap import BlockMap
from ..repo.codec import chunk_hash
from ..repo.restorepoint import (
    FULL, INCREMENTAL, DiskPoint, PointStore, RestorePoint,
)
from ..transport.base import TransportError
from ..vsphere import cbt
from ..vsphere.connection import VSphereError
from ..vsphere.inventory import describe_vm, resolve_flat_extent
from ..vsphere.snapshot import SnapshotSession

log = logging.getLogger(__name__)

#: How many 1 MiB blocks to request from the datastore in one pipelined batch.
#: The transport is latency-bound, so batching matters more than block size.
READ_BATCH_BLOCKS = 16


@dataclass
class DiskResult:
    key: int
    label: str
    capacity: int
    kind: str
    blocks_total: int = 0
    blocks_read: int = 0
    blocks_inherited: int = 0
    bytes_read: int = 0
    seconds: float = 0.0
    fell_back_to_full: bool = False
    reason: str = ""
    via: str = ""


@dataclass
class BackupResult:
    vm_name: str
    point_id: str
    kind: str
    seconds: float = 0.0
    disks: list[DiskResult] = field(default_factory=list)
    chunks_written: int = 0
    chunks_deduped: int = 0
    bytes_stored: int = 0
    quiesced: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def bytes_read(self) -> int:
        return sum(d.bytes_read for d in self.disks)

    @property
    def capacity(self) -> int:
        return sum(d.capacity for d in self.disks)


class BackupJob:
    """One backup run for one VM.

    :param force_full: read every allocated block even if a usable changeId
        exists. The escape hatch for when CBT is suspected of lying.
    :param enable_cbt: turn CBT on if it is off. Requires a brief stun, which
        a snapshot create/delete provides without downtime.
    """

    def __init__(self, conn, repo, vm_ref: str, *, quiesce: bool = True,
                 enable_cbt: bool = True, force_full: bool = False,
                 read_workers: int = 8, direct_nfs=None,
                 progress: Callable[[str, int, int], None] | None = None):
        #: Optional DirectNfsTransport. Where it covers a disk's datastore it
        #: is preferred over the HTTPS endpoint: it is several times faster and
        #: can see which parts of a sparse disk were never written.
        self.direct_nfs = direct_nfs
        self.conn = conn
        self.repo = repo
        self.vm_ref = vm_ref
        self.quiesce = quiesce
        self.enable_cbt = enable_cbt
        self.force_full = force_full
        self.read_workers = read_workers
        self.progress = progress

    # -- helpers ------------------------------------------------------------

    def _report(self, label: str, done: int, total: int) -> None:
        if self.progress is not None:
            self.progress(label, done, total)

    def _zero_hashes(self, bm: BlockMap, store) -> dict[int, bytes]:
        """Hashes for the all-zero blocks a thin disk is mostly made of.

        Unallocated space is never read from VMware, but every block map entry
        must still name a chunk or the disk cannot be restored. The full-size
        and short-final-block variants hash differently, so both are stored.
        """
        sizes = {bm.chunk_size}
        if bm.count:
            sizes.add(bm.block_range(bm.count - 1)[1])
        out = {}
        for size in sizes:
            out[size] = store.put(b"\0" * size).hash
        return out

    # -- the job ------------------------------------------------------------

    def run(self) -> BackupResult:
        started = time.monotonic()
        vm = self.conn.find_vm(self.vm_ref)
        info = describe_vm(self.conn, vm)
        log.info("%s: %s, %d disk(s), %.1f GiB provisioned, CBT=%s",
                 info.name, info.power_state, len(info.disks),
                 sum(d.capacity for d in info.disks) / 2**30, info.cbt_enabled)

        if not info.disks:
            raise VSphereError(f"{info.name} has no readable virtual disks")
        independent = [d for d in info.disks if d.independent]
        if independent:
            raise VSphereError(
                f"{info.name} has independent disk(s) "
                f"({', '.join(d.label for d in independent)}); snapshots skip "
                "them, so they cannot be captured consistently"
            )

        if self.enable_cbt and not info.cbt_enabled:
            cbt.ensure_enabled(vm, conn=self.conn)
            info = describe_vm(self.conn, vm)

        points = PointStore(self.repo.backend)
        previous = points.latest(info.instance_uuid)
        if previous:
            log.info("%s: previous restore point %s (%s)",
                     info.name, previous.id, previous.created_at)

        store = self.repo.store
        chunk_size = self.repo.config.chunk_size
        http_transport = self.conn.datastore_transport(
            max_workers=self.read_workers)

        def transport_for(datastore: str):
            if self.direct_nfs is not None and self.direct_nfs.handles(datastore):
                return self.direct_nfs, "direct-nfs"
            return http_transport, "datastore-https"

        result = BackupResult(vm_name=info.name, point_id="", kind=FULL)
        blockmaps: dict[int, BlockMap] = {}
        disk_points: list[DiskPoint] = []

        session = SnapshotSession(self.conn, vm, quiesce=self.quiesce)
        snap = session.create()
        snapshot_error: Exception | None = None
        try:
            result.quiesced = snap.quiesced
            for disk in info.disks:
                transport, via = transport_for(disk.datastore)
                log.info("%s %s: reading via %s", info.name, disk.label, via)
                resolve_flat_extent(transport, info.datacenter, disk)
                dr, bm, dp = self._backup_disk(
                    vm, snap, info, disk, previous, points, store,
                    transport, chunk_size,
                )
                dr.via = via
                result.disks.append(dr)
                blockmaps[disk.key] = bm
                disk_points.append(dp)

            # Make the chunks durable while we still hold the snapshot. A slow
            # or failed snapshot removal then costs an operator some cleanup
            # rather than discarding the whole read.
            store.flush()
        finally:
            try:
                session.remove()
            except Exception as exc:
                # The data we read is valid regardless: it came from a frozen
                # snapshot. A leftover snapshot is an operational problem, not
                # a reason to throw away a good backup -- but it grows until
                # someone deals with it, so say so loudly.
                snapshot_error = exc
                log.error(
                    "%s: BACKUP DATA IS GOOD but the snapshot could not be "
                    "removed (%s). Remove it manually before the delta grows.",
                    info.name, exc)

        kind = (INCREMENTAL
                if all(d.kind == INCREMENTAL for d in result.disks) and previous
                else FULL)
        point = RestorePoint.create(
            vm_uuid=info.uuid, vm_instance_uuid=info.instance_uuid,
            vm_name=info.name, kind=kind,
            parent_id=previous.id if previous and kind == INCREMENTAL else None,
            datacenter=info.datacenter, guest_full_name=info.guest_full_name,
            hardware_version=info.hardware_version, power_state=info.power_state,
            quiesced=result.quiesced, disks=disk_points,
        )
        points.save(point, blockmaps)

        result.point_id = point.id
        result.kind = kind
        result.seconds = time.monotonic() - started
        result.chunks_written = store.stats.chunks_written
        result.chunks_deduped = store.stats.chunks_deduped
        result.bytes_stored = store.stats.bytes_stored
        if snapshot_error is not None:
            result.warnings.append(
                f"snapshot left on {info.name}: {snapshot_error}")
        return result

    # -- one disk -----------------------------------------------------------

    def _backup_disk(self, vm, snap, info, disk, previous, points, store,
                     transport, chunk_size):
        started = time.monotonic()
        prev_disk = None
        if previous:
            prev_disk = next((d for d in previous.disks if d.key == disk.key), None)

        device = transport.open(
            datacenter=info.datacenter, datastore=disk.datastore,
            path=disk.flat_path, read_only=True,
        )
        try:
            if device.size != disk.capacity:
                raise VSphereError(
                    f"{disk.describe()}: datastore reports {device.size} bytes "
                    f"but the VM config says {disk.capacity}"
                )

            kind, extents, fell_back, reason = self._extents_to_read(
                vm, snap, disk, prev_disk, previous, chunk_size, device)

            bm, inherited = self._prepare_blockmap(
                disk, prev_disk, previous, points, info, kind, chunk_size, store)

            blocks = sorted({
                i for offset, length in extents
                for i in bm.blocks_for(offset, length)
            })
            log.info("%s %s: %s, %d of %d blocks to read (%.2f GiB)",
                     info.name, disk.label, kind, len(blocks), len(bm),
                     sum(bm.block_range(i)[1] for i in blocks) / 2**30)

            bytes_read = self._read_blocks(device, bm, blocks, store, disk.label)

            dr = DiskResult(
                key=disk.key, label=disk.label, capacity=disk.capacity,
                kind=kind, blocks_total=len(bm), blocks_read=len(blocks),
                blocks_inherited=inherited, bytes_read=bytes_read,
                seconds=time.monotonic() - started,
                fell_back_to_full=fell_back, reason=reason,
            )
            dp = DiskPoint(
                key=disk.key, label=disk.label, capacity=disk.capacity,
                datastore=disk.datastore, descriptor_path=disk.descriptor_path,
                flat_path=disk.flat_path, chunk_size=chunk_size, thin=disk.thin,
                # The changeId recorded here is the snapshot's, not the live
                # VM's: it names exactly the point in time we just read.
                change_id=snap.disks.get(disk.key).change_id
                if snap.disks.get(disk.key) else None,
                bytes_read=bytes_read, blocks_changed=len(blocks),
            )
            return dr, bm, dp
        finally:
            device.close()

    def _extents_to_read(self, vm, snap, disk, prev_disk, previous,
                         chunk_size, device):
        """Decide what to read: changed blocks, allocated blocks, or everything."""
        full_range = [(0, disk.capacity)]

        def narrow_to_allocated(extents, label):
            """Trim a whole-disk answer using filesystem sparseness.

            VMware's allocated-block query reports thin disks on NFS
            datastores as entirely allocated, so on its own it saves nothing.
            When the transport can see the real holes, that is a better answer
            and it is safe to intersect: a region the filesystem has never
            written cannot contain guest data.
            """
            if cbt.total_bytes(extents) < disk.capacity:
                return extents
            allocated = device.allocated_extents()
            if not allocated or cbt.total_bytes(allocated) >= disk.capacity:
                return extents
            log.info("%s: %s covered the whole disk; filesystem reports "
                     "%.2f GiB allocated, using that instead",
                     disk.label, label, cbt.total_bytes(allocated) / 2**30)
            return cbt.merge_extents(allocated)

        if self.force_full:
            return (FULL, narrow_to_allocated(full_range, "a forced full"),
                    False, "forced by request")

        change_id = prev_disk.change_id if prev_disk else None
        if change_id:
            try:
                extents = cbt.query_changed_areas(
                    vm, snap.snapshot, disk.key, change_id, disk.capacity)
                return INCREMENTAL, extents, False, ""
            except cbt.CbtError as exc:
                # A stale changeId is common and not an error condition: power
                # cycles, storage vMotion and disk growth all invalidate one.
                # Reading everything costs time; trusting it costs the data.
                log.warning("%s %s: %s", vm.name, disk.label, exc)
                reason = str(exc)
                try:
                    allocated = cbt.allocated_areas(
                        vm, snap.snapshot, disk.key, disk.capacity)
                    return (FULL, narrow_to_allocated(allocated, "the CBT "
                            "allocated query"), True, reason)
                except cbt.CbtError:
                    return (FULL, narrow_to_allocated(full_range, "no CBT"),
                            True, reason)

        # First backup of this disk. Allocated-block query still saves us from
        # reading space the guest has never written.
        try:
            allocated = cbt.allocated_areas(
                vm, snap.snapshot, disk.key, disk.capacity)
            return (FULL, narrow_to_allocated(allocated, "the CBT allocated "
                                              "query"), False, "")
        except cbt.CbtError as exc:
            log.warning("%s %s: allocated-block query unavailable (%s)",
                        vm.name, disk.label, exc)
            return (FULL, narrow_to_allocated(full_range, "no CBT"),
                    False, str(exc))

    def _prepare_blockmap(self, disk, prev_disk, previous, points, info,
                          kind, chunk_size, store):
        """Start from the parent's map for an incremental, or zeros for a full."""
        if kind == INCREMENTAL and prev_disk and previous:
            try:
                parent = points.load_blockmap(
                    info.instance_uuid, previous.id, disk.key)
            except Exception as exc:
                raise VSphereError(
                    f"{disk.describe()}: cannot read the parent block map for "
                    f"an incremental ({exc})"
                ) from exc
            if parent.chunk_size != chunk_size:
                raise VSphereError(
                    f"{disk.describe()}: parent used {parent.chunk_size}-byte "
                    f"blocks, repository now uses {chunk_size}")
            bm = (parent.clone() if parent.disk_size == disk.capacity
                  else parent.resized_clone(disk.capacity))
            inherited = len(bm)
        else:
            bm = BlockMap(disk.capacity, chunk_size=chunk_size)
            # Unallocated blocks are never read but still need a chunk named,
            # or the disk could not be restored.
            zeros = self._zero_hashes(bm, store)
            for i in range(len(bm)):
                bm[i] = zeros[bm.block_range(i)[1]]
            inherited = 0
        return bm, inherited

    def _read_blocks(self, device, bm, blocks, store, label) -> int:
        bytes_read = 0
        total = len(blocks)
        done = 0
        for start in range(0, total, READ_BATCH_BLOCKS):
            batch = blocks[start:start + READ_BATCH_BLOCKS]
            ranges = [bm.block_range(i) for i in batch]
            try:
                payloads = device.pread_batch(ranges)
            except TransportError as exc:
                raise TransportError(
                    f"{label}: reading blocks {batch[0]}-{batch[-1]} failed: {exc}"
                ) from exc
            for index, payload in zip(batch, payloads):
                bm[index] = store.put(payload).hash
                bytes_read += len(payload)
            done += len(batch)
            self._report(label, done, total)
        return bytes_read
