"""A stand-in for VSphereSource: VMs whose disks are local files, served to
the engine through a real nbdkit, with simulated snapshots and CBT."""

from __future__ import annotations

import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from openbackup.nbd.nbdkit import Nbdkit
from openbackup.vsphere.client import SNAPSHOT_PREFIX, VSphereError
from openbackup.vsphere.types import DiskInfo, Extent, SnapshotRef


@dataclass
class FakeSnapshot:
    moref: str
    name: str
    files: dict[int, Path]
    gen: int


@dataclass
class FakeVm:
    moref: str
    name: str
    instance_uuid: str
    disks: list[DiskInfo]
    files: dict[int, Path]
    cbt: bool = False
    power: str = "poweredOn"
    snapshots: list[FakeSnapshot] = field(default_factory=list)
    gen: int = 1
    # (generation, disk key, start, length) of every write, for CBT
    changes: list[tuple[int, int, int, int]] = field(default_factory=list)
    epoch: str = field(default_factory=lambda: uuid.uuid4().hex[:6])

    @property
    def snapshot(self):
        return self.snapshots or None


class FakeVSphere:
    def __init__(self, root: Path):
        self.root = root
        self.vms: dict[str, FakeVm] = {}
        self._n = 0
        self.snapshot_removals = 0
        self.fail_remove_snapshot = False
        self.cbt_broken = False
        self.calls: list[str] = []
        # VM moref -> number of consolidations that will fail before one works.
        self.consolidation_failures: dict[str, int] = {}
        self.needs_consolidation: set[str] = set()
        # Serve snapshot disks as flat VMDKs read through FlatDisk, the way
        # direct NFS reads a datastore.
        self.direct = False
        self.foreign_snapshot = False

    # ---------------------------------------------------------- test helpers

    def add_vm(self, name: str, disk_sizes: list[int]) -> FakeVm:
        self._n += 1
        moref = f"vm-{self._n}"
        disks, files = [], {}
        for i, size in enumerate(disk_sizes):
            key = 2000 + i
            f = self.root / moref / f"disk{i}.img"
            f.parent.mkdir(parents=True, exist_ok=True)
            with open(f, "wb") as fh:
                fh.truncate(size)
            files[key] = f
            disks.append(DiskInfo(key=key, label=f"Hard disk {i + 1}", capacity=size,
                                  file=f"[ds1] {name}/{name}_{i}.vmdk", datastore="ds1",
                                  controller_key=1000, unit_number=i))
        vm = FakeVm(moref, name, str(uuid.uuid4()), disks, files)
        self.vms[moref] = vm
        return vm

    def write(self, vm: FakeVm, key: int, offset: int, data: bytes) -> None:
        cap = next(d.capacity for d in vm.disks if d.key == key)
        assert offset + len(data) <= cap, "write past the end of the disk"
        with open(vm.files[key], "r+b") as f:
            f.seek(offset)
            f.write(data)
        vm.changes.append((vm.gen, key, offset, len(data)))

    def read(self, vm: FakeVm, key: int) -> bytes:
        return vm.files[key].read_bytes()

    # ------------------------------------------------------------ interface

    def get_vm(self, moref: str) -> FakeVm:
        if moref not in self.vms:
            raise VSphereError(f"VM {moref} no longer exists")
        return self.vms[moref]

    def vm_disks(self, vm: FakeVm) -> list[DiskInfo]:
        return [DiskInfo(**d.to_dict()) for d in vm.disks]

    def capture_config(self, vm: FakeVm) -> dict:
        return {"name": vm.name, "instance_uuid": vm.instance_uuid, "guest_id": "otherGuest64",
                "cpu": 1, "memory_mb": 512, "firmware": "bios",
                "controllers": [{"key": 1000, "type": "pvscsi", "bus": 0, "sharing": ""}],
                "disks": [d.to_dict() for d in vm.disks], "nics": []}

    def find_own_snapshots(self, vm: FakeVm) -> list[FakeSnapshot]:
        return [s for s in vm.snapshots if s.name.startswith(SNAPSHOT_PREFIX)]

    def ensure_cbt(self, vm: FakeVm) -> bool:
        if vm.cbt:
            return False
        vm.cbt = True
        return True

    def create_snapshot(self, vm: FakeVm, name: str, quiesce: bool):
        self.calls.append(f"snapshot:{name}")
        self._n += 1
        files = {}
        for key, f in vm.files.items():
            if self.direct:
                stem = f"{f.stem}-snap{self._n}"
                snapf = f.with_name(f"{stem}-flat.vmdk")
                shutil.copyfile(f, snapf)
                parent = "0000abcd" if self.foreign_snapshot else "ffffffff"
                f.with_name(f"{stem}.vmdk").write_text(
                    f'parentCID={parent}\ncreateType="vmfs"\n'
                    f'RW {snapf.stat().st_size // 512} VMFS "{snapf.name}"\n')
            else:
                snapf = f.with_name(f"{f.stem}-snap{self._n}.img")
                shutil.copyfile(f, snapf)
            files[key] = snapf
        snap = FakeSnapshot(f"snapshot-{self._n}", name, files, vm.gen)
        vm.snapshots.append(snap)
        vm.gen += 1
        disks = []
        for d in self.vm_disks(vm):
            d.change_id = f"{vm.epoch}/{snap.gen}"
            d.file = str(files[d.key])
            disks.append(d)
        return snap, SnapshotRef(snap.moref, disks)

    def consolidation_needed(self, vm: FakeVm) -> bool:
        return vm.moref in self.needs_consolidation

    def consolidate(self, vm: FakeVm) -> None:
        self.calls.append(f"consolidate:{vm.moref}")
        left = self.consolidation_failures.get(vm.moref, 0)
        if left:
            self.consolidation_failures[vm.moref] = left - 1
            raise VSphereError("simulated consolidation failure")
        self.needs_consolidation.discard(vm.moref)

    def remove_snapshot(self, snap: FakeSnapshot) -> None:
        if self.fail_remove_snapshot:
            raise VSphereError("simulated removal failure")
        for vm in self.vms.values():
            if snap in vm.snapshots:
                vm.snapshots.remove(snap)
                for f in snap.files.values():
                    f.unlink(missing_ok=True)
                    f.with_name(f.name.replace("-flat.vmdk", ".vmdk")).unlink(missing_ok=True)
        self.snapshot_removals += 1

    def changed_areas(self, vm: FakeVm, snap: FakeSnapshot, disk: DiskInfo,
                      change_id: str) -> list[Extent]:
        if self.cbt_broken:
            raise VSphereError("simulated CBT failure")
        if change_id == "*":
            data = snap.files[disk.key].read_bytes()
            step = 64 * 1024
            return [Extent(o, step) for o in range(0, len(data), step)
                    if data[o:o + step].count(0) != len(data[o:o + step])]
        epoch, _, gen = change_id.partition("/")
        if epoch != vm.epoch:
            raise VSphereError("change id is not valid for this disk")
        since = int(gen)
        return [Extent(o, n) for g, k, o, n in vm.changes
                if k == disk.key and since < g <= snap.gen]

    def reset_cbt(self, vm: FakeVm) -> None:
        vm.epoch = uuid.uuid4().hex[:6]

    @contextmanager
    def open_disk(self, vm_moref: str, disk: DiskInfo, snapshot_moref: str | None = None,
                  write: bool = False):
        if snapshot_moref:
            vm = self.vms[vm_moref]
            snap = next(s for s in vm.snapshots if s.moref == snapshot_moref)
            path = snap.files[disk.key]
            if self.direct and not write:
                from openbackup.vsphere.nfsdirect import FlatDisk, resolve_base_flat

                desc = path.with_name(path.name.replace("-flat.vmdk", ".vmdk"))
                with FlatDisk(resolve_base_flat(desc), "direct NFS (fake)") as fd:
                    yield fd
                return
        else:
            path = self.vms[vm_moref].files[disk.key]
        with Nbdkit(["file", f"file={path}"], readonly=not write) as srv, \
                srv.connect() as c:
            yield c

    def power_off(self, vm: FakeVm) -> None:
        vm.power = "poweredOff"

    def power_on(self, vm: FakeVm) -> None:
        vm.power = "poweredOn"

    def create_vm(self, config, *, name, folder, resource_pool, datastore, host=None,
                  network_map=None):
        vm = self.add_vm(name, [d["capacity"] for d in config["disks"]])
        mapping = {orig["key"]: new for orig, new in zip(config["disks"], vm.disks,
                                                          strict=True)}
        return vm.moref, mapping

    # ------------------------------------------------ connection / inventory

    def connect(self):
        return self

    def close(self) -> None:
        pass

    def about(self) -> dict:
        return {"name": "Fake vCenter", "version": "8.0.3", "build": "1",
                "api_type": "VirtualCenter", "instance_uuid": "fake"}

    def list_vms(self):
        from openbackup.vsphere.types import VmSummary

        return [VmSummary(moref=v.moref, name=v.name, instance_uuid=v.instance_uuid,
                          power_state=v.power, disks=len(v.disks), cbt_enabled=v.cbt,
                          provisioned_bytes=sum(d.capacity for d in v.disks))
                for v in self.vms.values()]

    def placement_options(self):
        from openbackup.vsphere.types import PlacementOptions

        return PlacementOptions(
            datacenters=[{"moref": "datacenter-1", "name": "DC1", "vm_folder": "group-v1"}],
            hosts=[{"moref": "host-1", "name": "esx01.lab", "connected": True,
                    "maintenance": False}],
            datastores=[{"moref": "datastore-1", "name": "ds1", "capacity": 2 << 40,
                         "free": 1 << 40, "type": "NFS", "accessible": True}],
            networks=[{"moref": "network-1", "name": "VM Network", "kind": "network"}],
            folders=[{"moref": "group-v1", "name": "vm"}],
            resource_pools=[{"moref": "resgroup-1", "name": "Resources", "owner": "Cluster1"}],
        )
