"""Describing a VM and its disks well enough to back them up.

The important work here is resolving where a disk's *data* actually lives. The
VM config names a descriptor (``installer.vmdk``), not the extent holding the
blocks (``installer-flat.vmdk``), and the datastore browser hides the extent
entirely -- it reports the descriptor with the disk's allocated size, which
looks convincing and is not what we need to read.

Deriving the extent name by string substitution would usually work and fail
silently when it did not. Instead the descriptor is fetched and parsed: it
states its own extents and its format, so we learn the real filename and can
refuse layouts we cannot read rather than producing a corrupt backup.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pyVmomi import vim

from ..transport.base import TransportError
from .connection import VSphereError

#: e.g. `RW 734003200 VMFS "installer-flat.vmdk"`
_EXTENT_RE = re.compile(
    r'^\s*(RW|RDONLY|NOACCESS)\s+(\d+)\s+(\w+)\s+"([^"]+)"', re.MULTILINE)
_CREATE_TYPE_RE = re.compile(r'^\s*createType\s*=\s*"([^"]+)"', re.MULTILINE)
_PARENT_RE = re.compile(r'^\s*parentFileNameHint\s*=\s*"([^"]+)"', re.MULTILINE)

#: Descriptor formats whose extent is a plain flat file we can read over HTTP.
FLAT_CREATE_TYPES = {
    "vmfs", "vmfsRaw", "monolithicFlat", "twoGbMaxExtentFlat", "vmfsPreallocated",
}

#: A descriptor is a few hundred bytes of text. Anything larger is not one.
MAX_DESCRIPTOR_BYTES = 64 * 1024


@dataclass
class Extent:
    access: str
    sectors: int
    kind: str
    filename: str

    @property
    def size(self) -> int:
        return self.sectors * 512


@dataclass
class DiskInfo:
    """One virtual disk, resolved to something we can read."""

    key: int                 # device key; CBT queries are addressed by this
    label: str
    capacity: int
    datastore: str
    descriptor_path: str     # relative to the datastore root
    thin: bool
    disk_mode: str
    uuid: str | None = None
    change_id: str | None = None
    flat_path: str | None = None
    extents: list[Extent] = field(default_factory=list)

    @property
    def folder(self) -> str:
        return self.descriptor_path.rsplit("/", 1)[0] if "/" in self.descriptor_path else ""

    @property
    def independent(self) -> bool:
        """Independent disks are excluded from snapshots, so a snapshot does
        not give us a consistent image of them and does not free their lock."""
        return "independent" in (self.disk_mode or "")

    def describe(self) -> str:
        return f"[{self.datastore}] {self.descriptor_path}"


@dataclass
class VmInfo:
    name: str
    uuid: str
    instance_uuid: str
    moref: str
    power_state: str
    datacenter: str
    guest_full_name: str
    hardware_version: str
    tools_status: str
    cbt_enabled: bool
    has_snapshots: bool
    disks: list[DiskInfo] = field(default_factory=list)

    @property
    def quiesce_possible(self) -> bool:
        """Application-consistent snapshots need Tools running in the guest."""
        return self.tools_status in ("toolsOk", "toolsOld")


def split_datastore_path(path: str) -> tuple[str, str]:
    """`[DS-NAS01] installer_1/installer.vmdk` -> (`DS-NAS01`, `installer_1/...`)"""
    match = re.match(r"^\[([^\]]+)\]\s*(.*)$", path)
    if not match:
        raise VSphereError(f"unparsable datastore path {path!r}")
    return match.group(1), match.group(2)


def parse_descriptor(text: str) -> tuple[str | None, list[Extent], str | None]:
    """Pull the format, extents and parent hint out of a VMDK descriptor."""
    create_type = None
    m = _CREATE_TYPE_RE.search(text)
    if m:
        create_type = m.group(1)
    parent = None
    m = _PARENT_RE.search(text)
    if m:
        parent = m.group(1)
    extents = [
        Extent(access=a, sectors=int(s), kind=k, filename=f)
        for a, s, k, f in _EXTENT_RE.findall(text)
    ]
    return create_type, extents, parent


def describe_vm(conn, vm: vim.VirtualMachine) -> VmInfo:
    """Collect everything a backup job needs to decide what to do."""
    config = vm.config
    if config is None:
        raise VSphereError(f"{vm.name} has no config (still being created?)")

    disks = []
    for device in config.hardware.device:
        if not isinstance(device, vim.vm.device.VirtualDisk):
            continue
        backing = device.backing
        filename = getattr(backing, "fileName", None)
        if filename is None:
            # RDM or another backing without a file we can read.
            continue
        datastore, rel = split_datastore_path(filename)
        disks.append(DiskInfo(
            key=device.key,
            label=device.deviceInfo.label if device.deviceInfo else f"disk-{device.key}",
            capacity=device.capacityInBytes,
            datastore=datastore,
            descriptor_path=rel,
            thin=bool(getattr(backing, "thinProvisioned", False)),
            disk_mode=getattr(backing, "diskMode", "") or "",
            uuid=getattr(backing, "uuid", None),
            change_id=getattr(backing, "changeId", None),
        ))

    return VmInfo(
        name=vm.name,
        uuid=config.uuid,
        instance_uuid=config.instanceUuid,
        moref=str(vm._moId),
        power_state=str(vm.runtime.powerState),
        datacenter=conn.datacenter_for(vm).name,
        guest_full_name=config.guestFullName or "",
        hardware_version=config.version or "",
        tools_status=str(vm.guest.toolsStatus) if vm.guest else "toolsNotInstalled",
        cbt_enabled=bool(config.changeTrackingEnabled),
        has_snapshots=vm.snapshot is not None,
        disks=disks,
    )


def resolve_flat_extent(transport, datacenter: str, disk: DiskInfo) -> DiskInfo:
    """Read the descriptor and record the extent holding the disk's blocks.

    Raises rather than guessing. A disk we cannot read is a backup that would
    silently contain the wrong bytes.
    """
    device = transport.open(
        datacenter=datacenter, datastore=disk.datastore,
        path=disk.descriptor_path, read_only=True,
    )
    try:
        if device.size > MAX_DESCRIPTOR_BYTES:
            # The named file is the data itself, not a descriptor pointing at
            # it. That happens with monolithic formats we cannot read by range
            # in a meaningful way.
            raise VSphereError(
                f"{disk.describe()} is {device.size} bytes, too large to be a "
                "descriptor; this disk format is not supported by the "
                "datastore transport"
            )
        text = device.read_all().decode("utf-8", "replace")
    except TransportError as exc:
        raise VSphereError(
            f"could not read the descriptor for {disk.describe()}: {exc}"
        ) from exc
    finally:
        device.close()

    create_type, extents, parent = parse_descriptor(text)

    if parent:
        raise VSphereError(
            f"{disk.describe()} is a delta with parent {parent!r}. The "
            "datastore transport reads flat extents only, so a VM with "
            "pre-existing snapshots cannot be backed up this way."
        )
    if create_type and create_type not in FLAT_CREATE_TYPES:
        raise VSphereError(
            f"{disk.describe()} has createType {create_type!r}, which is not a "
            f"flat extent (supported: {', '.join(sorted(FLAT_CREATE_TYPES))})"
        )
    if not extents:
        raise VSphereError(
            f"{disk.describe()} descriptor lists no extents; it may not be a "
            "VMDK descriptor at all"
        )
    if len(extents) > 1:
        names = ", ".join(e.filename for e in extents)
        raise VSphereError(
            f"{disk.describe()} is split across {len(extents)} extents "
            f"({names}); multi-extent disks are not supported yet"
        )

    extent = extents[0]
    folder = disk.folder
    disk.flat_path = f"{folder}/{extent.filename}" if folder else extent.filename
    disk.extents = extents

    # The descriptor's sector count is authoritative for what we must read; a
    # mismatch against the configured capacity means we would truncate or
    # overrun the disk.
    if extent.size != disk.capacity:
        raise VSphereError(
            f"{disk.describe()}: descriptor says {extent.size} bytes but the "
            f"VM config says {disk.capacity}; refusing to guess"
        )
    return disk
