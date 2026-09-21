"""BlockDevice over an ordinary file, used for Direct NFS access.

When a VM's datastore is an NFS export we can reach ourselves, reading the
flat VMDK from a mounted copy of that export beats going through the vSphere
datastore endpoint by a wide margin: no hostd in the path, no per-block HTTPS
request, and the kernel's readahead works for us. Measured against a lab NAS,
91 MiB/s versus 16 MiB/s.

It also gives us something the HTTPS endpoint cannot: the filesystem knows
which parts of a sparse file were never written. Where the export supports it
(NFSv4.2 or a local filesystem), :meth:`allocated_extents` skips unwritten
space entirely -- which matters because VMware's own allocated-block query
returns "all of it" for thin disks on NFS datastores.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from .base import BlockDevice, TransportError, UnsupportedOperation


class LocalFileBlockDevice(BlockDevice):
    """A disk image reached as a file."""

    def __init__(self, path: Path | str, *, read_only: bool = True):
        self.path = Path(path)
        self.read_only = read_only
        self.supports_random_write = not read_only
        flags = os.O_RDONLY if read_only else os.O_RDWR
        try:
            self._fd = os.open(self.path, flags)
        except FileNotFoundError as exc:
            raise TransportError(f"{self.path} does not exist") from exc
        except PermissionError as exc:
            raise TransportError(f"{self.path} is not readable: {exc}") from exc
        except OSError as exc:
            raise TransportError(f"cannot open {self.path}: {exc}") from exc
        try:
            st = os.fstat(self._fd)
        except OSError as exc:
            os.close(self._fd)
            raise TransportError(f"cannot stat {self.path}: {exc}") from exc
        self.size = st.st_size
        #: Bytes the filesystem has actually reserved. On a thin disk this is
        #: far below the logical size and is the honest measure of real data.
        self.allocated = st.st_blocks * 512
        self._closed = False

    # -- reading ------------------------------------------------------------

    def pread(self, offset: int, length: int) -> bytes:
        self._check_open()
        self._check_range(offset, length)
        if length == 0:
            return b""
        out = bytearray()
        while len(out) < length:
            try:
                chunk = os.pread(self._fd, length - len(out), offset + len(out))
            except OSError as exc:
                raise TransportError(
                    f"{self.path}: read at {offset + len(out)} failed: {exc}"
                ) from exc
            if not chunk:
                break
            out += chunk
        if len(out) != length:
            raise TransportError(
                f"{self.path}: read {len(out)} of {length} bytes at {offset}")
        return bytes(out)

    def pread_batch(self, ranges: Iterable[tuple[int, int]]) -> list[bytes]:
        # Serial is right here: this is a local file descriptor, so there is no
        # round-trip latency to hide, and the kernel's readahead already
        # handles sequential access better than we would by interleaving.
        return [self.pread(off, length) for off, length in ranges]

    # -- writing ------------------------------------------------------------

    def pwrite(self, offset: int, data: bytes) -> None:
        self._check_open()
        self._check_range(offset, len(data), writing=True)
        if not data:
            return
        written = 0
        while written < len(data):
            try:
                written += os.pwrite(self._fd, data[written:], offset + written)
            except OSError as exc:
                raise TransportError(
                    f"{self.path}: write at {offset + written} failed: {exc}"
                ) from exc

    def flush(self) -> None:
        if self._closed or self.read_only:
            return
        try:
            os.fsync(self._fd)
        except OSError as exc:
            raise TransportError(f"{self.path}: fsync failed: {exc}") from exc

    # -- sparseness ---------------------------------------------------------

    def allocated_extents(self, max_extents: int = 500_000
                          ) -> list[tuple[int, int]] | None:
        """Regions the filesystem says hold real data, or None if it cannot say.

        Returns None rather than an empty list when the filesystem does not
        implement SEEK_HOLE -- NFSv3 has no such operation. The distinction
        matters: "no holes reported" and "holes cannot be detected" would
        otherwise look identical, and treating the latter as the former would
        skip live data.
        """
        self._check_open()
        if self.size == 0:
            return []
        extents: list[tuple[int, int]] = []
        pos = 0
        while pos < self.size:
            try:
                data_start = os.lseek(self._fd, pos, os.SEEK_DATA)
            except OSError:
                # ENXIO means no more data: everything past here is a hole.
                break
            try:
                hole = os.lseek(self._fd, data_start, os.SEEK_HOLE)
            except OSError:
                hole = self.size
            if hole <= data_start:
                break
            extents.append((data_start, hole - data_start))
            pos = hole
            if len(extents) >= max_extents:
                # Pathologically fragmented; the extent list would cost more
                # than it saves, so fall back to treating the disk as full.
                return None
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
        except OSError:
            pass

        # A filesystem without SEEK_HOLE support reports the whole file as one
        # data extent, which is indistinguishable from a fully-written disk.
        # Only trust the answer when it actually tells us something.
        if len(extents) == 1 and extents[0] == (0, self.size):
            if self.allocated and self.allocated < self.size * 0.95:
                # st_blocks says the file is sparse, so SEEK_HOLE is lying to
                # us by omission rather than the disk being full.
                return None
            return extents
        return extents

    # -- lifecycle ----------------------------------------------------------

    def _check_open(self) -> None:
        if self._closed:
            raise TransportError(f"operation on closed {self.path}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self._fd)
        except OSError:
            pass
