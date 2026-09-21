"""End-to-end test against a real NFS mount.

Skipped unless a loopback export is available, because mounting needs root and
a running NFS server. To enable it::

    dnf install -y nfs-utils
    mkdir -p /srv/openbackup-nfs-test
    echo "/srv/openbackup-nfs-test 127.0.0.1(rw,sync,no_root_squash,no_subtree_check)" \\
        > /etc/exports.d/openbackup-test.exports
    systemctl enable --now nfs-server && exportfs -ra
"""

import os
import random
import shutil

import pytest

from openbackup.repo.backend import NfsMount
from openbackup.repo.blockmap import BlockMap
from openbackup.repo.index import ChunkIndex, rebuild
from openbackup.repo.store import PackedChunkStore

MIB = 1024 * 1024
EXPORT = "/srv/openbackup-nfs-test"

pytestmark = [
    pytest.mark.timeout(300),
    pytest.mark.skipif(os.geteuid() != 0, reason="mounting NFS needs root"),
    pytest.mark.skipif(not os.path.isdir(EXPORT),
                       reason=f"no loopback NFS export at {EXPORT}"),
]


def disk_block(i: int) -> bytes:
    """Deterministic content resembling a guest disk."""
    if i % 2 == 0:
        return b"\0" * MIB                       # unallocated
    if i % 5 == 1:
        return (b"SYSTEM32" * 128) * 1024        # repeated -> dedups
    return random.Random(i).randbytes(MIB)       # incompressible


@pytest.fixture
def nfs_repo(tmp_path):
    mountpoint = tmp_path / "mnt"
    mountpoint.mkdir()
    share_dir = os.path.join(EXPORT, f"repo-{os.getpid()}")
    mount = NfsMount("127.0.0.1", EXPORT, mountpoint, version="4.2")
    mount.mount()
    try:
        mount.verify_writable()
        backend = mount.backend(os.path.basename(share_dir))
        backend.init()
        index = ChunkIndex(tmp_path / "index.sqlite")
        yield mount, backend, index
        index.close()
    finally:
        mount.unmount()
        shutil.rmtree(share_dir, ignore_errors=True)


def test_backup_and_restore_over_nfs(nfs_repo):
    _mount, backend, index = nfs_repo
    store = PackedChunkStore(backend, index, pack_size=32 * MIB)

    disk_size = 96 * MIB
    bm = BlockMap(disk_size)
    for i in range(len(bm)):
        bm[i] = store.put(disk_block(i)).hash
    store.flush()
    bm.validate()

    # The point of packs: a small number of large files, not one per block.
    packs = [f for f in backend.list("packs") if f.endswith(".pack")]
    assert 0 < len(packs) <= 8, f"{len(packs)} packs for {len(bm)} blocks"

    for i in range(len(bm)):
        _off, length = bm.block_range(i)
        assert store.get(bm[i]) == disk_block(i)[:length]


def test_repository_survives_losing_the_local_index(nfs_repo, tmp_path):
    """The share is authoritative. A rebuilt server must reach every block."""
    _mount, backend, index = nfs_repo
    store = PackedChunkStore(backend, index, pack_size=16 * MIB)
    blocks = [disk_block(i) for i in range(24)]
    hashes = [store.put(b).hash for b in blocks]
    store.flush()

    fresh = ChunkIndex(tmp_path / "rebuilt.sqlite")
    try:
        assert rebuild(fresh, backend) == index.pack_count()
        recovered = PackedChunkStore(backend, fresh)
        for digest, block in zip(hashes, blocks):
            assert recovered.get(digest) == block
    finally:
        fresh.close()


def test_mount_is_released_afterwards(tmp_path):
    mountpoint = tmp_path / "mnt2"
    mountpoint.mkdir()
    mount = NfsMount("127.0.0.1", EXPORT, mountpoint, version="4.2")
    with mount:
        assert mount.is_mounted()
        mount.verify_writable()
    assert not mount.is_mounted()
