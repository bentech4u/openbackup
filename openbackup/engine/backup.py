"""Image-level backup of one VM into a repository.

CBT is used, but never trusted blindly: a missing or rejected change id, a
resized disk or a newly added disk all fall back to a full read. A needless
full read costs time; a wrong incremental costs the data.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from ..repo.blockmap import BlockMap
from ..repo.repository import Repository, new_point_id
from ..vsphere.client import SNAPSHOT_PREFIX, VSphereError
from ..vsphere.types import DiskInfo, Extent
from .context import TaskContext, check_cancel


class BackupError(Exception):
    pass


@dataclass
class BackupOptions:
    quiesce: bool = True
    active_full: bool = False
    job_id: int | None = None
    job_name: str = ""
    task_id: int | None = None
    vcenter: str = ""
    active_full_days: int = 0
    read_depth: int = 8


@dataclass
class BackupResult:
    point_id: str
    manifest: dict
    warnings: list[str] = field(default_factory=list)


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def blocks_for_extents(extents: list[Extent], block_size: int, capacity: int) -> list[int]:
    """Indices of every block touched by any extent, in order."""
    blocks: set[int] = set()
    last = (capacity - 1) // block_size
    for e in extents:
        if e.length <= 0:
            continue
        first = e.start // block_size
        end = min((e.start + e.length - 1) // block_size, last)
        blocks.update(range(first, end + 1))
    return sorted(blocks)


def latest_point_for_vm(repo: Repository, instance_uuid: str) -> dict | None:
    best = None
    for m in repo.list_points():
        if m.get("vm", {}).get("uuid") != instance_uuid:
            continue
        if best is None or m["created_at"] > best["created_at"]:
            best = m
    return best


def needs_active_full(prev: dict | None, days: int, now: datetime) -> bool:
    if prev is None or days <= 0:
        return False
    last_full = prev.get("last_full_at") or prev["created_at"]
    return now - datetime.fromisoformat(last_full) >= timedelta(days=days)


def backup_vm(source: Any, repo: Repository, vm_moref: str, opts: BackupOptions,
              ctx: TaskContext) -> BackupResult:
    t0 = time.monotonic()
    vm = source.get_vm(vm_moref)
    config = source.capture_config(vm)
    name, uuid = config["name"], config["instance_uuid"]
    warnings: list[str] = []

    def warn(msg: str) -> None:
        warnings.append(msg)
        ctx.log(f"{name}: {msg}", "warning")

    # Leftovers from a run that died before cleaning up would otherwise pile
    # up and slow the VM down.
    for snap in source.find_own_snapshots(vm):
        ctx.log(f"{name}: removing a leftover snapshot from an earlier run")
        try:
            source.remove_snapshot(snap)
        except VSphereError as e:
            warn(f"could not remove leftover snapshot: {e}")

    if source.ensure_cbt(vm):
        ctx.log(f"{name}: enabled Changed Block Tracking")
        # CBT only becomes active after the VM is stunned once.
        snap, _ = source.create_snapshot(vm, f"{SNAPSHOT_PREFIX}cbt-activate", False)
        try:
            source.remove_snapshot(snap)
        except VSphereError as e:
            warn(f"could not remove the CBT activation snapshot: {e}")

    prev = latest_point_for_vm(repo, uuid)
    now = datetime.now(UTC)
    active_full = opts.active_full
    if not active_full and needs_active_full(prev, opts.active_full_days, now):
        ctx.log(f"{name}: periodic active full is due")
        active_full = True
    check_cancel(ctx)

    ctx.log(f"{name}: creating snapshot (quiesce={'on' if opts.quiesce else 'off'})")
    snap_name = f"{SNAPSHOT_PREFIX}{opts.task_id or 'manual'}-{int(time.time())}"
    snap, sref = source.create_snapshot(vm, snap_name, opts.quiesce)
    point_id = new_point_id()
    maps: dict[str, BlockMap] = {}
    disk_records: list[dict] = []
    totals = {"logical": 0, "read": 0}
    any_full = False
    writer = repo.writer()
    try:
        disks = list(sref.disks)
        for d in disks:
            if d.independent:
                warn(f"disk {d.label} is independent and is excluded from snapshots; skipped")
        disks = [d for d in disks if not d.independent]
        if not disks:
            raise BackupError(f"{name} has no disks that can be backed up")
        # Plan every disk first (CBT queries are quick) so progress has a
        # meaningful total: the bytes that will actually be read.
        plans = []
        for d in disks:
            check_cancel(ctx)
            src = VmDisk(source, vm, snap, vm_moref, sref.moref, d)
            prev_disk = None
            if prev is not None and not active_full:
                prev_disk = next((p for p in prev.get("disks", []) if p["key"] == d.key), None)
            m, extents, mode = plan_disk(
                src, prev_disk, lambda k=src.key: repo.load_map(prev["id"], k), ctx, name, warn)
            blocks = blocks_for_extents(extents, m.block_size, d.capacity)
            plans.append((d, src, m, mode, blocks, sum(m.block_length(i) for i in blocks)))
        ctx.progress(0.0, total=sum(p[5] for p in plans))

        for d, src, m, mode, blocks, to_read in plans:
            check_cancel(ctx)
            any_full = any_full or mode != "incremental"
            item = f"{name} / {d.label}"
            ctx.item(item, vm=name, disk=d.label, mode=mode, capacity=d.capacity,
                     to_read=to_read, read=0, state="running")
            ctx.log(f"{name}: {d.label} {mode}, reading {human(to_read)} of {human(d.capacity)}")
            read = copy_blocks(src, m, blocks, writer, ctx, item, opts.read_depth)
            ctx.item(item, read=read, state="done")
            maps[str(d.key)] = m
            totals["logical"] += d.capacity
            totals["read"] += read
            disk_records.append({**d.to_dict(), "mode": mode, "read_bytes": read})

        check_cancel(ctx)
        writer.close()
        manifest = {
            "created_at": now.isoformat(),
            "kind": "full" if any_full else "incremental",
            "last_full_at": now.isoformat() if any_full else
            (prev or {}).get("last_full_at", now.isoformat()),
            "vm": {"moref": vm_moref, "name": name, "uuid": uuid},
            "vcenter": opts.vcenter,
            "job_id": opts.job_id,
            "job_name": opts.job_name,
            "task_id": opts.task_id,
            "config": config,
            "disks": disk_records,
            "logical_bytes": totals["logical"],
            "read_bytes": totals["read"],
            "new_bytes": writer.new_bytes,
            "duration_s": round(time.monotonic() - t0, 1),
            "warnings": warnings,
        }
        repo.save_point(point_id, manifest, maps)
    except BaseException:
        writer.abort()
        raise
    finally:
        try:
            ctx.log(f"{name}: removing snapshot")
            source.remove_snapshot(snap)
        except Exception as e:  # never mask the real error
            warn(f"snapshot {snap_name} could not be removed: {e}. It will be retried on "
                 "the next run; remove it manually if the VM is no longer backed up.")

    ctx.log(f"{name}: restore point {point_id} created ({manifest['kind']}, read "
            f"{human(totals['read'])}, stored {human(writer.new_bytes)} new)")
    manifest["id"] = point_id
    return BackupResult(point_id, manifest, warnings)


class DiskSource(Protocol):
    """One disk to back up, wherever it comes from (a VM disk under a VM
    snapshot, a First Class Disk under an FCD snapshot...)."""

    key: str
    label: str
    capacity: int

    def changed_areas(self, change_id: str) -> list[Extent]:
        """Changed extents since ``change_id``; "*" means all allocated areas.
        Raises VSphereError (or OSError) when the answer cannot be had."""

    def allocated_extents(self) -> list[tuple[int, int]] | None:
        """Allocated regions from the storage itself, or None if unknown."""

    def open(self) -> AbstractContextManager[Any]:
        """A reader with ``size``, ``pread_many`` and optionally ``description``."""


class VmDisk:
    """A VM disk, read under the VM's backup snapshot."""

    def __init__(self, source, vm, snap, vm_moref: str, snap_moref: str, d: DiskInfo):
        self.source, self.vm, self.snap = source, vm, snap
        self.vm_moref, self.snap_moref, self.d = vm_moref, snap_moref, d
        self.key, self.label, self.capacity = str(d.key), d.label, d.capacity

    def changed_areas(self, change_id: str) -> list[Extent]:
        return self.source.changed_areas(self.vm, self.snap, self.d, change_id)

    def allocated_extents(self) -> list[tuple[int, int]] | None:
        alloc = getattr(self.source, "allocated_extents", None)
        return alloc(self.d) if alloc is not None else None

    def open(self):
        return self.source.open_disk(self.vm_moref, self.d, snapshot_moref=self.snap_moref)


