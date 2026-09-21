"""Where a repository's files live, and how an NFS export gets attached.

Two concerns kept apart on purpose:

* :class:`FilesystemBackend` is the I/O -- atomic writes, ranged reads,
  listing. An NFS export behaves like a directory once mounted, so the same
  implementation serves both destinations.
* :class:`NfsMount` is the lifecycle -- mounting, verifying, and unmounting
  only what we mounted ourselves.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator


class BackendError(Exception):
    pass


class FilesystemBackend:
    """Repository files on a mounted filesystem, local or NFS.

    Writes go to a temporary file, are fsynced, then renamed into place.
    ``rename`` is atomic on NFS as well as locally, so a crash or a lost server
    can leave a stray temporary file but never a partial pack that the index
    would later treat as complete.
    """

    def __init__(self, root: Path | str, *, fsync: bool = True):
        self.root = Path(root)
        self.fsync = fsync
        self._tmp = self.root / "tmp"

    def __repr__(self) -> str:
        return f"FilesystemBackend({str(self.root)!r})"

    # -- layout -------------------------------------------------------------

    def path(self, relpath: str) -> Path:
        # Keep everything inside the repository root: a traversal here would
        # write backup data somewhere nobody expects.
        full = (self.root / relpath).resolve()
        root = self.root.resolve()
        if full != root and root not in full.parents:
            raise BackendError(f"path {relpath!r} escapes the repository root")
        return full

    def init(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._tmp.mkdir(parents=True, exist_ok=True)

    # -- writing ------------------------------------------------------------

    def write(self, relpath: str, data: bytes) -> None:
        dest = self.path(relpath)
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._tmp.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=self._tmp, prefix="w-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                if self.fsync:
                    # Without this the rename can land while the data is still
                    # only in the client's cache; on NFS a server reboot would
                    # leave a correctly named, empty pack.
                    os.fsync(fh.fileno())
            os.replace(tmp_name, dest)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # -- reading ------------------------------------------------------------

    def read(self, relpath: str) -> bytes:
        try:
            return self.path(relpath).read_bytes()
        except FileNotFoundError as exc:
            raise BackendError(f"{relpath} not found in repository") from exc

    def read_range(self, relpath: str, offset: int, length: int) -> bytes:
        """Read a slice, which is how a chunk is fetched out of its pack."""
        if offset < 0 or length < 0:
            raise ValueError("offset and length must be non-negative")
        path = self.path(relpath)
        try:
            with open(path, "rb") as fh:
                fh.seek(offset)
                data = fh.read(length)
        except FileNotFoundError as exc:
            raise BackendError(f"{relpath} not found in repository") from exc
        if len(data) != length:
            raise BackendError(
                f"{relpath}: read {len(data)} bytes at offset {offset}, "
                f"expected {length}"
            )
        return data

    def read_tail(self, relpath: str, length: int) -> bytes:
        """Read the last `length` bytes -- a pack's footer and index."""
        path = self.path(relpath)
        try:
            size = path.stat().st_size
        except FileNotFoundError as exc:
            raise BackendError(f"{relpath} not found in repository") from exc
        start = max(0, size - length)
        return self.read_range(relpath, start, size - start)

    def size(self, relpath: str) -> int:
        try:
            return self.path(relpath).stat().st_size
        except FileNotFoundError as exc:
            raise BackendError(f"{relpath} not found in repository") from exc

    def exists(self, relpath: str) -> bool:
        return self.path(relpath).exists()

    def delete(self, relpath: str) -> bool:
        try:
            self.path(relpath).unlink()
            return True
        except FileNotFoundError:
            return False

    def list(self, prefix: str = "") -> Iterator[str]:
        base = self.path(prefix) if prefix else self.root
        if not base.exists():
            return
        for path in base.rglob("*"):
            if path.is_file():
                yield str(path.relative_to(self.root))

    def free_space(self) -> int:
        usage = shutil.disk_usage(self.root)
        return usage.free

    def cleanup_temp(self) -> int:
        """Remove leftovers from interrupted writes. Returns how many went."""
        if not self._tmp.exists():
            return 0
        removed = 0
        for path in self._tmp.iterdir():
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed


# -- NFS --------------------------------------------------------------------

#: Defaults chosen for a backup repository.
#:
#: ``hard`` is the one that matters. A soft mount gives up after a timeout and
#: returns EIO, which during a backup means a half-written pack and, worse,
#: during a restore means silently incomplete data. A backup job should block
#: until the server comes back, not invent a failure it might not report well.
#: ``timeo`` is in deciseconds, so 600 is a 60-second major timeout.
DEFAULT_NFS_OPTIONS = "hard,timeo=600,retrans=2,rsize=1048576,wsize=1048576"

_EXPORT_RE = re.compile(r"^/[\w./+@:-]*$")
_SERVER_RE = re.compile(r"^[\w.\-]+$|^\[[0-9A-Fa-f:]+\]$")
_OPTIONS_RE = re.compile(r"^[\w,.=/-]+$")


