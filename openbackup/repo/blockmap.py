"""Per-disk block map: the ordered chunk ids that make up one disk image.

Every block is a fixed 1 MiB. Guest writes land at stable offsets inside a
block device, so a fixed grid aligns between backups and keeps restore a
direct index -> offset seek. An all-zero block is recorded as ZERO_ID and
never stored.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .crypto import ZERO_ID, IntegrityError

BLOCK_SIZE = 1 << 20
MAGIC = b"OBMAP001"
_HEAD = struct.Struct(">8sQI")


@dataclass
class BlockMap:
    capacity: int
    block_size: int = BLOCK_SIZE
    ids: list[bytes] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.ids:
            self.ids = [ZERO_ID] * self.block_count

    @property
    def block_count(self) -> int:
        return (self.capacity + self.block_size - 1) // self.block_size

    def block_length(self, index: int) -> int:
        start = index * self.block_size
        return min(self.block_size, self.capacity - start)

    def copy(self) -> BlockMap:
        return BlockMap(self.capacity, self.block_size, list(self.ids))

    def to_bytes(self) -> bytes:
        return _HEAD.pack(MAGIC, self.capacity, self.block_size) + b"".join(self.ids)

    @classmethod
    def from_bytes(cls, raw: bytes) -> BlockMap:
        magic, capacity, block_size = _HEAD.unpack_from(raw)
        if magic != MAGIC:
            raise IntegrityError("not a block map")
        body = raw[_HEAD.size:]
        m = cls.__new__(cls)
        m.capacity, m.block_size = capacity, block_size
        if len(body) != m.block_count * 32:
            raise IntegrityError("block map length does not match capacity")
        m.ids = [body[i:i + 32] for i in range(0, len(body), 32)]
        return m

    def stored_ids(self) -> set[bytes]:
        return {i for i in self.ids if i != ZERO_ID}
