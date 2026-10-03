"""vSphere access through pyvmomi.

Only this module touches pyvmomi. Everything it returns is plain data from
``types``, so the engines can run against a fake in tests.
"""

from __future__ import annotations

import hashlib
import socket
import ssl
import time
from collections.abc import Callable, Iterator
from typing import Any

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim, vmodl

from .types import DiskInfo, Extent, PlacementOptions, SnapshotRef, VmSummary

SNAPSHOT_PREFIX = "openbackup-"


class VSphereError(Exception):
    pass


def fetch_thumbprints(host: str, port: int = 443, timeout: float = 10) -> dict[str, str]:
    """SHA-1 (what VDDK wants) and SHA-256 of the server certificate, for the
    administrator to confirm when adding a vCenter."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw, \
                ctx.wrap_socket(raw, server_hostname=host) as s:
            der = s.getpeercert(binary_form=True)
    except OSError as e:
        raise VSphereError(f"Cannot reach {host}:{port}: {e}") from None

    def fmt(h: bytes) -> str:
        return ":".join(f"{b:02X}" for b in h)

    return {"sha1": fmt(hashlib.sha1(der).digest()), "sha256": fmt(hashlib.sha256(der).digest())}


def parse_ds_path(path: str) -> tuple[str, str]:
    """'[ds1] vm/vm.vmdk' -> ('ds1', 'vm/vm.vmdk')"""
    if not path.startswith("[") or "]" not in path:
        return "", path
    ds, _, rest = path[1:].partition("]")
    return ds, rest.strip()


def _moref(obj: Any) -> str:
    return obj._moId


_CONTROLLER_TYPES: dict[str, type] = {
    "pvscsi": vim.vm.device.ParaVirtualSCSIController,
    "lsilogic": vim.vm.device.VirtualLsiLogicController,
    "lsilogicsas": vim.vm.device.VirtualLsiLogicSASController,
    "buslogic": vim.vm.device.VirtualBusLogicController,
    "nvme": vim.vm.device.VirtualNVMEController,
    "sata": vim.vm.device.VirtualAHCIController,
    "ide": vim.vm.device.VirtualIDEController,
}

_NIC_TYPES: dict[str, type] = {
    "vmxnet3": vim.vm.device.VirtualVmxnet3,
    "e1000e": vim.vm.device.VirtualE1000e,
    "e1000": vim.vm.device.VirtualE1000,
    "vmxnet2": vim.vm.device.VirtualVmxnet2,
}


def _type_name(dev: Any, table: dict[str, type]) -> str | None:
    for name, cls in table.items():
        if type(dev) is cls or isinstance(dev, cls):
            return name
    return None


class VSphere:
    def __init__(self, host: str, user: str, password: str, thumbprint: str, port: int = 443,
                 timeout: int = 120):
        self.host, self.port, self.user = host, port, user
        self._password = password
        self.thumbprint = thumbprint
        self.timeout = timeout
        self.si: Any = None

    # -------------------------------------------------------------- session

    def connect(self) -> VSphere:
        try:
            self.si = SmartConnect(
                host=self.host, port=self.port, user=self.user, pwd=self._password,
                thumbprint=self.thumbprint.replace(":", "").lower(),
                disableSslCertValidation=True,
                connectionPoolTimeout=self.timeout,
            )
        except vim.fault.InvalidLogin:
            raise VSphereError("vCenter rejected the username or password") from None
        except vim.fault.HostConnectFault as e:
            raise VSphereError(f"vCenter connection failed: {e.msg}") from None
        except ssl.SSLError as e:
            raise VSphereError(f"TLS error talking to vCenter: {e}") from None
        except Exception as e:
            if "thumbprint" in str(e).lower():
                raise VSphereError(
                    "vCenter certificate does not match the pinned thumbprint. If the "
                    "certificate was replaced, update the thumbprint in the vCenter settings."
                ) from None
            raise VSphereError(f"Cannot connect to vCenter {self.host}: {e}") from None
        return self

    def close(self) -> None:
        if self.si is not None:
            try:
                Disconnect(self.si)
            except Exception:
                pass
            self.si = None

    def __enter__(self) -> VSphere:
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def content(self):
        return self.si.RetrieveContent()

    def about(self) -> dict:
        a = self.content.about
        return {"name": a.fullName, "version": a.version, "build": a.build,
                "api_type": a.apiType, "instance_uuid": a.instanceUuid}

    # ---------------------------------------------------------- collection

    def _collect(self, obj_type: type, paths: list[str]) -> Iterator[tuple[Any, dict]]:
        content = self.content
        view = content.viewManager.CreateContainerView(content.rootFolder, [obj_type], True)
        try:
            traversal = vmodl.query.PropertyCollector.TraversalSpec(
                name="view", path="view", skip=False, type=vim.view.ContainerView)
            spec = vmodl.query.PropertyCollector.FilterSpec(
                objectSet=[vmodl.query.PropertyCollector.ObjectSpec(
                    obj=view, skip=True, selectSet=[traversal])],
                propSet=[vmodl.query.PropertyCollector.PropertySpec(type=obj_type,
                                                                    pathSet=paths)],
            )
            pc = content.propertyCollector
            opts = vmodl.query.PropertyCollector.RetrieveOptions(maxObjects=500)
            result = pc.RetrievePropertiesEx([spec], opts)
            while result:
                for oc in result.objects:
                    yield oc.obj, {p.name: p.val for p in (oc.propSet or [])}
                if not result.token:
                    break
                result = pc.ContinueRetrievePropertiesEx(result.token)
        finally:
            view.Destroy()

    def vm(self, moref: str) -> Any:
        obj = vim.VirtualMachine(moref, self.si._stub)
        try:
            obj.name  # noqa: B018 - existence check
        except vmodl.fault.ManagedObjectNotFound:
            raise VSphereError(f"VM {moref} no longer exists") from None
        return obj

    def list_vms(self) -> list[VmSummary]:
        paths = ["name", "config.instanceUuid", "runtime.powerState", "config.guestFullName",
                 "config.hardware.numCPU", "config.hardware.memoryMB",
                 "config.hardware.device", "summary.storage.committed",
                 "summary.storage.uncommitted", "config.changeTrackingEnabled", "snapshot",
                 "config.template", "parent", "runtime.host", "datastore",
                 "guest.toolsRunningStatus"]
        out = []
        for obj, p in self._collect(vim.VirtualMachine, paths):
            if "config.instanceUuid" not in p:
                continue  # inaccessible or orphaned
            disks = [d for d in p.get("config.hardware.device", [])
                     if isinstance(d, vim.vm.device.VirtualDisk)]
            out.append(VmSummary(
                moref=_moref(obj),
                name=p.get("name", ""),
                instance_uuid=p["config.instanceUuid"],
                power_state=str(p.get("runtime.powerState", "")),
                guest_os=p.get("config.guestFullName", "") or "",
                cpu=p.get("config.hardware.numCPU", 0) or 0,
                memory_mb=p.get("config.hardware.memoryMB", 0) or 0,
                provisioned_bytes=sum(d.capacityInBytes or 0 for d in disks),
                disks=len(disks),
                cbt_enabled=bool(p.get("config.changeTrackingEnabled")),
                has_snapshots=p.get("snapshot") is not None,
                is_template=bool(p.get("config.template")),
                folder=getattr(p.get("parent"), "name", "") if p.get("parent") else "",
                host=getattr(p.get("runtime.host"), "name", "") if p.get("runtime.host") else "",
                datastores=[ds.name for ds in p.get("datastore", [])],
                tools_status=p.get("guest.toolsRunningStatus", "") or "",
            ))
        return sorted(out, key=lambda v: v.name.lower())

    def placement_options(self) -> PlacementOptions:
        po = PlacementOptions()
        for obj, p in self._collect(vim.Datacenter, ["name", "vmFolder"]):
            po.datacenters.append({"moref": _moref(obj), "name": p["name"],
                                   "vm_folder": _moref(p["vmFolder"])})
        for obj, p in self._collect(vim.HostSystem, ["name", "runtime.connectionState",
                                                     "runtime.inMaintenanceMode", "parent"]):
            po.hosts.append({"moref": _moref(obj), "name": p["name"],
                             "connected": str(p.get("runtime.connectionState")) == "connected",
                             "maintenance": bool(p.get("runtime.inMaintenanceMode"))})
        for obj, p in self._collect(vim.Datastore, ["name", "summary.capacity",
                                                    "summary.freeSpace", "summary.type",
                                                    "summary.accessible"]):
            po.datastores.append({"moref": _moref(obj), "name": p["name"],
                                  "capacity": p.get("summary.capacity", 0),
                                  "free": p.get("summary.freeSpace", 0),
                                  "type": p.get("summary.type", ""),
                                  "accessible": bool(p.get("summary.accessible"))})
        for obj, p in self._collect(vim.Network, ["name"]):
            kind = "dvportgroup" if isinstance(obj, vim.dvs.DistributedVirtualPortgroup) \
                else "network"
            po.networks.append({"moref": _moref(obj), "name": p["name"], "kind": kind})
        for obj, p in self._collect(vim.Folder, ["name", "childType"]):
            if "VirtualMachine" in (p.get("childType") or []):
                po.folders.append({"moref": _moref(obj), "name": p["name"]})
        for obj, p in self._collect(vim.ResourcePool, ["name", "owner"]):
            owner = p.get("owner")
            po.resource_pools.append({"moref": _moref(obj), "name": p["name"],
                                      "owner": owner.name if owner else ""})
        return po

    # ---------------------------------------------------------------- tasks

    def wait(self, task: Any, timeout: float = 3600,
             cancelled: Callable[[], bool] | None = None) -> Any:
        deadline = time.monotonic() + timeout
        while True:
            info = task.info
            if info.state == vim.TaskInfo.State.success:
                return info.result
            if info.state == vim.TaskInfo.State.error:
                err = info.error
                raise VSphereError(getattr(err, "msg", None) or str(err))
            if time.monotonic() > deadline:
                raise VSphereError(f"vCenter task {info.descriptionId} timed out")
            if cancelled and cancelled() and info.cancelable:
                try:
                    task.CancelTask()
                except Exception:
                    pass
            time.sleep(1)

    # ---------------------------------------------------------------- disks

    @staticmethod
    def _disks_from_devices(devices: list) -> list[DiskInfo]:
        out = []
        for d in devices:
            if not isinstance(d, vim.vm.device.VirtualDisk):
                continue
            b = d.backing
            file = getattr(b, "fileName", "") or ""
            mode = getattr(b, "diskMode", "") or ""
            out.append(DiskInfo(
                key=d.key,
                label=d.deviceInfo.label if d.deviceInfo else str(d.key),
                capacity=d.capacityInBytes or (d.capacityInKB or 0) * 1024,
                file=file,
                datastore=parse_ds_path(file)[0],
                thin=bool(getattr(b, "thinProvisioned", False)),
                controller_key=d.controllerKey,
                unit_number=d.unitNumber or 0,
                change_id=getattr(b, "changeId", None),
                independent=mode.startswith("independent"),
            ))
        return out

    def vm_disks(self, vm: Any) -> list[DiskInfo]:
        return self._disks_from_devices(vm.config.hardware.device)

    def ensure_cbt(self, vm: Any, cancelled=None) -> bool:
        """Enable CBT if needed. Returns True if it was just enabled, in which
        case it only becomes active after a snapshot cycle."""
        if vm.config.changeTrackingEnabled:
            return False
        spec = vim.vm.ConfigSpec(changeTrackingEnabled=True)
        self.wait(vm.ReconfigVM_Task(spec), timeout=600, cancelled=cancelled)
        return True

    def create_snapshot(self, vm: Any, name: str, quiesce: bool) -> tuple[Any, SnapshotRef]:
        desc = "Temporary snapshot taken by OpenBackup. Safe to delete if no backup is running."
        try:
            snap = self.wait(vm.CreateSnapshot_Task(name=name, description=desc, memory=False,
                                                    quiesce=quiesce), timeout=3600)
        except VSphereError as e:
            if quiesce:
                raise VSphereError(f"Quiesced snapshot failed ({e}). Check VMware Tools, "
                                   "or disable application-aware quiescing for this job.") from None
            raise
        return snap, SnapshotRef(_moref(snap),
                                 self._disks_from_devices(snap.config.hardware.device))

    def remove_snapshot(self, snap: Any) -> None:
        self.wait(snap.RemoveSnapshot_Task(removeChildren=False, consolidate=True),
                  timeout=6 * 3600)

    def snapshot_by_moref(self, moref: str) -> Any:
        return vim.vm.Snapshot(moref, self.si._stub)

    def find_own_snapshots(self, vm: Any) -> list[Any]:
        found = []

        def walk(nodes):
            for n in nodes or []:
                if n.name.startswith(SNAPSHOT_PREFIX):
                    found.append(n.snapshot)
                walk(n.childSnapshotList)

        if vm.snapshot:
            walk(vm.snapshot.rootSnapshotList)
        return found

    def changed_areas(self, vm: Any, snap: Any, disk: DiskInfo, change_id: str) -> list[Extent]:
        """Extents changed since ``change_id`` ("*" = all allocated areas)."""
        out: list[Extent] = []
        offset = 0
        while offset < disk.capacity:
            try:
                info = vm.QueryChangedDiskAreas(snapshot=snap, deviceKey=disk.key,
                                                startOffset=offset, changeId=change_id)
            except vim.fault.FileFault as e:
                raise VSphereError(f"CBT query failed for {disk.label}: {e.msg}") from None
            except vmodl.fault.InvalidArgument as e:
                raise VSphereError(f"CBT change id rejected for {disk.label}: {e.msg}") from None
            for a in info.changedArea or []:
                out.append(Extent(a.start, a.length))
            if info.length <= 0:
                break
            offset = info.startOffset + info.length
        return out

    def power_off(self, vm: Any) -> None:
        if vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOn:
            self.wait(vm.PowerOffVM_Task(), timeout=600)

    def power_on(self, vm: Any) -> None:
        if vm.runtime.powerState != vim.VirtualMachinePowerState.poweredOn:
            self.wait(vm.PowerOnVM_Task(), timeout=600)

    # --------------------------------------------------------------- config

    def capture_config(self, vm: Any) -> dict:
        """Everything needed to recreate the VM, as plain JSON."""
        c = vm.config
        controllers, nics = [], []
        for d in c.hardware.device:
            ctype = _type_name(d, _CONTROLLER_TYPES)
            if ctype:
                controllers.append({
                    "key": d.key, "type": ctype, "bus": d.busNumber,
                    "sharing": str(getattr(d, "sharedBus", "") or ""),
                })
            ntype = _type_name(d, _NIC_TYPES)
            if ntype:
                b = d.backing
                net = ""
                if isinstance(b, vim.vm.device.VirtualEthernetCard.NetworkBackingInfo):
                    net = b.deviceName
                elif isinstance(
                        b, vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo):
                    try:
                        pg = next(n for n in vm.network
                                  if isinstance(n, vim.dvs.DistributedVirtualPortgroup)
                                  and n.key == b.port.portgroupKey)
                        net = pg.name
                    except StopIteration:
                        net = b.port.portgroupKey
                nics.append({"type": ntype, "network": net, "mac": d.macAddress,
                             "connected": bool(d.connectable and d.connectable.startConnected)})
        return {
            "name": c.name,
            "guest_id": c.guestId,
            "guest_full_name": c.guestFullName,
            "version": c.version,
            "firmware": c.firmware,
            "efi_secure_boot": bool(getattr(c.bootOptions, "efiSecureBootEnabled", False)),
            "cpu": c.hardware.numCPU,
            "cores_per_socket": c.hardware.numCoresPerSocket,
            "memory_mb": c.hardware.memoryMB,
            "annotation": c.annotation or "",
            "uuid": c.uuid,
            "instance_uuid": c.instanceUuid,
            "nested_hv": bool(getattr(c, "nestedHVEnabled", False)),
            "controllers": controllers,
            "disks": [d.to_dict() for d in self.vm_disks(vm)],
            "nics": nics,
        }

    def create_vm(self, config: dict, *, name: str, folder: str, resource_pool: str,
                  datastore: str, host: str | None = None,
                  network_map: dict[str, str] | None = None) -> tuple[str, dict[int, DiskInfo]]:
        """Create an empty VM shaped like ``config``. Disks are created at full
        size and filled afterwards. Returns (moref, {original disk key: new
        disk})."""
        stub = self.si._stub
        network_map = network_map or {}
        nets = {n["name"]: n for n in self.placement_options().networks}
        changes = []
        key_map: dict[int, int] = {}
        temp = -100
        for ctl in config["controllers"]:
            if ctl["type"] == "ide":
                key_map[ctl["key"]] = 200 + ctl["bus"]  # present on every VM
                continue
            temp -= 1
            dev = _CONTROLLER_TYPES[ctl["type"]]()
            dev.key, dev.busNumber = temp, ctl["bus"]
            if hasattr(dev, "sharedBus"):
                dev.sharedBus = vim.vm.device.VirtualSCSIController.Sharing.noSharing
            key_map[ctl["key"]] = temp
            changes.append(vim.vm.device.VirtualDeviceSpec(
                operation=vim.vm.device.VirtualDeviceSpec.Operation.add, device=dev))
        for d in config["disks"]:
            temp -= 1
            backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo(
                diskMode="persistent", thinProvisioned=d.get("thin", True),
                fileName=f"[{datastore}]")
            disk = vim.vm.device.VirtualDisk(
                key=temp, backing=backing, capacityInBytes=d["capacity"],
                controllerKey=key_map[d["controller_key"]], unitNumber=d["unit_number"])
            changes.append(vim.vm.device.VirtualDeviceSpec(
                operation=vim.vm.device.VirtualDeviceSpec.Operation.add,
                fileOperation=vim.vm.device.VirtualDeviceSpec.FileOperation.create,
                device=disk))
        for nic in config["nics"]:
            target_name = network_map.get(nic["network"], nic["network"])
            target = nets.get(target_name) or next(
                (n for n in nets.values() if n["moref"] == target_name), None)
            if target is None:
                raise VSphereError(f"Network {target_name!r} not found; map it to an "
                                   "existing network")
            dev = _NIC_TYPES.get(nic["type"], vim.vm.device.VirtualVmxnet3)()
            if target["kind"] == "dvportgroup":
                pg = vim.dvs.DistributedVirtualPortgroup(target["moref"], stub)
                dev.backing = vim.vm.device.VirtualEthernetCard. \
                    DistributedVirtualPortBackingInfo(
                        port=vim.dvs.PortConnection(
                            portgroupKey=pg.key,
                            switchUuid=pg.config.distributedVirtualSwitch.uuid))
            else:
                dev.backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(
                    deviceName=target["name"],
                    network=vim.Network(target["moref"], stub))
            dev.addressType = "generated"
            dev.connectable = vim.vm.device.VirtualDevice.ConnectInfo(
                startConnected=nic.get("connected", True), allowGuestControl=True)
            changes.append(vim.vm.device.VirtualDeviceSpec(
                operation=vim.vm.device.VirtualDeviceSpec.Operation.add, device=dev))

        spec = vim.vm.ConfigSpec(
            name=name, guestId=config["guest_id"], version=config.get("version"),
            numCPUs=config["cpu"], numCoresPerSocket=config.get("cores_per_socket") or 1,
            memoryMB=config["memory_mb"], annotation=config.get("annotation", ""),
            firmware=config.get("firmware") or "bios",
            nestedHVEnabled=config.get("nested_hv", False),
            files=vim.vm.FileInfo(vmPathName=f"[{datastore}]"),
            deviceChange=changes,
        )
        if config.get("efi_secure_boot"):
            spec.bootOptions = vim.vm.BootOptions(efiSecureBootEnabled=True)
        vm = self.wait(vim.Folder(folder, stub).CreateVM_Task(
            config=spec, pool=vim.ResourcePool(resource_pool, stub),
            host=vim.HostSystem(host, stub) if host else None), timeout=1800)
        # Match new disks to the originals by controller (type, bus) and unit.
        new_ctl = {}
        for d in vm.config.hardware.device:
            t = _type_name(d, _CONTROLLER_TYPES)
            if t:
                new_ctl[d.key] = (t, d.busNumber)
        orig_ctl = {c["key"]: (c["type"], c["bus"]) for c in config["controllers"]}
        by_slot = {(new_ctl.get(d.controller_key), d.unit_number): d for d in self.vm_disks(vm)}
        mapping: dict[int, DiskInfo] = {}
        for d in config["disks"]:
            slot = (orig_ctl.get(d["controller_key"]), d["unit_number"])
            if slot not in by_slot:
                raise VSphereError(f"Created VM is missing disk {d['label']}")
            mapping[d["key"]] = by_slot[slot]
        return _moref(vm), mapping
