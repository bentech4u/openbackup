"""Round-trip tests: a disk image must come back exactly as it went in."""

import os

import pytest

from openbackup.jobs.restore import (
    RestoreError, RestoreToFile, descriptor_for, disk_stream, verify_point,
)
from openbackup.repo.backend import FilesystemBackend
from openbackup.repo.blockmap import BlockMap
from openbackup.repo.codec import chunk_hash
from openbackup.repo.index import ChunkIndex
from openbackup.repo.repository import Destination, open_repository
from openbackup.repo.restorepoint import DiskPoint, PointStore, RestorePoint

pytestmark = pytest.mark.timeout(120)
MIB = 1024 * 1024


@pytest.fixture
def repo(tmp_path):
    r = open_repository(
        Destination(kind="local", path=str(tmp_path / "repo")),
        create=True, index_dir=tmp_path / "idx")
    yield r
    r.close()


def make_disk(size: int, *, sparse: bool = True) -> bytes:
    """A disk image shaped like a real one: mostly holes, some data."""
    out = bytearray(size)
    blocks = size // MIB
    for i in range(blocks):
        if sparse and i % 3 != 0:
            continue                      # leave as zeros
        out[i * MIB:(i + 1) * MIB] = os.urandom(MIB)
    return bytes(out)


def back_up(repo, image: bytes, *, vm="testvm", chunk_size=MIB):
    """What the backup job does, without needing vSphere."""
    bm = BlockMap(len(image), chunk_size=chunk_size)
    for i in range(len(bm)):
        off, length = bm.block_range(i)
        bm[i] = repo.store.put(image[off:off + length]).hash
    repo.store.flush()

    point = RestorePoint.create(
        vm_uuid="uuid-1", vm_instance_uuid="iuuid-1", vm_name=vm,
        disks=[DiskPoint(
            key=2000, label="Hard disk 1", capacity=len(image),
            datastore="DS-NAS01", descriptor_path="vm/vm.vmdk",
            flat_path="vm/vm-flat.vmdk", chunk_size=chunk_size, thin=True)],
    )
    PointStore(repo.backend).save(point, {2000: bm})
    return point


# -- round trip -------------------------------------------------------------

def test_restored_disk_is_byte_identical(repo, tmp_path):
    image = make_disk(12 * MIB)
    point = back_up(repo, image)
    result = RestoreToFile(repo, point, tmp_path / "out").run()
    restored = (tmp_path / "out" / f"testvm-disk2000-flat.vmdk").read_bytes()
    assert restored == image
    assert result.disks[0].size == len(image)


def test_restore_handles_a_partial_final_block(repo, tmp_path):
    """A disk whose size is not a multiple of the block size must not gain or
    lose bytes at the end."""
    image = make_disk(8 * MIB)[: 8 * MIB - 12345]
    point = back_up(repo, image)
    RestoreToFile(repo, point, tmp_path / "out").run()
    restored = (tmp_path / "out" / "testvm-disk2000-flat.vmdk").read_bytes()
    assert len(restored) == len(image)
    assert restored == image


def test_unwritten_regions_are_left_as_holes(repo, tmp_path):
    """A 350 GiB disk holding 13 GiB should restore to a 13 GiB file, not a
    350 GiB one."""
    image = make_disk(24 * MIB)
    point = back_up(repo, image)
    result = RestoreToFile(repo, point, tmp_path / "out").run()
    dr = result.disks[0]
    assert dr.blocks_skipped_zero > 0
    assert dr.blocks_written + dr.blocks_skipped_zero == dr.blocks

    path = tmp_path / "out" / "testvm-disk2000-flat.vmdk"
    st = os.stat(path)
    assert st.st_size == len(image)
    assert st.st_blocks * 512 < len(image), "restored file is not sparse"


def test_a_descriptor_is_written_alongside(repo, tmp_path):
    image = make_disk(4 * MIB)
    point = back_up(repo, image)
    RestoreToFile(repo, point, tmp_path / "out").run()
    desc = (tmp_path / "out" / "testvm-disk2000.vmdk").read_text()
    assert 'RW 8192 VMFS "testvm-disk2000-flat.vmdk"' in desc
    assert 'createType="vmfs"' in desc


