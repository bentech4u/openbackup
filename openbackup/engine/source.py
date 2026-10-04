"""The VM source the engines talk to: vSphere for metadata and snapshots,
nbdkit's vddk plugin for disk data. Tests substitute a fake with the same
methods."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..nbd.client import NbdClient, NbdError
from ..nbd.nbdkit import Nbdkit, VddkTarget
from ..vsphere.client import VSphere
from ..vsphere.types import DiskInfo


class VSphereSource(VSphere):
    def __init__(self, *args, vddk_libdir: Path, nbdkit: str = "nbdkit",
                 transports: str = "nbdssl:nbd", **kwargs):
        super().__init__(*args, **kwargs)
        self.vddk_libdir = vddk_libdir
        self.nbdkit = nbdkit
        self.transports = transports

    def get_vm(self, moref: str) -> Any:
        return self.vm(moref)

    def vm_moref(self, vm: Any) -> str:
        return vm._moId

    @contextmanager
    def open_disk(self, vm_moref: str, disk: DiskInfo, snapshot_moref: str | None = None,
                  write: bool = False) -> Iterator[NbdClient]:
        target = VddkTarget(
            libdir=self.vddk_libdir, server=self.host, port=self.port, user=self.user,
            password=self._password, thumbprint=self.thumbprint, vm_moref=vm_moref,
            file=disk.file, snapshot_moref=snapshot_moref, transports=self.transports,
        )
        with Nbdkit.vddk(target, readonly=not write, nbdkit=self.nbdkit) as srv, \
                srv.connect() as client:
            try:
                yield client
            except (NbdError, OSError) as e:
                raise NbdError(f"{e}; {srv.failure_reason()}") from None
