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

Chunks are grouped into ~128 MB **pack files**. One file per 1 MiB chunk is
fine on a local disk but the wrong shape for NFS, where each chunk would cost
a create, a write, a commit and a rename, and a 100 GB VM would leave ~100,000
files to walk on every verify. Measured over a loopback NFS export, a 512 MiB
disk lands in 4 pack files instead of 512.

Blocks are a fixed 1 MiB, not content-defined. VM disk writes land at stable
offsets, so a fixed grid already aligns between backups; content-defined
chunking solves byte-insertion drift, which does not happen inside a block
device, and would cost the direct index -> offset mapping that keeps restore a
simple seek.

### Destinations

A repository lives on a local disk or an NFS export, chosen per job:

    open_repository(Destination(kind="local", path="/backup/openbackup"))

    open_repository(Destination(kind="nfs", server="10.0.0.5",
                                export="/vol/backup", path="site-a"))

NFS exports are mounted and unmounted around the job. A mount already in place
is adopted and left alone rather than torn down. Mounts default to `hard`: a
soft mount returns EIO on timeout, which during a restore means silently
incomplete data.

The share is authoritative. The local SQLite index is a rebuildable cache --
SQLite over NFS depends on a working lock daemon and corrupts badly without
one -- so each pack carries a trailer listing its contents, and a fresh server
recovers a repository by reading only pack tails.

## Layout

    openbackup/nbd/        NBD client (pure Python, pipelined)
    openbackup/repo/       codec, pack files, block maps, index, destinations
    openbackup/transport/  datastore HTTPS and VDDK/nbdkit transports
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
