# openbackup

Image-level backup and restore for VMware vSphere.

## Status

Foundations. The data path (NBD) and the backup format (chunk store + block
maps) are implemented and tested. vSphere orchestration, the VDDK transport
wrapper, and the job/CLI layer are next, and need a VDDK build and lab
credentials.

## Approach

VMware's backup surface, VADP, is three separate things:

| Piece | Role | We call it via |
|---|---|---|
| vSphere API | snapshots, VM config, quiesce via VMware Tools | `pyvmomi` |
| CBT | `QueryChangedDiskAreas` -> changed extents since a `changeId` | `pyvmomi` |
| VDDK | reads/writes VMDK blocks (NBD / HotAdd / SAN) | `nbdkit-vddk-plugin` |

CBT is what makes incrementals cheap. Called with `changeId="*"` it returns
*allocated* blocks, so even a first full backup skips unwritten space on a
thin disk.

### Backup format

Content-addressed rather than a Veeam-style VBK/VIB chain. A restore point is
an ordered list of chunk hashes per disk (a "block map") plus the VM config.
That gives synthetic fulls for free, makes every point independently
restorable, deduplicates across VMs, and reduces retention to a refcount
decrement instead of an increment merge.

Blocks are a fixed 1 MiB, not content-defined. VM disk writes land at stable
offsets, so a fixed grid already aligns between backups; content-defined
chunking solves byte-insertion drift, which does not happen inside a block
device, and would cost the direct index -> offset mapping that keeps restore a
simple seek.

## Layout

    openbackup/nbd/        NBD client (pure Python, pipelined)
    openbackup/repo/       chunk store, block maps
    openbackup/transport/  VDDK/nbdkit and datastore transports  [next]
    openbackup/vsphere/    connection, inventory, snapshots, CBT [next]
    openbackup/jobs/       backup and restore orchestration      [next]

## Development

    dnf install -y python3.12 nbdkit nbdkit-vddk-plugin libnbd qemu-img
    python3.12 -m venv .venv && .venv/bin/pip install -e .
    .venv/bin/pip install pytest pytest-timeout
    .venv/bin/python -m pytest

Tests run against real `nbdkit` and `qemu-nbd` servers plus an in-process fake
NBD server used to force protocol edge cases deterministically. No VDDK or
vCenter needed.
