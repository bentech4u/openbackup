from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from openbackup.repo import crypto
from openbackup.repo.blockmap import BLOCK_SIZE, BlockMap
from openbackup.repo.crypto import ZERO_ID, IntegrityError
from openbackup.repo.maintenance import collect_garbage, select_expired, verify_point
from openbackup.repo.repository import Repository, RepositoryError, new_point_id


@pytest.fixture(params=[None, "a strong repo passphrase"], ids=["plain", "encrypted"])
def repo(request, tmp_path):
    r = Repository.create(tmp_path / "repo", tmp_path / "index", passphrase=request.param)
    yield r
    r.close()


def _disk(blocks: int, seed: int = 0) -> bytes:
    rnd = __import__("random").Random(seed)
    return rnd.randbytes(blocks * BLOCK_SIZE)


def _backup(repo: Repository, data: bytes, prev: BlockMap | None = None) -> tuple[str, BlockMap]:
    m = BlockMap(len(data))
    with repo.writer() as w:
        for i in range(m.block_count):
            m.ids[i] = w.put(data[i * BLOCK_SIZE:(i + 1) * BLOCK_SIZE])
    pid = new_point_id()
    repo.save_point(pid, {"created_at": datetime.now(UTC).isoformat()}, {"2000": m})
    return pid, m


def _restore(repo: Repository, pid: str) -> bytes:
    m = repo.load_map(pid, "2000")
    return b"".join(repo.read_chunk(c, m.block_length(i)) for i, c in enumerate(m.ids))


def test_round_trip_with_partial_last_block(repo):
    data = _disk(3) + b"tail" * 1000
    pid, _ = _backup(repo, data)
    assert _restore(repo, pid) == data


def test_zero_blocks_are_not_stored(repo):
    data = bytes(4 * BLOCK_SIZE)
    pid, m = _backup(repo, data)
    assert m.ids == [ZERO_ID] * 4
    assert repo.stats().chunks == 0
    assert _restore(repo, pid) == data


def test_dedup_across_points(repo):
    data = _disk(4)
    _backup(repo, data)
    before = repo.stats().chunks
    changed = bytearray(data)
    changed[5] ^= 0xFF
    _backup(repo, bytes(changed))
    assert repo.stats().chunks == before + 1


def test_index_rebuilds_from_pack_trailers(repo, tmp_path):
    data = _disk(3)
    pid, _ = _backup(repo, data)
    passphrase = "a strong repo passphrase" if repo.codec.encrypted else None
    root = repo.root
    repo.close()
    for f in (tmp_path / "index").iterdir():
        f.unlink()
    with Repository.open(root, tmp_path / "index", passphrase) as r2:
        assert _restore(r2, pid) == data


def test_corruption_is_detected(repo):
    data = _disk(2)
    pid, m = _backup(repo, data)
    pack = next(repo.root.glob("packs/*/*.pack"))
    raw = bytearray(pack.read_bytes())
    raw[40] ^= 0xFF
    pack.write_bytes(bytes(raw))
    repo._fds.clear()
    with pytest.raises(IntegrityError):
        _restore(repo, pid)
    assert not verify_point(repo, pid).ok


def test_save_point_refuses_missing_chunks(repo):
    m = BlockMap(BLOCK_SIZE)
    m.ids[0] = os.urandom(32)
    with pytest.raises(RepositoryError):
        repo.save_point(new_point_id(), {}, {"2000": m})


def test_deleting_a_point_and_gc_frees_space_but_keeps_others(repo):
    a = _disk(4, seed=1)
    pid_a, _ = _backup(repo, a)
    b = a[:2 * BLOCK_SIZE] + _disk(2, seed=2)
    pid_b, _ = _backup(repo, b)
    repo.delete_point(pid_a)
    res = collect_garbage(repo)
    assert res.bytes_freed > 0
    assert repo.stats().chunks == 4
    assert _restore(repo, pid_b) == b


def test_gc_repacks_mostly_dead_packs(repo):
    a = _disk(8, seed=3)
    pid_a, _ = _backup(repo, a)
    keep = a[:BLOCK_SIZE] + bytes(7 * BLOCK_SIZE)
    pid_b, _ = _backup(repo, keep)
    repo.delete_point(pid_a)
    res = collect_garbage(repo)
    assert res.packs_repacked == 1
    assert repo.stats().chunks == 1
    assert _restore(repo, pid_b) == keep


def test_encrypted_repo_requires_right_passphrase(tmp_path):
    r = Repository.create(tmp_path / "r", tmp_path / "i", passphrase="right passphrase")
    _backup(r, _disk(1))
    r.close()
    with pytest.raises(RepositoryError):
        Repository.open(tmp_path / "r", tmp_path / "i")
    with pytest.raises(crypto.WrongPassphrase):
        Repository.open(tmp_path / "r", tmp_path / "i", "wrong passphrase")


def test_encrypted_repo_hides_content(tmp_path):
    marker = b"SECRET-MARKER-" * 1000
    data = marker + bytes(BLOCK_SIZE - len(marker))
    r = Repository.create(tmp_path / "r", tmp_path / "i", passphrase="right passphrase")
    _backup(r, data)
    r.close()
    for p in (tmp_path / "r").rglob("*"):
        if p.is_file():
            assert b"SECRET-MARKER" not in p.read_bytes()


def test_create_refuses_non_empty_dir(tmp_path):
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "something").write_text("x")
    with pytest.raises(RepositoryError):
        Repository.create(tmp_path / "r", tmp_path / "i")


def test_retention_selection():
    now = datetime(2026, 10, 1, tzinfo=UTC)
    points = [{"id": str(d), "created_at": (now - timedelta(days=d)).isoformat()}
              for d in range(10)]
    assert sorted(select_expired(points, 3, 0, now), key=int) == [str(d) for d in range(3, 10)]
    assert sorted(select_expired(points, 0, 5, now), key=int) == [str(d) for d in range(5, 10)]
    # Either rule keeps a point.
    assert sorted(select_expired(points, 7, 2, now), key=int) == ["7", "8", "9"]
    # The newest point always survives.
    assert select_expired(points[:1], 0, 0, now) == []


def test_check_path(tmp_path):
    from openbackup.repo.nfs import check_path

    res = check_path(tmp_path / "x")
    assert res.ok and res.free_bytes > 0


def test_nfs_validation():
    from openbackup.repo.nfs import NfsError, validate

    validate("10.0.0.5", "/volume1/backup", "nfsvers=4.2,hard")
    for args in [("10.0.0.5;rm", "/x", ""), ("h", "relative", ""), ("h", "/x", "soft"),
                 ("h", "/x/../etc", ""), ("h", "/x", "hard,$(id)")]:
        with pytest.raises(NfsError):
            validate(*args)
