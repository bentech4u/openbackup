"""Run nbdkit on a private unix socket for the duration of one disk transfer.

For VM disks the plugin is ``vddk`` with ``libdir`` pointing at OpenVDDK (or
VMware's own VDDK, which is a drop-in alternative). The vCenter password is
handed over through an inherited pipe, never on the command line.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .client import NbdClient, NbdError


@dataclass
class VddkTarget:
    libdir: Path
    server: str
    user: str
    password: str
    thumbprint: str
    vm_moref: str
    file: str  # e.g. "[datastore1] web01/web01.vmdk"
    snapshot_moref: str | None = None
    port: int = 443
    transports: str = "nbdssl:nbd"


def vddk_args(t: VddkTarget) -> list[str]:
    """Plugin arguments, less the password (added by Nbdkit.start)."""
    args = [
        "vddk",
        f"libdir={t.libdir}",
        f"server={t.server}",
        f"port={t.port}",
        f"user={t.user}",
        f"thumbprint={t.thumbprint}",
        f"vm=moref={t.vm_moref}",
        f"file={t.file}",
        f"transports={t.transports}",
    ]
    if t.snapshot_moref:
        args.append(f"snapshot={t.snapshot_moref}")
    return args


class Nbdkit:
    def __init__(self, plugin_args: list[str], *, readonly: bool = True, nbdkit: str = "nbdkit",
                 secret: str | None = None, threads: int = 8, start_timeout: float = 120):
        self.plugin_args = plugin_args
        self.readonly = readonly
        self.nbdkit = nbdkit
        self.secret = secret
        self.threads = threads
        self.start_timeout = start_timeout
        self._dir: Path | None = None
        self._proc: subprocess.Popen | None = None
        self.socket: Path | None = None

    @classmethod
    def vddk(cls, target: VddkTarget, *, readonly: bool = True, nbdkit: str = "nbdkit") -> Nbdkit:
        return cls(vddk_args(target), readonly=readonly, nbdkit=nbdkit,
                   secret=target.password)

    def _stderr_tail(self) -> str:
        try:
            return (self._dir / "nbdkit.log").read_text(errors="replace")[-2000:].strip()
        except OSError:
            return ""

    def start(self) -> Nbdkit:
        if shutil.which(self.nbdkit) is None:
            raise NbdError(f"{self.nbdkit} is not installed")
        self._dir = Path(tempfile.mkdtemp(prefix="openbackup-nbd-"))
        os.chmod(self._dir, 0o700)
        self.socket = self._dir / "nbd.sock"
        args = list(self.plugin_args)
        pass_fds: tuple[int, ...] = ()
        rfd = None
        if self.secret is not None:
            rfd, wfd = os.pipe()
            os.write(wfd, self.secret.encode())
            os.close(wfd)
            pass_fds = (rfd,)
            args.append(f"password=-{rfd}")
        cmd = [self.nbdkit, "--foreground", "--exit-with-parent", "--unix", str(self.socket),
               "--threads", str(self.threads)]
        if self.readonly:
            cmd.append("--readonly")
        cmd += args
        log = open(self._dir / "nbdkit.log", "wb")
        try:
            self._proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log,
                                          stderr=subprocess.STDOUT, pass_fds=pass_fds)
        finally:
            log.close()
            if rfd is not None:
                os.close(rfd)
        deadline = time.monotonic() + self.start_timeout
        while not self.socket.exists():
            if self._proc.poll() is not None:
                tail = self._stderr_tail()
                self.stop()
                raise NbdError(f"nbdkit exited during startup: {tail}")
            if time.monotonic() > deadline:
                self.stop()
                raise NbdError("nbdkit did not start in time")
            time.sleep(0.05)
        return self

    def failure_reason(self) -> str:
        """Why nbdkit stopped serving, for error messages: its exit status
        (a crash in the VDDK library shows up as a signal) and its output."""
        parts = []
        if self._proc is not None:
            rc = self._proc.poll()
            if rc is None:
                time.sleep(0.2)  # a crashing process may still be exiting
                rc = self._proc.poll()
            if rc is not None and rc < 0:
                try:
                    name = signal.Signals(-rc).name
                except ValueError:
                    name = f"signal {-rc}"
                parts.append(f"nbdkit crashed ({name}); this is usually a fault in the VDDK "
                             f"library at {self._vddk_libdir() or 'the configured libdir'}")
            elif rc is not None:
                parts.append(f"nbdkit exited with status {rc}")
        tail = self._stderr_tail()
        if tail:
            parts.append(f"nbdkit output: {tail}")
        return "; ".join(parts) or "nbdkit gave no further detail"

    def _vddk_libdir(self) -> str:
        return next((a.split("=", 1)[1] for a in self.plugin_args if a.startswith("libdir=")), "")

    def connect(self) -> NbdClient:
        try:
            return NbdClient.connect_unix(str(self.socket))
        except (NbdError, OSError) as e:
            raise NbdError(f"{e}; {self.failure_reason()}") from None

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        self._proc = None
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    def __enter__(self) -> Nbdkit:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
