"""Collect OpenShift etcd backups into a repository.

OpenShift's `cluster-backup.sh` writes sets like::

    20261001-0300/snapshot_2026-10-01_030001.db
    20261001-0300/static_kuberesources_2026-10-01_030001.tar.gz

Many sites already schedule it and copy the sets to a NAS. A collection job
mounts that location read-only and ingests every set it has not stored yet,
so they gain retention, encryption, dedup and copies, without OpenBackup
needing any access to the cluster. Restoring etcd is deliberately manual
(Red Hat's cluster-restore.sh); OpenBackup hands the files back.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..engine.context import TaskContext, check_cancel
from ..repo.blockmap import BLOCK_SIZE, BlockMap
from ..repo.repository import Repository, new_point_id

SET_RE = re.compile(r"^(\d{8})-(\d{4})$")
SNAPSHOT_RE = re.compile(r"^snapshot_.*\.db$")
RESOURCES_RE = re.compile(r"^static_kuberesources_.*\.tar\.gz$")


class EtcdError(Exception):
    pass


@dataclass
class EtcdSet:
    name: str
    path: Path
    files: list[Path]
    taken_at: datetime


def subject_id(label: str) -> str:
    return f"etcd:{label}"


def find_sets(root: Path) -> list[EtcdSet]:
    """Complete backup sets below ``root``: a snapshot and its static resources."""
    if not root.is_dir():
        raise EtcdError(f"{root} is not a folder on the mounted share")
    out = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        files = sorted(p for p in d.iterdir() if p.is_file())
        if not any(SNAPSHOT_RE.match(p.name) for p in files) or \
                not any(RESOURCES_RE.match(p.name) for p in files):
            continue  # incomplete, or still being written
        m = SET_RE.match(d.name)
        if m:
            taken = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M").astimezone(UTC)
        else:
            taken = datetime.fromtimestamp(max(p.stat().st_mtime for p in files), UTC)
        out.append(EtcdSet(d.name, d, files, taken))
    return out


def file_key(name: str) -> str:
    return "file-" + re.sub(r"[^A-Za-z0-9._-]", "_", name)


def ingest(repo: Repository, s: EtcdSet, label: str, job_id: int | None, job_name: str,
           ctx: TaskContext) -> dict:
    maps: dict[str, BlockMap] = {}
    records = []
    writer = repo.writer()
    try:
        for f in s.files:
            check_cancel(ctx)
            size = f.stat().st_size
            m = BlockMap(size)
            h = hashlib.sha256()
            with open(f, "rb") as fh:
                for i in range(m.block_count):
                    data = fh.read(BLOCK_SIZE)
                    if len(data) != m.block_length(i):
                        raise EtcdError(f"{f.name} changed while it was being read")
                    h.update(data)
                    m.ids[i] = writer.put(data)
            maps[file_key(f.name)] = m
            records.append({"key": file_key(f.name), "label": f.name, "name": f.name,
                            "capacity": size, "sha256": h.hexdigest(), "mode": "full",
                            "read_bytes": size})
            ctx.progress(read=size)
        writer.close()
    except BaseException:
        writer.abort()
        raise
    pid = new_point_id()
    manifest = {
        "created_at": s.taken_at.isoformat(),
        "collected_at": datetime.now(UTC).isoformat(),
        "kind": "full",
        "subject_kind": "etcd",
        "vm": {"uuid": subject_id(label), "name": f"{label} etcd", "moref": ""},
        "source_set": s.name,
        "files": records,
        "disks": records,
        "job_id": job_id,
        "job_name": job_name,
        "logical_bytes": sum(r["capacity"] for r in records),
        "read_bytes": sum(r["capacity"] for r in records),
        "new_bytes": writer.new_bytes,
        "warnings": [],
    }
    repo.save_point(pid, manifest, maps)
    manifest["id"] = pid
    return manifest


def collected_sets(repo: Repository, label: str) -> set[str]:
    sid = subject_id(label)
    return {m.get("source_set") for m in repo.list_points()
            if m.get("vm", {}).get("uuid") == sid}