def plan_disk(src: DiskSource, prev_disk: dict | None, load_prev_map: Callable[[], BlockMap],
              ctx: TaskContext, name: str, warn: Callable[[str], None]
              ) -> tuple[BlockMap, list[Extent], str]:
    """What to read from ``src``: CBT changes on top of the previous block map
    when that can be trusted, otherwise everything allocated."""
    if prev_disk is not None:
        reason = None
        if prev_disk["capacity"] != src.capacity:
            reason = "disk was resized"
        elif not prev_disk.get("change_id"):
            reason = "previous backup has no change id"
        if reason is None:
            try:
                extents = src.changed_areas(prev_disk["change_id"])
                return load_prev_map().copy(), extents, "incremental"
            except (VSphereError, OSError) as e:
                reason = f"CBT could not be used ({e})"
        warn(f"{src.label}: {reason}; reading the full disk")

    found = src.allocated_extents()
    if found is not None:
        return BlockMap(src.capacity), [Extent(o, n) for o, n in found], "full"
    try:
        return BlockMap(src.capacity), src.changed_areas("*"), "full"
    except VSphereError as e:
        ctx.log(f"{name}: {src.label}: allocation query unavailable ({e}); reading every block",
                "warning")
        return BlockMap(src.capacity), [Extent(0, src.capacity)], "full"


def copy_blocks(src: DiskSource, m: BlockMap, blocks: list[int], writer, ctx: TaskContext,
                item: str, depth: int = 8) -> int:
    # The disk is opened even when no blocks changed: opening is what checks
    # its size and, for direct NFS, that it is a current base disk.
    read = reported_read = 0
    reported_new = writer.new_bytes
    last_report = time.monotonic()

    def report() -> None:
        nonlocal reported_read, reported_new
        ctx.progress(read=read - reported_read, written=writer.new_bytes - reported_new)
        reported_read, reported_new = read, writer.new_bytes
        ctx.item(item, read=read)

    with src.open() as reader:
        if getattr(reader, "description", ""):
            ctx.log(f"{item}: reading via {reader.description}")
        if reader.size != src.capacity:
            raise BackupError(f"{src.label}: disk source is {reader.size} bytes, vSphere "
                              f"reports {src.capacity}")
        reqs = ((i * m.block_size, m.block_length(i)) for i in blocks)
        for i, data in zip(blocks, reader.pread_many(reqs, depth=depth), strict=True):
            m.ids[i] = writer.put(data)
            read += len(data)
            if time.monotonic() - last_report > 2:
                check_cancel(ctx)
                report()
                last_report = time.monotonic()
    report()
    return read
