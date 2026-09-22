"""Snapshot lifecycle around a backup.

A snapshot is what makes an image backup consistent: it freezes the base disk
so the blocks stop moving under us, and -- as the datastore transport depends
on -- releases the write lock so the base extent becomes readable over HTTPS.

Removal is the part that has to be right. A snapshot left behind on a busy VM
grows until the datastore fills, so it is removed on every exit path, and a
failure to remove is raised loudly rather than folded into the job's result.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pyVmomi import vim

from .connection import VSphereError
from .task import wait_for_task

log = logging.getLogger(__name__)

DEFAULT_SNAPSHOT_NAME = "openbackup"
DEFAULT_DESCRIPTION = "Created by openbackup; safe to delete if left behind."


@dataclass
class DiskSnapshotState:
    """What a disk looked like at the moment of the snapshot."""

    key: int
    file_name: str
    #: CBT identity of this point in time, stored so the next run can ask what
    #: changed since. Without it an incremental has no baseline.
    change_id: str | None = None


@dataclass
class SnapshotHandle:
    snapshot: vim.vm.Snapshot
    moref: str
    quiesced: bool
    disks: dict[int, DiskSnapshotState] = field(default_factory=dict)


class SnapshotSession:
    """Creates a snapshot for the duration of a backup, then removes it.

    :param quiesce: ask VMware Tools to flush and freeze the guest filesystems.
        Gives an application-consistent image on a guest that supports it;
        without Tools the result is crash-consistent, which is usually
        recoverable but is not the same guarantee.
    :param allow_existing_snapshots: by default a VM that already has snapshots
        is refused, because the datastore transport reads flat extents and the
        live data would be sitting in a delta. Set true only with a transport
        that resolves chains itself.
    """

    def __init__(self, conn, vm: vim.VirtualMachine, *,
                 name: str = DEFAULT_SNAPSHOT_NAME,
                 description: str = DEFAULT_DESCRIPTION,
                 quiesce: bool = True, memory: bool = False,
                 allow_existing_snapshots: bool = False,
                 timeout: float = 1800.0):
        self.conn = conn
        self.vm = vm
        self.name = name
        self.description = description
        self.quiesce = quiesce
        self.memory = memory
        self.allow_existing_snapshots = allow_existing_snapshots
        self.timeout = timeout
        self.handle: SnapshotHandle | None = None

    # -- preconditions ------------------------------------------------------

    def check_preconditions(self) -> None:
        if self.vm.snapshot is not None and not self.allow_existing_snapshots:
            names = [s.name for s in _walk_snapshots(self.vm.snapshot.rootSnapshotList)]
            raise VSphereError(
                f"{self.vm.name} already has snapshot(s): {', '.join(names)}. "
                "Its live data is in a delta disk, which the datastore "
                "transport cannot read, so a backup now would silently capture "
                "stale blocks. Remove them, or use a VDDK transport."
            )

    # -- lifecycle ----------------------------------------------------------

    def create(self) -> SnapshotHandle:
        self.conn.ensure_writable(f"snapshot {self.vm.name}")
        self.check_preconditions()

        quiesce = self.quiesce
        if quiesce:
            tools = str(self.vm.guest.toolsStatus) if self.vm.guest else ""
            powered_on = str(self.vm.runtime.powerState) == "poweredOn"
            if not powered_on:
                # A powered-off VM is already consistent; asking to quiesce it
                # fails the task outright.
                quiesce = False
            elif tools not in ("toolsOk", "toolsOld"):
                log.warning(
                    "%s: VMware Tools is %s, falling back to a crash-consistent "
                    "snapshot", self.vm.name, tools or "unavailable")
                quiesce = False

        log.info("%s: creating snapshot (quiesce=%s, memory=%s)",
                 self.vm.name, quiesce, self.memory)
        try:
            wait_for_task(
                self.vm.CreateSnapshot_Task(
                    name=self.name, description=self.description,
                    memory=self.memory, quiesce=quiesce),
                f"snapshot of {self.vm.name}", timeout=self.timeout,
            )
        except VSphereError:
            if quiesce:
                # Quiescing fails for reasons outside our control: a VSS writer
                # erroring inside the guest, or a stale Tools install. A
                # crash-consistent backup is far better than none.
                log.warning("%s: quiesced snapshot failed, retrying without "
                            "quiesce", self.vm.name)
                wait_for_task(
                    self.vm.CreateSnapshot_Task(
                        name=self.name, description=self.description,
                        memory=self.memory, quiesce=False),
                    f"crash-consistent snapshot of {self.vm.name}",
                    timeout=self.timeout,
                )
                quiesce = False
            else:
                raise

        snapshot = self.vm.snapshot.currentSnapshot
        handle = SnapshotHandle(
            snapshot=snapshot, moref=str(snapshot._moId), quiesced=quiesce)

        # Record each disk's changeId now. This is the baseline the *next*
        # incremental asks about, so it has to be captured from the snapshot
        # itself rather than the live VM.
        for device in snapshot.config.hardware.device:
            if isinstance(device, vim.vm.device.VirtualDisk):
                handle.disks[device.key] = DiskSnapshotState(
                    key=device.key,
                    file_name=getattr(device.backing, "fileName", ""),
                    change_id=getattr(device.backing, "changeId", None),
                )

        self.handle = handle
        return handle

    def remove(self) -> None:
        if self.handle is None:
            return
        log.info("%s: removing snapshot", self.vm.name)
        try:
            wait_for_task(
                self.handle.snapshot.RemoveSnapshot_Task(removeChildren=True),
                f"removing snapshot of {self.vm.name}", timeout=self.timeout,
            )
        finally:
            self.handle = None

    def __enter__(self) -> SnapshotHandle:
        return self.create()

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.remove()
        except Exception as cleanup_error:
            # A leftover snapshot grows until the datastore fills, so this can
            # never be swallowed. If the job was already failing, surface both.
            if exc_type is None:
                raise
            log.error(
                "%s: FAILED TO REMOVE SNAPSHOT after a failed job (%s). "
                "Remove it manually before the delta grows.",
                self.vm.name, cleanup_error)


def _walk_snapshots(nodes):
    for node in nodes or []:
        yield node
        yield from _walk_snapshots(node.childSnapshotList)
