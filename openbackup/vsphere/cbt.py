"""Changed Block Tracking.

CBT is what makes this a backup product rather than a copier. Two uses:

* with a real ``changeId``, it reports the blocks written since that point, so
  an incremental reads only what changed;
* with ``changeId="*"``, it reports *allocated* blocks, so even a first full
  backup skips space the guest has never written. On a 350 GB thin disk with
  13 GB in use, that is the difference between minutes and hours.

The dangerous part is not querying CBT, it is trusting it. A ``changeId`` is
silently invalidated by things an operator does routinely -- a power cycle
through certain paths, a storage vMotion, restoring the VM from a backup,
expanding a disk. vCenter's answer in those cases is not always an obvious
error. Treating a stale ``changeId`` as valid produces an incremental that
looks successful and restores to a corrupt disk, so anything ambiguous here
raises :class:`CbtInvalid` and the caller falls back to reading everything.
"""

from __future__ import annotations

import logging

from pyVmomi import vim

from .connection import VSphereError
from .task import wait_for_task

log = logging.getLogger(__name__)


class CbtError(VSphereError):
    pass


class CbtUnavailable(CbtError):
    """CBT is off, or not yet active, for this VM."""


class CbtInvalid(CbtError):
    """The changeId cannot be trusted; fall back to a full read."""


#: Fragments vCenter puts in faults when a changeId is stale or unusable.
_INVALID_MARKERS = (
    "changeid", "change id", "changetracking", "change tracking",
    "invalid argument", "not supported", "cannot be used",
)


def is_enabled(vm: vim.VirtualMachine) -> bool:
    return bool(vm.config and vm.config.changeTrackingEnabled)


def enable(vm: vim.VirtualMachine, *, conn=None,
           timeout: float = 300.0) -> bool:
    """Turn CBT on. Returns True if it changed anything.

    The setting takes effect only after the VM is "stunned" -- see
    :func:`activate`. Reconfiguring alone leaves CBT enabled but not yet
    tracking, and a query at that point reports nothing changed, which would
    look exactly like a successful empty incremental.
    """
    if is_enabled(vm):
        return False
    if conn is not None:
        conn.ensure_writable(f"enable CBT on {vm.name}")
    log.info("%s: enabling changed block tracking", vm.name)
    spec = vim.vm.ConfigSpec(changeTrackingEnabled=True)
    wait_for_task(vm.ReconfigVM_Task(spec), f"enabling CBT on {vm.name}",
                  timeout=timeout)
    return True


def activate(vm: vim.VirtualMachine, *, conn=None,
             timeout: float = 600.0) -> None:
    """Stun the VM so newly enabled CBT starts tracking.

    A snapshot create/delete cycle is enough, and unlike a power cycle it needs
    no downtime.
    """
    if conn is not None:
        conn.ensure_writable(f"stun {vm.name} to activate CBT")
    log.info("%s: stunning to activate CBT", vm.name)
    wait_for_task(
        vm.CreateSnapshot_Task(name="openbackup-cbt-activate",
                               description="Activating CBT; removed immediately.",
                               memory=False, quiesce=False),
        f"CBT activation snapshot on {vm.name}", timeout=timeout,
    )
    snapshot = vm.snapshot.currentSnapshot
    wait_for_task(snapshot.RemoveSnapshot_Task(removeChildren=True),
                  f"removing CBT activation snapshot on {vm.name}",
                  timeout=timeout)


def ensure_enabled(vm: vim.VirtualMachine, *, conn=None,
                   activate_now: bool = True) -> bool:
    """Enable CBT and make it live. Returns True if we changed the VM."""
    changed = enable(vm, conn=conn)
    if changed and activate_now:
        activate(vm, conn=conn)
    return changed


def query_changed_areas(vm: vim.VirtualMachine, snapshot: vim.vm.Snapshot,
                        device_key: int, change_id: str,
                        capacity: int) -> list[tuple[int, int]]:
    """Extents changed since `change_id`, as (offset, length) pairs.

    vCenter answers a bounded region per call, so this walks the disk until it
    is covered.
    """
    if not change_id:
        raise CbtInvalid("no changeId supplied")

    areas: list[tuple[int, int]] = []
    offset = 0
    guard = 0
    while offset < capacity:
        guard += 1
        if guard > 100_000:
            raise CbtError(
                f"CBT query for disk {device_key} did not terminate; "
                f"stopped at offset {offset} of {capacity}")
        try:
            result = vm.QueryChangedDiskAreas(
                snapshot=snapshot, deviceKey=device_key,
                startOffset=offset, changeId=change_id,
            )
        except (vim.fault.FileFault, vim.fault.InvalidArgument,
                vim.fault.NotFound) as exc:
            raise _classify(exc, device_key, change_id) from exc
        except vim.fault.VimFault as exc:
            raise _classify(exc, device_key, change_id) from exc

        for area in result.changedArea or []:
            if area.length > 0:
                areas.append((area.start, area.length))

        covered = (result.startOffset or 0) + (result.length or 0)
        if covered <= offset:
            # No forward progress: vCenter has told us all it will. Stopping
            # here rather than looping is right, but the disk is not fully
            # covered, so the caller must not treat this as complete.
            if covered < capacity and not areas:
                raise CbtInvalid(
                    f"CBT returned no coverage for disk {device_key} at offset "
                    f"{offset}; the changeId is probably stale")
            break
        offset = covered

    return merge_extents(areas)


def allocated_areas(vm: vim.VirtualMachine, snapshot: vim.vm.Snapshot,
                    device_key: int, capacity: int) -> list[tuple[int, int]]:
    """Allocated extents, via the special ``changeId="*"``.

    Lets a first full backup skip never-written space on a thin disk.
    """
    return query_changed_areas(vm, snapshot, device_key, "*", capacity)


def merge_extents(areas: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort and coalesce touching or overlapping extents.

    CBT can report adjacent runs separately; merging keeps the read count down
    and makes the totals we report meaningful.
    """
    if not areas:
        return []
    ordered = sorted(areas)
    merged = [list(ordered[0])]
    for start, length in ordered[1:]:
        last = merged[-1]
        if start <= last[0] + last[1]:
            end = max(last[0] + last[1], start + length)
            last[1] = end - last[0]
        else:
            merged.append([start, length])
    return [(s, l) for s, l in merged]


def total_bytes(areas: list[tuple[int, int]]) -> int:
    return sum(length for _start, length in areas)


def _classify(exc: Exception, device_key: int, change_id: str) -> CbtError:
    """Decide whether a fault means "stale changeId" or something worse."""
    message = (getattr(exc, "msg", None) or str(exc) or "").lower()
    if any(marker in message for marker in _INVALID_MARKERS):
        return CbtInvalid(
            f"changeId {change_id!r} is not usable for disk {device_key}: "
            f"{getattr(exc, 'msg', None) or exc}"
        )
    if "not enabled" in message or "disabled" in message:
        return CbtUnavailable(
            f"CBT is not active for disk {device_key}: "
            f"{getattr(exc, 'msg', None) or exc}"
        )
    # Unrecognised faults are treated as untrustworthy rather than fatal: a
    # needless full backup costs time, a wrong incremental costs the data.
    return CbtInvalid(
        f"CBT query failed for disk {device_key} ({type(exc).__name__}: "
        f"{getattr(exc, 'msg', None) or exc}); falling back to a full read"
    )
