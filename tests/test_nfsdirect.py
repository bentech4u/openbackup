from __future__ import annotations

import os

import pytest

from openbackup.vsphere.nfsdirect import (
    DirectNfsError,
    FlatDisk,
    datastore_relpath,
    parse_descriptor,
    resolve_base_flat,
)

MiB = 1 << 20

# As written by ESXi 8 on an NFS datastore.
DESCRIPTOR = """# Disk DescriptorFile
version=3
encoding="UTF-8"
CID=fd71677b
parentCID=ffffffff
createType="vmfs"

# Extent description
RW {sectors} VMFS "{name}-flat.vmdk"

# Change Tracking File
changeTrackPath="{name}-ctk.vmdk"

# The Disk Data Base
#DDB
ddb.adapterType = "buslogic"
ddb.thinProvisioned = "1"
"""


def make_disk(d, name: str, data: bytes, size: int) -> os.PathLike:
    d.mkdir(parents=True, exist_ok=True)
    flat = d / f"{name}-flat.vmdk"
    with open(flat, "wb") as f:
        f.truncate(size)
        f.write(data)
    desc = d / f"{name}.vmdk"
    desc.write_text(DESCRIPTOR.format(sectors=size // 512, name=name))
    return desc


def test_parse_real_descriptor():
    fields, extents = parse_descriptor(DESCRIPTOR.format(sectors=734003200, name="installer"))
    assert fields["parentCID"] == "ffffffff"
    assert extents == [("RW", 734003200, "VMFS", "installer-flat.vmdk", 0)]


def test_reads_base_disk(tmp_path):
    data = os.urandom(3 * MiB + 100)
    desc = make_disk(tmp_path / "vm", "vm", data, 8 * MiB)
    with FlatDisk(resolve_base_flat(desc)) as fd:
        assert fd.size == 8 * MiB
        assert fd.pread(0, len(data)) == data
        assert fd.pread(7 * MiB, MiB) == bytes(MiB)
        with pytest.raises(DirectNfsError):
            fd.pread(8 * MiB - 1, 2)


def test_multiple_extents(tmp_path):
    d = tmp_path / "vm"
    d.mkdir()
    a, b = os.urandom(2 * MiB), os.urandom(MiB)
    (d / "s-f001.vmdk").write_bytes(a)
    (d / "s-f002.vmdk").write_bytes(b)
    (d / "s.vmdk").write_text('parentCID=ffffffff\ncreateType="twoGbMaxExtentFlat"\n'
                              f'RW {len(a) // 512} FLAT "s-f001.vmdk" 0\n'
                              f'RW {len(b) // 512} FLAT "s-f002.vmdk" 0\n')
    with FlatDisk(resolve_base_flat(d / "s.vmdk")) as fd:
        assert fd.size == 3 * MiB
        assert fd.pread(MiB + 5, 2 * MiB - 5) == (a + b)[MiB + 5:]


def test_refuses_snapshot_delta(tmp_path):
    d = tmp_path / "vm"
    d.mkdir()
    (d / "vm-000001.vmdk").write_text('CID=1\nparentCID=fd71677b\ncreateType="seSparse"\n'
                                      'RW 16384 SESPARSE "vm-000001-sesparse.vmdk"\n')
    with pytest.raises(DirectNfsError, match="snapshots of its own"):
        resolve_base_flat(d / "vm-000001.vmdk")


def test_refuses_sparse_and_escaping_extents(tmp_path):
    d = tmp_path / "vm"
    d.mkdir()
    (d / "a.vmdk").write_text('parentCID=ffffffff\nRW 2048 SPARSE "a-s001.vmdk"\n')
    with pytest.raises(DirectNfsError, match="SPARSE"):
        resolve_base_flat(d / "a.vmdk")
    (d / "b.vmdk").write_text('parentCID=ffffffff\nRW 2048 FLAT "../../etc/shadow"\n')
    with pytest.raises(DirectNfsError):
        resolve_base_flat(d / "b.vmdk")
    for bad in ("[ds] ../x.vmdk", "[ds] /etc/x.vmdk", "[ds] "):
        with pytest.raises(DirectNfsError):
            datastore_relpath(bad)


def test_missing_descriptor_is_explained(tmp_path):
    with pytest.raises(DirectNfsError, match="export setting"):
        resolve_base_flat(tmp_path / "nope.vmdk")


def test_allocated_extents_from_holes(tmp_path):
    desc = make_disk(tmp_path / "vm", "vm", b"", 64 * MiB)
    flat = tmp_path / "vm" / "vm-flat.vmdk"
    with open(flat, "r+b") as f:
        f.seek(10 * MiB)
        f.write(os.urandom(MiB))
    with FlatDisk(resolve_base_flat(desc)) as fd:
        ext = fd.allocated_extents()
    if ext is None or ext == [(0, 64 * MiB)]:
        pytest.skip("filesystem does not report holes")
    assert any(o <= 10 * MiB and o + n >= 11 * MiB for o, n in ext)
    assert sum(n for _, n in ext) < 8 * MiB


def test_opened_read_only(tmp_path):
    desc = make_disk(tmp_path / "vm", "vm", b"x" * 512, MiB)
    with FlatDisk(resolve_base_flat(desc)) as fd:
        with pytest.raises(OSError):
            os.write(fd._fds[0], b"y")
