"""Direct NFS access: read VMDKs from the datastore's own NFS export.

An NFS datastore is just an export on a NAS. If the backup host can reach that
export, it can read the flat VMDK itself instead of asking vCenter to stream
it block by block over HTTPS.

Mounts are read-only, without exception. The export holds running VMs, and a
write to a live VMDK would corrupt a guest, so the possibility is removed
rather than guarded against.

The export is usually shared on a different network from the one vSphere uses:
here, ESXi mounts it as 10.0.0.1:/volume1/homelab over a storage VLAN while
the same volume is reachable as 192.168.68.126:/volume1/homelab. The path is
therefore configured, not inferred -- the address vSphere reports may be one
this host cannot route to at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..repo.backend import BackendError, NfsMount
from .base import TransportError
from .localfile import LocalFileBlockDevice

log = logging.getLogger(__name__)

DEFAULT_MOUNT_ROOT = Path("/mnt/openbackup/datastores")


@dataclass
class DatastoreMount:
    """How to reach one datastore's export from this host.

    :param datastore: the datastore name as vSphere knows it, e.g. `DS-NAS01`.
    :param server: the address *this host* uses, which may differ from the one
        ESXi uses.
    """

    datastore: str
    server: str
    export: str
    mountpoint: str | None = None
    nfs_version: str | None = "3"

    def resolved_mountpoint(self, root: Path = DEFAULT_MOUNT_ROOT) -> Path:
        if self.mountpoint:
            return Path(self.mountpoint)
        safe = "".join(c if c.isalnum() or c in "-_" else "_"
                       for c in self.datastore)
        return root / safe


class DirectNfsTransport:
    """Opens disks from read-only mounts of their datastores' exports."""

    def __init__(self, mounts: list[DatastoreMount], *,
                 mount_root: Path = DEFAULT_MOUNT_ROOT):
        self.mount_root = Path(mount_root)
        self._configured = {m.datastore: m for m in mounts}
        self._active: dict[str, NfsMount] = {}

    def handles(self, datastore: str) -> bool:
        return datastore in self._configured

    def _ensure_mounted(self, datastore: str) -> Path:
        if datastore in self._active:
            return self._active[datastore].mountpoint

        spec = self._configured.get(datastore)
        if spec is None:
            raise TransportError(
                f"no direct NFS mapping configured for datastore {datastore!r}")

        mountpoint = spec.resolved_mountpoint(self.mount_root)
        mountpoint.mkdir(parents=True, exist_ok=True)
        mount = NfsMount(
            spec.server, spec.export, mountpoint,
            version=spec.nfs_version,
            read_only=True,   # never writable: these are live VM files
        )
        try:
            mount.mount()
        except BackendError as exc:
            raise TransportError(
                f"could not mount {spec.server}:{spec.export} for datastore "
                f"{datastore!r}: {exc}"
            ) from exc
        log.info("direct NFS: %s mounted read-only at %s",
                 mount.spec, mountpoint)
        self._active[datastore] = mount
        return mountpoint

    def open(self, *, datacenter: str, datastore: str, path: str,
             read_only: bool = True) -> LocalFileBlockDevice:
        if not read_only:
            raise TransportError(
                "direct NFS access is read-only; restores must use the "
                "datastore HTTPS transport"
            )
        mountpoint = self._ensure_mounted(datastore)
        full = mountpoint / path.lstrip("/")
        resolved = full.resolve()
        if resolved != mountpoint.resolve() and mountpoint.resolve() not in resolved.parents:
            raise TransportError(f"path {path!r} escapes the datastore mount")
        return LocalFileBlockDevice(resolved, read_only=True)

    def close(self) -> None:
        for datastore, mount in list(self._active.items()):
            try:
                mount.unmount()
            except BackendError as exc:
                log.warning("could not unmount %s: %s", mount.spec, exc)
            self._active.pop(datastore, None)

    def __enter__(self) -> "DirectNfsTransport":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
