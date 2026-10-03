"""A backup repository: content-addressed chunks in pack files, plus restore
points that reference them.

On-share layout::

    openbackup-repo.json        header: id, format, encryption parameters
    packs/<xx>/<pack-id>.pack   chunk data
    points/<point-id>.json      restore point manifest (encrypted if the repo is)
    maps/<point-id>/<disk>.map  block map per disk

The share is authoritative. The chunk index is a local SQLite cache, kept off
NFS on purpose, and rebuilt from pack trailers whenever it disagrees with the
pack files present on the share.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import crypto
from .blockmap import BlockMap
from .crypto import ZERO_ID, Codec, IntegrityError
from .packfile import PackWriter, fsync_dir, read_blob, read_trailer

HEADER_NAME = "openbackup-repo.json"
FORMAT = 1
PACK_TARGET = 128 << 20


class RepositoryError(Exception):
    pass


def new_point_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, path)
    fsync_dir(path.parent)


@dataclass
class RepoStats:
    packs: int
    chunks: int
    stored_bytes: int
    points: int


class Repository:
    def __init__(self, root: Path, header: dict, codec: Codec, index_dir: Path):
        self.root = root
        self.header = header
        self.codec = codec
        self.id: str = header["id"]
        self._lock = threading.RLock()
        index_dir.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(index_dir / f"{self.id}.sqlite", check_same_thread=False,
                                   isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                id BLOB PRIMARY KEY, pack TEXT NOT NULL, offset INTEGER NOT NULL,
                length INTEGER NOT NULL, raw_length INTEGER NOT NULL) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS chunks_pack ON chunks(pack);
            CREATE TABLE IF NOT EXISTS packs (
                id TEXT PRIMARY KEY, size INTEGER NOT NULL, chunks INTEGER NOT NULL);
            """
        )
        self._fds: OrderedDict[str, object] = OrderedDict()

    # ---------------------------------------------------------------- setup

    @classmethod
    def create(cls, root: Path, index_dir: Path, passphrase: str | None = None) -> Repository:
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        if (root / HEADER_NAME).exists():
            raise RepositoryError(f"{root} already contains a repository")
        if any(p for p in root.iterdir() if not p.name.startswith(".")):
            raise RepositoryError(f"{root} is not empty")
        header: dict = {"format": FORMAT, "id": str(uuid.uuid4()),
                        "created": datetime.now(UTC).isoformat(), "encryption": None}
        codec = Codec()
        if passphrase:
            enc, enc_key, id_key = crypto.new_encryption_header(passphrase)
            header["encryption"] = enc
            codec = Codec(enc_key, id_key)
        for d in ("packs", "points", "maps"):
            (root / d).mkdir(exist_ok=True)
        write_atomic(root / HEADER_NAME, json.dumps(header, indent=2).encode())
        return cls(root, header, codec, index_dir)

    @classmethod
    def open(cls, root: Path, index_dir: Path, passphrase: str | None = None) -> Repository:
        root = Path(root)
        try:
            header = json.loads((root / HEADER_NAME).read_text())
        except FileNotFoundError:
            raise RepositoryError(f"No repository at {root}") from None
        if header.get("format") != FORMAT:
            raise RepositoryError(f"Unsupported repository format {header.get('format')}")
        codec = Codec()
        if header.get("encryption"):
            if not passphrase:
                raise RepositoryError("Repository is encrypted; a passphrase is required")
            codec = Codec(*crypto.unwrap_keys(header["encryption"], passphrase))
        repo = cls(root, header, codec, index_dir)
        repo.sync_index()
        return repo

    @staticmethod
    def exists(root: Path) -> bool:
        return (Path(root) / HEADER_NAME).exists()

    def close(self) -> None:
        with self._lock:
            for f in self._fds.values():
                f.close()
            self._fds.clear()
            self._db.close()

    def __enter__(self) -> Repository:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------------------------------------------------------- index

    def _pack_path(self, pack_id: str) -> Path:
        return self.root / "packs" / pack_id[:2] / f"{pack_id}.pack"

    def _pack_files(self) -> dict[str, Path]:
        out = {}
        packs = self.root / "packs"
        for sub in packs.iterdir() if packs.exists() else ():
            if sub.is_dir():
                for p in sub.iterdir():
                    if p.suffix == ".pack":
                        out[p.stem] = p
        return out

    def sync_index(self) -> tuple[int, int]:
        """Bring the index in line with the packs on the share. Returns
        (packs added, packs removed)."""
        with self._lock:
            on_share = self._pack_files()
            indexed = {r[0] for r in self._db.execute("SELECT id FROM packs")}
            added = removed = 0
            for pack_id in indexed - on_share.keys():
                self._forget_pack(pack_id)
                removed += 1
            for pack_id in on_share.keys() - indexed:
                try:
                    entries = read_trailer(on_share[pack_id])
                except IntegrityError:
                    # An incomplete pack is never renamed into place, so this is
                    # damage; leave it for verify to report rather than abort.
                    continue
                self._index_pack(pack_id, on_share[pack_id].stat().st_size, entries)
                added += 1
            return added, removed

    def reindex(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM chunks")
            self._db.execute("DELETE FROM packs")
        self.sync_index()

    def _index_pack(self, pack_id: str, size: int, entries) -> None:
        with self._lock:
            self._db.execute("BEGIN")
            self._db.executemany(
                "INSERT OR IGNORE INTO chunks VALUES (?,?,?,?,?)",
                [(e.chunk_id, pack_id, e.offset, e.length, e.raw_length) for e in entries],
            )
            self._db.execute("INSERT OR REPLACE INTO packs VALUES (?,?,?)",
                             (pack_id, size, len(entries)))
            self._db.execute("COMMIT")

    def _forget_pack(self, pack_id: str) -> None:
        with self._lock:
            f = self._fds.pop(pack_id, None)
            if f:
                f.close()
            self._db.execute("BEGIN")
            self._db.execute("DELETE FROM chunks WHERE pack=?", (pack_id,))
            self._db.execute("DELETE FROM packs WHERE id=?", (pack_id,))
            self._db.execute("COMMIT")

    # --------------------------------------------------------------- chunks

    def has_chunk(self, chunk_id: bytes) -> bool:
        if chunk_id == ZERO_ID:
            return True
        with self._lock:
            return self._db.execute("SELECT 1 FROM chunks WHERE id=?",
                                    (chunk_id,)).fetchone() is not None

    def _locate(self, chunk_id: bytes) -> tuple[str, int, int, int]:
        with self._lock:
            row = self._db.execute("SELECT pack, offset, length, raw_length FROM chunks "
                                   "WHERE id=?", (chunk_id,)).fetchone()
        if row is None:
            raise IntegrityError(f"chunk {chunk_id.hex()} is missing from the repository")
        return row

    def _fd(self, pack_id: str):
        f = self._fds.get(pack_id)
        if f is None:
            f = open(self._pack_path(pack_id), "rb")
            self._fds[pack_id] = f
            if len(self._fds) > 32:
                self._fds.popitem(last=False)[1].close()
        else:
            self._fds.move_to_end(pack_id)
        return f

    def read_chunk(self, chunk_id: bytes, length: int | None = None) -> bytes:
        if chunk_id == ZERO_ID:
            if length is None:
                raise ValueError("length is required for a zero block")
            return bytes(length)
        pack_id, offset, blen, _raw = self._locate(chunk_id)
        with self._lock:
            blob = read_blob(self._fd(pack_id), offset, blen)
        return self.codec.decode(chunk_id, blob)

    def writer(self) -> ChunkWriter:
        return ChunkWriter(self)

    def new_pack_writer(self) -> tuple[str, PackWriter]:
        pack_id = secrets.token_hex(16)
        return pack_id, PackWriter(self._pack_path(pack_id))

    # --------------------------------------------------------------- points

    def save_point(self, point_id: str, manifest: dict, maps: dict[str, BlockMap]) -> None:
        """Commit a restore point. Every chunk it references must already be
        durable (the writer closed); the manifest is written last, so a point
        either exists completely or not at all."""
        for disk_key, m in maps.items():
            for cid in m.stored_ids():
                if not self.has_chunk(cid):
                    raise RepositoryError(f"point {point_id} references a missing chunk")
            write_atomic(self.root / "maps" / point_id / f"{disk_key}.map",
                         self.codec.seal_metadata(m.to_bytes()))
        manifest = {**manifest, "id": point_id, "disk_keys": sorted(maps)}
        write_atomic(self.root / "points" / f"{point_id}.json",
                     self.codec.seal_metadata(json.dumps(manifest, indent=1).encode()))

    def load_manifest(self, point_id: str) -> dict:
        try:
            raw = (self.root / "points" / f"{point_id}.json").read_bytes()
        except FileNotFoundError:
            raise RepositoryError(f"No restore point {point_id}") from None
        return json.loads(self.codec.open_metadata(raw))

    def load_map(self, point_id: str, disk_key: str) -> BlockMap:
        raw = (self.root / "maps" / point_id / f"{disk_key}.map").read_bytes()
        return BlockMap.from_bytes(self.codec.open_metadata(raw))

    def point_ids(self) -> list[str]:
        d = self.root / "points"
        return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []

    def list_points(self) -> Iterator[dict]:
        for pid in self.point_ids():
            try:
                yield self.load_manifest(pid)
            except (IntegrityError, ValueError):
                continue

    def delete_point(self, point_id: str) -> None:
        (self.root / "points" / f"{point_id}.json").unlink(missing_ok=True)
        fsync_dir(self.root / "points")
        mdir = self.root / "maps" / point_id
        if mdir.exists():
            for p in mdir.iterdir():
                p.unlink()
            mdir.rmdir()

    def stats(self) -> RepoStats:
        with self._lock:
            packs, size = self._db.execute(
                "SELECT count(*), coalesce(sum(size),0) FROM packs").fetchone()
            chunks = self._db.execute("SELECT count(*) FROM chunks").fetchone()[0]
        return RepoStats(packs, chunks, size, len(self.point_ids()))

    def cleanup_temp_files(self, older_than: float = 24 * 3600) -> int:
        cutoff = time.time() - older_than
        removed = 0
        for p in self.root.rglob("*.tmp"):
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                removed += 1
        return removed


class ChunkWriter:
    """Deduplicating writer for one backup task."""

    def __init__(self, repo: Repository, pack_target: int = PACK_TARGET):
        self.repo = repo
        self.pack_target = pack_target
        self._pack_id: str | None = None
        self._pack: PackWriter | None = None
        self._pending: set[bytes] = set()
        self.new_bytes = 0  # stored (compressed) bytes added

    def put(self, data: bytes) -> bytes:
        if data.count(0) == len(data):
            return ZERO_ID
        cid = self.repo.codec.chunk_id(data)
        if cid in self._pending or self.repo.has_chunk(cid):
            return cid
        blob = self.repo.codec.encode(cid, data)
        if self._pack is None:
            self._pack_id, self._pack = self.repo.new_pack_writer()
        self._pack.add(cid, blob, len(data))
        self._pending.add(cid)
        self.new_bytes += len(blob)
        if self._pack.size >= self.pack_target:
            self.flush()
        return cid

    def flush(self) -> None:
        if self._pack is None:
            return
        entries = self._pack.finish()
        self.repo._index_pack(self._pack_id, self._pack.size, entries)
        self._pack = self._pack_id = None
        self._pending.clear()

    def close(self) -> None:
        self.flush()

    def abort(self) -> None:
        if self._pack is not None:
            self._pack.abort()
        self._pack = self._pack_id = None
        self._pending.clear()

    def __enter__(self) -> ChunkWriter:
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.close()
        else:
            self.abort()
