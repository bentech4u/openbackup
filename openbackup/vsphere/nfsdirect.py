"""Read VM disks straight from an NFS datastore.

After the backup snapshot is taken, ESXi sends new guest writes to a delta
file and the base disk is frozen. That base is a plain flat file on the NFS
export, so it can be read directly from the NAS: neither vCenter nor the
ESXi host carries any of the data.

Two rules keep this safe:

* The datastore is only ever mounted read-only, and files are opened
  O_RDONLY. Production VM files are never written.
* Only a *base* flat disk is read. If the VM already had snapshots of its
  own, the disk backing at our snapshot is a delta, and its base holds stale
  data; that is refused rather than silently backed up.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..vsphere.client import parse_ds_path

SECTOR = 512
MAX_DESCRIPTOR = 64 * 1024
ROOT_PARENT_CID = "ffffffff"
FLAT_EXTENT_TYPES = {"FLAT", "VMFS"}
_EXTENT_RE = re.compile(r'^(RW|RDONLY|NOACCESS)\s+(\d+)\s+(\w+)\s+"([^"]+)"(?:\s+(\d+))?\s*$')


class DirectNfsError(Exception):
    pass


@dataclass
class FlatExtent:
    path: Path
    sectors: int
    offset_sectors: int = 0  # into the extent file

    @property
    def size(self) -> int:
        return self.sectors * SECTOR


def datastore_relpath(ds_path: str) -> PurePosixPath:
    """'[ds] dir/disk.vmdk' -> dir/disk.vmdk, refusing anything that could
    escape the datastore."""
    _ds, rel = parse_ds_path(ds_path)
    p = PurePosixPath(rel)
    if not rel or p.is_absolute() or ".." in p.parts:
        raise DirectNfsError(f"Unexpected disk path {ds_path!r}")
    return p


def parse_descriptor(text: str) -> tuple[dict[str, str], list[tuple[str, int, str, str, int]]]:
    fields: dict[str, str] = {}
    extents = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _EXTENT_RE.match(line)
        if m:
            extents.append((m.group(1), int(m.group(2)), m.group(3).upper(), m.group(4),
                            int(m.group(5) or 0)))
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            fields[k.strip()] = v.strip().strip('"')
    return fields, extents


def resolve_base_flat(descriptor: Path) -> list[FlatExtent]:
    """The flat extent files of a base (non-delta) disk."""
    try:
        size = descriptor.stat().st_size
        if size > MAX_DESCRIPTOR:
            raise DirectNfsError(f"{descriptor.name} is not a text descriptor (monolithic "
                                 "sparse disks cannot be read directly)")
        text = descriptor.read_text(errors="replace")
    except FileNotFoundError:
        raise DirectNfsError(f"{descriptor} not found on the datastore mount; check the "
                             "datastore's NFS export setting") from None
    fields, extents = parse_descriptor(text)
    if fields.get("parentCID", ROOT_PARENT_CID).lower() != ROOT_PARENT_CID:
        raise DirectNfsError(
            f"{descriptor.name} is a snapshot delta: the VM had snapshots of its own before "
            "the backup started. Delete them (or consolidate) so the base disk is current.")
    if not extents:
        raise DirectNfsError(f"{descriptor.name} lists no extents")
    out = []
    for access, sectors, etype, name, offset in extents:
        if etype not in FLAT_EXTENT_TYPES:
            raise DirectNfsError(f"{descriptor.name}: extent type {etype} cannot be read "
                                 "directly (only flat disks can)")
        if access == "NOACCESS" or "/" in name or name in (".", ".."):
            raise DirectNfsError(f"{descriptor.name}: unusable extent {name!r}")
        path = descriptor.parent / name
        try:
            fsize = path.stat().st_size
        except FileNotFoundError:
            raise DirectNfsError(f"extent {name} is missing") from None
        if fsize < (offset + sectors) * SECTOR:
            raise DirectNfsError(f"extent {name} is shorter than its descriptor says")
        out.append(FlatExtent(path, sectors, offset))
    return out


class FlatDisk:
    """Read-only, NBD-client-shaped access to a flat disk's extents, so the
    backup engine can treat it like any other disk source."""

    read_only = True

    def __init__(self, extents: list[FlatExtent], description: str = ""):
        self.extents = extents
        self.size = sum(e.size for e in extents)
        self.description = description
        self._fds = []
        try:
            for e in extents:
                fd = os.open(e.path, os.O_RDONLY | os.O_CLOEXEC)
                self._fds.append(fd)
                try:
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
                except OSError:
                    pass
        except BaseException:
            self.close()
            raise
        self._starts = []
        pos = 0
        for e in extents:
            self._starts.append(pos)
            pos += e.size

    def pread(self, offset: int, length: int) -> bytes:
        if offset < 0 or offset + length > self.size:
            raise DirectNfsError(f"read {offset}+{length} is outside the disk")
        out = bytearray()
        while length > 0:
            i = max(j for j, s in enumerate(self._starts) if s <= offset)
            e, start = self.extents[i], self._starts[i]
            within = offset - start
            n = min(length, e.size - within)
            chunk = os.pread(self._fds[i], n, e.offset_sectors * SECTOR + within)
            if len(chunk) != n:
                raise DirectNfsError(f"short read from {e.path.name} at {within}")
            out += chunk
            offset += n
            length -= n
        return bytes(out)

    def pread_many(self, requests, depth: int = 8):
        for off, length in requests:
            yield self.pread(off, length)

    def allocated_extents(self) -> list[tuple[int, int]] | None:
        """Data regions according to the filesystem, or None when it cannot
        tell (NFS before v4.2 reports every file as fully allocated, which
        is useless rather than wrong)."""
        out: list[tuple[int, int]] = []
        for i, e in enumerate(self.extents):
            fd = self._fds[i]
            st = os.fstat(fd)
            base = e.offset_sectors * SECTOR
            end = base + e.size
            off = base
            found = []
            while off < end:
                try:
                    d = os.lseek(fd, off, os.SEEK_DATA)
                except OSError:  # ENXIO: no more data
                    break
                if d >= end:
                    break
                h = min(os.lseek(fd, d, os.SEEK_HOLE), end)
                found.append((d, h))
                off = h
            covered = sum(h - d for d, h in found)
            if covered >= e.size and st.st_blocks * 512 < e.size * 0.9:
                return None  # filesystem hides its holes
            out += [(self._starts[i] + d - base, h - d) for d, h in found]
        return out

    def flush(self) -> None:
        pass

    def close(self) -> None:
        for fd in self._fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds = []

    def __enter__(self) -> FlatDisk:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
