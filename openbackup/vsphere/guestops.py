"""Write files into a running VM through VMware Tools guest operations.

vCenter only handles small API calls here (validate the guest credentials,
start a transfer). The file data goes from this server straight to the ESXi
host running the VM, over HTTPS pinned to that host's certificate
thumbprint as vCenter reports it, and VMware Tools writes it in the guest.

Guest credentials are used for the duration of one restore and never stored.
"""

from __future__ import annotations

import hashlib
import http.client
import ntpath
import posixpath
import re
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pyVmomi import vim, vmodl

from .client import VSphere, VSphereError


class GuestError(Exception):
    pass


@dataclass
class GuestInfo:
    family: str  # windowsGuest | linuxGuest | ...
    os_name: str
    hostname: str

    @property
    def windows(self) -> bool:
        return self.family == "windowsGuest"


def _fingerprint(der: bytes) -> str:
    return ":".join(f"{b:02X}" for b in hashlib.sha1(der).digest())


def put_file(url: str, local: Path, thumbprint: str, timeout: float = 600) -> None:
    """HTTP PUT a file to an ESXi /guestFile URL, refusing any server whose
    certificate does not match ``thumbprint`` (SHA-1, as vCenter reports)."""
    u = urlsplit(url)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # verified by thumbprint below, before any data
    conn = http.client.HTTPSConnection(u.hostname, u.port or 443, context=ctx, timeout=timeout)
    try:
        conn.connect()
        got = _fingerprint(conn.sock.getpeercert(binary_form=True))
        if got.replace(":", "").upper() != thumbprint.replace(":", "").upper():
            raise GuestError(f"ESXi host {u.hostname} presented certificate {got}, expected "
                             f"{thumbprint}; refusing to send data")
        size = local.stat().st_size
        conn.putrequest("PUT", u.path + (f"?{u.query}" if u.query else ""),
                        skip_accept_encoding=True)
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(size))
        conn.endheaders()
        with open(local, "rb") as f:
            while chunk := f.read(1 << 20):
                conn.send(chunk)
        resp = conn.getresponse()
        body = resp.read(2000)
        if resp.status != 200:
            raise GuestError(f"ESXi rejected the upload: HTTP {resp.status} "
                             f"{body.decode(errors='replace')[:300]}")
    finally:
        conn.close()


