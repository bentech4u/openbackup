"""What an engine needs from whoever runs it: logging, progress and
cancellation. The worker backs this with the database; tests use a stub."""

from __future__ import annotations

from typing import Protocol


class Cancelled(Exception):
    pass


class TaskContext(Protocol):
    def log(self, message: str, level: str = "info") -> None: ...

    def progress(self, fraction: float | None = None, *, read: int = 0, written: int = 0,
                 total: int | None = None) -> None:
        """``read``/``written`` are increments; ``total`` replaces."""

    def item(self, name: str, **fields) -> None:
        """Create or update a per-item status line (one per VM or disk)."""

    def cancelled(self) -> bool: ...


class NullContext:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.items: dict[str, dict] = {}
        self.read = self.written = 0
        self.cancel = False

    def log(self, message: str, level: str = "info") -> None:
        self.messages.append((level, message))

    def progress(self, fraction=None, *, read=0, written=0, total=None) -> None:
        self.read += read
        self.written += written

    def item(self, name: str, **fields) -> None:
        self.items.setdefault(name, {}).update(fields)

    def cancelled(self) -> bool:
        return self.cancel

    def check(self) -> None:
        if self.cancel:
            raise Cancelled()


def check_cancel(ctx: TaskContext) -> None:
    if ctx.cancelled():
        raise Cancelled("Cancelled by user")
