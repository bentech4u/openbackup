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
    options: frozenset[str] = frozenset()

    @property
    def read_only(self) -> bool:
        return "ro" in self.options

    @property
    def nfs_version(self) -> str:
        return next((o.split("=", 1)[1] for o in self.options if o.startswith("vers=")), "")


def _unescape(s: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def mounts() -> list[MountInfo]:
    out = []
    with open("/proc/self/mountinfo") as f:
        for line in f:
            left, _, right = line.partition(" - ")
            fields = left.split()
            rfields = right.split()
            # Per-mount options (where "ro" lives) plus superblock options
            # (where NFS reports its negotiated version).
            opts = set(fields[5].split(",")) | set(rfields[2].split(",") if len(rfields) > 2
                                                   else ())
            out.append(MountInfo(_unescape(rfields[1]), Path(_unescape(fields[4])), rfields[0],
                                 frozenset(opts)))
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
                   options: str = DEFAULT_OPTIONS, timeout: int = 60,
                   read_only: bool = False) -> bool:
    """Mount if needed. Returns True if this call mounted it, False if it was
    already mounted (in which case the caller must not unmount it).

    ``read_only`` is for production datastores: the mount gets ``ro``, and an
    existing mount at the same place is only adopted if it is read-only too."""
    validate(server, export, options)
    source = f"{server}:{export}"
    existing = find_mount(mountpoint)
    if existing is not None:
        if not existing.fstype.startswith("nfs") or existing.source.rstrip("/") != \
                source.rstrip("/"):
            raise NfsError(f"{mountpoint} is already mounted from {existing.source}")
        if read_only and not existing.read_only:
            raise NfsError(f"{mountpoint} is mounted read-write; refusing to use it for a "
                           "datastore, which must only ever be mounted read-only")
        return False
    mountpoint.mkdir(parents=True, exist_ok=True)
    opts = options or DEFAULT_OPTIONS
    parts = [o for o in opts.split(",") if o and o != "rw"]
    if "hard" not in parts:
        parts.append("hard")
    if read_only and "ro" not in parts:
        parts.insert(0, "ro")
    opts = ",".join(parts)
    try:
        proc = subprocess.run(
            ["mount", "-t", "nfs", "-o", opts, "--", source, str(mountpoint)],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise NfsError(f"Timed out mounting {source}") from None
    if proc.returncode != 0:
        raise NfsError(f"mount {source} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    if read_only:
        info = find_mount(mountpoint)
        if info is None or not info.read_only:
            unmount(mountpoint)
            raise NfsError(f"{source} did not mount read-only; unmounted it again")
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
