"""Back up an OpenShift namespace into a repository, and restore it.

A namespace point is an ordinary restore point: its API objects are one
"disk" (``__resources__``, JSON) and each persistent volume is another, so
dedup, encryption, retention, GC and verification all apply unchanged.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..engine.context import TaskContext, check_cancel
from ..repo.blockmap import BlockMap
from ..repo.repository import Repository, new_point_id
from .client import KubeClient, KubeError
from .resources import capture_namespace, plan_restore, summarize

RESOURCES_KEY = "__resources__"


def subject_id(cluster: str, namespace: str) -> str:
    return f"k8s:{cluster}/{namespace}"


def store_blob(writer, data: bytes) -> BlockMap:
    m = BlockMap(len(data))
    for i in range(m.block_count):
        m.ids[i] = writer.put(data[i * m.block_size:(i + 1) * m.block_size])
    return m


def read_blob(repo: Repository, point_id: str, key: str) -> bytes:
    m = repo.load_map(point_id, key)
    return b"".join(repo.read_chunk(c, m.block_length(i)) for i, c in enumerate(m.ids))


@dataclass
class NamespaceBackupOptions:
    cluster_id: int
    cluster_name: str
    api_url: str
    job_id: int | None = None
    job_name: str = ""
    task_id: int | None = None
    include_secrets: bool = False


def backup_namespace(c: KubeClient, repo: Repository, namespace: str,
                     opts: NamespaceBackupOptions, ctx: TaskContext,
                     volumes: Any = None) -> dict:
    """``volumes``, when given, backs up persistent volume data (see
    kube/volumes.py) and returns (maps, disk records, warnings)."""
    t0 = time.monotonic()
    now = datetime.now(UTC)
    name = f"{opts.cluster_name}/{namespace}"
    ctx.log(f"{name}: reading API objects")
    if opts.include_secrets and not repo.codec.encrypted:
        # Enforced here as well as when the job is saved.
        raise ValueError("Secrets are only backed up into encrypted repositories")
    captured = capture_namespace(c, namespace, include_secrets=opts.include_secrets)
    counts = summarize(captured)
    ctx.log(f"{name}: {sum(counts.values())} objects "
            f"({', '.join(f'{v} {k}' for k, v in counts.items()) or 'none'})")
    for s in captured["skipped_types"]:
        ctx.log(f"{name}: not readable, skipped: {s}. Update the cluster's permissions "
                "(OpenShift page) to include it.", "warning")
    if captured.get("platform_types"):
        ctx.log(f"{name}: {len(captured['platform_types'])} OpenShift platform kinds are not "
                "readable by the backup account and are not part of application backups: "
                f"{', '.join(sorted(captured['platform_types']))}")
    check_cancel(ctx)

    warnings: list[str] = []
    maps: dict[str, BlockMap] = {}
    disks: list[dict] = []
    writer = repo.writer()
    try:
        maps[RESOURCES_KEY] = store_blob(writer, json.dumps(captured).encode())
        if volumes is not None:
            vmaps, vdisks, vwarn = volumes(captured, writer)
            maps.update(vmaps)
            disks += vdisks
            warnings += vwarn
        writer.close()
    except BaseException:
        writer.abort()
        raise
    pvcs = [{"name": o["metadata"]["name"],
             "storage_class": (o.get("spec") or {}).get("storageClassName") or "",
             "size": ((o.get("spec") or {}).get("resources") or {}).get("requests", {}).get(
                 "storage", ""),
             "volume_mode": (o.get("spec") or {}).get("volumeMode") or "Filesystem",
             "data": any(d.get("pvc") == o["metadata"]["name"] for d in disks)}
            for o in captured["objects"] if o["kind"] == "PersistentVolumeClaim"]
    any_full = any(d.get("mode") != "incremental" for d in disks) or not disks
    point_id = new_point_id()
    manifest = {
        "created_at": now.isoformat(),
        "kind": "full" if any_full else "incremental",
        "subject_kind": "namespace",
        "vm": {"uuid": subject_id(opts.cluster_name, namespace), "name": name, "moref": ""},
        "cluster": {"id": opts.cluster_id, "name": opts.cluster_name, "api_url": opts.api_url},
        "namespace": namespace,
        "resources": counts,
        "secrets_included": opts.include_secrets,
        "skipped_types": captured["skipped_types"],
        "pvcs": pvcs,
        "disks": disks,
        "job_id": opts.job_id,
        "job_name": opts.job_name,
        "task_id": opts.task_id,
        "logical_bytes": sum(d["capacity"] for d in disks),
        "read_bytes": sum(d.get("read_bytes", 0) for d in disks),
        "new_bytes": writer.new_bytes,
        "duration_s": round(time.monotonic() - t0, 1),
        "warnings": warnings,
        "last_full_at": now.isoformat(),
    }
    repo.save_point(point_id, manifest, maps)
    manifest["id"] = point_id
    ctx.log(f"{name}: restore point {point_id} created")
    return manifest


@dataclass
class NamespaceRestoreOptions:
    target_namespace: str
    storage_class_map: dict[str, str] = field(default_factory=dict)
    route_host_map: dict[str, str] = field(default_factory=dict)
    keep_uid_range: bool = True
    merge: bool = False  # restore into an existing namespace
    include_data: bool = True
    restore_secrets: bool = True


@dataclass
class NamespaceRestoreResult:
    applied: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def restore_namespace(c: KubeClient, repo: Repository, point_id: str,
                      opts: NamespaceRestoreOptions, ctx: TaskContext,
                      volume_restore: Any = None) -> NamespaceRestoreResult:
    """``volume_restore(pvc_objects)``, when given, fills the restored PVCs
    with their backed-up data before any workload starts."""
    captured = json.loads(read_blob(repo, point_id, RESOURCES_KEY))
    ns, objects = plan_restore(captured, opts.target_namespace,
                               storage_class_map=opts.storage_class_map,
                               route_host_map=opts.route_host_map,
                               keep_uid_range=opts.keep_uid_range,
                               restore_secrets=opts.restore_secrets)
    res = NamespaceRestoreResult()
    target = opts.target_namespace

    try:
        c.get(f"/api/v1/namespaces/{target}")
        exists = True
    except KubeError as e:
        if e.status != 404:
            raise
        exists = False
    if exists and not opts.merge:
        raise KubeError(f"Namespace {target} already exists on the target cluster. Choose "
                        "another name, or restore into it explicitly (merge).")
    if not exists:
        ctx.log(f"Creating namespace {target}")
        c.create("/api/v1/namespaces", ns)

    ctx.progress(0.0, total=len(objects))
    pvcs_done = False
    for i, o in enumerate(objects):
        check_cancel(ctx)
        # Volumes are filled once every PVC exists and before anything that
        # could mount them starts.
        if not pvcs_done and o["kind"] != "PersistentVolumeClaim" and any(
                x["kind"] == "PersistentVolumeClaim" for x in objects[:i]):
            pvcs_done = True
            _fill_volumes(objects, volume_restore, opts, ctx, res)
        label = f"{o['kind']}/{o['metadata']['name']}"
        r = c.resource(o["apiVersion"], o["kind"])
        if r is None:
            res.skipped.append(f"{label}: {o['apiVersion']} is not served by the target cluster")
            ctx.log(f"Skipped {label}: {o['apiVersion']} not available on the target cluster",
                    "warning")
            continue
        try:
            c.apply(r.path(target, o["metadata"]["name"]), o)
            res.applied += 1
        except KubeError as e:
            res.errors.append(f"{label}: {e}")
            ctx.log(f"Could not restore {label}: {e}", "error")
        ctx.progress((i + 1) / len(objects))
    if not pvcs_done:
        _fill_volumes(objects, volume_restore, opts, ctx, res)
    ctx.log(f"Applied {res.applied} of {len(objects)} objects into {target}")
    return res


def _fill_volumes(objects, volume_restore, opts, ctx, res) -> None:
    pvcs = [o for o in objects if o["kind"] == "PersistentVolumeClaim"]
    if not pvcs or not opts.include_data:
        return
    if volume_restore is None:
        ctx.log("Volume data is not restored by this version; the volume claims are empty",
                "warning")
        return
    for err in volume_restore(pvcs):
        res.errors.append(err)
