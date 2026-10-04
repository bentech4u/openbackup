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
    # certificate they pin for it. By default they connect to public_address
    # (else this host's default-route address) and verify the certificate
    # under the name it carries, so no DNS record is needed. Setting
    # public_url makes them use that URL and DNS instead.
    public_url: str | None = None
    public_address: str | None = None
    public_port: int = 8443
    tls_cert_file: Path = Path("/etc/openbackup/tls.crt")
    # Image for mover pods; default is the cluster's own openshift/tools.
    mover_image: str | None = None

    # Worker.
    worker_poll_seconds: float = 2.0
    worker_concurrency: int = 2

    def mover_endpoint(self) -> tuple[str, str | None]:
        """(base URL, address to pin the URL's host to, or None to use DNS)."""
        if self.public_url:
            return self.public_url.rstrip("/"), None
        host = _cert_name(self.tls_cert_file) or "openbackup"
        return f"https://{host}:{self.public_port}", self.public_address or _default_address()

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{self.data_dir / 'openbackup.db'}"


def _cert_name(path: Path) -> str | None:
    """The first DNS name the server certificate covers."""
    try:
        from cryptography import x509

        cert = x509.load_pem_x509_certificate(path.read_bytes())
        try:
            names = cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
        except x509.ExtensionNotFound:
            names = []
        if names:
            return names[0]
        cn = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
        return cn[0].value if cn else None
    except (OSError, ValueError):
        return None


def _default_address() -> str | None:
    """This host's address on its default route (what other machines
    usually reach it by)."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # no packet is sent for UDP connect
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


@lru_cache
def get_settings() -> Settings:
    return Settings()
