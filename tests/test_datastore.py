"""Tests for the datastore HTTPS transport's Range handling."""

import os

import pytest

from fake_datastore import FakeDatastore
from openbackup.transport.base import TransportError, UnsupportedOperation
from openbackup.transport.datastore import DatastoreBlockDevice, DatastoreTransport

pytestmark = pytest.mark.timeout(60)

MIB = 1024 * 1024


def make_device(srv, read_only=True, workers=4):
    t = DatastoreTransport("127.0.0.1", auth=("u", "p"), max_workers=workers)
    return DatastoreBlockDevice(t, srv.url, read_only=read_only)


@pytest.fixture
def disk():
    data = os.urandom(4 * MIB)
    srv = FakeDatastore(data)
    yield srv, data
    srv.stop()


def test_size_comes_from_a_head_request(disk):
    srv, data = disk
    dev = make_device(srv)
    assert dev.size == len(data)
    assert srv.requests[0].startswith("HEAD")


def test_range_read_returns_exactly_that_range(disk):
    srv, data = disk
    dev = make_device(srv)
    assert dev.pread(MIB, 65536) == data[MIB:MIB + 65536]


def test_read_of_the_final_partial_block(disk):
    srv, data = disk
    dev = make_device(srv)
    tail = dev.size - 1000
    assert dev.pread(tail, 1000) == data[tail:]


def test_zero_length_read_makes_no_request(disk):
    srv, _ = disk
    dev = make_device(srv)
    before = len(srv.requests)
    assert dev.pread(0, 0) == b""
    assert len(srv.requests) == before


def test_read_past_the_end_is_rejected(disk):
    srv, _ = disk
    dev = make_device(srv)
    with pytest.raises(ValueError, match="past the disk size"):
        dev.pread(dev.size - 10, 100)


def test_batch_read_preserves_request_order(disk):
    """CBT extents come back in the order asked for, despite concurrency."""
    srv, data = disk
    dev = make_device(srv, workers=4)
    extents = [(3 * MIB, 4096), (0, 8192), (MIB, 4096), (2 * MIB, 16384)]
    got = dev.pread_batch(extents)
    assert got == [data[o:o + n] for o, n in extents]


def test_batch_read_is_concurrent(disk):
    srv, data = disk
    dev = make_device(srv, workers=4)
    extents = [(i * 4096, 4096) for i in range(32)]
    assert dev.pread_batch(extents) == [data[o:o + n] for o, n in extents]


def test_server_ignoring_range_is_fatal_not_silently_wrong():
    """A 200 with the whole file would put the wrong bytes at this offset and
    blow up memory on a real disk. It has to fail loudly."""
    data = os.urandom(256 * 1024)
    srv = FakeDatastore(data, ignore_range=True)
    try:
        dev = make_device(srv)
        with pytest.raises(TransportError, match="ignored the Range header"):
            dev.pread(1024, 4096)
    finally:
        srv.stop()


def test_server_returning_a_different_range_is_detected():
    """Content-Range is checked against what we asked for, so a shifted
    response cannot end up recorded at the wrong offset."""
    data = os.urandom(256 * 1024)
    srv = FakeDatastore(data, wrong_range=True)
    try:
        dev = make_device(srv)
        with pytest.raises(TransportError, match="but we asked for"):
            dev.pread(1024, 4096)
    finally:
        srv.stop()


def test_short_body_is_rejected():
    data = os.urandom(256 * 1024)
    srv = FakeDatastore(data, short_body=True)
    try:
        dev = make_device(srv)
        with pytest.raises(TransportError):
            dev.pread(0, 4096)
    finally:
        srv.stop()


def test_random_writes_are_refused_with_a_useful_message(disk):
    srv, _ = disk
    dev = make_device(srv, read_only=False)
    assert dev.supports_random_write is False
    with pytest.raises(UnsupportedOperation, match="write_stream"):
        dev.pwrite(0, b"x")


def test_write_stream_replaces_the_whole_file(disk):
    srv, _ = disk
    dev = make_device(srv, read_only=False)
    new = os.urandom(dev.size)
    dev.write_stream(iter([new[i:i + 65536]
                           for i in range(0, len(new), 65536)]), len(new))
    assert srv.uploads and srv.uploads[0] == new


def test_write_stream_on_a_read_only_disk_is_refused(disk):
    srv, _ = disk
    dev = make_device(srv, read_only=True)
    with pytest.raises(UnsupportedOperation, match="read-only"):
        dev.write_stream(iter([b"x"]), 1)


def test_write_stream_detects_a_short_stream(disk):
    """Declaring more bytes than we send would leave a truncated disk."""
    srv, _ = disk
    dev = make_device(srv, read_only=False)
    with pytest.raises(TransportError, match="supplied"):
        dev.write_stream(iter([b"a" * 100]), 200)


def test_missing_file_reports_clearly():
    srv = FakeDatastore(b"")
    try:
        t = DatastoreTransport("127.0.0.1", auth=("u", "p"))
        dev = DatastoreBlockDevice(t, srv.url)
        assert dev.size == 0
    finally:
        srv.stop()


def test_transport_requires_credentials():
    with pytest.raises(ValueError, match="cookie or basic-auth"):
        DatastoreTransport("vc.lab")


# -- whole-file reads -------------------------------------------------------

def test_read_all_fetches_a_small_file(disk):
    """Descriptors are read whole; Range is not involved at all."""
    srv, data = disk
    dev = make_device(srv)
    assert dev.read_all() == data


def test_read_all_refuses_a_disk_sized_file(disk):
    srv, _ = disk
    dev = make_device(srv)
    dev.size = 64 * 1024 * 1024 * 1024
    with pytest.raises(TransportError, match="refusing a whole-file read"):
        dev.read_all()


def test_whole_file_range_on_a_small_file_is_accepted():
    """RFC 7233 lets a server answer 200 to a Range it chooses to ignore.
    Harmless when the request covered the whole of a small file -- which is
    exactly what vCenter does for VMDK descriptors."""
    data = os.urandom(554)
    srv = FakeDatastore(data, ignore_range=True)
    try:
        dev = make_device(srv)
        assert dev.pread(0, len(data)) == data
    finally:
        srv.stop()


def test_whole_file_response_is_still_refused_for_a_partial_range():
    """The dangerous case: a 200 for a slice of a large file would mean
    streaming the entire disk for every block we read."""
    data = os.urandom(256 * 1024)
    srv = FakeDatastore(data, ignore_range=True)
    try:
        dev = make_device(srv)
        with pytest.raises(TransportError, match="ignored the Range header"):
            dev.pread(0, 4096)
    finally:
        srv.stop()
