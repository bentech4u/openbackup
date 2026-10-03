"""NFS mounts for repositories, and a health check for any repository path.

Mounts default to ``hard``: a soft mount returns EIO on timeout, which during
a backup or restore means silently incomplete data. A mount that is already
in place is adopted and never unmounted by us.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_OPTIONS = "nfsvers=4.2,hard"
_SAFE_OPT = re.compile(r"^[A-Za-z0-9_.=,:-]*$")
_SAFE_HOST = re.compile(r"^[A-Za-z0-9_.:\[\]-]+$")


class NfsError(Exception):
    pass


@dataclass
class MountInfo:
    source: str
    mountpoint: Path
    fstype: str


def _unescape(s: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def mounts() -> list[MountInfo]:
    out = []
    with open("/proc/self/mountinfo") as f:
        for line in f:
            left, _, right = line.partition(" - ")
            fields = left.split()
            rfields = right.split()
            out.append(MountInfo(_unescape(rfields[1]), Path(_unescape(fields[4])), rfields[0]))
    return out


def find_mount(mountpoint: Path) -> MountInfo | None:
    mountpoint = Path(mountpoint)
    for m in reversed(mounts()):
        if m.mountpoint == mountpoint:
            return m
    return None


def validate(server: str, export: str, options: str) -> None:
    if not _SAFE_HOST.match(server or ""):
        raise NfsError("Invalid NFS server name")
    if not export.startswith("/") or "\0" in export or ".." in export.split("/"):
        raise NfsError("NFS export must be an absolute path")
    if not _SAFE_OPT.match(options or ""):
        raise NfsError("Invalid characters in mount options")
    opts = {o.split("=")[0] for o in options.split(",") if o}
    if "soft" in opts:
        raise NfsError("Soft mounts can silently lose backup data; use 'hard'")


def ensure_mounted(server: str, export: str, mountpoint: Path,
                   options: str = DEFAULT_OPTIONS, timeout: int = 60) -> bool:
    """Mount if needed. Returns True if this call mounted it, False if it was
    already mounted (in which case the caller must not unmount it)."""
    validate(server, export, options)
    source = f"{server}:{export}"
    existing = find_mount(mountpoint)
    if existing is not None:
        if not existing.fstype.startswith("nfs") or existing.source.rstrip("/") != \
                source.rstrip("/"):
            raise NfsError(f"{mountpoint} is already mounted from {existing.source}")
        return False
    mountpoint.mkdir(parents=True, exist_ok=True)
    opts = options or DEFAULT_OPTIONS
    if "hard" not in opts.split(","):
        opts += ",hard"
    try:
        proc = subprocess.run(
            ["mount", "-t", "nfs", "-o", opts, "--", source, str(mountpoint)],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise NfsError(f"Timed out mounting {source}") from None
    if proc.returncode != 0:
        raise NfsError(f"mount {source} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return True


def unmount(mountpoint: Path, timeout: int = 120) -> None:
    proc = subprocess.run(["umount", "--", str(mountpoint)], capture_output=True, text=True,
                          timeout=timeout)
    if proc.returncode != 0:
        raise NfsError(f"umount {mountpoint} failed: {proc.stderr.strip()}")


@dataclass
class PathCheck:
    ok: bool
    message: str
    capacity_bytes: int = 0
    free_bytes: int = 0
    write_mib_s: float = 0.0


def check_path(path: Path) -> PathCheck:
    """Prove a repository location is usable: create, write, fsync, read
    back, delete, and report space."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".openbackup-probe-{secrets.token_hex(4)}"
        data = os.urandom(4 << 20)
        t0 = time.monotonic()
        with open(probe, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        elapsed = time.monotonic() - t0
        if probe.read_bytes() != data:
            probe.unlink(missing_ok=True)
            return PathCheck(False, "Data read back does not match what was written")
        probe.unlink()
        st = os.statvfs(path)
    except OSError as e:
        return PathCheck(False, f"{e.strerror or e} ({path})")
    return PathCheck(
        True, "OK",
        capacity_bytes=st.f_blocks * st.f_frsize,
        free_bytes=st.f_bavail * st.f_frsize,
        write_mib_s=round(4 / max(elapsed, 1e-6), 1),
    )
