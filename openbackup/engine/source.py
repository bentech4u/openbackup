"""The VM source the engines talk to: vSphere for metadata and snapshots,
and for disk data either a direct read-only NFS mount of the datastore or
nbdkit's vddk plugin. Tests substitute a fake with the same methods."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyVmomi import vim

from ..nbd.client import NbdClient, NbdError
from ..nbd.nbdkit import Nbdkit, VddkTarget
from ..repo import nfs
from ..vsphere.client import VSphere, VSphereError
from ..vsphere.nfsdirect import (
    DirectNfsError,
    FlatDisk,
    FlatDiskWriter,
    datastore_relpath,
    resolve_base_flat,
)
from ..vsphere.types import DiskInfo


@dataclass
class DirectNfsAccess:
    """Where this server can read an NFS datastore, always read-only.

    NFS v3 by default, as ESXi itself uses: with v4 the NAS may grant this
    server a lease (delegation) on a disk it has read, and ESXi's writes to
    that disk during snapshot consolidation then fail."""

    id: int
    datastore: str
    server: str
    export: str
    options: str = "nfsvers=3,hard"

    def mountpoint(self, root: Path, writable: bool = False) -> Path:
        # Named after the address and options: changing them gives new tasks
        # a fresh mount while running ones finish on the old one. Restores get
        # a separate, writable mount; backups never use it.
        h = hashlib.sha1(f"{self.server}|{self.export}|{self.options}".encode()).hexdigest()
        return root / f"{'dsw' if writable else 'ds'}-{self.id}-{h[:8]}"


class VSphereSource(VSphere):
    def __init__(self, *args, vddk_libdir: Path, nbdkit: str = "nbdkit",
                 transports: str = "nbdssl:nbd", direct_nfs: list[DirectNfsAccess] | None = None,
                 mount_root: Path = Path("/mnt/openbackup"), **kwargs):
        super().__init__(*args, **kwargs)
        self.vddk_libdir = vddk_libdir
        self.nbdkit = nbdkit
        self.transports = transports
        self.direct_nfs = {a.datastore: a for a in direct_nfs or []}
        self.mount_root = mount_root
        self._rw_mounts: list[Path] = []

    def get_vm(self, moref: str) -> Any:
        return self.vm(moref)

    def vm_moref(self, vm: Any) -> str:
        return vm._moId

    # ----------------------------------------------------------- direct NFS

    def _flat_disk(self, disk: DiskInfo) -> FlatDisk | None:
        access = self.direct_nfs.get(disk.datastore)
        if access is None:
            return None
        mp = access.mountpoint(self.mount_root)
        try:
            nfs.ensure_mounted(access.server, access.export, mp, access.options, read_only=True)
        except nfs.NfsError as e:
            raise DirectNfsError(f"datastore {disk.datastore}: {e}") from None
        extents = resolve_base_flat(mp / datastore_relpath(disk.file))
        return FlatDisk(extents, f"direct NFS {access.server}:{access.export} (read-only)")

    def can_write(self, datastore: str) -> bool:
        """Whether restores can write to this datastore (direct NFS access)."""
        return datastore in self.direct_nfs

    def _writable_flat_disk(self, vm_moref: str, disk: DiskInfo) -> FlatDiskWriter:
        """The disk files of ``vm_moref``, opened for writing, after checking
        everything that makes that safe. Every write path goes through here."""
        access = self.direct_nfs.get(disk.datastore)
        if access is None:
            raise VSphereError(
                f"Datastore {disk.datastore} has no direct NFS access configured; whole-VM "
                "restores can only write to NFS datastores OpenBackup can reach directly")
        vm = self.vm(vm_moref)
        if str(vm.runtime.powerState) != "poweredOff":
            raise VSphereError(f"{vm.name} must be powered off before its disks are written")
        if vm.snapshot is not None:
            raise VSphereError(f"{vm.name} has snapshots; its base disks cannot be written")
        own = {d.file for d in self.vm_disks(vm)}
        if disk.file not in own:
            raise VSphereError(f"{disk.file} is not one of {vm.name}'s disks")
        mp = access.mountpoint(self.mount_root, writable=True)
        try:
            ours = nfs.ensure_mounted(access.server, access.export, mp, access.options)
        except nfs.NfsError as e:
            raise DirectNfsError(f"datastore {disk.datastore}: {e}") from None
        if ours:
            self._rw_mounts.append(mp)
        writer = FlatDiskWriter(resolve_base_flat(mp / datastore_relpath(disk.file)),
                                f"direct NFS {access.server}:{access.export} (writing)")
        if writer.size != disk.capacity:
            writer.close()
            raise VSphereError(f"{disk.file} is {writer.size} bytes, vSphere reports "
                               f"{disk.capacity}")
        return writer

    def reset_cbt(self, vm) -> None:
        """Invalidate the VM's change IDs (CBT off, then on) so the next backup
        reads everything. Needed after writing its disks directly, which CBT
        does not see."""
        for flag in (False, True):
            self.wait(self.rebind(vm).ReconfigVM_Task(
                vim.vm.ConfigSpec(changeTrackingEnabled=flag)), timeout=600)

    def close(self) -> None:
        for mp in self._rw_mounts:
            try:
                nfs.unmount(mp)
                mp.rmdir()
            except (nfs.NfsError, OSError):
                pass
        self._rw_mounts = []
        super().close()

    def allocated_extents(self, disk: DiskInfo) -> list[tuple[int, int]] | None:
        """Allocated regions from the datastore filesystem, when it can tell."""
        fd = self._flat_disk(disk)
        if fd is None:
            return None
        with fd:
            return fd.allocated_extents()

    # ---------------------------------------------------------------- disks

    @contextmanager
    def open_disk(self, vm_moref: str, disk: DiskInfo, snapshot_moref: str | None = None,
                  write: bool = False) -> Iterator[Any]:
        if not write:
            flat = self._flat_disk(disk)
            if flat is not None:
                with flat:
                    yield flat
                return
        elif disk.datastore in self.direct_nfs:
            with self._writable_flat_disk(vm_moref, disk) as w:
                yield w
            return
        target = VddkTarget(
            libdir=self.vddk_libdir, server=self.host, port=self.port, user=self.user,
            password=self._password, thumbprint=self.thumbprint, vm_moref=vm_moref,
            file=disk.file, snapshot_moref=snapshot_moref, transports=self.transports,
        )
        with Nbdkit.vddk(target, readonly=not write, nbdkit=self.nbdkit) as srv, \
                srv.connect() as client:
            client.description = "VDDK via nbdkit"
            try:
                yield client
            except (NbdError, OSError) as e:
                raise NbdError(f"{e}; {srv.failure_reason()}") from None


__all__ = ["DirectNfsAccess", "NbdClient", "VSphereSource"]