class GuestFiles:
    def __init__(self, vs: VSphere, vm_moref: str, username: str, password: str):
        self.vs = vs
        self.vm_moref = vm_moref
        self._auth = vim.vm.guest.NamePasswordAuthentication(
            username=username, password=password, interactiveSession=False)
        self.info: GuestInfo | None = None

    @property
    def _vm(self) -> Any:
        return self.vs.vm(self.vm_moref)

    @property
    def _gom(self) -> Any:
        self.vs.ensure_session()
        return self.vs.content.guestOperationsManager

    # ----------------------------------------------------------------- checks

    def check(self) -> GuestInfo:
        """VMware Tools running and the credentials valid. Changes nothing."""
        vm = self._vm
        g = vm.guest
        if vm.runtime.powerState != vim.VirtualMachinePowerState.poweredOn:
            raise GuestError("The VM is not powered on")
        if g.toolsRunningStatus != "guestToolsRunning":
            raise GuestError("VMware Tools is not running in the VM")
        if not g.guestOperationsReady:
            raise GuestError("VMware Tools is running but not ready for guest operations")
        try:
            self._gom.authManager.ValidateCredentialsInGuest(vm=vm, auth=self._auth)
        except vim.fault.InvalidGuestLogin:
            raise GuestError("The guest OS rejected the username or password") from None
        except vim.fault.NoPermission as e:
            raise GuestError(f"The vCenter account lacks guest operation privileges "
                             f"({e.privilegeId})") from None
        except vim.fault.GuestOperationsFault as e:
            raise GuestError(f"Guest operations failed: {e.msg}") from None
        self.info = GuestInfo(g.guestFamily or "", g.guestFullName or "", g.hostName or "")
        return self.info

    # ------------------------------------------------------------------ paths

    @property
    def windows(self) -> bool:
        return bool(self.info and self.info.windows)

    def join(self, base: str, rel: str) -> str:
        """Append a '/'-separated relative path to a guest path."""
        parts = [p for p in rel.split("/") if p]
        if self.windows:
            if base.endswith(":"):
                base += "\\"
            return ntpath.join(base, *parts)
        return posixpath.join(base, *parts)

    def split(self, path: str) -> tuple[str, str]:
        return (ntpath if self.windows else posixpath).split(path)

    def renamed(self, path: str, stamp: str, n: int = 1) -> str:
        parent, name = self.split(path)
        stem, ext = (name, "") if name.startswith(".") else (ntpath if self.windows else
                                                             posixpath).splitext(name)
        suffix = f"_restored_{stamp}" + (f"_{n}" if n > 1 else "")
        return (ntpath if self.windows else posixpath).join(parent, f"{stem}{suffix}{ext}")

    # ------------------------------------------------------------- operations

    def exists(self, path: str) -> str | None:
        """'file', 'directory', or None."""
        parent, name = self.split(path)
        if not name:
            return "directory"
        flags = re.IGNORECASE if self.windows else 0
        try:
            res = self._gom.fileManager.ListFilesInGuest(
                vm=self._vm, auth=self._auth, filePath=parent or "/",
                matchPattern="^" + re.escape(name) + "$")
        except (vim.fault.FileNotFound, vim.fault.FileFault):
            return None
        except vim.fault.GuestOperationsFault as e:
            raise GuestError(f"Cannot list {parent}: {e.msg}") from None
        for f in res.files or []:
            if re.fullmatch(re.escape(name), f.path, flags):
                return "directory" if f.type == "directory" else "file"
        return None

    def mkdirs(self, path: str) -> None:
        try:
            self._gom.fileManager.MakeDirectoryInGuest(
                vm=self._vm, auth=self._auth, directoryPath=path, createParentDirectories=True)
        except vim.fault.FileAlreadyExists:
            pass
        except vim.fault.GuestOperationsFault as e:
            raise GuestError(f"Cannot create folder {path}: {e.msg}") from None

    def _attributes(self, meta: dict, with_owner: bool) -> Any:
        mtime = datetime.fromtimestamp(meta.get("mtime", 0), UTC) if meta.get("mtime") else None
        if self.windows:
            return vim.vm.guest.FileManager.WindowsFileAttributes(modificationTime=mtime)
        a = vim.vm.guest.FileManager.PosixFileAttributes(modificationTime=mtime)
        if meta.get("mode") is not None:
            a.permissions = meta["mode"]
        if with_owner:
            a.ownerId, a.groupId = meta.get("uid"), meta.get("gid")
        return a

    def upload(self, local: Path, guest_path: str, overwrite: bool, meta: dict) -> None:
        vm = self._vm
        host = vm.runtime.host
        address, thumbprint = host.name, host.summary.config.sslThumbprint
        if not thumbprint:
            raise GuestError(f"vCenter reports no certificate thumbprint for {address}")
        size = local.stat().st_size
        url = None
        for with_owner in (True, False):
            try:
                url = self._gom.fileManager.InitiateFileTransferToGuest(
                    vm=vm, auth=self._auth, guestFilePath=guest_path,
                    fileAttributes=self._attributes(meta, with_owner), fileSize=size,
                    overwrite=overwrite)
                break
            except vim.fault.FileAlreadyExists:
                raise GuestError(f"{guest_path} already exists") from None
            except (vim.fault.GuestPermissionDenied, vmodl.fault.InvalidArgument) as e:
                if not with_owner:
                    raise GuestError(f"Cannot write {guest_path}: "
                                     f"{getattr(e, 'msg', e)}") from None
                # A non-root guest account cannot set ownership; keep the
                # permissions and time, drop the owner.
            except vim.fault.GuestOperationsFault as e:
                raise GuestError(f"Cannot write {guest_path}: {e.msg}") from None
        u = urlsplit(url)
        if u.hostname in (None, "*") or u.netloc.startswith("*"):
            url = url.replace("*", address, 1)
        put_file(url, local, thumbprint)


__all__ = ["GuestError", "GuestFiles", "GuestInfo", "VSphereError", "put_file"]