def test_incremental_point_restores_standalone(repo, tmp_path):
    """The property that separates this from a forward-incremental chain: the
    parent is provenance, not a dependency."""
    first = make_disk(8 * MIB)
    p1 = back_up(repo, first)

    second = bytearray(first)
    second[2 * MIB:3 * MIB] = os.urandom(MIB)       # one block changes
    second = bytes(second)

    points = PointStore(repo.backend)
    parent_bm = points.load_blockmap("iuuid-1", p1.id, 2000)
    bm = parent_bm.clone()
    for i in bm.blocks_for(2 * MIB, MIB):
        off, length = bm.block_range(i)
        bm[i] = repo.store.put(second[off:off + length]).hash
    repo.store.flush()

    p2 = RestorePoint.create(
        vm_uuid="uuid-1", vm_instance_uuid="iuuid-1", vm_name="testvm",
        kind="incremental", parent_id=p1.id, disks=list(p1.disks))
    points.save(p2, {2000: bm})

    # Delete the parent entirely; the incremental must still restore.
    points.delete("iuuid-1", p1.id)

    RestoreToFile(repo, p2, tmp_path / "out").run()
    assert (tmp_path / "out" / "testvm-disk2000-flat.vmdk").read_bytes() == second


def test_disk_stream_yields_the_whole_image(repo):
    image = make_disk(6 * MIB)
    point = back_up(repo, image)
    bm = PointStore(repo.backend).load_blockmap("iuuid-1", point.id, 2000)
    assert b"".join(disk_stream(repo.store, bm)) == image


# -- failure handling -------------------------------------------------------

def test_missing_chunk_fails_loudly(repo, tmp_path):
    """Half-restoring a disk and reporting success would be the worst outcome."""
    image = make_disk(6 * MIB)
    point = back_up(repo, image)
    bm = PointStore(repo.backend).load_blockmap("iuuid-1", point.id, 2000)

    # Remove a pack so one chunk can no longer be fetched.
    for relpath in list(repo.backend.list("packs")):
        if relpath.endswith(".pack"):
            repo.backend.delete(relpath)
            break
    repo.index.clear()

    with pytest.raises(RestoreError, match="missing from the repository"):
        RestoreToFile(repo, point, tmp_path / "out").run()


def test_blockmap_size_mismatch_is_refused(repo, tmp_path):
    image = make_disk(4 * MIB)
    point = back_up(repo, image)
    point.disks[0].capacity = 8 * MIB       # lie about the size
    with pytest.raises(RestoreError, match="block map covers"):
        RestoreToFile(repo, point, tmp_path / "out").run()


# -- verification -----------------------------------------------------------

def test_verify_passes_on_a_good_point(repo):
    point = back_up(repo, make_disk(8 * MIB))
    result = verify_point(repo, point)
    assert result.ok
    assert result.chunks_checked > 0
    assert not result.missing and not result.corrupt


def test_verify_checks_each_distinct_chunk_once(repo):
    """A mostly-empty disk names the same zero chunk thousands of times."""
    point = back_up(repo, make_disk(16 * MIB))
    bm = PointStore(repo.backend).load_blockmap("iuuid-1", point.id, 2000)
    result = verify_point(repo, point)
    assert result.chunks_checked == len(bm.distinct_hashes())
    assert result.chunks_checked < result.blocks


def test_verify_reports_a_missing_chunk(repo):
    point = back_up(repo, make_disk(6 * MIB))
    for relpath in list(repo.backend.list("packs")):
        if relpath.endswith(".pack"):
            repo.backend.delete(relpath)
            break
    repo.index.clear()
    result = verify_point(repo, point)
    assert not result.ok
    assert result.missing


def test_verify_detects_bitrot(repo):
    """Corruption inside a pack must surface here, not during a restore."""
    point = back_up(repo, make_disk(6 * MIB))
    bm = PointStore(repo.backend).load_blockmap("iuuid-1", point.id, 2000)
    target = next(h for h in bm.distinct_hashes()
                  if h != chunk_hash(b"\0" * MIB))
    loc = repo.index.locate(target)
    from openbackup.repo.store import pack_path
    path = repo.backend.path(pack_path(loc.pack_id))
    blob = bytearray(path.read_bytes())
    blob[loc.offset + loc.length - 1] ^= 0xFF
    path.write_bytes(bytes(blob))

    result = verify_point(repo, point)
    assert not result.ok
    assert result.corrupt
