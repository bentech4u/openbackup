"""Tests for retention policy and garbage collection."""

import os
from datetime import datetime, timedelta, timezone

import pytest

from openbackup.repo.blockmap import BlockMap
from openbackup.repo.gc import collect_garbage
from openbackup.repo.repository import Destination, open_repository
from openbackup.repo.restorepoint import DiskPoint, PointStore, RestorePoint
from openbackup.repo.retention import RetentionPolicy, apply, select

pytestmark = pytest.mark.timeout(120)
MIB = 1024 * 1024
BASE = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)


def point_at(days_ago: int, pid: str | None = None) -> RestorePoint:
    when = BASE - timedelta(days=days_ago)
    return RestorePoint(
        id=pid or f"p{days_ago:03d}", vm_uuid="u", vm_instance_uuid="iu",
        vm_name="vm", created_at=when.isoformat())


# -- retention --------------------------------------------------------------

def test_keep_last_keeps_the_newest():
    points = [point_at(i) for i in range(10)]
    kept, expired = select(points, RetentionPolicy(keep_last=3))
    assert [p.id for p in kept] == ["p000", "p001", "p002"]
    assert len(expired) == 7


def test_daily_keeps_the_last_backup_of_each_day():
    """'The daily for Tuesday' means Tuesday's last backup, not its first."""
    points = [
        RestorePoint(id="early", vm_uuid="u", vm_instance_uuid="iu", vm_name="vm",
                     created_at=(BASE - timedelta(hours=8)).isoformat()),
        RestorePoint(id="late", vm_uuid="u", vm_instance_uuid="iu", vm_name="vm",
                     created_at=BASE.isoformat()),
    ]
    kept, expired = select(points, RetentionPolicy(keep_last=0, keep_daily=1))
    assert [p.id for p in kept] == ["late"]
    assert [p.id for p in expired] == ["early"]


def test_grandfather_father_son_overlaps_as_expected():
    points = [point_at(i) for i in range(40)]
    kept, _ = select(points, RetentionPolicy(
        keep_last=3, keep_weekly=2, keep_monthly=2))
    ids = {p.id for p in kept}
    assert {"p000", "p001", "p002"} <= ids      # the last three
    assert len(ids) >= 4                         # plus at least one older period


def test_a_policy_that_keeps_nothing_is_refused():
    """Silently deleting every backup is never what anyone configured."""
    with pytest.raises(ValueError, match="keep no restore points"):
        select([point_at(0)], RetentionPolicy(keep_last=0))


def test_empty_repository_is_fine():
    assert select([], RetentionPolicy(keep_last=5)) == ([], [])


def test_apply_deletes_only_expired_points(tmp_path):
    repo = open_repository(Destination(kind="local", path=str(tmp_path / "r")),
                           create=True, index_dir=tmp_path / "i")
    try:
        store = PointStore(repo.backend)
        bm = BlockMap(MIB)
        bm[0] = repo.store.put(os.urandom(MIB)).hash
        repo.store.flush()
        for i in range(5):
            p = point_at(i)
            p.disks = [DiskPoint(key=2000, label="d", capacity=MIB,
                                 datastore="ds", descriptor_path="a.vmdk",
                                 flat_path="a-flat.vmdk", chunk_size=MIB)]
            store.save(p, {2000: bm})
        kept, expired = apply(store, "iu", RetentionPolicy(keep_last=2))
        assert len(kept) == 2 and len(expired) == 3
        assert len(store.list_points("iu")) == 2
    finally:
        repo.close()


# -- garbage collection -----------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    r = open_repository(Destination(kind="local", path=str(tmp_path / "repo")),
                        create=True, index_dir=tmp_path / "idx")
    yield r
    r.close()


