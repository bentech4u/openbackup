"""Configuration: where vCenter is, and where backups go."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .repo.repository import Destination
from .transport.directnfs import DatastoreMount
from .vsphere.connection import VSphereConfig

DEFAULT_PATHS = (
    Path("/etc/openbackup/config.json"),
    Path("openbackup.json"),
)


class ConfigError(Exception):
    pass


@dataclass
class Config:
    vcenter: VSphereConfig
    repository: Destination
    index_dir: Path = Path("/var/lib/openbackup/index")
    #: Datastores this host can mount itself, read-only, to read VMDKs
    #: directly. Far faster than the vSphere datastore endpoint. The address
    #: is configured rather than inferred: ESXi often reaches its storage over
    #: a network the backup host cannot route to.
    direct_nfs: list = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Config":
        candidates = [Path(path)] if path else list(DEFAULT_PATHS)
        for candidate in candidates:
            if candidate.exists():
                return cls.from_file(candidate)
        raise ConfigError(
            "no configuration found (looked in "
            f"{', '.join(str(c) for c in candidates)}). "
            "Run `openbackup init-config` to write one."
        )

    @classmethod
    def from_file(cls, path: Path) -> "Config":
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from exc

        vc = data.get("vcenter")
        if not vc:
            raise ConfigError(f"{path} has no 'vcenter' section")

        # Credentials can live in a separate mode-600 file so the main config
        # stays safe to read, copy or check in.
        env_file = vc.get("env_file")
        if env_file:
            vcenter = VSphereConfig.from_env_file(
                Path(env_file).expanduser())
            if vc.get("read_only"):
                vcenter.read_only = True
        else:
            missing = [k for k in ("host", "user", "password") if not vc.get(k)]
            if missing:
                raise ConfigError(
                    f"{path}: vcenter is missing {', '.join(missing)}")
            vcenter = VSphereConfig(
                host=vc["host"], user=vc["user"], password=vc["password"],
                port=int(vc.get("port", 443)),
                verify_ssl=not vc.get("insecure", False),
                read_only=bool(vc.get("read_only", False)),
            )

        repo = data.get("repository")
        if not repo:
            raise ConfigError(f"{path} has no 'repository' section")
        try:
            destination = Destination.from_dict(repo)
        except ValueError as exc:
            raise ConfigError(f"{path}: repository: {exc}") from exc

        mounts = []
        for entry in data.get("direct_nfs", []):
            try:
                mounts.append(DatastoreMount(**entry))
            except TypeError as exc:
                raise ConfigError(f"{path}: direct_nfs entry {entry}: {exc}") from exc

        return cls(
            vcenter=vcenter,
            repository=destination,
            index_dir=Path(data.get("index_dir", "/var/lib/openbackup/index")),
            direct_nfs=mounts,
        )


EXAMPLE_CONFIG = {
    "vcenter": {
        "env_file": "/opt/openbackup/lab.env",
        "_comment": "or set host/user/password/insecure here directly",
    },
    "repository": {
        "kind": "nfs",
        "server": "10.0.0.5",
        "export": "/volume1/backup",
        "mountpoint": "/backup",
        "path": "openbackup",
        "_comment": "or {'kind': 'local', 'path': '/backup/openbackup'}",
    },
    "direct_nfs": [
        {
            "datastore": "DS-NAS01",
            "server": "10.0.0.5",
            "export": "/volume1/homelab",
            "nfs_version": "3",
            "_comment": "address as THIS host reaches it, not as ESXi does",
        }
    ],
    "index_dir": "/var/lib/openbackup/index",
}
