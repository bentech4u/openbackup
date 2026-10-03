"""Pack files: many chunks in one file.

One file per chunk is cheap locally but wrong for NFS, where every file costs
a create, a commit and a rename, and a large VM would leave hundreds of
thousands of files to walk. Packs are ~128 MB.

Layout::

    b"OBPACK01"
    repeated: u32 length, blob
    trailer:  repeated (32-byte id, u64 offset, u32 length, u32 raw length)
    footer:   u64 trailer offset, u32 entry count, b"OBPKEND1"

The trailer makes every pack self-describing, so the local chunk index can be
rebuilt by reading only the tail of each pack.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from pathlib import Path

from .crypto import IntegrityError

MAGIC = b"OBPACK01"
END_MAGIC = b"OBPKEND1"
_ENTRY = struct.Struct(">32sQII")
_FOOTER = struct.Struct(">QI8s")
_LEN = struct.Struct(">I")


@dataclass(frozen=True)
class PackEntry:
    chunk_id: bytes
    offset: int  # of the blob, after its length prefix
    length: int
    raw_length: int


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class PackWriter:
    def __init__(self, final_path: Path):
        self.final_path = final_path
        self.tmp_path = final_path.with_name(final_path.name + ".tmp")
        final_path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(self.tmp_path, "wb")
        self._f.write(MAGIC)
        self.size = len(MAGIC)
        self.entries: list[PackEntry] = []

    def add(self, chunk_id: bytes, blob: bytes, raw_length: int) -> PackEntry:
        self._f.write(_LEN.pack(len(blob)))
        self._f.write(blob)
        e = PackEntry(chunk_id, self.size + _LEN.size, len(blob), raw_length)
        self.entries.append(e)
        self.size += _LEN.size + len(blob)
        return e

    def finish(self) -> list[PackEntry]:
        trailer_offset = self.size
        for e in self.entries:
            self._f.write(_ENTRY.pack(e.chunk_id, e.offset, e.length, e.raw_length))
        self._f.write(_FOOTER.pack(trailer_offset, len(self.entries), END_MAGIC))
        self.size = trailer_offset + len(self.entries) * _ENTRY.size + _FOOTER.size
        self._f.flush()
        os.fsync(self._f.fileno())
        self._f.close()
        os.rename(self.tmp_path, self.final_path)
        fsync_dir(self.final_path.parent)
        return self.entries

    def abort(self) -> None:
        self._f.close()
        self.tmp_path.unlink(missing_ok=True)


def read_trailer(path: Path) -> list[PackEntry]:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        if end < len(MAGIC) + _FOOTER.size:
            raise IntegrityError(f"{path.name}: too short to be a pack")
        f.seek(end - _FOOTER.size)
        trailer_offset, count, magic = _FOOTER.unpack(f.read(_FOOTER.size))
        if magic != END_MAGIC:
            raise IntegrityError(f"{path.name}: missing pack footer")
        if trailer_offset + count * _ENTRY.size + _FOOTER.size != end:
            raise IntegrityError(f"{path.name}: footer does not match file size")
        f.seek(trailer_offset)
        raw = f.read(count * _ENTRY.size)
    return [PackEntry(*_ENTRY.unpack_from(raw, i * _ENTRY.size)) for i in range(count)]


def read_blob(f, offset: int, length: int) -> bytes:
    f.seek(offset - _LEN.size)
    (stored_len,) = _LEN.unpack(f.read(_LEN.size))
    if stored_len != length:
        raise IntegrityError(f"blob length at {offset} is {stored_len}, index says {length}")
    blob = f.read(length)
    if len(blob) != length:
        raise IntegrityError(f"short read at {offset}")
    return blob
