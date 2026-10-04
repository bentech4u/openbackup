"""Persistent volume data for namespace backups (vSphere CSI volumes).

Each volume is a First Class Disk. All of a namespace's volumes are
snapshotted together (KubeVirt guests frozen around that moment when
asked), the frozen base disks are read straight from the NAS over the
read-only datastore mount, and the snapshots are always deleted. CBT gives
incrementals, with the same fallbacks to full reads as VM disks.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ..engine.backup import blocks_for_extents, copy_blocks, human, plan_disk
from ..engine.context import TaskContext, check_cancel
from ..repo.blockmap import BlockMap
from ..vsphere.client import SNAPSHOT_PREFIX, VSphereError
from ..vsphere.types import DiskInfo, Extent
from .inventory import VSPHERE_CSI


def disk_key(pvc: str) -> str:
    return f"pvc-{pvc}"


class FcdDisk:
    """DiskSource for one FCD under its backup snapshot."""

    def __init__(self, fcd, open_flat: Callable[[DiskInfo], Any], fcd_id: str, snap_id: str,
                 info, pvc: str):
        self.fcd, self.open_flat = fcd, open_flat
        self.fcd_id, self.snap_id, self.info = fcd_id, snap_id, info
        self.key, self.label, self.capacity = disk_key(pvc), pvc, info.capacity
        # The base disk as it was before our snapshot; frozen while it exists.
        self.disk = DiskInfo(key=0, label=pvc, capacity=info.capacity, file=info.file,
                             datastore=info.datastore)

    def changed_areas(self, change_id: str) -> list[Extent]:
        return self.fcd.changed_areas(self.fcd_id, self.snap_id, self.capacity, change_id)

    def allocated_extents(self) -> list[tuple[int, int]] | None:
        with self.open() as r:
            alloc = getattr(r, "allocated_extents", None)
            return alloc() if alloc else None

    def open(self):
        flat = self.open_flat(self.disk)
        if flat is None:
            raise VSphereError(f"no direct NFS access to datastore {self.info.datastore}")
        return flat


def vm_claims(captured: dict) -> dict[str, list[str]]:
    """KubeVirt VM name -> the PVC names its disks use."""
    out: dict[str, list[str]] = {}
    for o in captured["objects"]:
        if o["kind"] != "VirtualMachine":
            continue
        vols = (((o.get("spec") or {}).get("template") or {}).get("spec") or {}).get(
            "volumes") or []
        claims = []
        for v in vols:
            if "persistentVolumeClaim" in v:
                claims.append(v["persistentVolumeClaim"].get("claimName"))
            elif "dataVolume" in v:
                claims.append(v["dataVolume"].get("name"))  # a DataVolume's PVC shares its name
        out[o["metadata"]["name"]] = [c for c in claims if c]
    return out


def volume_backup(fcd, open_flat, prev_manifest: dict | None, load_prev_map,
                  ctx: TaskContext, name: str, *, active_full: bool = False,
                  freeze: Callable[[str], None] | None = None,
                  unfreeze: Callable[[str], None] | None = None,
                  task_id: int | None = None):
    """Build the ``volumes`` callable for kube.engine.backup_namespace."""

    def run(captured: dict, writer) -> tuple[dict[str, BlockMap], list[dict], list[str]]:
        warnings: list[str] = []

        def warn(msg: str) -> None:
            warnings.append(msg)
            ctx.log(f"{name}: {msg}", "warning")

        targets = []
        for o in captured["objects"]:
            if o["kind"] != "PersistentVolumeClaim":
                continue
            pvc = o["metadata"]["name"]
            pv = captured["pvs"].get(pvc)
            csi = ((pv or {}).get("spec") or {}).get("csi") or {}
            if not pv:
                warn(f"volume claim {pvc} is not bound; no data to back up")
            elif csi.get("driver") != VSPHERE_CSI:
                warn(f"volume claim {pvc} uses {csi.get('driver') or 'a non-CSI volume'}; "
                     "only its definition is backed up, not its data")
            elif fcd is None:
                warn(f"volume claim {pvc}: the cluster has no vCenter configured; only its "
                     "definition is backed up")
            else:
                targets.append((pvc, csi["volumeHandle"], o))
        if not targets:
            return {}, [], warnings

        infos = {}
        for pvc, fcd_id, _o in targets:
            check_cancel(ctx)
            for leftover in fcd.own_snapshots(fcd_id):
                ctx.log(f"{name}: {pvc}: removing a leftover snapshot from an earlier run")
                try:
                    fcd.delete_snapshot(fcd_id, leftover)
                except VSphereError as e:
                    warn(f"{pvc}: could not remove leftover snapshot: {e}")
            info = fcd.info(fcd_id)
            # Check the disk can be read directly, and is a current base disk,
            # before any snapshot exists.
            base = DiskInfo(key=0, label=pvc, capacity=info.capacity, file=info.file,
                            datastore=info.datastore)
            reader = open_flat(base)
            if reader is None:
                warn(f"volume claim {pvc}: datastore {info.datastore} has no direct NFS access "
                     "configured; only its definition is backed up")
                continue
            reader.close()
            if not info.cbt:
                try:
                    fcd.enable_cbt(fcd_id)
                    ctx.log(f"{name}: {pvc}: enabled Changed Block Tracking")
                except VSphereError as e:
                    # Typical for a volume attached to a running node: its CBT
                    # follows the node VM. Back it up anyway, reading it whole.
                    warn(f"{pvc}: Changed Block Tracking could not be enabled ({e}); this "
                         "volume is read in full on every run. Enable CBT on the OpenShift "
                         "node VMs for incremental volume backups.")
            infos[fcd_id] = info
        targets = [t for t in targets if t[1] in infos]
        if not targets:
            return {}, [], warnings

        # Snapshot everything at once, with KubeVirt guests frozen around it.
        claims = vm_claims(captured)
        wanted = {p for p, _i, _o in targets}
        to_freeze = [vm for vm, cl in claims.items() if wanted & set(cl)]
        frozen: list[str] = []
        snaps: dict[str, str] = {}
        maps: dict[str, BlockMap] = {}
        disks: list[dict] = []
        desc = f"{SNAPSHOT_PREFIX}{task_id or 'manual'}-{int(time.time())}"
        # One try/finally from the first snapshot on: whatever fails, every
        # snapshot taken so far is deleted.
        try:
            try:
                if freeze is not None:
                    for vm in to_freeze:
                        try:
                            freeze(vm)
                            frozen.append(vm)
                        except Exception as e:  # a stopped VM or no guest agent
                            warn(f"VM {vm} could not be frozen ({e}); its disks are "
                                 "crash-consistent")
                elif to_freeze:
                    ctx.log(f"{name}: VMs {', '.join(to_freeze)} are not frozen; their disks "
                            "are crash-consistent")
                for _pvc, fcd_id, _o in targets:
                    snaps[fcd_id] = fcd.create_snapshot(fcd_id, desc)
            finally:
                for vm in frozen:
                    try:
                        unfreeze(vm)
                    except Exception as e:
                        warn(f"VM {vm} could not be unfrozen: {e}. It thaws by itself after "
                             "the freeze timeout.")

            plans = []
            for pvc, fcd_id, o in targets:
                info = infos[fcd_id]
                src = FcdDisk(fcd, open_flat, fcd_id, snaps[fcd_id], info, pvc)
                prev_disk = None
                if prev_manifest and not active_full:
                    prev_disk = next((d for d in prev_manifest.get("disks", [])
                                      if d["key"] == src.key and d.get("fcd_id") == fcd_id),
                                     None)
                m, extents, mode = plan_disk(src, prev_disk,
                                             lambda k=src.key: load_prev_map(k), ctx, name, warn)
                blocks = blocks_for_extents(extents, m.block_size, src.capacity)
                plans.append((pvc, fcd_id, o, src, m, mode, blocks,
                              sum(m.block_length(i) for i in blocks)))
            ctx.progress(0.0, total=sum(p[7] for p in plans))
            for pvc, fcd_id, o, src, m, mode, blocks, to_read in plans:
                check_cancel(ctx)
                item = f"{name} / {pvc}"
                ctx.item(item, disk=pvc, mode=mode, capacity=src.capacity, to_read=to_read,
                         read=0, state="running")
                ctx.log(f"{name}: volume {pvc} {mode}, reading {human(to_read)} of "
                        f"{human(src.capacity)}")
                read = copy_blocks(src, m, blocks, writer, ctx, item)
                ctx.item(item, read=read, state="done")
                maps[src.key] = m
                spec = o.get("spec") or {}
                disks.append({
                    "key": src.key, "label": pvc, "pvc": pvc, "fcd_id": fcd_id,
                    "capacity": src.capacity, "file": src.info.file,
                    "datastore": src.info.datastore, "mode": mode, "read_bytes": read,
                    "change_id": fcd.snapshot_change_id(fcd_id, snaps[fcd_id]),
                    "storage_class": spec.get("storageClassName") or "",
                    "volume_mode": spec.get("volumeMode") or "Filesystem",
                })
        finally:
            for fcd_id, snap_id in snaps.items():
                try:
                    fcd.delete_snapshot(fcd_id, snap_id)
                except Exception as e:  # never mask the real error
                    warn(f"snapshot of volume {fcd_id} could not be removed: {e}. It will be "
                         "retried on the next run.")
        return maps, disks, warnings

    return run