def save_point(repo, image: bytes, pid: str, days_ago: int = 0):
    bm = BlockMap(len(image), chunk_size=MIB)
    for i in range(len(bm)):
        off, length = bm.block_range(i)
        bm[i] = repo.store.put(image[off:off + length]).hash
    repo.store.flush()
    point = point_at(days_ago, pid)
    point.disks = [DiskPoint(key=2000, label="Hard disk 1", capacity=len(image),
                             datastore="ds", descriptor_path="a.vmdk",
                             flat_path="a-flat.vmdk", chunk_size=MIB)]
    PointStore(repo.backend).save(point, {2000: bm})
    return point, bm


def save_points_sharing_a_pack(repo, images):
    """Write several points whose chunks all land in one pack.

    Flushing between points would give each its own pack, and every pack would
    then be either entirely live or entirely dead -- which never exercises
    repacking.
    """
    maps = []
    for image in images:
        bm = BlockMap(len(image), chunk_size=MIB)
        for i in range(len(bm)):
            off, length = bm.block_range(i)
            bm[i] = repo.store.put(image[off:off + length]).hash
        maps.append(bm)
    repo.store.flush()                       # one pack for all of them

    points = []
    for n, (image, bm) in enumerate(zip(images, maps)):
        point = point_at(n, f"shared{n}")
        point.disks = [DiskPoint(key=2000, label="Hard disk 1",
                                 capacity=len(image), datastore="ds",
                                 descriptor_path="a.vmdk",
                                 flat_path="a-flat.vmdk", chunk_size=MIB)]
        PointStore(repo.backend).save(point, {2000: bm})
        points.append(point)
    return points, maps


def test_nothing_is_collected_while_points_reference_it(repo):
    image = os.urandom(6 * MIB)
    save_point(repo, image, "keep")
    before = repo.index.pack_count()
    result = collect_garbage(repo)
    assert result.packs_deleted == 0
    assert repo.index.pack_count() == before


def test_packs_are_freed_once_nothing_references_them(repo):
    point, _ = save_point(repo, os.urandom(6 * MIB), "doomed")
    PointStore(repo.backend).delete("iu", point.id)
    result = collect_garbage(repo)
    assert result.packs_deleted > 0
    assert result.bytes_reclaimed > 0
    assert repo.index.pack_count() == 0


def test_surviving_data_is_still_readable_after_collection(repo):
    """The property that matters most: collecting garbage must not damage
    what is still referenced."""
    keep_image = os.urandom(4 * MIB)
    drop_image = os.urandom(4 * MIB)
    keep_point, keep_bm = save_point(repo, keep_image, "keep")
    drop_point, _ = save_point(repo, drop_image, "drop")

    PointStore(repo.backend).delete("iu", drop_point.id)
    collect_garbage(repo, repack_below=1.0)   # rewrite anything partly dead

    rebuilt = b"".join(repo.store.get(keep_bm[i]) for i in range(len(keep_bm)))
    assert rebuilt == keep_image


def test_a_mostly_dead_pack_is_rewritten(repo):
    """Live and dead chunks interleaved in one pack: space only comes back by
    rewriting the pack with just the survivors."""
    images = [os.urandom(2 * MIB) for _ in range(4)]
    points, maps = save_points_sharing_a_pack(repo, images)
    assert repo.index.pack_count() == 1

    store = PointStore(repo.backend)
    for point in points[1:]:
        store.delete("iu", point.id)         # 1 of 4 left alive

    result = collect_garbage(repo, repack_below=0.5)
    assert result.packs_repacked == 1, "a 25%-live pack should be rewritten"
    assert result.packs_written == 1
    assert result.bytes_reclaimed > 0

    rebuilt = b"".join(repo.store.get(maps[0][i]) for i in range(len(maps[0])))
    assert rebuilt == images[0]


def test_a_mostly_live_pack_is_left_alone(repo):
    """Rewriting 128 MB to recover a little space is not worth it."""
    image = os.urandom(8 * MIB)
    save_point(repo, image, "live")
    result = collect_garbage(repo, repack_below=0.5)
    assert result.packs_repacked == 0


