"""Restore points, stored in the repository rather than beside the server.

A restore point is metadata plus one block map per disk. It is written to the
repository because it is the thing that makes the backup usable: losing the
server must cost a reindex, not the ability to restore.

Each point is independently restorable. An incremental inherits unchanged
block hashes from its parent, so the parent is a *provenance* record, not a
dependency -- deleting it does not strand its children the way removing a link
from a forward-incremental chain would.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .blockmap import BlockMap

POINT_VERSION = 1

FULL = "full"
INCREMENTAL = "incremental"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_point_id() -> str:
    """Sortable id: a restore point's name orders it in time."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


@dataclass
class DiskPoint:
    """One disk within a restore point."""

    key: int
    label: str
    capacity: int
    datastore: str
    descriptor_path: str
    flat_path: str
    chunk_size: int
    thin: bool = False
    #: CBT identity of this backup, which the next incremental asks against.
    change_id: str | None = None
    #: Bytes actually read from VMware for this disk in this run.
    bytes_read: int = 0
    blocks_changed: int = 0

    @property
    def blockmap_name(self) -> str:
        return f"disk-{self.key}.bm"


@dataclass
class RestorePoint:
    id: str
    vm_uuid: str
    vm_instance_uuid: str
    vm_name: str
    created_at: str
    kind: str = FULL
    parent_id: str | None = None
    version: int = POINT_VERSION
    datacenter: str = ""
    guest_full_name: str = ""
    hardware_version: str = ""
    power_state: str = ""
    quiesced: bool = False
    disks: list[DiskPoint] = field(default_factory=list)

    @classmethod
    def create(cls, vm_uuid: str, vm_instance_uuid: str, vm_name: str,
               **kwargs) -> "RestorePoint":
        return cls(
            id=new_point_id(), vm_uuid=vm_uuid,
            vm_instance_uuid=vm_instance_uuid, vm_name=vm_name,
            created_at=_now(), **kwargs,
        )

    def to_json(self) -> bytes:
        return json.dumps(asdict(self), indent=2, sort_keys=True).encode()

    @classmethod
    def from_json(cls, blob: bytes) -> "RestorePoint":
        data = json.loads(blob)
        version = data.get("version")
        if version != POINT_VERSION:
            raise ValueError(
                f"restore point is version {version}, this build speaks "
                f"{POINT_VERSION}")
        disks = [DiskPoint(**d) for d in data.pop("disks", [])]
        return cls(disks=disks, **data)

    @property
    def total_bytes_read(self) -> int:
        return sum(d.bytes_read for d in self.disks)

    @property
    def total_capacity(self) -> int:
        return sum(d.capacity for d in self.disks)


class PointStore:
    """Reads and writes restore points in a repository."""

    def __init__(self, backend):
        self.backend = backend

    # -- layout -------------------------------------------------------------

    def _vm_dir(self, vm_uuid: str) -> str:
        return f"vms/{vm_uuid}"

    def point_path(self, vm_uuid: str, point_id: str) -> str:
        return f"{self._vm_dir(vm_uuid)}/points/{point_id}.json"

    def blockmap_path(self, vm_uuid: str, point_id: str, disk_key: int) -> str:
        return f"{self._vm_dir(vm_uuid)}/blockmaps/{point_id}/disk-{disk_key}.bm"

    # -- writing ------------------------------------------------------------

    def save(self, point: RestorePoint, blockmaps: dict[int, BlockMap]) -> None:
        """Write the block maps, then the point that references them.

        Order matters. The point is the record that says a backup exists, so
        it is written last: a crash leaves orphaned block maps, never a restore
        point pointing at maps that were never written.
        """
        for key, bm in blockmaps.items():
            bm.validate()
            self.backend.write(
                self.blockmap_path(point.vm_uuid, point.id, key), bm.to_bytes())
        self.backend.write(
            self.point_path(point.vm_uuid, point.id), point.to_json())

    # -- reading ------------------------------------------------------------

    def load(self, vm_uuid: str, point_id: str) -> RestorePoint:
        return RestorePoint.from_json(
            self.backend.read(self.point_path(vm_uuid, point_id)))

    def load_blockmap(self, vm_uuid: str, point_id: str,
                      disk_key: int) -> BlockMap:
        return BlockMap.from_bytes(
            self.backend.read(self.blockmap_path(vm_uuid, point_id, disk_key)))

    def list_points(self, vm_uuid: str) -> list[str]:
        """Point ids for a VM, oldest first (the id sorts chronologically)."""
        prefix = f"{self._vm_dir(vm_uuid)}/points"
        ids = []
        for relpath in self.backend.list(prefix):
            if relpath.endswith(".json"):
                ids.append(relpath.rsplit("/", 1)[-1][:-5])
        return sorted(ids)

    def latest(self, vm_uuid: str) -> RestorePoint | None:
        ids = self.list_points(vm_uuid)
        return self.load(vm_uuid, ids[-1]) if ids else None

    def list_vms(self) -> list[str]:
        seen = set()
        for relpath in self.backend.list("vms"):
            parts = relpath.split("/")
            if len(parts) >= 2:
                seen.add(parts[1])
        return sorted(seen)

    def delete(self, vm_uuid: str, point_id: str) -> None:
        """Remove a point and its block maps.

        The chunks it referenced are left alone: another point may share them,
        and deciding that is garbage collection's job, not this one's.
        """
        self.backend.delete(self.point_path(vm_uuid, point_id))
        prefix = f"{self._vm_dir(vm_uuid)}/blockmaps/{point_id}"
        for relpath in list(self.backend.list(prefix)):
            self.backend.delete(relpath)
