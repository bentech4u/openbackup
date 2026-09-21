"""The transport abstraction: random-access views of a virtual disk.

VADP offers several ways to move blocks, and they differ in more than speed.
The interface therefore states capabilities rather than pretending they are
interchangeable:

* **VDDK** (NBD / HotAdd / SAN, via nbdkit) resolves snapshot chains itself and
  supports arbitrary-offset writes. Fastest, but the library is entitlement-
  gated behind Broadcom's support portal.
* **Datastore HTTPS** reads flat VMDKs over the `/folder` endpoint using Range
  requests. No VDDK needed and it supports the offset-addressed reads that CBT
  incrementals require, but it hands back raw files, so it can only read a disk
  whose contents live in a single flat file.

A backup job is written against this interface, so which one is in use is a
configuration decision rather than a different code path.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable, Iterator


class TransportError(Exception):
    """Any failure moving blocks to or from a virtual disk."""


class UnsupportedOperation(TransportError):
    """The backend cannot do this: check the capability flag before calling."""


class BlockDevice(ABC):
    """A random-access view of one virtual disk.

    Implementations must either satisfy a read in full or raise. Returning
    short data would put a truncated block into the repository, and nothing
    would notice until someone tried to restore from it.
    """

    #: Logical size of the disk in bytes.
    size: int = 0
    #: Whether writes are rejected outright.
    read_only: bool = True
    #: Whether :meth:`pwrite` can write at an arbitrary offset. When false, the
    #: only way to write the disk is :meth:`write_stream` from offset zero,
    #: which rules out restoring just the blocks that differ.
    supports_random_write: bool = False

    # -- reading ------------------------------------------------------------

    @abstractmethod
    def pread(self, offset: int, length: int) -> bytes:
        """Read exactly `length` bytes at `offset`."""

    def pread_batch(self, ranges: Iterable[tuple[int, int]]) -> list[bytes]:
        """Read several disjoint ranges.

        This is the shape CBT hands us. Backends that can overlap requests
        should override this; the default is a serial fallback.
        """
        return [self.pread(off, length) for off, length in ranges]

    # -- writing ------------------------------------------------------------

    def pwrite(self, offset: int, data: bytes) -> None:
        """Write `data` at `offset`."""
        raise UnsupportedOperation(
            f"{type(self).__name__} cannot write at an arbitrary offset"
        )

    def write_stream(self, chunks: Iterator[bytes], total: int) -> None:
        """Write a whole disk sequentially from offset zero.

        The fallback path for backends without random writes. `total` must be
        the full disk size: some backends have to declare the length before
        sending any data.
        """
        offset = 0
        for chunk in chunks:
            self.pwrite(offset, chunk)
            offset += len(chunk)
        if offset != total:
            raise TransportError(
                f"stream supplied {offset} bytes, expected {total}"
            )

    def flush(self) -> None:
        """Push writes to stable storage. A no-op where it does not apply."""

    # -- lifecycle ----------------------------------------------------------

    @abstractmethod
    def close(self) -> None:
        ...

    def __enter__(self) -> "BlockDevice":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- helpers ------------------------------------------------------------

    def _check_range(self, offset: int, length: int, writing: bool = False) -> None:
        if offset < 0 or length < 0:
            raise ValueError("offset and length must be non-negative")
        if offset + length > self.size:
            raise ValueError(
                f"range {offset}+{length} extends past the disk size {self.size}"
            )
        if writing and self.read_only:
            raise UnsupportedOperation("disk is open read-only")
