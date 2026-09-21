"""Tests for the backup format: chunk storage and block maps."""

import os

import pytest

from openbackup.repo.blockmap import BlockMap, BlockMapError, DEFAULT_CHUNK_SIZE
from openbackup.repo.chunkstore import (
    ChunkCorruption, ChunkMissing, ChunkStore, chunk_hash, HASH_BYTES,
)

MIB = 1024 * 1024


@pytest.fixture
def store(tmp_path):
    cs = ChunkStore(tmp_path / "chunks")
    cs.init()
    return cs


# -- chunk store ------------------------------------------------------------

def test_roundtrip_preserves_contents(store):
    data = os.urandom(64 * 1024)
    res = store.put(data)
    assert store.get(res.hash) == data


def test_identical_blocks_are_stored_once(store):
    data = os.urandom(32 * 1024)
    first, second = store.put(data), store.put(data)
    assert first.hash == second.hash
    assert first.is_new and not second.is_new
    assert sum(1 for _ in store.iter_hashes()) == 1


def test_zero_blocks_collapse(store):
    """Unallocated space in a thin disk is the most common block there is."""
    res = store.put(b"\0" * MIB)
    assert res.stored_size < 1024


def test_incompressible_blocks_are_not_inflated(store):
    """Already-compressed guest data must not pay a compression penalty."""
    data = os.urandom(MIB)
    res = store.put(data)
    assert res.stored_size < len(data) + 64
    assert store.get(res.hash) == data


def test_empty_block_roundtrips(store):
    res = store.put(b"")
    assert store.get(res.hash) == b""


def test_missing_chunk_raises(store):
    with pytest.raises(ChunkMissing):
        store.get(chunk_hash(b"never stored"))


def test_bitrot_is_detected_not_returned(store):
    """Silently restoring altered blocks is worse than failing loudly."""
    data = os.urandom(8192)
    res = store.put(data)
    path = store.path_for(res.hash)
    blob = bytearray(path.read_bytes())
    blob[-1] ^= 0xFF
    path.write_bytes(blob)
    with pytest.raises(ChunkCorruption):
        store.get(res.hash)


def test_truncated_chunk_file_is_detected(store):
    res = store.put(os.urandom(8192))
    path = store.path_for(res.hash)
    path.write_bytes(path.read_bytes()[:8])
    with pytest.raises(ChunkCorruption):
        store.get(res.hash)


def test_delete_removes_the_chunk(store):
    res = store.put(b"transient")
    assert store.delete(res.hash) is True
    assert not store.exists(res.hash)
    assert store.delete(res.hash) is False


def test_a_failed_put_leaves_no_partial_chunk(store, monkeypatch):
    """A crash mid-backup must not leave a file whose name promises contents
    it does not have -- everything downstream trusts that name."""
    import openbackup.repo.chunkstore as mod

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(mod.os, "replace", boom)
    with pytest.raises(OSError):
        store.put(os.urandom(4096))
    assert sum(1 for _ in store.iter_hashes()) == 0


# -- encryption -------------------------------------------------------------

def test_encrypted_chunks_roundtrip(tmp_path):
    key = os.urandom(32)
    cs = ChunkStore(tmp_path / "enc", key=key)
    cs.init()
    data = os.urandom(16 * 1024)
    res = cs.put(data)
    assert cs.get(res.hash) == data
    assert data not in cs.path_for(res.hash).read_bytes()


def test_encrypted_repo_is_unreadable_without_the_key(tmp_path):
    key = os.urandom(32)
    enc = ChunkStore(tmp_path / "enc", key=key)
    enc.init()
    res = enc.put(os.urandom(4096))

    plain = ChunkStore(tmp_path / "enc")
    with pytest.raises(ChunkCorruption, match="no key"):
        plain.get(res.hash)

    wrong = ChunkStore(tmp_path / "enc", key=os.urandom(32))
    with pytest.raises(ChunkCorruption, match="authentication"):
        wrong.get(res.hash)


# -- block maps -------------------------------------------------------------

def test_block_count_covers_a_partial_final_block():
    bm = BlockMap(DEFAULT_CHUNK_SIZE * 2 + 1)
    assert len(bm) == 3


