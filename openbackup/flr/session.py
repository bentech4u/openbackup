"""A file-level browse session over one restore point: repository ->
read-only NBD server -> libguestfs helper process.

Opening one boots the libguestfs appliance, which takes seconds with KVM and
up to a minute without, so sessions are kept open while in use and closed
after a period of inactivity.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path

from ..db.models import Repository as RepoRow
from ..repo.repository import Repository
from .nbdserver import PointNbdServer

HELPER = Path(__file__).with_name("guestfs_helper.py")
SYSTEM_PYTHON = os.environ.get("OPENBACKUP_FLR_PYTHON", "/usr/bin/python3")


class FlrError(Exception):
    pass


class FlrSession:
    OPEN_TIMEOUT = 600
    CALL_TIMEOUT = 300

    def __init__(self, repo_row: RepoRow | None, point_id: str, owner: str = "",
                 repo: Repository | None = None):
        self.id = secrets.token_urlsafe(16)
        self.point_id = point_id
        self.repository_id = repo_row.id if repo_row is not None else None
        self._repo = repo
        self.owner = owner
        self.last_used = time.monotonic()
        self.volumes: list[dict] = []
        self.os: dict | None = None
        self._repo_row = repo_row
        self._stack = ExitStack()
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._dir: Path | None = None
        self._seq = 0

    # ------------------------------------------------------------- lifecycle

    def open(self) -> FlrSession:
        from .. import services

        try:
            repo = self._repo or self._stack.enter_context(
                services.open_repository(self._repo_row))
            manifest = repo.load_manifest(self.point_id)
            self._dir = Path(tempfile.mkdtemp(prefix="openbackup-flr-"))
            os.chmod(self._dir, 0o700)
            server = PointNbdServer(repo, self.point_id, self._dir / "nbd.sock").start()
            self._stack.callback(server.stop)
            env = {**os.environ, "LIBGUESTFS_BACKEND": "direct",
                   "LIBGUESTFS_CACHEDIR": str(Path(tempfile.gettempdir()))}
            self._proc = subprocess.Popen(
                [SYSTEM_PYTHON, str(HELPER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=open(self._dir / "helper.log", "wb"), text=True, env=env, cwd=self._dir)
            # Disks in the order the VM had them, so guest device names
            # (sda, sdb...) and OS inspection match the original.
            disks = sorted(manifest.get("disks", []),
                           key=lambda d: (d.get("controller_key", 0), d.get("unit_number", 0)))
            keys = [str(d["key"]) for d in disks]
            labels = [d.get("label", "") for d in disks]
            if not keys:
                keys = [k for k in manifest["disk_keys"] if not k.startswith("__")]
                labels = keys
            res = self._call("open", {"drives": [{"socket": str(server.socket_path),
                                                  "export": k} for k in keys]},
                             timeout=self.OPEN_TIMEOUT)
        except BaseException:
            self.close()
            raise
        self.volumes, self.os = res["volumes"], res["os"]
        # Name each filesystem after the disk it is on (for OpenShift volumes,
        # the claim name): /dev/sdb2 is on the second disk added.
        for v in self.volumes:
            m = re.match(r"/dev/[sv]d([a-z])", v["device"])
            if m and ord(m.group(1)) - ord("a") < len(labels):
                v["disk"] = labels[ord(m.group(1)) - ord("a")]
        return self

    def close(self) -> None:
        if self._proc is not None:
            try:
                if self._proc.poll() is None:
                    self._proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                    self._proc.stdin.flush()
                    self._proc.wait(timeout=30)
            except (OSError, subprocess.TimeoutExpired, ValueError):
                self._proc.kill()
                self._proc.wait()
            self._proc = None
        self._stack.close()
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    def __enter__(self) -> FlrSession:
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------------------------------------------------------- calls

    def _helper_log(self) -> str:
        try:
            return (self._dir / "helper.log").read_text(errors="replace")[-1500:]
        except (OSError, TypeError):
            return ""

    def _call(self, cmd: str, args: dict, timeout: float | None = None) -> object:
        timeout = timeout or self.CALL_TIMEOUT
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                raise FlrError(f"file browser stopped: {self._helper_log()}")
            self.last_used = time.monotonic()
            self._seq += 1
            self._proc.stdin.write(json.dumps({"id": self._seq, "cmd": cmd, "args": args}) + "\n")
            self._proc.stdin.flush()
            line = _readline(self._proc, timeout)
            if line is None:
                self._proc.kill()
                raise FlrError(f"file browser did not answer within {int(timeout)}s")
            if not line:
                raise FlrError(f"file browser exited: {self._helper_log()}")
            resp = json.loads(line)
            self.last_used = time.monotonic()
        if not resp.get("ok"):
            raise FlrError(resp.get("error", "unknown error"))
        return resp["result"]

    def ls(self, path: str) -> list[dict]:
        return self._call("ls", {"path": path})

    def stat(self, path: str) -> dict:
        return self._call("stat", {"path": path})

    def walk(self, path: str) -> list[dict]:
        return self._call("walk", {"path": path}, timeout=1800)

    def download(self, path: str, dest: Path) -> dict:
        return self._call("download", {"path": path, "dest": str(dest)}, timeout=6 * 3600)

    def volume_of(self, path: str) -> dict:
        vid = path.split("/")[1] if path.startswith("/") else ""
        for v in self.volumes:
            if v["id"] == vid:
                return v
        raise FlrError("path is not on a known volume")


def _readline(proc: subprocess.Popen, timeout: float) -> str | None:
    """Read one line from the helper, or None on timeout."""
    result: list[str] = []
    t = threading.Thread(target=lambda: result.append(proc.stdout.readline()), daemon=True)
    t.start()
    t.join(timeout)
    return result[0] if result else None


class SessionRegistry:
    """Open browse sessions in the API process, closed when idle."""

    IDLE_SECONDS = 15 * 60
    MAX_SESSIONS = 4

    def __init__(self) -> None:
        self._sessions: dict[str, FlrSession] = {}
        self._lock = threading.Lock()
        self._reaper: threading.Thread | None = None

    def add(self, s: FlrSession) -> None:
        with self._lock:
            if len(self._sessions) >= self.MAX_SESSIONS:
                oldest = min(self._sessions.values(), key=lambda x: x.last_used)
                self._sessions.pop(oldest.id)
                threading.Thread(target=oldest.close, daemon=True).start()
            self._sessions[s.id] = s
            if self._reaper is None:
                self._reaper = threading.Thread(target=self._reap, daemon=True)
                self._reaper.start()

    def get(self, sid: str) -> FlrSession | None:
        with self._lock:
            return self._sessions.get(sid)

    def remove(self, sid: str) -> None:
        with self._lock:
            s = self._sessions.pop(sid, None)
        if s is not None:
            s.close()

    def _reap(self) -> None:
        while True:
            time.sleep(30)
            now = time.monotonic()
            with self._lock:
                idle = [s for s in self._sessions.values()
                        if now - s.last_used > self.IDLE_SECONDS]
                for s in idle:
                    self._sessions.pop(s.id)
            for s in idle:
                s.close()

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            s.close()


registry = SessionRegistry()
