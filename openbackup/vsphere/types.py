"""Plain data passed between the vSphere layer and the engines, so engines
can be tested against a fake without pyvmomi objects."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class DiskInfo:
    key: int
    label: str
    capacity: int
    file: str  # "[datastore] dir/name.vmdk"
    datastore: str
    thin: bool = True
    controller_key: int = 0
    unit_number: int = 0
    change_id: str | None = None  # from a snapshot's view of the disk
    # Independent disks are excluded from snapshots and cannot be backed up.
    independent: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VmSummary:
    moref: str
    name: str
    instance_uuid: str
    power_state: str
    guest_os: str = ""
    cpu: int = 0
    memory_mb: int = 0
    provisioned_bytes: int = 0
    disks: int = 0
    cbt_enabled: bool = False
    has_snapshots: bool = False
    is_template: bool = False
    folder: str = ""
    host: str = ""
    datastores: list[str] = field(default_factory=list)
    tools_status: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Extent:
    start: int
    length: int


@dataclass
class SnapshotRef:
    moref: str
    disks: list[DiskInfo]


@dataclass
class PlacementOptions:
    """Where vCenter offers to put a restored VM."""

    datacenters: list[dict] = field(default_factory=list)
    hosts: list[dict] = field(default_factory=list)
    datastores: list[dict] = field(default_factory=list)
    networks: list[dict] = field(default_factory=list)
    folders: list[dict] = field(default_factory=list)
    resource_pools: list[dict] = field(default_factory=list)
