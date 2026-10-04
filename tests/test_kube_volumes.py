from __future__ import annotations

import os

import pytest
from fake_fcd import FakeFcd
from fake_kube import FakeKube

from openbackup.engine.backup import latest_point_for_vm
from openbackup.engine.context import NullContext
from openbackup.kube.client import KubeClient
from openbackup.kube.engine import NamespaceBackupOptions, backup_namespace, subject_id
from openbackup.kube.volumes import disk_key, volume_backup
from openbackup.repo.repository import Repository
from openbackup.vsphere.nfsdirect import DirectNfsError

MiB = 1 << 20


def seed(fk: FakeKube, vols: dict[str, tuple[str, str]]) -> None:
    """vols: pvc name -> (csi driver, volume handle)"""
    fk.add({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "db"}})
    for pvc, (driver, handle) in vols.items():
        fk.add({"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": f"pv-{pvc}"},
                "spec": {"csi": {"driver": driver, "volumeHandle": handle}}})
        fk.add({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                "metadata": {"name": pvc, "namespace": "db"},
                "spec": {"volumeName": f"pv-{pvc}", "storageClassName": "thin-csi",
                         "resources": {"requests": {"storage": "8Mi"}}}})
    fk.add({"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
            "metadata": {"name": "pgvm", "namespace": "db"},
            "spec": {"template": {"spec": {"volumes": [
                {"name": "root", "dataVolume": {"name": "data"}}]}}}})


@pytest.fixture
def env(tmp_path):
    fcd = FakeFcd(tmp_path / "ds")
    repo = Repository.create(tmp_path / "repo", tmp_path / "idx")
    yield fcd, repo
    repo.close()


def run(fk, fcd, repo, ctx=None, active_full=False, freeze=None, unfreeze=None):
    ctx = ctx or NullContext()
    c = KubeClient("https://k:6443", "t", "", transport=fk.transport())
    sid = subject_id("homelab", "db")
    prev = latest_point_for_vm(repo, sid)
    vols = volume_backup(fcd, fcd.open_flat, prev, lambda k: repo.load_map(prev["id"], k),
                         ctx, "homelab/db", active_full=active_full, freeze=freeze,
                         unfreeze=unfreeze)
    return backup_namespace(c, repo, "db", NamespaceBackupOptions(1, "homelab", "u"), ctx,
                            volumes=vols)


def restored(repo, point_id, key):
    m = repo.load_map(point_id, key)
    return b"".join(repo.read_chunk(c, m.block_length(i)) for i, c in enumerate(m.ids))


def test_full_then_incremental(env):
    fcd, repo = env
    a = fcd.add(8 * MiB)
    fcd.write(a, 0, os.urandom(3 * MiB))
    fk = FakeKube()
    seed(fk, {"data": ("csi.vsphere.vmware.com", a)})

    m1 = run(fk, fcd, repo)
    assert m1["kind"] == "full"
    (d,) = m1["disks"]
    assert d["pvc"] == "data" and d["fcd_id"] == a and d["change_id"]
    assert restored(repo, m1["id"], disk_key("data")) == fcd.read(a)
    assert fcd.vols[a]["cbt"] and fcd.vols[a]["snaps"] == {}

    fcd.write(a, 6 * MiB + 5, b"changed")
    m2 = run(fk, fcd, repo)
    assert m2["kind"] == "incremental" and m2["read_bytes"] == MiB
    assert restored(repo, m2["id"], disk_key("data")) == fcd.read(a)
    assert m2["pvcs"][0]["data"] is True


def test_vms_are_frozen_around_the_snapshot(env):
    fcd, repo = env
    a = fcd.add(4 * MiB)
    fk = FakeKube()
    seed(fk, {"data": ("csi.vsphere.vmware.com", a)})
    events = []

    def freeze(vm):
        events.append(f"freeze:{vm}")

    def unfreeze(vm):
        events.append(f"unfreeze:{vm}")

    orig = fcd.create_snapshot
    fcd.create_snapshot = lambda fid, d: events.append("snapshot") or orig(fid, d)
    run(fk, fcd, repo, freeze=freeze, unfreeze=unfreeze)
    assert events == ["freeze:pgvm", "snapshot", "unfreeze:pgvm"]


def test_other_drivers_keep_their_definition_only(env):
    fcd, repo = env
    a = fcd.add(4 * MiB)
    fk = FakeKube()
    seed(fk, {"data": ("csi.vsphere.vmware.com", a), "shared": ("nfs.csi.k8s.io", "x")})
    m = run(fk, fcd, repo)
    assert [d["pvc"] for d in m["disks"]] == ["data"]
    assert any("nfs.csi.k8s.io" in w for w in m["warnings"])
    assert {p["name"]: p["data"] for p in m["pvcs"]} == {"data": True, "shared": False}


def test_all_snapshots_are_removed_when_one_fails(env):
    fcd, repo = env
    a, b = fcd.add(4 * MiB), fcd.add(4 * MiB)
    fk = FakeKube()
    seed(fk, {"aa": ("csi.vsphere.vmware.com", a), "bb": ("csi.vsphere.vmware.com", b)})
    fcd.fail_snapshot_of = b
    with pytest.raises(Exception, match="simulated"):
        run(fk, fcd, repo)
    assert fcd.vols[a]["snaps"] == {} and fcd.vols[b]["snaps"] == {}
    assert repo.point_ids() == []


def test_leftover_snapshots_are_cleaned_up(env):
    fcd, repo = env
    a = fcd.add(4 * MiB)
    fcd.vols[a]["snaps"]["old"] = {"desc": "openbackup-9-123", "gen": 0}
    fcd.vols[a]["snaps"]["theirs"] = {"desc": "velero backup", "gen": 0}
    fk = FakeKube()
    seed(fk, {"data": ("csi.vsphere.vmware.com", a)})
    run(fk, fcd, repo)
    assert set(fcd.vols[a]["snaps"]) == {"theirs"}


def test_volume_with_its_own_snapshots_is_refused(env):
    fcd, repo = env
    a = fcd.add(4 * MiB)
    fcd.parent_cid = "1234abcd"
    fcd._descriptor(a, 4 * MiB)
    fk = FakeKube()
    seed(fk, {"data": ("csi.vsphere.vmware.com", a)})
    with pytest.raises(DirectNfsError, match="snapshots of its own"):
        run(fk, fcd, repo)
    assert fcd.vols[a]["snaps"] == {}


def test_attached_volume_without_cbt_is_read_in_full(env):
    fcd, repo = env
    a = fcd.add(4 * MiB)
    fcd.write(a, MiB, os.urandom(1000))
    fcd.attached.add(a)  # vSphere refuses to toggle CBT on an attached FCD
    fk = FakeKube()
    seed(fk, {"data": ("csi.vsphere.vmware.com", a)})
    m1 = run(fk, fcd, repo)
    assert any("could not be enabled" in w for w in m1["warnings"])
    # Without CBT the volume is read whole, or only its data where the storage
    # reports holes itself (this local test filesystem does; NFS v3 does not).
    assert MiB <= m1["read_bytes"] <= 4 * MiB
    assert restored(repo, m1["id"], disk_key("data")) == fcd.read(a)
    fcd.write(a, 3 * MiB, b"x")
    m2 = run(fk, fcd, repo)
    assert m2["kind"] == "full" and restored(repo, m2["id"], disk_key("data")) == fcd.read(a)
