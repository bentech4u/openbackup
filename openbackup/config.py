"""Runtime settings.

Read from environment variables prefixed ``OPENBACKUP_``, optionally loaded
from ``/etc/openbackup/openbackup.env`` (or the file named by
``OPENBACKUP_ENV_FILE``). The systemd units point at that file.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPENBACKUP_",
        env_file=os.environ.get("OPENBACKUP_ENV_FILE", "/etc/openbackup/openbackup.env"),
        extra="ignore",
    )

    # State that must live on local disk. SQLite over NFS corrupts without a
    # working lock daemon, so the database is never placed on a repository.
    data_dir: Path = Path("/var/lib/openbackup")
    database_url: str | None = None
    secret_key_file: Path = Path("/etc/openbackup/secret.key")

    # Where NFS repositories are mounted.
    mount_root: Path = Path("/mnt/openbackup")

    # Disk access.
    nbdkit: str = "nbdkit"
    vddk_libdir: Path = Path("/opt/openvddk")

    # Web.
    cookie_secure: bool = True
    session_idle_minutes: int = 8 * 60
    session_absolute_hours: int = 7 * 24
    login_max_failures: int = 5
    login_lockout_minutes: int = 15

    # How restore "mover" pods inside OpenShift reach this server, and the
    # certificate they pin for it. Default: https://<this host's FQDN>:8443.
    public_url: str | None = None
    tls_cert_file: Path = Path("/etc/openbackup/tls.crt")
    # Image for mover pods; default is the cluster's own openshift/tools.
    mover_image: str | None = None

    # Worker.
    worker_poll_seconds: float = 2.0
    worker_concurrency: int = 2

    @property
    def mover_url(self) -> str:
        import socket

        return (self.public_url or f"https://{socket.getfqdn()}:8443").rstrip("/")

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{self.data_dir / 'openbackup.db'}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
