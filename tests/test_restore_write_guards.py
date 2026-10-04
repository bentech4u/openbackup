"""The only path that writes VM disks: VSphereSource._writable_flat_disk."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_nfsdirect import make_disk

from openbackup.engine.source import DirectNfsAccess, VSphereSource
from openbackup.repo import nfs
from openbackup.vsphere.client import VSphereError
from openbackup.vsphere.types import DiskInfo

MiB = 1 << 20


@pytest.fixture
def setup(tmp_path, monkeypatch):
    share = tmp_path / "share"
    make_disk(share / "web", "web", b"old" * 1000, 4 * MiB)
    mounted = []

    def fake_mount(server, export, mp, options="", timeout=60, read_only=False):
        assert not read_only and Path(mp).name.startswith("dsw-")
        mounted.append(mp)
        if not Path(mp).exists():
            Path(mp).parent.mkdir(parents=True, exist_ok=True)
            Path(mp).symlink_to(share)
        return True

    monkeypatch.setattr(nfs, "ensure_mounted", fake_mount)
    monkeypatch.setattr(nfs, "unmount", lambda mp, timeout=120: Path(mp).unlink())
    src = VSphereSource("vc", "u", "p", "AB", vddk_libdir=Path("/x"), mount_root=tmp_path / "mnt",
                        direct_nfs=[DirectNfsAccess(1, "ds1", "10.0.0.1", "/vol")])
    disk = DiskInfo(key=2000, label="Hard disk 1", capacity=4 * MiB,
                    file="[ds1] web/web.vmdk", datastore="ds1")
    vm = SimpleNamespace(name="web", snapshot=None,
                         runtime=SimpleNamespace(powerState="poweredOff"))
    src.vm = lambda moref: vm
    src.vm_disks = lambda v: [disk]
    src.si = None
    return src, vm, disk, share, mounted


def test_writes_land_in_the_vms_own_flat_file(setup):
    src, _vm, disk, share, mounted = setup
    with src.open_disk("vm-1", disk, write=True) as w:
        assert not w.read_only and w.size == 4 * MiB
        w.pwrite(MiB, b"new")
        w.write_zeroes(0, 512)
        w.flush()
    data = (share / "web" / "web-flat.vmdk").read_bytes()
    assert data[MiB:MiB + 3] == b"new" and data[:512] == bytes(512)
    assert len(data) == 4 * MiB  # never truncated or grown
    src.close()
    assert mounted and not Path(mounted[0]).exists()  # the writable mount is gone


@pytest.mark.parametrize("problem,match", [
    ("powered_on", "powered off"),
    ("snapshot", "snapshots"),
    ("foreign_disk", "not one of"),
])
def test_refuses_unsafe_writes(setup, problem, match):
    src, vm, disk, share, _ = setup
    if problem == "powered_on":
        vm.runtime.powerState = "poweredOn"
    elif problem == "snapshot":
        vm.snapshot = object()
    elif problem == "foreign_disk":
        disk = DiskInfo(**{**disk.to_dict(), "file": "[ds1] db/db.vmdk"})
    before = (share / "web" / "web-flat.vmdk").read_bytes()
    with pytest.raises(VSphereError, match=match):
        with src.open_disk("vm-1", disk, write=True):
            pass
    assert (share / "web" / "web-flat.vmdk").read_bytes() == before


def test_refuses_a_disk_that_is_a_snapshot_delta(setup):
    from openbackup.vsphere.nfsdirect import DirectNfsError

    src, _vm, disk, share, _ = setup
    desc = share / "web" / "web.vmdk"
    desc.write_text(desc.read_text().replace("parentCID=ffffffff", "parentCID=12345678"))
    with pytest.raises(DirectNfsError, match="snapshot delta"):
        with src.open_disk("vm-1", disk, write=True):
            pass


def test_writer_never_creates_files(tmp_path):
    from openbackup.vsphere.nfsdirect import FlatDiskWriter, FlatExtent

    with pytest.raises(FileNotFoundError):
        FlatDiskWriter([FlatExtent(tmp_path / "missing-flat.vmdk", 8)])
    assert not (tmp_path / "missing-flat.vmdk").exists()
    os.sync()


def test_datastores_without_direct_access_are_not_writable(setup):
    # The restore engine checks this before creating or powering off any VM;
    # such datastores are only writable through a real VMware VDDK.
    src, *_ = setup
    assert src.can_write("ds1") and not src.can_write("vsanDatastore")
