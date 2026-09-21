"""Waiting on vCenter tasks."""

from __future__ import annotations

import time
from typing import Callable

from pyVmomi import vim

from .connection import VSphereError


class TaskError(VSphereError):
    def __init__(self, what: str, fault):
        self.fault = fault
        message = getattr(fault, "msg", None) or str(fault)
        super().__init__(f"{what} failed: {message}")


def wait_for_task(task, what: str, *, timeout: float = 3600.0,
                  poll: float = 1.0,
                  on_progress: Callable[[int], None] | None = None):
    """Block until a task finishes, raising TaskError if it fails."""
    deadline = time.monotonic() + timeout
    last_progress = -1
    while True:
        info = task.info
        state = info.state
        if state == vim.TaskInfo.State.success:
            return info.result
        if state == vim.TaskInfo.State.error:
            raise TaskError(what, info.error)
        if time.monotonic() > deadline:
            raise VSphereError(f"{what} did not finish within {timeout:.0f}s")
        if on_progress is not None and info.progress is not None:
            if info.progress != last_progress:
                last_progress = info.progress
                on_progress(info.progress)
        time.sleep(poll)
