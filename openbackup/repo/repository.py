"""Opening a repository, wherever it lives.

This is the seam the rest of the system uses: a job says "open this
destination" and gets a chunk store back, without caring whether the bytes
land on a local disk or an NFS export.

The repository carries its own ``config.json``. Chunk size, pack size and
whether it is encrypted are properties of the data already written, not of the
server reading it, so they travel with the repository. Pointing a fresh
install at an existing share picks up the right settings instead of silently
writing incompatible chunks alongside the old ones.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .backend import DEFAULT_NFS_OPTIONS, BackendError, FilesystemBackend, NfsMount
from .blockmap import DEFAULT_CHUNK_SIZE
from .codec import ChunkCodec
from .index import ChunkIndex
from .packfile import DEFAULT_PACK_SIZE
from .store import PackedChunkStore

CONFIG_PATH = "config.json"
REPO_VERSION = 1

#: Where local index caches live. They are rebuildable, so this is state, not
#: data, and it deliberately does not go on the share.
DEFAULT_INDEX_DIR = Path("/var/lib/openbackup/index")


class RepositoryError(Exception):
    pass


@dataclass
class Destination:
    """Where a repository lives.

    Local::

        Destination(kind="local", path="/backup/openbackup")

    NFS, which openbackup mounts itself::

        Destination(kind="nfs", server="10.0.0.5", export="/vol/backup",
                    path="site-a", mountpoint="/mnt/openbackup/site-a")
    """

    kind: str = "local"
    path: str = ""
    server: str | None = None
    export: str | None = None
    mountpoint: str | None = None
    options: str = DEFAULT_NFS_OPTIONS
    nfs_version: str | None = "4.2"

    def __post_init__(self) -> None:
        if self.kind not in ("local", "nfs"):
            raise ValueError(f"unknown destination kind {self.kind!r}")
        if self.kind == "local" and not self.path:
            raise ValueError("a local destination needs a path")
        if self.kind == "nfs":
            missing = [f for f in ("server", "export") if not getattr(self, f)]
            if missing:
                raise ValueError(
                    f"an NFS destination needs {' and '.join(missing)}")
            if not self.mountpoint:
                # Derive a stable mountpoint so repeat runs reuse the same one.
                safe = f"{self.server}{self.export}".replace("/", "_")
                self.mountpoint = f"/mnt/openbackup/{safe}"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Destination":
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown destination fields: {sorted(unknown)}")
        return cls(**data)

    def describe(self) -> str:
        if self.kind == "nfs":
            return f"nfs://{self.server}{self.export}" + (
                f"/{self.path}" if self.path else "")
        return self.path


@dataclass
class RepositoryConfig:
    """Settings baked into the repository when it is created."""

    version: int = REPO_VERSION
    chunk_size: int = DEFAULT_CHUNK_SIZE
    pack_size: int = DEFAULT_PACK_SIZE
    encrypted: bool = False

    def to_json(self) -> bytes:
        return json.dumps({
            "version": self.version,
            "chunk_size": self.chunk_size,
            "pack_size": self.pack_size,
            "encrypted": self.encrypted,
        }, indent=2).encode()

    @classmethod
    def from_json(cls, blob: bytes) -> "RepositoryConfig":
        data = json.loads(blob)
        version = data.get("version")
        if version != REPO_VERSION:
            raise RepositoryError(
                f"repository is version {version}, this build speaks "
                f"{REPO_VERSION}")
        return cls(
            version=version,
            chunk_size=data["chunk_size"],
            pack_size=data["pack_size"],
            encrypted=data.get("encrypted", False),
        )


class Repository:
    """An opened repository: mount held, store ready, index attached."""

    def __init__(self, destination: Destination, *, key: bytes | None = None,
                 index_dir: Path | str = DEFAULT_INDEX_DIR):
        self.destination = destination
        self._key = key
        self.index_dir = Path(index_dir)
        self._mount: NfsMount | None = None
        self.backend: FilesystemBackend | None = None
        self.index: ChunkIndex | None = None
        self.store: PackedChunkStore | None = None
        self.config: RepositoryConfig | None = None

    # -- attach -------------------------------------------------------------

    def _attach_backend(self) -> FilesystemBackend:
        dest = self.destination
        if dest.kind == "local":
            return FilesystemBackend(dest.path)

        self._mount = NfsMount(
            dest.server, dest.export, dest.mountpoint,
            options=dest.options, version=dest.nfs_version,
        )
        self._mount.mount()
        try:
            self._mount.verify_writable()
        except BackendError:
            self._mount.unmount()
            raise
        return self._mount.backend(dest.path)

    def _index_path(self) -> Path:
        # One index per destination. Keyed by the destination description so
        # two repositories on the same server do not share a cache.
        safe = "".join(c if c.isalnum() else "_" for c in
                       self.destination.describe())
        return self.index_dir / f"{safe}.sqlite"

    def open(self, *, create: bool = False,
             config: RepositoryConfig | None = None) -> "Repository":
        self.backend = self._attach_backend()

        if self.backend.exists(CONFIG_PATH):
            self.config = RepositoryConfig.from_json(
                self.backend.read(CONFIG_PATH))
            if config is not None and create:
                raise RepositoryError(
                    f"{self.destination.describe()} already holds a repository")
        elif create:
            self.backend.init()
            self.config = config or RepositoryConfig(
                encrypted=self._key is not None)
            self.config.encrypted = self._key is not None
            self.backend.write(CONFIG_PATH, self.config.to_json())
        else:
            self.close()
            raise RepositoryError(
                f"no repository at {self.destination.describe()}; "
                "create it first"
            )

        if self.config.encrypted and self._key is None:
            self.close()
            raise RepositoryError(
                "repository is encrypted but no key was supplied")
        if self._key is not None and not self.config.encrypted:
            self.close()
            raise RepositoryError(
                "a key was supplied but this repository is not encrypted")

        self.index = ChunkIndex(self._index_path())
        self.store = PackedChunkStore(
            self.backend, self.index,
            ChunkCodec(key=self._key),
            pack_size=self.config.pack_size,
        )
        return self

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        if self.store is not None:
            self.store.flush()
            self.store = None
        if self.index is not None:
            self.index.close()
            self.index = None
        self.backend = None
        if self._mount is not None:
            self._mount.unmount()
            self._mount = None

    def abort(self) -> None:
        """Close without committing the open pack, after a failed job."""
        self.store = None
        if self.index is not None:
            self.index.close()
            self.index = None
        self.backend = None
        if self._mount is not None:
            self._mount.unmount()
            self._mount = None

    def __enter__(self) -> "Repository":
        return self

    def __exit__(self, exc_type, *rest) -> None:
        if exc_type is None:
            self.close()
        else:
            self.abort()


def open_repository(destination: Destination | dict, *, create: bool = False,
                    key: bytes | None = None,
                    index_dir: Path | str = DEFAULT_INDEX_DIR) -> Repository:
    """Open (or create) a repository at a local path or on an NFS export."""
    if isinstance(destination, dict):
        destination = Destination.from_dict(destination)
    return Repository(destination, key=key, index_dir=index_dir).open(create=create)
