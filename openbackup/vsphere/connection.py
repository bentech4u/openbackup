"""Connecting to vCenter, and handing that session to the datastore transport.

The datastore endpoint accepts the same ``vmware_soap_session`` cookie as the
SOAP API, so a job authenticates once and reuses it for both control and data.
That avoids putting credentials on every one of the many thousands of range
requests a backup makes.
"""

from __future__ import annotations

import os
import ssl
from dataclasses import dataclass
from pathlib import Path

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim

from ..transport.datastore import DatastoreTransport


class VSphereError(Exception):
    pass


@dataclass
class VSphereConfig:
    host: str
    user: str
    password: str
    port: int = 443
    #: Verifying TLS is the default. Lab vCenters use self-signed certificates,
    #: so turning it off is normal there, but it has to be a deliberate choice:
    #: an unverified session can be intercepted, credentials included.
    verify_ssl: bool = True

    @classmethod
    def from_env_file(cls, path: Path | str) -> "VSphereConfig":
        values: dict[str, str] = {}
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
        missing = [k for k in ("VCENTER_HOST", "VCENTER_USER", "VCENTER_PASS")
                   if not values.get(k)]
        if missing:
            raise VSphereError(f"{path} is missing {', '.join(missing)}")
        return cls(
            host=values["VCENTER_HOST"],
            user=values["VCENTER_USER"],
            password=values["VCENTER_PASS"],
            port=int(values.get("VCENTER_PORT", 443)),
            verify_ssl=values.get("VCENTER_INSECURE", "0") not in ("1", "true", "yes"),
        )

    @classmethod
    def from_env(cls) -> "VSphereConfig":
        missing = [k for k in ("VCENTER_HOST", "VCENTER_USER", "VCENTER_PASS")
                   if not os.environ.get(k)]
        if missing:
            raise VSphereError(f"environment is missing {', '.join(missing)}")
        return cls(
            host=os.environ["VCENTER_HOST"],
            user=os.environ["VCENTER_USER"],
            password=os.environ["VCENTER_PASS"],
            port=int(os.environ.get("VCENTER_PORT", 443)),
            verify_ssl=os.environ.get("VCENTER_INSECURE", "0")
            not in ("1", "true", "yes"),
        )


class VSphereConnection:
    """An authenticated vCenter session."""

    def __init__(self, config: VSphereConfig):
        self.config = config
        self._si: vim.ServiceInstance | None = None
        self._content = None

    def connect(self) -> "VSphereConnection":
        if self._si is not None:
            return self

        if self.config.verify_ssl:
            ctx = ssl.create_default_context()
        else:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        try:
            self._si = SmartConnect(
                host=self.config.host, port=self.config.port,
                user=self.config.user, pwd=self.config.password,
                sslContext=ctx,
            )
        except vim.fault.InvalidLogin as exc:
            raise VSphereError(
                f"vCenter rejected the credentials for {self.config.user}"
            ) from exc
        except ssl.SSLCertVerificationError as exc:
            raise VSphereError(
                f"TLS verification failed for {self.config.host}: {exc}. "
                "Set VCENTER_INSECURE=1 to accept a self-signed lab certificate."
            ) from exc
        except OSError as exc:
            raise VSphereError(
                f"could not reach {self.config.host}:{self.config.port}: {exc}"
            ) from exc

        self._content = self._si.RetrieveContent()
        return self

    # -- accessors ----------------------------------------------------------

    @property
    def si(self) -> vim.ServiceInstance:
        if self._si is None:
            raise VSphereError("not connected")
        return self._si

    @property
    def content(self):
        if self._content is None:
            raise VSphereError("not connected")
        return self._content

    @property
    def about(self):
        return self.content.about

    @property
    def is_vcenter(self) -> bool:
        return self.content.about.apiType == "VirtualCenter"

    @property
    def cookie(self) -> str:
        """The session cookie the datastore endpoint accepts."""
        return self.si._stub.cookie

    def datastore_transport(self, **kwargs) -> DatastoreTransport:
        return DatastoreTransport(
            self.config.host, cookie=self.cookie,
            verify=self.config.verify_ssl, **kwargs,
        )

    # -- lookup -------------------------------------------------------------

    def find_all(self, kind, root=None) -> list:
        container = root or self.content.rootFolder
        view = self.content.viewManager.CreateContainerView(
            container, [kind], True)
        try:
            return list(view.view)
        finally:
            view.Destroy()

    def find_vm(self, name_or_uuid: str) -> vim.VirtualMachine:
        """Locate a VM by name, BIOS UUID or instance UUID.

        Name is matched last and must be unique: vCenter allows duplicate names
        across folders, and backing up the wrong VM because two share a name is
        not a mistake that should be possible to make silently.
        """
        for by_instance in (True, False):
            found = self.content.searchIndex.FindByUuid(
                None, name_or_uuid, True, by_instance)
            if found is not None:
                return found

        matches = [vm for vm in self.find_all(vim.VirtualMachine)
                   if vm.name == name_or_uuid]
        if not matches:
            raise VSphereError(f"no VM named {name_or_uuid!r}")
        if len(matches) > 1:
            paths = ", ".join(self.inventory_path(vm) for vm in matches)
            raise VSphereError(
                f"{len(matches)} VMs are named {name_or_uuid!r} ({paths}); "
                "use the instance UUID instead"
            )
        return matches[0]

    def inventory_path(self, entity) -> str:
        parts = []
        node = entity
        while node is not None and hasattr(node, "parent"):
            parts.append(node.name)
            node = node.parent
        return "/".join(reversed(parts))

    def datacenter_for(self, entity) -> vim.Datacenter:
        """The datacenter an object sits in -- the datastore endpoint needs its
        name in the dcPath parameter."""
        node = entity
        while node is not None:
            if isinstance(node, vim.Datacenter):
                return node
            node = getattr(node, "parent", None)
        raise VSphereError(f"could not find a datacenter for {entity}")

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        if self._si is not None:
            try:
                Disconnect(self._si)
            except Exception:
                pass
            self._si = None
            self._content = None

    def __enter__(self) -> "VSphereConnection":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()
