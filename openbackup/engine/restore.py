"""Restore a point: as a new VM, over the original VM, or as disk image files."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..repo.blockmap import BlockMap
from ..repo.crypto import ZERO_ID
from ..repo.repository import Repository
from ..vsphere.types import DiskInfo
from .context import TaskContext, check_cancel


class RestoreError(Exception):
    pass


@dataclass
class NewVmTarget:
    name: str
    folder: str
    resource_pool: str
    datastore: str
    host: str | None = None
    network_map: dict[str, str] = field(default_factory=dict)
    power_on: bool = False


def zero_runs(m: BlockMap) -> list[tuple[int, int]]:
    """(offset, length) of each run of zero blocks."""
    runs: list[tuple[int, int]] = []
    for i, cid in enumerate(m.ids):
        if cid != ZERO_ID:
            continue
        off, length = i * m.block_size, m.block_length(i)
        if runs and runs[-1][0] + runs[-1][1] == off:
            runs[-1] = (runs[-1][0], runs[-1][1] + length)
        else:
            runs.append((off, length))
    return runs


class _Progress:
    def __init__(self, ctx: TaskContext, item: str):
        self.ctx, self.item = ctx, item
        self.done = self.reported = 0
        self.last = time.monotonic()

    def add(self, n: int) -> None:
        self.done += n
        if time.monotonic() - self.last > 2:
            check_cancel(self.ctx)
            self.flush()

    def flush(self) -> None:
        self.ctx.progress(written=self.done - self.reported)
        self.ctx.item(self.item, written=self.done)
        self.reported, self.last = self.done, time.monotonic()


def _write_disk(source: Any, repo: Repository, vm_moref: str, disk: DiskInfo, m: BlockMap,
                ctx: TaskContext, item: str, skip_zero: bool) -> int:
    """Write a block map onto a VM disk. ``skip_zero`` is only safe when the
    target disk is known to read as zeros (a freshly created disk)."""
    if disk.capacity < m.capacity:
        raise RestoreError(f"{disk.label} is smaller than the backed-up disk")
    prog = _Progress(ctx, item)
    with source.open_disk(vm_moref, disk, write=True) as nbd:
        if nbd.read_only:
            raise RestoreError("disk was opened read-only")
        if not skip_zero:
            # Overwriting an existing disk: zero regions must be written too, or
            # stale data survives. Done before the pipelined data writes so
            # replies never interleave.
            for off, length in zero_runs(m):
                nbd.write_zeroes(off, length)
                prog.add(length)

        def writes():
            for i, cid in enumerate(m.ids):
                if cid == ZERO_ID:
                    continue
                length = m.block_length(i)
                yield i * m.block_size, repo.read_chunk(cid, length)
                prog.add(length)

        nbd.pwrite_many(writes())
        nbd.flush()
    prog.flush()
    ctx.item(item, state="done")
    return prog.done


def restore_new_vm(source: Any, repo: Repository, point_id: str, target: NewVmTarget,
                   ctx: TaskContext) -> str:
    manifest = repo.load_manifest(point_id)
    config = dict(manifest["config"])
    backed_up = {d["key"] for d in manifest["disks"]}
    # Only recreate disks that were actually backed up.
    config["disks"] = [d for d in config["disks"] if d["key"] in backed_up]
    can_write = getattr(source, "can_write", None)
    if can_write is not None and not can_write(target.datastore):
        raise RestoreError(
            f"Datastore {target.datastore} has no direct NFS access configured, so the restored "
            "disks cannot be written. Choose an NFS datastore that has it (vCenters page > "
            "Datastores).")
    ctx.progress(0.0, total=sum(d["capacity"] for d in config["disks"]))
    ctx.log(f"Creating VM {target.name} on datastore {target.datastore}")
    moref, mapping = source.create_vm(
        config, name=target.name, folder=target.folder, resource_pool=target.resource_pool,
        datastore=target.datastore, host=target.host, network_map=target.network_map)
    ctx.log(f"Created {target.name} ({moref}); writing {len(mapping)} disk(s)")
    for d in config["disks"]:
        check_cancel(ctx)
        m = repo.load_map(point_id, str(d["key"]))
        new_disk = mapping[d["key"]]
        item = f"{target.name} / {new_disk.label}"
        ctx.item(item, disk=new_disk.label, capacity=m.capacity, written=0, state="running")
        _write_disk(source, repo, moref, new_disk, m, ctx, item, skip_zero=True)
    if target.power_on:
        ctx.log(f"Powering on {target.name}")
        source.power_on(source.get_vm(moref))
    ctx.log(f"Restore of {manifest['vm']['name']} as {target.name} finished")
    return moref


def restore_in_place(source: Any, repo: Repository, point_id: str, ctx: TaskContext,
                     vm_moref: str | None = None, power_on: bool = False) -> None:
    """Roll the original VM back to the restore point. The VM is powered off;
    its disks must match the backup in number and size, and it must have no
    snapshots (writing a base disk under a snapshot would corrupt it).

    Quick rollback: CBT says which blocks changed since the backup, and only
    those are written back. If CBT cannot answer, every block is compared and
    only differing ones are written. Either way thin disks stay thin."""
    manifest = repo.load_manifest(point_id)
    moref = vm_moref or manifest["vm"]["moref"]
    name = manifest["vm"]["name"]
    vm = source.get_vm(moref)
    current = {d.key: d for d in source.vm_disks(vm)}
    cfg = source.capture_config(vm)
    if cfg["instance_uuid"] != manifest["vm"]["uuid"]:
        raise RestoreError("The VM at the original location is not the one that was backed up")
    if source.find_own_snapshots(vm) or getattr(vm, "snapshot", None):
        raise RestoreError("The VM has snapshots; delete them before restoring in place")
    for d in manifest["disks"]:
        cur = current.get(d["key"])
        if cur is None or cur.capacity != d["capacity"]:
            raise RestoreError(f"Disk {d['label']} no longer matches the backup; "
                               "restore to a new VM instead")
        can_write = getattr(source, "can_write", None)
        if can_write is not None and not can_write(cur.datastore):
            raise RestoreError(f"Datastore {cur.datastore} of {d['label']} has no direct NFS "
                               "access configured, so the disk cannot be written")
    ctx.progress(0.0, total=sum(d["capacity"] for d in manifest["disks"]))
    ctx.log(f"Powering off {name}")
    source.power_off(vm)
    try:
        for d in manifest["disks"]:
            check_cancel(ctx)
            m = repo.load_map(point_id, str(d["key"]))
            item = f"{name} / {d['label']}"
            ctx.item(item, disk=d["label"], capacity=m.capacity, written=0, state="running")
            _rollback_disk(source, repo, vm, moref, current[d["key"]], m, d, ctx, item)
    finally:
        # Our writes bypassed ESXi, so CBT did not see them; the change IDs the
        # last backup recorded no longer describe the disk. Reset CBT so the
        # next backup reads everything instead of trusting them.
        try:
            source.reset_cbt(vm)
            ctx.log(f"{name}: Changed Block Tracking reset; its next backup will be a full")
        except Exception as e:
            ctx.log(f"{name}: could not reset Changed Block Tracking ({e}). Run its backup "
                    "job with 'Run active full' next, or the next incremental will be wrong.",
                    "error")
    if power_on:
        source.power_on(vm)
    ctx.log(f"In-place restore of {name} finished")


def _rollback_disk(source, repo: Repository, vm, moref: str, disk: DiskInfo, m: BlockMap,
                   rec: dict, ctx: TaskContext, item: str) -> None:
    from .backup import blocks_for_extents

    blocks: list[int] | None = None
    if rec.get("change_id"):
        try:
            extents = source.changed_areas(vm, None, disk, rec["change_id"])
            blocks = blocks_for_extents(extents, m.block_size, m.capacity)
            ctx.log(f"{item}: quick rollback, {len(blocks)} changed block(s) since the backup")
        except Exception as e:
            ctx.log(f"{item}: CBT cannot say what changed ({e}); comparing every block",
                    "warning")
    if blocks is None:
        ctx.log(f"{item}: comparing all {m.block_count} block(s) with the backup")
    prog = _Progress(ctx, item)
    written = 0
    with source.open_disk(moref, disk, write=True) as w:
        if w.read_only:
            raise RestoreError("disk was opened read-only")
        for i in (blocks if blocks is not None else range(m.block_count)):
            if i % 256 == 0:
                check_cancel(ctx)
            length = m.block_length(i)
            off = i * m.block_size
            cid = m.ids[i]
            if blocks is None:  # compare mode: only write what differs
                cur = w.pread(off, length)
                if cid == ZERO_ID:
                    if cur.count(0) == len(cur):
                        prog.add(length)
                        continue
                elif repo.codec.chunk_id(cur) == cid:
                    prog.add(length)
                    continue
            if cid == ZERO_ID:
                w.write_zeroes(off, length)
            else:
                w.pwrite(off, repo.read_chunk(cid, length))
            written += length
            prog.add(length)
        w.flush()
    prog.flush()
    ctx.item(item, state="done", rewritten=written)
    ctx.log(f"{item}: {written / 2**20:.0f} MiB written back")


def export_disks(repo: Repository, point_id: str, dest: Path, fmt: str, ctx: TaskContext
                 ) -> list[Path]:
    """Write each disk as a sparse raw image, then convert if asked."""
    if fmt not in ("raw", "vmdk", "qcow2"):
        raise RestoreError(f"Unsupported format {fmt}")
    manifest = repo.load_manifest(point_id)
    dest.mkdir(parents=True, exist_ok=True)
    ctx.progress(0.0, total=sum(d["capacity"] for d in manifest["disks"]))
    out = []
    base = "".join(c if c.isalnum() or c in "-_." else "_" for c in manifest["vm"]["name"])
    for d in manifest["disks"]:
        check_cancel(ctx)
        m = repo.load_map(point_id, str(d["key"]))
        raw = dest / f"{base}-{d['key']}.img"
        ctx.log(f"Writing {d['label']} to {raw}")
        prog = _Progress(ctx, d["label"])
        with open(raw, "wb") as f:
            f.truncate(m.capacity)
            for i, cid in enumerate(m.ids):
                if cid == ZERO_ID:
                    continue
                length = m.block_length(i)
                f.seek(i * m.block_size)
                f.write(repo.read_chunk(cid, length))
                prog.add(length)
            f.flush()
            os.fsync(f.fileno())
        prog.flush()
        if fmt == "raw":
            out.append(raw)
            continue
        if shutil.which("qemu-img") is None:
            raise RestoreError("qemu-img is required for vmdk/qcow2 export")
        target = raw.with_suffix(f".{fmt}")
        ctx.log(f"Converting to {fmt}")
        opts = ["-o", "subformat=streamOptimized"] if fmt == "vmdk" else []
        proc = subprocess.run(["qemu-img", "convert", "-f", "raw", "-O", fmt, *opts, str(raw),
                               str(target)], capture_output=True, text=True)
        raw.unlink()
        if proc.returncode != 0:
            raise RestoreError(f"qemu-img failed: {proc.stderr.strip()}")
        out.append(target)
    ctx.log(f"Exported {len(out)} disk(s) to {dest}")
    return out
