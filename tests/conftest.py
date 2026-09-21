import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NBDKIT = shutil.which("nbdkit")
requires_nbdkit = pytest.mark.skipif(NBDKIT is None, reason="nbdkit not installed")


class NbdkitServer:
    """A throwaway nbdkit instance on a unix socket, for tests."""

    def __init__(self, *args: str, readonly: bool = False):
        self.dir = tempfile.mkdtemp(prefix="obtest-")
        self.sock = os.path.join(self.dir, "nbd.sock")
        cmd = [NBDKIT, "-f", "-U", self.sock]
        if readonly:
            cmd.append("-r")
        cmd.extend(args)
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self._wait_ready()

    def _wait_ready(self, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                err = self.proc.stderr.read().decode("utf-8", "replace")
                raise RuntimeError(f"nbdkit exited early: {err}")
            if os.path.exists(self.sock):
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.connect(self.sock)
                    s.close()
                    return
                except OSError:
                    pass
            time.sleep(0.05)
        raise RuntimeError("nbdkit did not become ready in time")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        for pipe in (self.proc.stdout, self.proc.stderr):
            if pipe:
                pipe.close()
        shutil.rmtree(self.dir, ignore_errors=True)


@pytest.fixture
def nbdkit_memory():
    """Factory for a memory-backed nbdkit export."""
    servers = []

    def _make(size: str = "64M", readonly: bool = False) -> NbdkitServer:
        srv = NbdkitServer("memory", size, readonly=readonly)
        servers.append(srv)
        return srv

    yield _make
    for srv in servers:
        srv.stop()
