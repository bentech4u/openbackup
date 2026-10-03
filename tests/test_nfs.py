"""Against a real NFS export. Needs root and an export; set
OPENBACKUP_TEST_NFS=server:/export (default: this host's loopback export used
in development, if present)."""

from __future__ import annotations

import os
import secrets
import shutil

import pytest

from openbackup.repo import nfs
from openbackup.repo.blockmap import BLOCK_SIZE, BlockMap
from openbackup.repo.maintenance import verify_point
from openbackup.repo.repository import Repository, new_point_id

TARGET = os.environ.get("OPENBACKUP_TEST_NFS", "127.0.0.1:/srv/openbackup-nfs-test")
SERVER, _, EXPORT = TARGET.partition(":")

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0 or (SERVER == "127.0.0.1" and not os.path.isdir(EXPORT)),
    reason="needs root and an NFS export")


@pytest.fixture
def mounted(tmp_path):
    mp = tmp_path / "mnt"
    try:
        ours = nfs.ensure_mounted(SERVER, EXPORT, mp, "nfsvers=4.2,hard,timeo=50,retrans=2")
    except nfs.NfsError as e:
        pytest.skip(f"cannot mount {TARGET}: {e}")
    sub = mp / f"pytest-{secrets.token_hex(4)}"
    yield sub
    shutil.rmtree(sub, ignore_errors=True)
    if ours:
        nfs.unmount(mp)


def test_mount_is_hard_and_adopted(mounted):
    mp = mounted.parent
    info = nfs.find_mount(mp)
    assert info is not None and info.fstype.startswith("nfs")
    opts = open("/proc/self/mounts").read()
    line = next(line for line in opts.splitlines() if f" {mp} " in line)
    assert "hard" in line or "soft" not in line
    # A second call adopts the existing mount rather than stacking another.
    assert nfs.ensure_mounted(SERVER, EXPORT, mp) is False


def test_check_path_on_nfs(mounted):
    res = nfs.check_path(mounted)
    assert res.ok, res.message
    assert res.capacity_bytes > 0


def test_repository_on_nfs(mounted, tmp_path):
    data = os.urandom(3 * BLOCK_SIZE + 77)
    with Repository.create(mounted, tmp_path / "idx", passphrase="nfs test passphrase") as r:
        m = BlockMap(len(data))
        with r.writer() as w:
            for i in range(m.block_count):
                m.ids[i] = w.put(data[i * BLOCK_SIZE:(i + 1) * BLOCK_SIZE])
        pid = new_point_id()
        r.save_point(pid, {"created_at": "2026-10-03T00:00:00+00:00"}, {"2000": m})
    # Fresh index, as on a rebuilt server.
    with Repository.open(mounted, tmp_path / "idx2", "nfs test passphrase") as r:
        assert verify_point(r, pid).ok
        m = r.load_map(pid, "2000")
        out = b"".join(r.read_chunk(c, m.block_length(i)) for i, c in enumerate(m.ids))
        assert out == data


def test_mount_failure_is_reported(tmp_path):
    with pytest.raises(nfs.NfsError):
        nfs.ensure_mounted(SERVER, "/does/not/exist", tmp_path / "m",
                           "nfsvers=4.2,hard,timeo=10,retrans=1")
