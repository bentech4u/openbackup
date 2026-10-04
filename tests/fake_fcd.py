"""Fake vSphere First Class Disks over local flat VMDKs, with simulated
snapshots and CBT, read through the real FlatDisk."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from openbackup.vsphere.client import VSphereError
from openbackup.vsphere.fcd import FcdInfo
from openbackup.vsphere.nfsdirect import FlatDisk, datastore_relpath, resolve_base_flat
from openbackup.vsphere.types import Extent


class FakeFcd:
    def __init__(self, root: Path):
        self.root = root  # the datastore
        (root / "fcd").mkdir(parents=True, exist_ok=True)
        self.vols: dict[str, dict] = {}
        self.calls: list[str] = []
        self.fail_snapshot_of: str | None = None
        self.attached: set[str] = set()
        self.parent_cid = "ffffffff"

    def add(self, size: int) -> str:
        fid = str(uuid.uuid4())
        flat = self.root / "fcd" / f"{fid}-flat.vmdk"
        with open(flat, "wb") as f:
            f.truncate(size)
        self._descriptor(fid, size)
        self.vols[fid] = {"size": size, "cbt": False, "gen": 1, "changes": [], "snaps": {},
                          "epoch": uuid.uuid4().hex[:6]}
        return fid

    def _descriptor(self, fid: str, size: int) -> None:
        (self.root / "fcd" / f"{fid}.vmdk").write_text(
            f'parentCID={self.parent_cid}\ncreateType="vmfs"\n'
            f'RW {size // 512} VMFS "{fid}-flat.vmdk"\n')

    def write(self, fid: str, offset: int, data: bytes) -> None:
        v = self.vols[fid]
        with open(self.root / "fcd" / f"{fid}-flat.vmdk", "r+b") as f:
            f.seek(offset)
            f.write(data)
        v["changes"].append((v["gen"], offset, len(data)))

    def read(self, fid: str) -> bytes:
        return (self.root / "fcd" / f"{fid}-flat.vmdk").read_bytes()

    # ------------------------------------------------------------ interface

    def info(self, fid: str) -> FcdInfo:
        v = self.vols[fid]
        return FcdInfo(fid, f"pvc-{fid[:8]}", v["size"], f"[ds1] fcd/{fid}.vmdk", "ds1",
                       v["cbt"])

    def enable_cbt(self, fid: str) -> None:
        if fid in self.attached:
            raise VSphereError("The operation is not allowed in the current state.")
        self.vols[fid]["cbt"] = True

    def create_snapshot(self, fid: str, description: str) -> str:
        self.calls.append(f"snapshot:{fid}")
        if fid == self.fail_snapshot_of:
            raise VSphereError("simulated snapshot failure")
        v = self.vols[fid]
        sid = str(uuid.uuid4())
        v["snaps"][sid] = {"desc": description, "gen": v["gen"]}
        v["gen"] += 1
        return sid

    def snapshot_change_id(self, fid: str, sid: str) -> str | None:
        v = self.vols[fid]
        if not v["cbt"]:
            return None
        return f"{v['epoch']}/{v['snaps'][sid]['gen']}"

    def delete_snapshot(self, fid: str, sid: str) -> None:
        self.calls.append(f"delete:{fid}")
        del self.vols[fid]["snaps"][sid]

    def own_snapshots(self, fid: str) -> list[str]:
        return [s for s, d in self.vols[fid]["snaps"].items()
                if d["desc"].startswith("openbackup-")]

    def changed_areas(self, fid, sid, capacity, change_id) -> list[Extent]:
        v = self.vols[fid]
        if not v["cbt"]:
            raise VSphereError("Change tracking is not enabled for this disk")
        if change_id == "*":
            data = self.read(fid)
            step = 64 * 1024
            return [Extent(o, step) for o in range(0, len(data), step)
                    if data[o:o + step].count(0) != step]
        epoch, _, gen = change_id.partition("/")
        if epoch != v["epoch"]:
            raise VSphereError("change id is not valid")
        snap_gen = v["snaps"][sid]["gen"]
        return [Extent(o, n) for g, o, n in v["changes"] if int(gen) < g <= snap_gen]

    def open_flat(self, disk) -> FlatDisk:
        return FlatDisk(resolve_base_flat(self.root / datastore_relpath(disk.file)),
                        "direct NFS (fake)")


def fake_volume(size: int = 8 << 20) -> bytes:
    return os.urandom(size)
