"""vSphere First Class Disks (FCDs): the disks behind vSphere CSI persistent
volumes. Snapshot, CBT and cleanup go through vCenter's VStorageObjectManager
as small API calls; the data itself is read from the datastore directly,
like VM disks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pyVmomi import vim

from .client import SNAPSHOT_PREFIX, VSphere, VSphereError, parse_ds_path
from .types import Extent


@dataclass
class FcdInfo:
    id: str
    name: str
    capacity: int
    file: str  # "[datastore] fcd/xxxx.vmdk"
    datastore: str
    cbt: bool


class FcdManager:
    def __init__(self, vs: VSphere):
        self.vs = vs
        self._where: dict[str, Any] | None = None

    @property
    def _mgr(self) -> Any:
        self.vs.ensure_session()
        return self.vs.content.vStorageObjectManager

    @staticmethod
    def _id(fcd_id: str) -> Any:
        return vim.vslm.ID(id=fcd_id)

    def _ds(self, fcd_id: str) -> Any:
        """The datastore holding an FCD. vSphere CSI only records the disk id,
        so every datastore's catalogue is listed once and remembered."""
        if self._where is None:
            self._where = {}
            for obj, _p in self.vs._collect(vim.Datastore, ["name"]):
                try:
                    for i in self._mgr.ListVStorageObject(datastore=obj) or []:
                        self._where[i.id] = obj
                except vim.fault.NotFound:
                    continue
                except Exception:  # a datastore without an FCD catalogue
                    continue
        ds = self._where.get(fcd_id)
        if ds is None:
            raise VSphereError(f"Volume {fcd_id} was not found on any datastore")
        return self.vs.rebind(ds)

    def info(self, fcd_id: str) -> FcdInfo:
        ds = self._ds(fcd_id)
        obj = self._mgr.RetrieveVStorageObject(id=self._id(fcd_id), datastore=ds)
        cfg = obj.config
        path = cfg.backing.filePath
        return FcdInfo(id=fcd_id, name=cfg.name, capacity=cfg.capacityInMB * 1024 * 1024,
                       file=path, datastore=parse_ds_path(path)[0] or ds.name,
                       cbt=bool(getattr(cfg, "changedBlockTrackingEnabled", False)))

    def enable_cbt(self, fcd_id: str) -> None:
        self._mgr.SetVStorageObjectControlFlags(id=self._id(fcd_id), datastore=self._ds(fcd_id),
                                                controlFlags=["enableChangedBlockTracking"])

    def create_snapshot(self, fcd_id: str, description: str) -> str:
        task = self._mgr.VStorageObjectCreateSnapshot_Task(
            id=self._id(fcd_id), datastore=self._ds(fcd_id), description=description)
        snap = self.vs.wait(task, timeout=3600)
        return snap.id

    def snapshot_change_id(self, fcd_id: str, snap_id: str) -> str | None:
        d = self._mgr.RetrieveSnapshotDetails(id=self._id(fcd_id), datastore=self._ds(fcd_id),
                                              snapshotId=self._id(snap_id))
        return getattr(d, "changedBlockTrackingId", None) or None

    def delete_snapshot(self, fcd_id: str, snap_id: str) -> None:
        self.vs.wait(self._mgr.DeleteSnapshot_Task(
            id=self._id(fcd_id), datastore=self._ds(fcd_id), snapshotId=self._id(snap_id)),
            timeout=6 * 3600)

    def own_snapshots(self, fcd_id: str) -> list[str]:
        """Snapshots this tool left behind (by description prefix)."""
        info = self._mgr.RetrieveSnapshotInfo(id=self._id(fcd_id), datastore=self._ds(fcd_id))
        return [s.id.id for s in info.snapshots or []
                if (s.description or "").startswith(SNAPSHOT_PREFIX)]

    def changed_areas(self, fcd_id: str, snap_id: str, capacity: int,
                      change_id: str) -> list[Extent]:
        out: list[Extent] = []
        offset = 0
        while offset < capacity:
            try:
                info = self._mgr.QueryChangedDiskAreas(
                    id=self._id(fcd_id), datastore=self._ds(fcd_id),
                    snapshotId=self._id(snap_id), startOffset=offset, changeId=change_id)
            except vim.fault.VimFault as e:
                raise VSphereError(f"CBT query failed for volume {fcd_id}: "
                                   f"{getattr(e, 'msg', e)}") from None
            for a in info.changedArea or []:
                out.append(Extent(a.start, a.length))
            if info.length <= 0:
                break
            offset = info.startOffset + info.length
        return out
