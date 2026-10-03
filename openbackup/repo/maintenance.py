"""Retention, garbage collection and verification.

GC is mark-and-sweep over the block maps of every remaining point rather
than reference counting: a crash mid-backup or mid-delete can leave
refcounts wrong, but it cannot make the set of live maps wrong.

GC and verify-all must not run concurrently with a backup into the same
repository; the worker serialises them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .crypto import IntegrityError
from .repository import Repository

Log = Callable[[str], None]


def _noop(_msg: str) -> None:
    pass


# ------------------------------------------------------------------ retention


def select_expired(points: list[dict], keep_points: int, keep_days: int,
                   now: datetime | None = None) -> list[str]:
    """Points of one VM that fall outside the policy. A point is kept if it
    is among the newest ``keep_points`` or younger than ``keep_days``. The
    newest point is always kept."""
    now = now or datetime.now(UTC)
    ordered = sorted(points, key=lambda p: p["created_at"], reverse=True)
    expired = []
    for i, p in enumerate(ordered):
        if i == 0:
            continue
        by_count = keep_points > 0 and i < keep_points
        age = now - datetime.fromisoformat(p["created_at"])
        by_age = keep_days > 0 and age < timedelta(days=keep_days)
        if not (by_count or by_age):
            expired.append(p["id"])
    return expired


# ------------------------------------------------------------------------ gc


@dataclass
class GcResult:
    live_chunks: int = 0
    packs_deleted: int = 0
    packs_repacked: int = 0
    bytes_freed: int = 0
    temp_files_removed: int = 0


def collect_garbage(repo: Repository, repack_below: float = 0.5, log: Log = _noop) -> GcResult:
    res = GcResult()
    repo.sync_index()
    db = repo._db
    with repo._lock:
        db.execute("CREATE TEMP TABLE IF NOT EXISTS live (id BLOB PRIMARY KEY) WITHOUT ROWID")
        db.execute("DELETE FROM live")
        db.execute("BEGIN")
        for pid in repo.point_ids():
            manifest = repo.load_manifest(pid)
            for key in manifest.get("disk_keys", []):
                m = repo.load_map(pid, key)
                db.executemany("INSERT OR IGNORE INTO live VALUES (?)",
                               [(c,) for c in m.stored_ids()])
        db.execute("COMMIT")
        res.live_chunks = db.execute("SELECT count(*) FROM live").fetchone()[0]
        packs = db.execute(
            """SELECT p.id, p.size,
                      coalesce(sum(CASE WHEN l.id IS NOT NULL THEN c.length + 4 END), 0)
               FROM packs p LEFT JOIN chunks c ON c.pack = p.id
               LEFT JOIN live l ON l.id = c.id
               GROUP BY p.id"""
        ).fetchall()

    for pack_id, size, live_bytes in packs:
        if live_bytes == 0:
            repo._forget_pack(pack_id)
            repo._pack_path(pack_id).unlink(missing_ok=True)
            res.packs_deleted += 1
            res.bytes_freed += size
        elif live_bytes / max(size, 1) < repack_below:
            res.bytes_freed += size - _repack(repo, pack_id)
            res.packs_repacked += 1

    with repo._lock:
        db.execute("DROP TABLE IF EXISTS live")
    res.temp_files_removed = repo.cleanup_temp_files()
    log(f"GC: {res.packs_deleted} packs deleted, {res.packs_repacked} repacked, "
        f"{res.bytes_freed / 2**20:.1f} MiB freed")
    return res


def _repack(repo: Repository, pack_id: str) -> int:
    """Copy the live chunks of a pack into a new pack, then drop the old one.
    Returns the size of the new pack."""
    with repo._lock:
        rows = repo._db.execute(
            "SELECT c.id, c.offset, c.length, c.raw_length FROM chunks c "
            "JOIN live l ON l.id = c.id WHERE c.pack=?", (pack_id,)).fetchall()
    new_id, writer = repo.new_pack_writer()
    try:
        for cid, _off, _len, raw_len in rows:
            data = repo.read_chunk(cid)  # verifies before copying
            writer.add(cid, repo.codec.encode(cid, data), raw_len)
        entries = writer.finish()
    except BaseException:
        writer.abort()
        raise
    # New pack is durable before the old one's index rows go, so a crash in
    # between leaves duplicates, never a missing chunk.
    repo._forget_pack(pack_id)
    repo._index_pack(new_id, writer.size, entries)
    repo._pack_path(pack_id).unlink(missing_ok=True)
    return writer.size


# -------------------------------------------------------------------- verify


@dataclass
class VerifyResult:
    chunks_checked: int = 0
    bytes_checked: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def verify_point(repo: Repository, point_id: str, log: Log = _noop,
                 progress: Callable[[float], None] | None = None,
                 cancelled: Callable[[], bool] | None = None) -> VerifyResult:
    """Read back every chunk a point references and check it against its id."""
    res = VerifyResult()
    manifest = repo.load_manifest(point_id)
    ids: set[bytes] = set()
    for key in manifest["disk_keys"]:
        try:
            ids |= repo.load_map(point_id, key).stored_ids()
        except (OSError, IntegrityError) as e:
            res.errors.append(f"disk {key}: block map unreadable: {e}")
    total = len(ids) or 1
    for i, cid in enumerate(sorted(ids)):
        if cancelled and cancelled():
            res.errors.append("cancelled")
            break
        try:
            res.bytes_checked += len(repo.read_chunk(cid))
            res.chunks_checked += 1
        except (OSError, IntegrityError) as e:
            res.errors.append(str(e))
            log(f"ERROR {e}")
        if progress and i % 64 == 0:
            progress(i / total)
    if progress:
        progress(1.0)
    return res