def test_dry_run_changes_nothing(repo):
    point, _ = save_point(repo, os.urandom(4 * MIB), "doomed")
    PointStore(repo.backend).delete("iu", point.id)
    packs_before = repo.index.pack_count()
    files_before = len([f for f in repo.backend.list("packs")
                        if f.endswith(".pack")])

    result = collect_garbage(repo, dry_run=True)
    assert result.dry_run and result.packs_deleted > 0
    assert repo.index.pack_count() == packs_before
    assert len([f for f in repo.backend.list("packs")
                if f.endswith(".pack")]) == files_before


def test_an_unreadable_blockmap_aborts_collection(repo):
    """A block map we cannot read makes its chunks look unreferenced. Deleting
    on that basis would destroy data a restore point still needs."""
    point, _ = save_point(repo, os.urandom(4 * MIB), "p")
    store = PointStore(repo.backend)
    repo.backend.delete(store.blockmap_path("iu", point.id, 2000))

    with pytest.raises(RuntimeError, match="refusing to collect garbage"):
        collect_garbage(repo)
    assert repo.index.pack_count() > 0, "nothing should have been deleted"


def test_indexed_but_missing_packs_are_forgotten(repo):
    save_point(repo, os.urandom(4 * MIB), "p")
    from openbackup.repo.packfile import PackEntry
    repo.index.add_pack("deadbeef" * 4, 123, [PackEntry(b"\x01" * 32, 0, 10)])
    result = collect_garbage(repo)
    assert result.orphans_removed >= 1
    assert "deadbeef" * 4 not in repo.index.known_packs()


def test_collection_is_idempotent(repo):
    point, _ = save_point(repo, os.urandom(4 * MIB), "doomed")
    PointStore(repo.backend).delete("iu", point.id)
    first = collect_garbage(repo)
    second = collect_garbage(repo)
    assert first.bytes_reclaimed > 0
    assert second.bytes_reclaimed == 0
    assert second.packs_deleted == 0


def test_a_fully_live_pack_is_never_rewritten_into_itself(repo):
    """Pack ids are content hashes, so rewriting a pack whose chunks are all
    still live reproduces it byte for byte -- same id. Deleting the 'old' pack
    afterwards would delete the copy just written, losing live data."""
    keep_image = os.urandom(4 * MIB)
    drop_image = os.urandom(4 * MIB)
    _keep_point, keep_bm = save_point(repo, keep_image, "keep")
    drop_point, _ = save_point(repo, drop_image, "drop")
    PointStore(repo.backend).delete("iu", drop_point.id)

    # repack_below=1.0 asks for every partly-dead pack to be rewritten, which
    # is what exposed this.
    collect_garbage(repo, repack_below=1.0)

    for i in range(len(keep_bm)):
        assert repo.index.locate(keep_bm[i]) is not None, (
            f"block {i} lost its chunk during collection")
    rebuilt = b"".join(repo.store.get(keep_bm[i]) for i in range(len(keep_bm)))
    assert rebuilt == keep_image


def test_repacking_verifies_chunks_on_the_way_through(repo):
    """Repacking is the one moment we touch every surviving chunk, so
    corruption should surface there rather than be copied faithfully into a
    new pack and silently preserved."""
    from openbackup.repo.codec import ChunkCorruption
    from openbackup.repo.store import pack_path

    images = [os.urandom(2 * MIB) for _ in range(4)]
    points, maps = save_points_sharing_a_pack(repo, images)
    store = PointStore(repo.backend)
    for point in points[1:]:
        store.delete("iu", point.id)

    loc = repo.index.locate(maps[0][0])
    path = repo.backend.path(pack_path(loc.pack_id))
    blob = bytearray(path.read_bytes())
    blob[loc.offset + loc.length - 1] ^= 0xFF
    path.write_bytes(bytes(blob))

    with pytest.raises(ChunkCorruption):
        collect_garbage(repo, repack_below=0.5)
