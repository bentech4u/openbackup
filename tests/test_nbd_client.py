"""Exercise the NBD client against a real nbdkit server."""

import os

import pytest

from openbackup.nbd import protocol as p
from openbackup.nbd.client import NbdClient, NbdError, NbdServerError
from conftest import requires_nbdkit

pytestmark = [requires_nbdkit, pytest.mark.timeout(120)]

MIB = 1024 * 1024


@pytest.fixture
def client(nbdkit_memory):
    srv = nbdkit_memory("64M")
    with NbdClient.connect_unix(srv.sock) as c:
        yield c


def test_handshake_reports_export_geometry(client):
    assert client.size == 64 * MIB
    assert not client.info.read_only
    assert client.info.min_block >= 1
    assert client.info.max_block <= p.MAX_REQUEST_SIZE


def test_write_then_read_roundtrip(client):
    data = os.urandom(128 * 1024)
    client.pwrite(3 * MIB, data)
    assert client.pread(3 * MIB, len(data)) == data


def test_unwritten_regions_read_as_zero(client):
    assert client.pread(10 * MIB, 8192) == b"\0" * 8192


def test_read_past_end_is_rejected_locally(client):
    with pytest.raises(ValueError, match="past the export size"):
        client.pread(client.size - 512, 1024)


def test_zero_length_read_is_a_noop(client):
    assert client.pread(0, 0) == b""


def test_large_transfer_is_split_across_requests(client):
    """A transfer bigger than the server's per-request cap must still work.

    max_block is forced low so one logical 4 MiB write becomes many commands,
    which is also what keeps the pipelining path under test.
    """
    client.info.max_block = 64 * 1024
    data = os.urandom(4 * MIB)
    client.pwrite(0, data)
    assert client.pread(0, len(data)) == data


def test_pipelined_write_does_not_deadlock(client):
    """The failure mode this guards: we block in send() filling the socket with
    WRITE payloads while the server blocks sending replies we are not reading.
    Many small commands in flight at once is exactly when that bites."""
    client.info.max_block = 32 * 1024
    client._depth = 64
    data = os.urandom(16 * MIB)
    client.pwrite(0, data)
    assert client.pread(0, len(data)) == data


def test_pread_batch_returns_ranges_in_order(client):
    """CBT hands us a list of disjoint extents; they must come back aligned to
    the request order even though NBD replies may arrive out of order."""
    extents = [(0, 4096), (20 * MIB, 8192), (5 * MIB, 4096), (63 * MIB, 1024)]
    expected = []
    for off, size in extents:
        blob = os.urandom(size)
        client.pwrite(off, blob)
        expected.append(blob)
    assert client.pread_batch(extents) == expected


def test_pread_batch_splits_oversized_extents(client):
    client.info.max_block = 16 * 1024
    blob = os.urandom(1 * MIB)
    client.pwrite(8 * MIB, blob)
    got = client.pread_batch([(8 * MIB, 1 * MIB), (0, 4096)])
    assert got[0] == blob
    assert got[1] == b"\0" * 4096


def test_zero_clears_previously_written_data(client):
    client.pwrite(0, b"\xff" * (256 * 1024))
    client.zero(0, 256 * 1024)
    assert client.pread(0, 256 * 1024) == b"\0" * (256 * 1024)


def test_flush_is_accepted(client):
    client.pwrite(0, b"data")
    client.flush()


def test_readonly_export_refuses_writes(nbdkit_memory):
    srv = nbdkit_memory("8M", readonly=True)
    with NbdClient.connect_unix(srv.sock) as c:
        assert c.info.read_only
        with pytest.raises(NbdError, match="read-only"):
            c.pwrite(0, b"nope")


def test_operations_after_close_are_rejected(nbdkit_memory):
    srv = nbdkit_memory("8M")
    c = NbdClient.connect_unix(srv.sock)
    c.close()
    c.close()  # idempotent
    with pytest.raises(NbdError, match="closed"):
        c.pread(0, 512)


def test_connecting_to_a_non_nbd_server_fails_clearly(tmp_path):
    import socket as _socket
    import threading

    path = str(tmp_path / "junk.sock")
    srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        conn.sendall(b"HELLO-NOT-NBD-AT-ALL-" + b"x" * 64)
        conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    with pytest.raises(NbdError, match="not an NBD server"):
        NbdClient.connect_unix(path)
    t.join(timeout=5)
    srv.close()
