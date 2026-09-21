"""Pack files: many chunks in one object.

Storing one file per 1 MiB chunk is fine on a local filesystem, where creates
are page-cache cheap, but it is the wrong shape for NFS. Each chunk would cost
a create, a write, a commit and a rename -- four or more round trips -- and a
100 GB VM would leave ~100,000 files to walk on every verify and garbage
collection. Grouping chunks into ~128 MB packs turns that into a few hundred
large sequential writes.

Layout::

    blob[0] .. blob[N-1]     each a self-describing ChunkCodec blob
    entry[0] .. entry[N-1]   hash(32) + offset(u64) + length(u32)
    footer                   count(u32) + version(u16) + pad(u16) + "OBP1"

The trailing index makes a pack self-describing: the repository index can be
rebuilt by reading the last few KB of each pack, and because blobs carry their
own headers a pack can be salvaged by walking it even if the trailer is lost.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .codec import HASH_BYTES

_MAGIC = b"OBP1"
_VERSION = 1
_ENTRY = struct.Struct(f"<{HASH_BYTES}sQI")   # hash, offset, length
_FOOTER = struct.Struct("<IHH4s")             # count, version, pad, magic

#: Default target pack size. Large enough that per-pack overhead and file
#: counts stay negligible; small enough that a pack is a reasonable unit to
#: buffer in memory and to rewrite when repacking frees space inside it.
DEFAULT_PACK_SIZE = 128 * 1024 * 1024


class PackError(Exception):
    pass


@dataclass(frozen=True)
class PackEntry:
    """Where one chunk lives inside a pack."""

    hash: bytes
    offset: int
    length: int


class PackWriter:
    """Accumulates encoded chunks, then emits one pack.

    Held in memory rather than streamed to a temporary file: a pack is bounded
    by `target_size`, and writing it as a single sequential operation is
    exactly what makes this worthwhile over NFS.
    """

    def __init__(self, target_size: int = DEFAULT_PACK_SIZE):
        self.target_size = target_size
        self._buf = bytearray()
        self._entries: list[PackEntry] = []
        self._index: dict[bytes, PackEntry] = {}

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def data_size(self) -> int:
        return len(self._buf)

    @property
    def is_empty(self) -> bool:
        return not self._entries

    @property
    def is_full(self) -> bool:
        return len(self._buf) >= self.target_size

    def contains(self, digest: bytes) -> bool:
        """Whether this not-yet-written pack already holds a chunk.

        Needed so that duplicate blocks within a single backup deduplicate
        against the open pack, not just against what is already committed.
        """
        return digest in self._index

    def add(self, digest: bytes, blob: bytes) -> PackEntry:
        if len(digest) != HASH_BYTES:
            raise ValueError(f"hash must be {HASH_BYTES} bytes")
        existing = self._index.get(digest)
        if existing is not None:
            return existing
        entry = PackEntry(digest, len(self._buf), len(blob))
        self._buf += blob
        self._entries.append(entry)
        self._index[digest] = entry
        return entry

    def blob(self, digest: bytes) -> bytes | None:
        """The encoded blob for a chunk still sitting in the open pack.

        A chunk written moments ago is not in the index yet, so reads have to
        be able to find it here or a backup could not verify its own output
        before the pack is closed.
        """
        entry = self._index.get(digest)
        if entry is None:
            return None
        return bytes(self._buf[entry.offset:entry.offset + entry.length])

    def finish(self) -> tuple[bytes, list[PackEntry]]:
        """Serialise the pack. Returns the bytes and the entries it contains."""
        if not self._entries:
            raise PackError("refusing to write an empty pack")
        out = bytearray(self._buf)
        for entry in self._entries:
            out += _ENTRY.pack(entry.hash, entry.offset, entry.length)
        out += _FOOTER.pack(len(self._entries), _VERSION, 0, _MAGIC)
        return bytes(out), list(self._entries)

    def reset(self) -> None:
        self._buf = bytearray()
        self._entries = []
        self._index = {}


def parse_footer(tail: bytes) -> int:
    """Read a pack's chunk count from its last bytes."""
    if len(tail) < _FOOTER.size:
        raise PackError("pack is too small to contain a footer")
    count, version, _pad, magic = _FOOTER.unpack(tail[-_FOOTER.size:])
    if magic != _MAGIC:
        raise PackError(f"not a pack file (footer magic {magic!r})")
    if version != _VERSION:
        raise PackError(f"unsupported pack version {version}")
    return count


def index_offset(pack_size: int, count: int) -> int:
    """Byte offset of the entry table within a pack of known size."""
    start = pack_size - _FOOTER.size - count * _ENTRY.size
    if start < 0:
        raise PackError(
            f"pack claims {count} entries, which do not fit in {pack_size} bytes")
    return start


def parse_entries(blob: bytes, count: int) -> list[PackEntry]:
    """Decode the entry table read from a pack."""
    if len(blob) != count * _ENTRY.size:
        raise PackError(
            f"entry table is {len(blob)} bytes, expected {count * _ENTRY.size}")
    entries = []
    for i in range(count):
        digest, offset, length = _ENTRY.unpack_from(blob, i * _ENTRY.size)
        entries.append(PackEntry(digest, offset, length))
    return entries


def read_index(pack: bytes) -> list[PackEntry]:
    """Read a whole pack's index. Convenience for tests and recovery."""
    count = parse_footer(pack)
    start = index_offset(len(pack), count)
    return parse_entries(pack[start:len(pack) - _FOOTER.size], count)


#: Size of the fixed-length footer, so readers know how much tail to fetch.
FOOTER_SIZE = _FOOTER.size
ENTRY_SIZE = _ENTRY.size
