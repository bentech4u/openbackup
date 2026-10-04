from __future__ import annotations

import os
import shutil

import pytest
from fake_vsphere import FakeVSphere

from openbackup.engine.backup import BackupOptions, backup_vm, blocks_for_extents
from openbackup.engine.context import Cancelled, NullContext
from openbackup.engine.restore import (
    NewVmTarget,
    RestoreError,
    export_disks,
    restore_in_place,
    restore_new_vm,
)
from openbackup.repo.repository import Repository
from openbackup.vsphere.types import Extent

pytestmark = pytest.mark.skipif(shutil.which("nbdkit") is None, reason="nbdkit not installed")

MiB = 1 << 20


@pytest.fixture
def env(tmp_path):
    vs = FakeVSphere(tmp_path / "vsphere")
    repo = Repository.create(tmp_path / "repo", tmp_path / "index")
    yield vs, repo, tmp_path
    repo.close()


def _populate(vs, vm):
    # Sparse-ish: data in a few places, the rest zeros, odd-sized second disk.
    vs.write(vm, 2000, 0, os.urandom(3 * MiB))
    vs.write(vm, 2000, 10 * MiB + 123, os.urandom(5000))
    vs.write(vm, 2001, MiB, os.urandom(MiB // 2))


def _restore_bytes(repo, point_id, tmp_path) -> list[bytes]:
    out = export_disks(repo, point_id, tmp_path / f"export-{point_id}", "raw", NullContext())
    return [p.read_bytes() for p in sorted(out)]


def test_blocks_for_extents():
    exts = [Extent(0, 1), Extent(MiB - 1, 2), Extent(5 * MiB, MiB), Extent(9 * MiB, 0)]
    assert blocks_for_extents(exts, MiB, 8 * MiB) == [0, 1, 5]
    assert blocks_for_extents([Extent(7 * MiB, 10 * MiB)], MiB, 8 * MiB) == [7]


def test_full_then_incremental_round_trip(env):
    vs, repo, tmp = env
    vm = vs.add_vm("web01", [16 * MiB, 3 * MiB + 4096])
    _populate(vs, vm)
    snapshot_before = [vs.read(vm, 2000), vs.read(vm, 2001)]

    ctx = NullContext()
    r1 = backup_vm(vs, repo, vm.moref, BackupOptions(), ctx)
    assert r1.manifest["kind"] == "full"
    assert vm.snapshots == []  # snapshot cleaned up
    # Only allocated areas were read, not the whole 19 MiB.
    assert r1.manifest["read_bytes"] < 8 * MiB

    vs.write(vm, 2000, 12 * MiB, os.urandom(100))
    ctx2 = NullContext()
    r2 = backup_vm(vs, repo, vm.moref, BackupOptions(), ctx2)
    assert r2.manifest["kind"] == "incremental"
    assert r2.manifest["read_bytes"] == MiB  # one changed block

    assert _restore_bytes(repo, r1.point_id, tmp) == snapshot_before
    assert _restore_bytes(repo, r2.point_id, tmp) == [vs.read(vm, 2000), vs.read(vm, 2001)]


def test_points_survive_deleting_their_parent(env):
    from openbackup.repo.maintenance import collect_garbage

    vs, repo, tmp = env
    vm = vs.add_vm("db01", [8 * MiB])
    _populate_one = os.urandom(4 * MiB)
    vs.write(vm, 2000, 0, _populate_one)
    r1 = backup_vm(vs, repo, vm.moref, BackupOptions(), NullContext())
    vs.write(vm, 2000, 6 * MiB, os.urandom(MiB))
    r2 = backup_vm(vs, repo, vm.moref, BackupOptions(), NullContext())
    repo.delete_point(r1.point_id)
    collect_garbage(repo)
    assert _restore_bytes(repo, r2.point_id, tmp) == [vs.read(vm, 2000)]


def test_invalid_change_id_falls_back_to_full(env):
    vs, repo, tmp = env
    vm = vs.add_vm("app01", [8 * MiB])
    vs.write(vm, 2000, 0, os.urandom(2 * MiB))
    backup_vm(vs, repo, vm.moref, BackupOptions(), NullContext())
    vs.reset_cbt(vm)  # e.g. the VM was restored or storage-vMotioned
    vs.write(vm, 2000, 5 * MiB, os.urandom(10))
    r = backup_vm(vs, repo, vm.moref, BackupOptions(), NullContext())
    assert r.manifest["kind"] == "full"
    assert any("CBT could not be used" in w for w in r.warnings)
    assert _restore_bytes(repo, r.point_id, tmp) == [vs.read(vm, 2000)]


def test_broken_allocation_query_reads_everything(env):
    vs, repo, tmp = env
    vm = vs.add_vm("app02", [4 * MiB])
    vs.write(vm, 2000, MiB, os.urandom(MiB))
    vs.cbt_broken = True
    r = backup_vm(vs, repo, vm.moref, BackupOptions(), NullContext())
    assert r.manifest["read_bytes"] == 4 * MiB
    assert _restore_bytes(repo, r.point_id, tmp) == [vs.read(vm, 2000)]


def test_active_full_ignores_cbt(env):
    vs, repo, _ = env
    vm = vs.add_vm("app03", [4 * MiB])
    vs.write(vm, 2000, 0, os.urandom(2 * MiB))
    backup_vm(vs, repo, vm.moref, BackupOptions(), NullContext())
    r = backup_vm(vs, repo, vm.moref, BackupOptions(active_full=True), NullContext())
    assert r.manifest["kind"] == "full"
    assert r.manifest["new_bytes"] == 0  # all deduplicated


def test_cbt_enabled_on_first_backup(env):
    vs, repo, _ = env
    vm = vs.add_vm("new01", [2 * MiB])
    ctx = NullContext()
    backup_vm(vs, repo, vm.moref, BackupOptions(), ctx)
    assert vm.cbt
    assert any("Changed Block Tracking" in m for _, m in ctx.messages)
    assert any("cbt-activate" in c for c in vs.calls)


def test_snapshot_removed_when_backup_is_cancelled(env):
    vs, repo, _ = env
    vm = vs.add_vm("big01", [8 * MiB])
    vs.write(vm, 2000, 0, os.urandom(8 * MiB))
    ctx = NullContext()

    class CancelAfterSnapshot(NullContext):
        def log(self, message, level="info"):
            super().log(message, level)
            if "reading" in message:
                self.cancel = True

    ctx = CancelAfterSnapshot()
    with pytest.raises(Cancelled):
        backup_vm(vs, repo, vm.moref, BackupOptions(), ctx)
    assert vm.snapshots == []
    assert repo.point_ids() == []


def test_leftover_snapshot_is_cleaned_up(env):
    vs, repo, _ = env
    vm = vs.add_vm("left01", [2 * MiB])
    vm.cbt = True
    vs.fail_remove_snapshot = True
    r = backup_vm(vs, repo, vm.moref, BackupOptions(), NullContext())
    assert any("could not be removed" in w for w in r.warnings)
    assert len(vm.snapshots) == 1
    vs.fail_remove_snapshot = False
    backup_vm(vs, repo, vm.moref, BackupOptions(), NullContext())
    assert vm.snapshots == []


def test_restore_as_new_vm(env):
    vs, repo, _ = env
    vm = vs.add_vm("web02", [16 * MiB, 2 * MiB])
    _populate(vs, vm)
    r = backup_vm(vs, repo, vm.moref, BackupOptions(), NullContext())
    moref = restore_new_vm(vs, repo, r.point_id,
                           NewVmTarget(name="web02-restored", folder="f", resource_pool="rp",
                                       datastore="ds1", power_on=True), NullContext())
    new = vs.vms[moref]
    assert new.power == "poweredOn"
    assert vs.read(new, 2000) == vs.read(vm, 2000)
    assert vs.read(new, 2001) == vs.read(vm, 2001)


def test_restore_in_place_overwrites_including_zeros(env):
    vs, repo, _ = env
    vm = vs.add_vm("db02", [8 * MiB])
    vs.write(vm, 2000, 0, os.urandom(2 * MiB))
    good = vs.read(vm, 2000)
    r = backup_vm(vs, repo, vm.moref, BackupOptions(), NullContext())
    # Damage: overwrite data and scribble on a region that was zero.
    vs.write(vm, 2000, 0, os.urandom(MiB))
    vs.write(vm, 2000, 6 * MiB, b"ransomware" * 1000)
    restore_in_place(vs, repo, r.point_id, NullContext())
    assert vs.read(vm, 2000) == good
    assert vm.power == "poweredOff"


def test_restore_in_place_refuses_mismatched_disks(env):
    vs, repo, _ = env
    vm = vs.add_vm("db03", [4 * MiB])
    r = backup_vm(vs, repo, vm.moref, BackupOptions(), NullContext())
    vm.disks[0].capacity = 8 * MiB
    with pytest.raises(RestoreError):
        restore_in_place(vs, repo, r.point_id, NullContext())


def test_export_vmdk(env):
    vs, repo, tmp = env
    if shutil.which("qemu-img") is None:
        pytest.skip("qemu-img not installed")
    vm = vs.add_vm("exp01", [4 * MiB])
    vs.write(vm, 2000, 0, os.urandom(MiB))
    r = backup_vm(vs, repo, vm.moref, BackupOptions(), NullContext())
    (out,) = export_disks(repo, r.point_id, tmp / "exp", "vmdk", NullContext())
    assert out.suffix == ".vmdk"
    import subprocess

    raw = tmp / "check.raw"
    subprocess.run(["qemu-img", "convert", "-O", "raw", str(out), str(raw)], check=True)
    assert raw.read_bytes() == vs.read(vm, 2000)


def test_direct_nfs_backup_round_trip(env):
    vs, repo, tmp = env
    vs.direct = True
    vm = vs.add_vm("nfs01", [16 * MiB])
    vs.write(vm, 2000, 0, os.urandom(3 * MiB))
    ctx = NullContext()
    r1 = backup_vm(vs, repo, vm.moref, BackupOptions(), ctx)
    assert any("direct NFS" in m for _, m in ctx.messages)
    vs.write(vm, 2000, 9 * MiB, os.urandom(10))
    r2 = backup_vm(vs, repo, vm.moref, BackupOptions(), NullContext())
    assert r2.manifest["kind"] == "incremental" and r2.manifest["read_bytes"] == MiB
    assert _restore_bytes(repo, r2.point_id, tmp) == [vs.read(vm, 2000)]
    assert r1.point_id != r2.point_id


def test_direct_nfs_refuses_vm_with_own_snapshots(env):
    from openbackup.vsphere.nfsdirect import DirectNfsError

    vs, repo, _ = env
    vs.direct = True
    vs.foreign_snapshot = True
    vm = vs.add_vm("snappy", [4 * MiB])
    vm.cbt = True
    with pytest.raises(DirectNfsError, match="snapshots of its own"):
        backup_vm(vs, repo, vm.moref, BackupOptions(), NullContext())
    assert vm.snapshots == []  # ours was still removed
