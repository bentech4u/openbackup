"""Configuration: where vCenter is, and where backups go."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .repo.repository import Destination
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
        else:
            missing = [k for k in ("host", "user", "password") if not vc.get(k)]
            if missing:
                raise ConfigError(
                    f"{path}: vcenter is missing {', '.join(missing)}")
            vcenter = VSphereConfig(
                host=vc["host"], user=vc["user"], password=vc["password"],
                port=int(vc.get("port", 443)),
                verify_ssl=not vc.get("insecure", False),
            )

        repo = data.get("repository")
        if not repo:
            raise ConfigError(f"{path} has no 'repository' section")
        try:
            destination = Destination.from_dict(repo)
        except ValueError as exc:
            raise ConfigError(f"{path}: repository: {exc}") from exc

        return cls(
            vcenter=vcenter,
            repository=destination,
            index_dir=Path(data.get("index_dir", "/var/lib/openbackup/index")),
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
    "index_dir": "/var/lib/openbackup/index",
}
