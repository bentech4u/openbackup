"""Regression tests for NBD reply framing.

These use FakeNbdServer so the packetisation is deterministic rather than at
the mercy of the kernel and the real server's buffering.
"""

import os

import pytest

from openbackup.nbd import protocol as p
from openbackup.nbd.client import NbdClient, NbdError
from fake_nbd import FakeNbdServer

pytestmark = pytest.mark.timeout(60)

MIB = 1024 * 1024


@pytest.fixture
def split_server(tmp_path):
    srv = FakeNbdServer(str(tmp_path / "split.sock"), size=4 * MIB,
                        split_read_replies=True)
    yield srv
    srv.stop()


def test_read_completes_when_payload_trails_its_header(split_server):
    """The last read of a batch must not return while its payload is in flight.

    A command leaves the in-flight set when its reply *header* is parsed. If
    that alone ends the transfer loop, the final read comes back truncated.
    """
    expected = os.urandom(64 * 1024)
    split_server.data[:len(expected)] = expected
    with NbdClient.connect_unix(split_server.path, depth=8) as c:
        assert c.pread(0, len(expected)) == expected


def test_pipelined_batch_survives_trailing_payloads(split_server):
    """Same hazard across many commands, where only the last one is exposed."""
    blob = os.urandom(1 * MIB)
    split_server.data[:len(blob)] = blob
    with NbdClient.connect_unix(split_server.path, depth=8) as c:
        c.info.max_block = 32 * 1024      # -> 32 pipelined commands
        assert c.pread(0, len(blob)) == blob


def test_truncated_payload_raises_instead_of_returning_short_data(tmp_path):
    """Short data must never be returned as if it were complete.

    A truncated block written into the repository is silent corruption: nothing
    detects it until someone tries to restore from that point.
    """
    srv = FakeNbdServer(str(tmp_path / "short.sock"), size=1 * MIB,
                        short_read_by=1024)
    try:
        with NbdClient.connect_unix(srv.path) as c:
            with pytest.raises(NbdError) as excinfo:
                c.pread(0, 4096)
            msg = str(excinfo.value)
            assert "short read" in msg or "closed the connection" in msg
    finally:
        srv.stop()


def test_multiple_sequential_batches_leave_no_residue(split_server):
    """Leftover bytes between calls are how a framing bug turns into a bogus
    reply magic on the *next* operation."""
    blob = os.urandom(256 * 1024)
    split_server.data[:len(blob)] = blob
    with NbdClient.connect_unix(split_server.path, depth=4) as c:
        c.info.max_block = 64 * 1024
        for _ in range(8):
            assert c.pread(0, len(blob)) == blob
            assert len(c._rbuf) == 0, "unconsumed bytes left in the read buffer"


def test_write_then_read_against_fake_server(tmp_path):
    srv = FakeNbdServer(str(tmp_path / "rw.sock"), size=2 * MIB)
    try:
        with NbdClient.connect_unix(srv.path, depth=8) as c:
            data = os.urandom(512 * 1024)
            c.pwrite(1 * MIB, data)
            c.flush()
            assert bytes(srv.data[1 * MIB:1 * MIB + len(data)]) == data
            assert c.pread(1 * MIB, len(data)) == data
    finally:
        srv.stop()
    assert srv.error is None
