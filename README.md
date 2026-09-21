# openbackup

Image-level backup and restore for VMware vSphere.

## Status

Backup and restore work end to end against a real vCenter 8.0.3. 147 tests,
almost all of which run with no vCenter and no NAS.

Not yet built: a scheduler, retention and garbage collection, restore directly
into a new VM, and any web interface.

## Approach

VMware's backup surface, VADP, is three separate things:

| Piece | Role | We call it via |
|---|---|---|
| vSphere API | snapshots, VM config, quiesce via VMware Tools | `pyvmomi` |
| CBT | `QueryChangedDiskAreas` -> changed extents since a `changeId` | `pyvmomi` |
| VDDK | reads/writes VMDK blocks (NBD / HotAdd / SAN) | `nbdkit-vddk-plugin` |

VDDK is gated behind a Broadcom support entitlement, so nothing here depends on
it. The NBD client and its transport exist and are tested; if the library ever
becomes available it drops in behind the same interface as a speed upgrade.

### Why a snapshot is required

A running VM holds a write lock on its flat extent, and the datastore endpoint
returns HTTP 500 for it. Taking a snapshot freezes the base disk and releases
that lock, so the base becomes readable and is exactly the consistent
point-in-time image we want. The delta stays locked, which does not matter
because we never read it.

This only works when the VM has no snapshots of its own. Otherwise the live
data sits in a delta in VMware's sparse format, and a backup of the base would
silently capture stale blocks. `inventory` flags such VMs and a backup refuses
them.

### Transports

**Direct NFS** (preferred where available). An NFS datastore is an export; if
the backup host can reach it, we mount it read-only and read the flat VMDK
from the filesystem. Measured against a lab NAS: **91 MiB/s versus 16 MiB/s**
through vCenter. It also lets the filesystem report which parts of a sparse
disk were never written, where the export supports `SEEK_HOLE` (NFSv4.2).

The export address is configured, not inferred. ESXi commonly reaches storage
over a VLAN the backup host cannot route to at all.

Mounts are read-only without exception: the export holds running VMs.

**Datastore HTTPS.** Reads flat VMDKs over the `/folder` endpoint using Range
requests. Works anywhere vCenter does, needs no extra network access, and is
the fallback when a datastore cannot be mounted directly.

Two responses are treated as fatal rather than tolerated, because both would
put wrong bytes into the repository with nothing noticing until a restore: a
`200` answer to a ranged read of a large file, and a `Content-Range` that does
not match what was requested.

### CBT is not trusted by default

A `changeId` is silently invalidated by routine operations -- a power cycle
through certain paths, storage vMotion, restoring the VM, growing a disk --
and vCenter does not always report this clearly. Anything ambiguous falls back
to a full read. A needless full costs time; a wrong incremental costs the data.

One thing worth knowing: **the allocated-block query (`changeId="*"`) reports
thin disks on NFS datastores as entirely allocated.** A 350 GiB disk holding
12.6 GiB came back as a single extent covering all of it. Where the transport
can see real filesystem holes, that answer is used instead.

### Backup format

Content-addressed rather than a Veeam-style VBK/VIB chain. A restore point is
an ordered list of chunk hashes per disk (a "block map") plus the VM config.
Synthetic fulls come for free, every point is independently restorable,
deduplication works across VMs, and retention is a refcount decrement instead
of an increment merge. A test deletes a parent point outright and restores its
child to keep that property honest.

Blocks are a fixed 1 MiB, not content-defined. Guest writes land at stable
offsets, so a fixed grid already aligns between backups; content-defined
chunking solves byte-insertion drift, which cannot happen inside a block
device, and would cost the direct index -> offset mapping that keeps restore a
seek.

Chunks are grouped into ~128 MB **pack files**. One file per chunk is nearly
free locally but wrong for NFS, where each chunk would cost a create, write,
commit and rename, and a 100 GB VM would leave ~100,000 files to walk on every
verify. Chunks are verified against their own hash on every read.

### Destinations

    {"kind": "local", "path": "/backup/openbackup"}
    {"kind": "nfs", "server": "10.0.0.5", "export": "/volume1/backup",
     "mountpoint": "/backup", "path": "openbackup"}

NFS exports are mounted and unmounted around the job. A mount already in place
is adopted and left alone, since tearing down an operator's mount mid-job is
worse than leaving ours up. Mounts default to `hard`: a soft mount returns
EIO on timeout, which during a restore means silently incomplete data.

The share is authoritative; the local SQLite index is a rebuildable cache.
SQLite over NFS depends on a working lock daemon and corrupts badly without
one, so each pack carries a trailer listing its contents and `repo reindex`
recovers a repository by reading only pack tails.

## Usage

    openbackup init-config
    openbackup inventory
    openbackup backup installer
    openbackup points
    openbackup verify <point-id>
    openbackup restore <point-id> --to-dir /backup/restored
    openbackup repo info

## Layout

    openbackup/nbd/        NBD client (pure Python, pipelined)
    openbackup/repo/       codec, packs, block maps, index, restore points
    openbackup/transport/  direct NFS, datastore HTTPS, VDDK/nbdkit
    openbackup/vsphere/    connection, inventory, snapshots, CBT
    openbackup/jobs/       backup, restore, verify

## Development

    dnf install -y python3.12 nbdkit nbdkit-vddk-plugin libnbd qemu-img nfs-utils
    python3.12 -m venv .venv && .venv/bin/pip install -e .
    .venv/bin/pip install pytest pytest-timeout
    .venv/bin/python -m pytest

Tests run against real `nbdkit` and `qemu-nbd` servers, an in-process fake NBD
server for protocol edge cases, and a fake datastore endpoint. A handful of NFS
integration tests need root and a loopback export, and skip without one.
