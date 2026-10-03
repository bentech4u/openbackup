"""TaskContext backed by the database, so the web UI can follow a running
task: log lines, progress counters, per-item status, and cancellation."""

from __future__ import annotations

import logging
import threading
import time

from ..db import session_scope
from ..db.models import Task, TaskLog

log = logging.getLogger("openbackup.worker")


class DbTaskContext:
    FLUSH_SECONDS = 2.0

    def __init__(self, task_id: int):
        self.task_id = task_id
        self._lock = threading.Lock()
        self._read = self._written = 0
        self._total: int | None = None
        self._fraction: float | None = None
        self._items: dict[str, dict] = {}
        self._items_dirty = False
        self._last_flush = 0.0
        self._cancel_checked = 0.0
        self._cancelled = False
        self.warnings = 0
        self.errors = 0

    def log(self, message: str, level: str = "info") -> None:
        if level == "warning":
            self.warnings += 1
        elif level == "error":
            self.errors += 1
        getattr(log, level if level != "warning" else "warning", log.info)(
            "task %s: %s", self.task_id, message)
        with session_scope() as db:
            db.add(TaskLog(task_id=self.task_id, level=level, message=message[:4000]))

    def progress(self, fraction=None, *, read: int = 0, written: int = 0, total=None) -> None:
        with self._lock:
            self._read += read
            self._written += written
            if total is not None:
                self._total = total
            if fraction is not None:
                self._fraction = fraction
        self._maybe_flush()

    def item(self, name: str, **fields) -> None:
        with self._lock:
            self._items.setdefault(name, {"name": name}).update(fields)
            self._items_dirty = True
        self._maybe_flush()

    def cancelled(self) -> bool:
        now = time.monotonic()
        if not self._cancelled and now - self._cancel_checked > 1.0:
            self._cancel_checked = now
            with session_scope() as db:
                self._cancelled = bool(db.get(Task, self.task_id).cancel_requested)
        return self._cancelled

    def _maybe_flush(self) -> None:
        if time.monotonic() - self._last_flush >= self.FLUSH_SECONDS:
            self.flush()

    def flush(self) -> None:
        with self._lock:
            read, written, total = self._read, self._written, self._total
            items = [dict(v) for v in self._items.values()] if self._items_dirty else None
            self._items_dirty = False
            self._last_flush = time.monotonic()
            fraction = self._fraction
        with session_scope() as db:
            t = db.get(Task, self.task_id)
            t.bytes_read = read
            t.bytes_written = written
            if total is not None:
                t.bytes_total = total
            if fraction is not None:
                t.progress = min(fraction, 0.999)
            elif total:
                t.progress = min(max(read, written) / total, 0.999)
            if items is not None:
                t.items = items


class ScopedContext:
    """Presents one of ``count`` sequential sub-tasks (e.g. VMs in a job) as
    a whole task to an engine, mapping its progress into the parent's."""

    def __init__(self, parent: DbTaskContext, index: int, count: int, base_total: int):
        self.parent, self.index, self.count = parent, index, count
        self.base_total = base_total
        self.total = 0
        self.done = 0

    def log(self, message: str, level: str = "info") -> None:
        self.parent.log(message, level)

    def progress(self, fraction=None, *, read: int = 0, written: int = 0, total=None) -> None:
        if total is not None:
            self.total = total
        self.done += max(read, written)
        own = fraction if fraction is not None and not self.total else (
            min(self.done / self.total, 1.0) if self.total else 0.0)
        self.parent.progress((self.index + own) / self.count, read=read, written=written,
                             total=self.base_total + self.total)

    def item(self, name: str, **fields) -> None:
        self.parent.item(name, **fields)

    def cancelled(self) -> bool:
        return self.parent.cancelled()
