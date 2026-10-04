from __future__ import annotations

import os
import shutil
import subprocess
from datetime import UTC, datetime

import pytest

from openbackup.flr.nbdserver import PointNbdServer
from openbackup.nbd.client import NbdClient, NbdError
from openbackup.repo.blockmap import BLOCK_SIZE, BlockMap
from openbackup.repo.repository import Repository, new_point_id


@pytest.fixture
def point(tmp_path):
    repo = Repository.create(tmp_path / "repo", tmp_path / "idx", passphrase="flr passphrase!!")
    # Whole sectors, like any VM disk.
    disks = {"2000": os.urandom(3 * BLOCK_SIZE) + bytes(2 * BLOCK_SIZE) + b"tail" * 256,
             "2001": os.urandom(BLOCK_SIZE // 2)}
    maps = {}
    with repo.writer() as w:
        for key, data in disks.items():
            m = BlockMap(len(data))
            for i in range(m.block_count):
                m.ids[i] = w.put(data[i * BLOCK_SIZE:(i + 1) * BLOCK_SIZE])
            maps[key] = m
    pid = new_point_id()
    repo.save_point(pid, {"created_at": datetime.now(UTC).isoformat()}, maps)
    srv = PointNbdServer(repo, pid, tmp_path / "nbd.sock").start()
    yield srv, disks
    srv.stop()
    repo.close()


def test_serves_each_disk_by_key(point):
    srv, disks = point
    for key, data in disks.items():
        with NbdClient.connect_unix(str(srv.socket_path), export=key) as c:
            assert c.size == len(data) and c.read_only
            reqs = [(o, min(777_777, len(data) - o)) for o in range(0, len(data), 777_777)]
            assert b"".join(c.pread_many(reqs, depth=4)) == data


def test_unknown_export_and_writes_are_refused(point):
    srv, _ = point
    with pytest.raises(NbdError):
        NbdClient.connect_unix(str(srv.socket_path), export="9999")
    with NbdClient.connect_unix(str(srv.socket_path), export="2000") as c:
        with pytest.raises(NbdError):
            c.pwrite(0, b"x" * 512)
        assert c.pread(0, 16)  # connection still usable after a refused write


@pytest.mark.skipif(shutil.which("qemu-img") is None, reason="qemu-img not installed")
def test_qemu_reads_it(point, tmp_path):
    srv, disks = point
    out = tmp_path / "out.raw"
    uri = f"nbd+unix:///2000?socket={srv.socket_path}"
    subprocess.run(["qemu-img", "convert", "-f", "raw", uri, "-O", "raw", str(out)], check=True)
    assert out.read_bytes() == disks["2000"]