def test_final_short_block_is_clamped_to_the_disk():
    """Writing a full-length final chunk would run past the end of the disk."""
    bm = BlockMap(int(2.5 * MIB))
    assert bm.block_range(2) == (2 * MIB, MIB // 2)


def test_cbt_extent_maps_to_the_blocks_it_touches():
    bm = BlockMap(100 * MIB)
    assert list(bm.blocks_for(0, 1)) == [0]
    assert list(bm.blocks_for(MIB, MIB)) == [1]
    assert list(bm.blocks_for(MIB - 1, 2)) == [0, 1]
    assert list(bm.blocks_for(0, 0)) == []


def test_extent_past_the_end_is_clamped():
    bm = BlockMap(4 * MIB)
    assert list(bm.blocks_for(3 * MIB, 100 * MIB)) == [3]


def test_serialisation_roundtrip():
    bm = BlockMap(10 * MIB)
    for i in range(len(bm)):
        bm[i] = chunk_hash(f"block-{i}".encode())
    restored = BlockMap.from_bytes(bm.to_bytes())
    assert restored.disk_size == bm.disk_size
    assert restored.chunk_size == bm.chunk_size
    assert list(restored) == list(bm)


def test_serialised_map_is_small():
    """30 restore points of a 100 GiB VM should not cost more than the data."""
    bm = BlockMap(100 * 1024 ** 3)
    for i in range(len(bm)):
        bm[i] = chunk_hash(b"same")
    assert len(bm.to_bytes()) < 64 * 1024


def test_corrupt_map_is_rejected():
    with pytest.raises(BlockMapError, match="not a block map"):
        BlockMap.from_bytes(b"JUNKJUNKJUNKJUNKJUNKJUNKJUNK")
    with pytest.raises(BlockMapError, match="truncated"):
        BlockMap.from_bytes(b"OBM1")


def test_clone_inherits_every_block():
    """An incremental rewrites only changed blocks and must still restore
    standalone."""
    base = BlockMap(4 * MIB)
    for i in range(len(base)):
        base[i] = chunk_hash(f"v1-{i}".encode())
    inc = base.clone()
    inc[2] = chunk_hash(b"v2-2")
    assert inc[0] == base[0] and inc[1] == base[1] and inc[3] == base[3]
    assert inc[2] != base[2]
    inc.validate()


def test_growing_a_disk_keeps_existing_blocks():
    base = BlockMap(4 * MIB)
    for i in range(len(base)):
        base[i] = chunk_hash(f"v1-{i}".encode())
    grown = base.resized_clone(8 * MIB)
    assert len(grown) == 8
    assert [grown[i] for i in range(4)] == [base[i] for i in range(4)]
    assert grown[4] == b"\0" * HASH_BYTES


def test_growing_past_a_short_final_block_invalidates_it():
    """The old last block was short; at the new size it is full length, so its
    stored chunk no longer covers the whole block and must be re-read."""
    base = BlockMap(int(2.5 * MIB))
    for i in range(len(base)):
        base[i] = chunk_hash(f"v1-{i}".encode())
    grown = base.resized_clone(8 * MIB)
    assert grown[0] == base[0] and grown[1] == base[1]
    assert grown[2] == b"\0" * HASH_BYTES


def test_validate_rejects_unwritten_blocks():
    bm = BlockMap(4 * MIB)
    bm[0] = chunk_hash(b"a")
    with pytest.raises(BlockMapError, match="3 unwritten"):
        bm.validate()


def test_distinct_hashes_for_refcounting():
    bm = BlockMap(4 * MIB)
    for i in range(len(bm)):
        bm[i] = chunk_hash(b"identical")
    assert len(bm.distinct_hashes()) == 1


def test_disk_and_map_survive_a_full_store_roundtrip(store, tmp_path):
    """The end-to-end property that matters: chunk a disk image, throw the
    original away, and rebuild it byte for byte from the map."""
    disk = os.urandom(5 * MIB + 12345)
    bm = BlockMap(len(disk), chunk_size=MIB)
    for i in range(len(bm)):
        off, length = bm.block_range(i)
        bm[i] = store.put(disk[off:off + length]).hash
    bm.validate()
    bm.save(tmp_path / "disk0.bm")

    reloaded = BlockMap.load(tmp_path / "disk0.bm")
    rebuilt = bytearray(len(disk))
    for i in range(len(reloaded)):
        off, length = reloaded.block_range(i)
        rebuilt[off:off + length] = store.get(reloaded[i])
    assert bytes(rebuilt) == disk
