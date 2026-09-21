"""Tests for block maps: what a restore point actually records."""

import os

import pytest

from openbackup.repo.blockmap import BlockMap, BlockMapError, DEFAULT_CHUNK_SIZE
from openbackup.repo.codec import HASH_BYTES, chunk_hash

MIB = 1024 * 1024


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


def test_disk_and_map_survive_a_full_store_roundtrip(packed_store, tmp_path):
    """The end-to-end property that matters: chunk a disk image, throw the
    original away, and rebuild it byte for byte from the map."""
    disk = os.urandom(5 * MIB + 12345)
    bm = BlockMap(len(disk), chunk_size=MIB)
    for i in range(len(bm)):
        off, length = bm.block_range(i)
        bm[i] = packed_store.put(disk[off:off + length]).hash
    packed_store.flush()
    bm.validate()
    bm.save(tmp_path / "disk0.bm")

    reloaded = BlockMap.load(tmp_path / "disk0.bm")
    rebuilt = bytearray(len(disk))
    for i in range(len(reloaded)):
        off, length = reloaded.block_range(i)
        rebuilt[off:off + length] = packed_store.get(reloaded[i])
    assert bytes(rebuilt) == disk
