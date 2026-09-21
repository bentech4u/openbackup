"""Per-disk block maps: the thing a restore point actually stores.

A block map is an ordered array of chunk hashes, one per fixed-size block of a
virtual disk. Restoring a disk means walking the array and writing each chunk
at its index; the map alone is enough, with no dependency on any other restore
point. That is what makes every point here behave like a synthetic full.

Fixed-size blocks rather than content-defined chunking: VM disk writes land at
stable offsets, so a fixed grid already lines up between two backups of the
same disk. Content-defined chunking exists to survive byte insertions shifting
everything downstream, which does not happen inside a block device. It would
cost us the direct index -> offset mapping that makes restore a simple seek.
"""

from __future__ import annotations

import struct
from pathlib import Path

import zstandard

from .codec import HASH_BYTES

#: 1 MiB. Large enough that per-chunk overhead stays negligible, small enough
#: that a small guest write does not force us to re-store a huge block. Also a
#: whole multiple of VMware's 64 KiB CBT reporting granularity.
DEFAULT_CHUNK_SIZE = 1024 * 1024

_MAGIC = b"OBM1"
_HEADER = struct.Struct("<4sHHQQ")  # magic, version, reserved, chunk size, disk size


class BlockMapError(Exception):
    pass


class BlockMap:
    """Chunk hashes for one disk, indexed by block number.

    An entry of all zero bytes means "never written in this backup", which can
    only survive into a committed map as a bug; :meth:`validate` catches it
    before we record a restore point that cannot be restored.
    """

    def __init__(self, disk_size: int, chunk_size: int = DEFAULT_CHUNK_SIZE):
        if chunk_size <= 0:
            raise ValueError("chunk size must be positive")
        if disk_size < 0:
            raise ValueError("disk size must be non-negative")
        self.disk_size = disk_size
        self.chunk_size = chunk_size
        self.count = (disk_size + chunk_size - 1) // chunk_size
        self._buf = bytearray(self.count * HASH_BYTES)

    # -- indexing -----------------------------------------------------------

    def __len__(self) -> int:
        return self.count

    def block_range(self, index: int) -> tuple[int, int]:
        """Byte offset and length of a block, clamped to the disk size.

        The last block of a disk whose size is not a multiple of the chunk size
        is short. Writing a full-length final chunk on restore would run past
        the end of the disk.
        """
        if not 0 <= index < self.count:
            raise IndexError(f"block {index} out of range (count {self.count})")
        offset = index * self.chunk_size
        return offset, min(self.chunk_size, self.disk_size - offset)

    def blocks_for(self, offset: int, length: int) -> range:
        """Which blocks a byte range touches, e.g. a CBT changed extent."""
        if length <= 0:
            return range(0)
        first = offset // self.chunk_size
        last = (offset + length - 1) // self.chunk_size
        return range(first, min(last + 1, self.count))

    def __getitem__(self, index: int) -> bytes:
        if not 0 <= index < self.count:
            raise IndexError(index)
        start = index * HASH_BYTES
        return bytes(self._buf[start:start + HASH_BYTES])

    def __setitem__(self, index: int, digest: bytes) -> None:
        if not 0 <= index < self.count:
            raise IndexError(index)
        if len(digest) != HASH_BYTES:
            raise ValueError(f"hash must be {HASH_BYTES} bytes, got {len(digest)}")
        start = index * HASH_BYTES
        self._buf[start:start + HASH_BYTES] = digest

    def __iter__(self):
        for i in range(self.count):
            yield self[i]

    def distinct_hashes(self) -> set[bytes]:
        """Unique chunks referenced, for refcounting at commit time."""
        return {self[i] for i in range(self.count)}

    # -- incremental --------------------------------------------------------

    def clone(self) -> "BlockMap":
        """Start an incremental from the previous point's map.

        An incremental backup rewrites only the blocks CBT reports as changed;
        every other entry is inherited unchanged, which is exactly why the
        result is still independently restorable.
        """
        other = BlockMap(self.disk_size, self.chunk_size)
        other._buf = bytearray(self._buf)
        return other

    def resized_clone(self, new_disk_size: int) -> "BlockMap":
        """Clone onto a grown disk, inheriting the blocks that still exist.

        Guests get disks extended between backups. Growth keeps existing block
        contents at the same offsets, so the inherited entries stay valid and
        only the new tail needs backing up. Shrinking is not a thing vSphere
        permits online, but truncating here keeps us consistent if it happens.
        """
        other = BlockMap(new_disk_size, self.chunk_size)
        keep = min(self.count, other.count) * HASH_BYTES
        other._buf[:keep] = self._buf[:keep]
        # A grown disk's final old block may have been short; it is now full
        # length, so its stored chunk no longer covers the whole block.
        if other.count > self.count and self.count:
            last_old = self.count - 1
            if self.disk_size % self.chunk_size:
                other[last_old] = b"\0" * HASH_BYTES
        return other

    def validate(self) -> None:
        empty = b"\0" * HASH_BYTES
        missing = [i for i in range(self.count) if self[i] == empty]
        if missing:
            shown = ", ".join(str(i) for i in missing[:10])
            more = f" (and {len(missing) - 10} more)" if len(missing) > 10 else ""
            raise BlockMapError(
                f"block map has {len(missing)} unwritten block(s): {shown}{more}"
            )

    # -- serialisation ------------------------------------------------------

    def to_bytes(self) -> bytes:
        header = _HEADER.pack(_MAGIC, 1, 0, self.chunk_size, self.disk_size)
        return header + zstandard.ZstdCompressor(level=3).compress(bytes(self._buf))

    @classmethod
    def from_bytes(cls, blob: bytes) -> "BlockMap":
        if len(blob) < _HEADER.size:
            raise BlockMapError("block map is truncated")
        magic, version, _r, chunk_size, disk_size = _HEADER.unpack(
            blob[:_HEADER.size])
        if magic != _MAGIC:
            raise BlockMapError(f"not a block map (magic {magic!r})")
        if version != 1:
            raise BlockMapError(f"unsupported block map version {version}")
        bm = cls(disk_size, chunk_size)
        body = zstandard.ZstdDecompressor().decompress(
            blob[_HEADER.size:], max_output_size=bm.count * HASH_BYTES)
        if len(body) != bm.count * HASH_BYTES:
            raise BlockMapError(
                f"block map body is {len(body)} bytes, expected "
                f"{bm.count * HASH_BYTES}")
        bm._buf = bytearray(body)
        return bm

    def save(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(self.to_bytes())
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path | str) -> "BlockMap":
        return cls.from_bytes(Path(path).read_bytes())