class NfsMount:
    """Mounts an NFS export for the life of a job, and unmounts it after.

    Only unmounts what it mounted. Finding the export already mounted -- by
    fstab, by an operator, or by a previous run -- is treated as success and
    left alone on exit, because tearing down someone else's mount mid-job is a
    far worse failure than leaving one up.
    """

    def __init__(self, server: str, export: str, mountpoint: Path | str, *,
                 options: str = DEFAULT_NFS_OPTIONS, version: str | None = None,
                 read_only: bool = False):
        if not _SERVER_RE.match(server):
            raise ValueError(f"invalid NFS server {server!r}")
        if not _EXPORT_RE.match(export):
            raise ValueError(f"invalid NFS export path {export!r}")
        if not _OPTIONS_RE.match(options):
            raise ValueError(f"invalid NFS mount options {options!r}")
        if version is not None and not re.match(r"^\d(\.\d)?$", version):
            raise ValueError(f"invalid NFS version {version!r}")

        self.server = server
        self.export = export
        self.mountpoint = Path(mountpoint)
        self.options = options
        self.version = version
        self.read_only = read_only
        self._mounted_by_us = False

    @property
    def spec(self) -> str:
        return f"{self.server}:{self.export}"

    # -- state --------------------------------------------------------------

    @staticmethod
    def _mount_table() -> list[tuple[str, str, str]]:
        """(source, target, fstype) from /proc/mounts."""
        out = []
        try:
            with open("/proc/mounts", "r") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 3:
                        # Paths are escaped octal-style in /proc/mounts.
                        target = parts[1].replace("\\040", " ")
                        out.append((parts[0], target, parts[2]))
        except OSError:
            pass
        return out

    def is_mounted(self) -> bool:
        target = str(self.mountpoint)
        for source, mnt, fstype in self._mount_table():
            if mnt == target and fstype.startswith("nfs"):
                return True
        return False

    def mounted_source(self) -> str | None:
        target = str(self.mountpoint)
        for source, mnt, fstype in self._mount_table():
            if mnt == target and fstype.startswith("nfs"):
                return source
        return None

    # -- lifecycle ----------------------------------------------------------

    def mount(self) -> None:
        if self.is_mounted():
            existing = self.mounted_source()
            if existing and existing.rstrip("/") != self.spec.rstrip("/"):
                raise BackendError(
                    f"{self.mountpoint} already has {existing} mounted, "
                    f"not {self.spec}; refusing to stack mounts"
                )
            # Someone else's mount, or a leftover from a previous run: use it,
            # but do not take ownership, so we will not unmount it later.
            return

        self.mountpoint.mkdir(parents=True, exist_ok=True)
        if any(self.mountpoint.iterdir()):
            raise BackendError(
                f"mountpoint {self.mountpoint} is not empty; mounting over it "
                "would hide existing files"
            )

        opts = self.options
        if self.version:
            opts = f"vers={self.version},{opts}"
        if self.read_only:
            opts = f"ro,{opts}"

        # List arguments, never a shell string: the server, export and options
        # come from configuration and must not be able to become commands.
        cmd = ["mount", "-t", "nfs", "-o", opts, self.spec, str(self.mountpoint)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except FileNotFoundError as exc:
            raise BackendError(
                "mount(8) not found; install nfs-utils to use an NFS repository"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise BackendError(
                f"mounting {self.spec} timed out after 120s; the server may be "
                "unreachable"
            ) from exc

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise BackendError(f"mounting {self.spec} failed: {detail}")

        if not self.is_mounted():
            raise BackendError(
                f"mount reported success but {self.mountpoint} is not an NFS mount"
            )
        self._mounted_by_us = True

    def verify_writable(self) -> None:
        """Prove we can actually write, before a job starts moving data."""
        probe = self.mountpoint / ".openbackup-write-test"
        try:
            probe.write_bytes(b"openbackup")
            probe.unlink()
        except OSError as exc:
            if exc.errno == errno.EROFS:
                raise BackendError(f"{self.spec} is mounted read-only") from exc
            if exc.errno == errno.EACCES:
                raise BackendError(
                    f"no write permission on {self.spec}; check the export's "
                    "squash settings (root_squash maps root to nobody)"
                ) from exc
            if exc.errno == errno.ESTALE:
                raise BackendError(
                    f"stale NFS handle on {self.mountpoint}; the export was "
                    "likely replaced on the server"
                ) from exc
            raise BackendError(f"{self.spec} is not writable: {exc}") from exc

    def unmount(self, force: bool = False) -> None:
        if not self._mounted_by_us:
            # Never tear down a mount we found already in place.
            return
        if not self.is_mounted():
            self._mounted_by_us = False
            return
        cmd = ["umount", str(self.mountpoint)]
        if force:
            cmd.insert(1, "-f")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0 and not force:
            # Busy is common right after a job; a lazy unmount detaches now and
            # cleans up when the last reference goes.
            proc = subprocess.run(
                ["umount", "-l", str(self.mountpoint)],
                capture_output=True, text=True, timeout=120,
            )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise BackendError(f"unmounting {self.mountpoint} failed: {detail}")
        self._mounted_by_us = False

    def backend(self, subdir: str = "") -> FilesystemBackend:
        root = self.mountpoint / subdir if subdir else self.mountpoint
        return FilesystemBackend(root)

    def __enter__(self) -> "NfsMount":
        self.mount()
        return self

    def __exit__(self, *exc) -> None:
        self.unmount()
