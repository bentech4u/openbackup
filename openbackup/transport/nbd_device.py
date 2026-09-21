"""BlockDevice over NBD, which is how the VDDK transport is reached.

nbdkit's VDDK plugin exposes a VMDK as an NBD export, so once nbdkit is
running this is a thin adapter over :class:`~openbackup.nbd.client.NbdClient`.
Unlike the datastore transport, VDDK resolves snapshot chains itself, so this
works against a VM that already has snapshots of its own.
"""

from __future__ import annotations

from typing import Iterable

from ..nbd.client import NbdClient, NbdError
from .base import BlockDevice, TransportError


class NbdBlockDevice(BlockDevice):
    def __init__(self, client: NbdClient, owns_client: bool = True):
        self._client = client
        self._owns = owns_client
        self.size = client.size
        self.read_only = client.info.read_only
        self.supports_random_write = not client.info.read_only

    @classmethod
    def connect_unix(cls, path: str, **kw) -> "NbdBlockDevice":
        return cls(NbdClient.connect_unix(path, **kw))

    @classmethod
    def connect_tcp(cls, host: str, port: int = 10809, **kw) -> "NbdBlockDevice":
        return cls(NbdClient.connect_tcp(host, port, **kw))

    def pread(self, offset: int, length: int) -> bytes:
        self._check_range(offset, length)
        try:
            return self._client.pread(offset, length)
        except NbdError as exc:
            raise TransportError(str(exc)) from exc

    def pread_batch(self, ranges: Iterable[tuple[int, int]]) -> list[bytes]:
        ranges = list(ranges)
        for off, length in ranges:
            self._check_range(off, length)
        try:
            # The NBD client pipelines these, so a list of CBT extents costs
            # one round-trip rather than one per extent.
            return self._client.pread_batch(ranges)
        except NbdError as exc:
            raise TransportError(str(exc)) from exc

    def pwrite(self, offset: int, data: bytes) -> None:
        self._check_range(offset, len(data), writing=True)
        try:
            self._client.pwrite(offset, data)
        except NbdError as exc:
            raise TransportError(str(exc)) from exc

    def flush(self) -> None:
        try:
            self._client.flush()
        except NbdError as exc:
            raise TransportError(str(exc)) from exc

    def close(self) -> None:
        if self._owns:
            self._client.close()
