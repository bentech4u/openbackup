"""Local index mapping a chunk hash to its location inside a pack.

Deliberately *not* stored in the repository. SQLite relies on POSIX locking,
which over NFS depends on a working lock daemon and behaves badly when it is
not; a corrupted index on the share would be a single point of failure for
every job pointed at it.

Instead this is a local, rebuildable cache. Everything it holds can be
recovered from the repository itself, because each pack carries a trailer
listing the chunks it contains -- see :func:`rebuild`. The authoritative data
(packs, block maps, restore points) lives in the repository; losing the server
costs a reindex, not a backup.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from .packfile import (
    ENTRY_SIZE, FOOTER_SIZE, PackEntry, index_offset, parse_entries, parse_footer,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS packs (
    pack_id     TEXT PRIMARY KEY,
    size        INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    hash    BLOB PRIMARY KEY,
    pack_id TEXT NOT NULL REFERENCES packs(pack_id) ON DELETE CASCADE,
    offset  INTEGER NOT NULL,
    length  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_by_pack ON chunks(pack_id);
"""

SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class ChunkLocation:
    pack_id: str
    offset: int
    length: int


class ChunkIndex:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        # Durability here is not critical: the index is a cache and a lost
        # write costs a reindex, not data. NORMAL avoids an fsync per commit
        # while a backup inserts hundreds of thousands of rows.
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_SCHEMA)
        self._db.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema', ?)",
            (SCHEMA_VERSION,),
        )

    # -- lookup -------------------------------------------------------------

    def locate(self, digest: bytes) -> ChunkLocation | None:
        row = self._db.execute(
            "SELECT pack_id, offset, length FROM chunks WHERE hash = ?",
            (digest,),
        ).fetchone()
        return ChunkLocation(*row) if row else None

    def has(self, digest: bytes) -> bool:
        return self._db.execute(
            "SELECT 1 FROM chunks WHERE hash = ? LIMIT 1", (digest,)
        ).fetchone() is not None

    def have_any(self, digests: Iterable[bytes]) -> set[bytes]:
        """Which of these chunks are already stored.

        Asked once per batch rather than once per chunk: a backup checks
        thousands of hashes and the per-statement overhead dominates otherwise.
        """
        found: set[bytes] = set()
        digests = list(digests)
        for i in range(0, len(digests), 500):
            batch = digests[i:i + 500]
            marks = ",".join("?" * len(batch))
            rows = self._db.execute(
                f"SELECT hash FROM chunks WHERE hash IN ({marks})", batch
            ).fetchall()
            found.update(r[0] for r in rows)
        return found

    def known_packs(self) -> set[str]:
        return {r[0] for r in self._db.execute("SELECT pack_id FROM packs")}

    def chunk_count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    def pack_count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM packs").fetchone()[0]

    def chunks_in(self, pack_id: str) -> Iterator[ChunkLocation]:
        for row in self._db.execute(
            "SELECT pack_id, offset, length FROM chunks WHERE pack_id = ?",
            (pack_id,),
        ):
            yield ChunkLocation(*row)

    # -- mutation -----------------------------------------------------------

    def add_pack(self, pack_id: str, size: int,
                 entries: Iterable[PackEntry]) -> None:
        """Record a pack and its contents in one transaction.

        A pack is only ever indexed after it is durably in the repository, so
        the index can lag reality but never point at something that is not
        there. The other direction -- an unindexed pack -- is recoverable by
        reindexing.
        """
        entries = list(entries)
        with self._db:
            self._db.execute("BEGIN")
            self._db.execute(
                "INSERT OR REPLACE INTO packs(pack_id, size, chunk_count) "
                "VALUES(?, ?, ?)",
                (pack_id, size, len(entries)),
            )
            self._db.executemany(
                "INSERT OR REPLACE INTO chunks(hash, pack_id, offset, length) "
                "VALUES(?, ?, ?, ?)",
                [(e.hash, pack_id, e.offset, e.length) for e in entries],
            )

    def forget_pack(self, pack_id: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM packs WHERE pack_id = ?", (pack_id,))

    def clear(self) -> None:
        with self._db:
            self._db.execute("DELETE FROM chunks")
            self._db.execute("DELETE FROM packs")

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "ChunkIndex":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def read_pack_index(backend, relpath: str) -> tuple[int, list[PackEntry]]:
    """Read one pack's trailer without pulling the whole pack across.

    Two reads: the fixed footer to learn the entry count, then the entry table.
    Over NFS that keeps reindexing to a few KB per pack rather than the
    hundreds of MB the pack actually holds.
    """
    size = backend.size(relpath)
    footer = backend.read_range(relpath, size - FOOTER_SIZE, FOOTER_SIZE)
    count = parse_footer(footer)
    start = index_offset(size, count)
    table = backend.read_range(relpath, start, count * ENTRY_SIZE)
    return size, parse_entries(table, count)


def rebuild(index: ChunkIndex, backend, prefix: str = "packs") -> int:
    """Rebuild the index from the repository. Returns the pack count.

    This is what makes the local index disposable: point a fresh server at an
    existing repository, reindex, and every restore point is reachable again.
    """
    index.clear()
    packs = 0
    for relpath in backend.list(prefix):
        if not relpath.endswith(".pack"):
            continue
        pack_id = Path(relpath).stem
        size, entries = read_pack_index(backend, relpath)
        index.add_pack(pack_id, size, entries)
        packs += 1
    return packs
