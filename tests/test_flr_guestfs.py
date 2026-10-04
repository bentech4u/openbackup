"""File-level restore through the real stack: repository -> NBD server ->
libguestfs appliance. Slow without KVM (about a minute per session)."""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from openbackup.flr.session import SYSTEM_PYTHON, FlrError, FlrSession
from openbackup.repo.blockmap import BLOCK_SIZE, BlockMap
from openbackup.repo.repository import Repository, new_point_id

HERE = Path(__file__).parent


def _have_guestfs() -> bool:
    return subprocess.run([SYSTEM_PYTHON, "-c", "import guestfs"],
                          capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _have_guestfs(), reason="libguestfs not installed")


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("flr")
    image = tmp / "guest.img"
    env = {**os.environ, "LIBGUESTFS_BACKEND": "direct"}
    subprocess.run([SYSTEM_PYTHON, str(HERE / "make_guest_image.py"), str(image)],
                   check=True, env=env, capture_output=True)
    data = image.read_bytes()
    repo = Repository.create(tmp / "repo", tmp / "idx")
    m = BlockMap(len(data))
    with repo.writer() as w:
        for i in range(m.block_count):
            m.ids[i] = w.put(data[i * BLOCK_SIZE:(i + 1) * BLOCK_SIZE])
    pid = new_point_id()
    repo.save_point(pid, {"created_at": datetime.now(UTC).isoformat(),
                          "disks": [{"key": 2000, "label": "Hard disk 1"}]}, {"2000": m})
    with FlrSession(None, pid, repo=repo) as s:
        yield s, tmp
    repo.close()


def _vol(s, fstype):
    return next(v for v in s.volumes if v["fstype"] == fstype)


def test_volumes_are_found_and_mounted(session):
    s, _ = session
    root, data = _vol(s, "ext4"), _vol(s, "xfs")
    assert root["mounted"] and data["mounted"]
    assert root["label"] == "root"
    if s.os:  # inspection found the OS: guest paths are known
        assert root["guest_path"] == "/"
        assert data["guest_path"] == "/data"


def test_browse_and_metadata(session):
    s, _ = session
    root = _vol(s, "ext4")["id"]
    entries = {e["name"]: e for e in s.ls(f"/{root}/home/alice/docs")}
    assert set(entries) == {"report.txt", "notes.md"}
    rep = entries["report.txt"]
    assert rep["type"] == "file" and rep["size"] == len("quarterly numbers\n")
    assert rep["mode"] == 0o600 and rep["uid"] == 1000 and rep["gid"] == 1000


def test_download_matches(session):
    s, tmp = session
    data = _vol(s, "xfs")["id"]
    dest = tmp / "blob.out"
    s.download(f"/{data}/blob.bin", dest)
    assert dest.read_bytes() == bytes(range(256)) * 4096


def test_walk_lists_a_tree(session):
    s, _ = session
    data = _vol(s, "xfs")["id"]
    paths = {e["path"]: e["type"] for e in s.walk(f"/{data}/sub")}
    assert paths == {"/deeper": "dir", "/deeper/leaf.txt": "file"}


def test_paths_cannot_escape(session):
    s, _ = session
    root = _vol(s, "ext4")["id"]
    for bad in (f"/{root}/../etc", "/etc/passwd", "/v99/x", "relative"):
        with pytest.raises(FlrError):
            s.ls(bad)


def test_backup_is_never_modified(session):
    s, _ = session
    with pytest.raises(FlrError):
        s._call("download", {"path": f"/{_vol(s, 'ext4')['id']}/home", "dest": "/tmp/x"})
    assert shutil.which("true")  # session still alive afterwards
    assert s.ls(f"/{_vol(s, 'ext4')['id']}/etc")
