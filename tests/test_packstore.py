"""Tests for pack files, the chunk index, and the store built on them."""

import os

import pytest

from openbackup.repo.backend import FilesystemBackend
from openbackup.repo.codec import ChunkCodec, ChunkCorruption, ChunkMissing, chunk_hash
from openbackup.repo.index import ChunkIndex, rebuild
from openbackup.repo.packfile import PackError, PackWriter, read_index
from openbackup.repo.store import PackedChunkStore, pack_path

MIB = 1024 * 1024


@pytest.fixture
def repo(tmp_path):
    backend = FilesystemBackend(tmp_path / "repo")
    backend.init()
    index = ChunkIndex(tmp_path / "index.sqlite")
    store = PackedChunkStore(backend, index, pack_size=2 * MIB)
    yield store, backend, index
    index.close()


# -- pack format ------------------------------------------------------------

def test_pack_roundtrip_via_its_own_trailer():
    """A pack must be readable from itself alone -- that is what lets the
    index be thrown away and rebuilt."""
    codec = ChunkCodec()
    writer = PackWriter()
    blocks = [os.urandom(4096) for _ in range(8)]
    for b in blocks:
        writer.add(chunk_hash(b), codec.encode(b))
    pack, entries = writer.finish()

    recovered = read_index(pack)
    assert [e.hash for e in recovered] == [e.hash for e in entries]
    for block, entry in zip(blocks, recovered):
        assert codec.decode(pack[entry.offset:entry.offset + entry.length],
                            entry.hash) == block


def test_empty_pack_is_refused():
    with pytest.raises(PackError, match="empty pack"):
        PackWriter().finish()


def test_pack_reports_fullness_against_its_target():
    writer = PackWriter(target_size=8192)
    assert not writer.is_full
    writer.add(chunk_hash(b"a"), b"x" * 9000)
    assert writer.is_full


def test_duplicate_within_a_pack_is_stored_once():
    writer = PackWriter()
    blob = b"y" * 100
    digest = chunk_hash(b"a")
    first, second = writer.add(digest, blob), writer.add(digest, blob)
    assert first == second
    assert len(writer) == 1


def test_rejects_a_non_pack():
    with pytest.raises(PackError, match="not a pack file"):
        read_index(b"x" * 64)


# -- store ------------------------------------------------------------------

def test_put_then_get(repo):
    store, _, _ = repo
    data = os.urandom(128 * 1024)
    res = store.put(data)
    store.flush()
    assert store.get(res.hash) == data


def test_chunk_is_readable_before_its_pack_is_flushed(repo):
    """A job must be able to verify its own output before the pack closes."""
    store, _, _ = repo
    data = os.urandom(4096)
    res = store.put(data)
    assert store.get(res.hash) == data
    assert store.exists(res.hash)


def test_duplicate_blocks_deduplicate(repo):
    store, _, _ = repo
    data = os.urandom(64 * 1024)
    first, second = store.put(data), store.put(data)
    assert first.hash == second.hash
    assert first.is_new and not second.is_new
    assert store.stats.chunks_deduped == 1


def test_deduplication_survives_a_flush(repo):
    """Dedup has to work against what is already committed, not just the open
    pack -- that is what makes a second VM cheap."""
    store, _, _ = repo
    data = os.urandom(64 * 1024)
    store.put(data)
    store.flush()
    again = store.put(data)
    assert not again.is_new


def test_many_chunks_become_few_files(repo):
    """The whole point over NFS: file count stays small."""
    store, backend, _ = repo
    for i in range(64):
        store.put(os.urandom(128 * 1024))   # 8 MiB across 2 MiB packs
    store.flush()
    files = [f for f in backend.list("packs") if f.endswith(".pack")]
    assert 3 <= len(files) <= 6, f"expected a handful of packs, got {len(files)}"


def test_pack_rolls_over_at_the_target_size(repo):
    store, _, index = repo
    for _ in range(40):
        store.put(os.urandom(128 * 1024))
    assert index.pack_count() >= 1, "should have rolled at least one pack"


def test_missing_chunk_raises(repo):
    store, _, _ = repo
    with pytest.raises(ChunkMissing):
        store.get(chunk_hash(b"never stored"))


def test_missing_reports_only_absent_chunks(repo):
    store, _, _ = repo
    here = [store.put(os.urandom(4096)).hash for _ in range(3)]
    store.flush()
    absent = chunk_hash(b"absent")
    assert store.missing(here + [absent]) == {absent}


def test_bitrot_inside_a_pack_is_detected(repo):
    """A pack holds thousands of chunks, so silent corruption would poison
    many restore points at once."""
    store, backend, index = repo
    data = os.urandom(8192)
    res = store.put(data)
    store.flush()

    loc = index.locate(res.hash)
    path = backend.path(pack_path(loc.pack_id))
    blob = bytearray(path.read_bytes())
    blob[loc.offset + loc.length - 1] ^= 0xFF
    path.write_bytes(bytes(blob))

    with pytest.raises(ChunkCorruption):
        store.get(res.hash)


def test_failed_job_does_not_write_its_partial_pack(repo):
    """Chunks from a job that never records a restore point would be
    unreferenced anyway; leaving them out keeps the repository clean."""
    store, backend, _ = repo
    with pytest.raises(RuntimeError):
        with store:
            store.put(os.urandom(4096))
            raise RuntimeError("job failed")
    assert not [f for f in backend.list("packs") if f.endswith(".pack")]


# -- index is disposable ----------------------------------------------------

def test_index_rebuilds_from_the_repository(repo, tmp_path):
    """The property that makes the local index safe to lose: point a fresh
    server at an existing repository and every chunk is reachable again."""
    store, backend, index = repo
    blocks = [os.urandom(64 * 1024) for _ in range(20)]
    hashes = [store.put(b).hash for b in blocks]
    store.flush()
    original_packs = index.pack_count()

    fresh = ChunkIndex(tmp_path / "fresh.sqlite")
    try:
        assert rebuild(fresh, backend) == original_packs
        assert fresh.chunk_count() == index.chunk_count()
        recovered = PackedChunkStore(backend, fresh)
        for digest, block in zip(hashes, blocks):
            assert recovered.get(digest) == block
    finally:
        fresh.close()


def test_reindex_adopts_a_pack_the_index_never_learned_about(repo, tmp_path):
    """Crash between writing a pack and indexing it: the pack is an orphan,
    not a loss."""
    store, backend, index = repo
    data = os.urandom(32 * 1024)
    digest = store.put(data).hash
    store.flush()

    index.clear()
    assert not index.has(digest)

    assert rebuild(index, backend) == 1
    assert store.get(digest) == data


def test_reindex_reads_only_pack_tails(repo, monkeypatch, tmp_path):
    """Reindexing must not drag whole packs across the network."""
    store, backend, index = repo
    for _ in range(10):
        store.put(os.urandom(128 * 1024))
    store.flush()

    read_bytes = 0
    original = FilesystemBackend.read_range

    def counting(self, relpath, offset, length):
        nonlocal read_bytes
        read_bytes += length
        return original(self, relpath, offset, length)

    monkeypatch.setattr(FilesystemBackend, "read_range", counting)
    fresh = ChunkIndex(tmp_path / "tails.sqlite")
    try:
        rebuild(fresh, backend)
    finally:
        fresh.close()
    total = sum(backend.size(f) for f in backend.list("packs")
                if f.endswith(".pack"))
    assert read_bytes < total // 10, (
        f"reindex read {read_bytes} of {total} bytes; should only touch tails")
