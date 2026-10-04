"""Task executors: one function per task kind. Each returns (state, summary)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, select

from .. import services
from ..db import session_scope
from ..db.models import (
    Job,
    JobKind,
    KubeCluster,
    RestorePoint,
    Task,
    TaskKind,
    TaskState,
    VCenter,
)
from ..db.models import Repository as RepoRow
from ..engine.backup import BackupOptions, backup_vm
from ..engine.context import Cancelled
from ..engine.restore import NewVmTarget, export_disks, restore_in_place, restore_new_vm
from ..repo.maintenance import collect_garbage, select_expired, verify_point
from ..repo.repository import Repository
from .context import DbTaskContext, ScopedContext

# Tests replace these to run against fakes.
source_factory = services.vsphere_source


def guest_factory(source, vm_moref: str, username: str, password: str):
    from ..vsphere.guestops import GuestFiles

    return GuestFiles(source, vm_moref, username, password)


def flr_session_factory(repo_row, point_id: str, repo):
    from ..flr.session import FlrSession

    return FlrSession(repo_row, point_id, repo=repo).open()


SECRET_PARAMS = ("guest_password_enc", "restore_token_enc")


def scrub_secrets(task: Task) -> None:
    """Drop one-time credentials from a task once it can no longer run."""
    if any(k in (task.params or {}) for k in SECRET_PARAMS):
        task.params = {k: v for k, v in task.params.items() if k not in SECRET_PARAMS}


def _load(task_id: int) -> tuple[Task, Job | None, RepoRow | None, VCenter | None]:
    with session_scope() as db:
        t = db.get(Task, task_id)
        job = db.get(Job, t.job_id) if t.job_id else None
        repo = db.get(RepoRow, t.repository_id) if t.repository_id else None
        vc_id = job.vcenter_id if job else t.params.get("vcenter_id")
        vc = db.get(VCenter, vc_id) if vc_id else None
        db.expunge_all()
    return t, job, repo, vc


def record_point(repository_id: int, manifest: dict) -> None:
    with session_scope() as db:
        db.merge(RestorePoint(
            id=manifest["id"], repository_id=repository_id, job_id=manifest.get("job_id"),
            vm_uuid=manifest["vm"]["uuid"], vm_name=manifest["vm"]["name"],
            vm_moref=manifest["vm"].get("moref", ""),
            created_at=datetime.fromisoformat(manifest["created_at"]),
            kind=manifest["kind"], subject_kind=manifest.get("subject_kind", "vm"),
            logical_bytes=manifest.get("logical_bytes", 0),
            read_bytes=manifest.get("read_bytes", 0), new_bytes=manifest.get("new_bytes", 0),
            disks=[{"key": d["key"], "label": d["label"], "capacity": d["capacity"]}
                   for d in manifest.get("disks", [])],
        ))


def sync_points(repository_id: int, repo: Repository) -> int:
    """Make the database's list of points match the repository's."""
    manifests = {m["id"]: m for m in repo.list_points()}
    with session_scope() as db:
        known = set(db.scalars(select(RestorePoint.id)
                               .where(RestorePoint.repository_id == repository_id)))
        stale = known - manifests.keys()
        if stale:
            db.execute(delete(RestorePoint).where(RestorePoint.id.in_(stale)))
        jobs = set(db.scalars(select(Job.id)))
    for pid in manifests.keys() - known:
        m = manifests[pid]
        if m.get("job_id") not in jobs:
            m = {**m, "job_id": None}
        record_point(repository_id, m)
    return len(manifests)


# ---------------------------------------------------------------------- backup


def fcd_factory(source):
    from ..vsphere.fcd import FcdManager

    return FcdManager(source)


def kube_client_factory(api_url: str, token: str, ca_pem: str):
    from ..kube.client import KubeClient

    return KubeClient(api_url, token, ca_pem)


def run_backup(task_id: int, ctx: DbTaskContext) -> tuple[TaskState, str]:
    task, job, repo_row, vc = _load(task_id)
    if job is not None and job.kind == JobKind.openshift:
        return _run_openshift_backup(task_id, task, job, repo_row, vc, ctx)
    if job is not None and job.kind == JobKind.etcd:
        return _run_etcd_collection(job, repo_row, ctx)
    if job is None or repo_row is None or vc is None:
        return TaskState.failed, "Job, repository or vCenter no longer exists"
    wanted = task.params.get("vms") or [v["moref"] for v in job.vms]
    vms = [v for v in job.vms if v["moref"] in wanted]
    if not vms:
        return TaskState.failed, "Job has no VMs"
    ctx.log(f"Backing up {len(vms)} VM(s) to repository {repo_row.name}")
    for v in vms:
        ctx.item(v["name"], kind="vm", state="pending")

    ok, failed, warned = [], [], []
    source = source_factory(vc)
    source.connect()
    try:
        with services.open_repository(repo_row) as repo:
            base_total = 0
            for i, v in enumerate(vms):
                if ctx.cancelled():
                    raise Cancelled()
                ctx.item(v["name"], state="running")
                scoped = ScopedContext(ctx, i, len(vms), base_total)
                opts = BackupOptions(
                    quiesce=job.quiesce, active_full=bool(task.params.get("active_full")),
                    active_full_days=job.active_full_days, job_id=job.id, job_name=job.name,
                    task_id=task_id, vcenter=vc.host)
                try:
                    res = backup_vm(source, repo, v["moref"], opts, scoped)
                except Cancelled:
                    ctx.item(v["name"], state="cancelled")
                    raise
                except Exception as e:  # one VM failing must not stop the others
                    ctx.log(f"{v['name']}: backup failed: {e}", "error")
                    ctx.item(v["name"], state="failed", error=str(e))
                    failed.append(v["name"])
                    continue
                finally:
                    base_total += scoped.total
                record_point(repo_row.id, res.manifest)
                ctx.item(v["name"], state="warning" if res.warnings else "success",
                         point_id=res.point_id, kind=res.manifest["kind"],
                         read=res.manifest["read_bytes"], new=res.manifest["new_bytes"])
                (warned if res.warnings else ok).append(v["name"])
                _apply_retention(ctx, repo, repo_row.id, job, res.manifest["vm"]["uuid"])
            _gc_if_needed(ctx, repo)
    finally:
        source.close()

    with session_scope() as db:
        j = db.get(Job, job.id)
        if j:
            j.last_run_at = datetime.now(UTC)
    summary = f"{len(ok) + len(warned)} of {len(vms)} VMs backed up"
    if failed:
        summary += f"; failed: {', '.join(failed)}"
    if failed and not (ok or warned):
        return TaskState.failed, summary
    if failed or warned:
        return TaskState.warning, summary
    return TaskState.success, summary


_deleted_since_gc: dict[str, int] = {}


def _apply_retention(ctx, repo: Repository, repository_id: int, job: Job, vm_uuid: str) -> None:
    points = [m for m in repo.list_points()
              if m.get("job_id") == job.id and m["vm"]["uuid"] == vm_uuid]
    expired = select_expired(points, job.retention_points, job.retention_days)
    for pid in expired:
        repo.delete_point(pid)
        ctx.log(f"Retention: removed restore point {pid}")
    if expired:
        with session_scope() as db:
            db.execute(delete(RestorePoint).where(RestorePoint.id.in_(expired)))
        _deleted_since_gc[repo.id] = _deleted_since_gc.get(repo.id, 0) + len(expired)


def _gc_if_needed(ctx, repo: Repository) -> None:
    if _deleted_since_gc.pop(repo.id, 0):
        res = collect_garbage(repo, log=ctx.log)
        ctx.log(f"Freed {res.bytes_freed / 2**30:.2f} GiB")


def _run_openshift_backup(task_id, task, job, repo_row, vc, ctx) -> tuple[TaskState, str]:
    from ..auth.secrets import decrypt
    from ..engine.backup import latest_point_for_vm
    from ..kube.engine import NamespaceBackupOptions, backup_namespace, subject_id
    from ..kube.volumes import volume_backup

    with session_scope() as db:
        cluster = db.get(KubeCluster, job.cluster_id) if job.cluster_id else None
        if cluster is not None:
            db.expunge(cluster)
    if cluster is None or repo_row is None:
        return TaskState.failed, "Cluster or repository no longer exists"
    wanted = task.params.get("namespaces") or job.selection.get("namespaces", [])
    if not wanted:
        return TaskState.failed, "Job has no namespaces"
    ctx.log(f"Backing up {len(wanted)} namespace(s) of {cluster.name} to repository "
            f"{repo_row.name}")
    for ns in wanted:
        ctx.item(ns, kind="namespace", state="pending")
    ok, failed, warned = [], [], []
    kube = kube_client_factory(cluster.api_url, decrypt(cluster.backup_token_enc),
                               cluster.ca_pem)
    source = fcd = None
    if vc is not None:
        source = source_factory(vc)
        source.connect()
        fcd = fcd_factory(source)
    else:
        ctx.log(f"{cluster.name} has no vCenter configured: volume data is not backed up, "
                "only volume claims", "warning")
    try:
        with services.open_repository(repo_row) as repo:
            for i, ns in enumerate(wanted):
                if ctx.cancelled():
                    raise Cancelled()
                ctx.item(ns, state="running")
                scoped = ScopedContext(ctx, i, len(wanted), 0)
                opts = NamespaceBackupOptions(cluster.id, cluster.name, cluster.api_url,
                                              job.id, job.name, task_id)
                prev = latest_point_for_vm(repo, subject_id(cluster.name, ns))
                freeze = job.selection.get("freeze_vms", True)
                volumes = volume_backup(
                    fcd, source._flat_disk if source is not None else None, prev,
                    lambda k, p=prev: repo.load_map(p["id"], k), scoped, f"{cluster.name}/{ns}",
                    active_full=bool(task.params.get("active_full")),
                    freeze=(lambda vm, n=ns: kube.freeze_vm(n, vm)) if freeze else None,
                    unfreeze=lambda vm, n=ns: kube.unfreeze_vm(n, vm), task_id=task_id)
                try:
                    manifest = backup_namespace(kube, repo, ns, opts, scoped, volumes=volumes)
                except Cancelled:
                    ctx.item(ns, state="cancelled")
                    raise
                except Exception as e:  # one namespace failing must not stop the others
                    ctx.log(f"{cluster.name}/{ns}: backup failed: {e}", "error")
                    ctx.item(ns, state="failed", error=str(e))
                    failed.append(ns)
                    continue
                record_point(repo_row.id, manifest)
                w = manifest.get("warnings")
                ctx.item(ns, state="warning" if w else "success", point_id=manifest["id"],
                         objects=sum(manifest["resources"].values()),
                         read=manifest["read_bytes"], new=manifest["new_bytes"])
                (warned if w else ok).append(ns)
                _apply_retention(ctx, repo, repo_row.id, job, subject_id(cluster.name, ns))
            _gc_if_needed(ctx, repo)
    finally:
        kube.close()
        if source is not None:
            source.close()
    with session_scope() as db:
        j = db.get(Job, job.id)
        if j:
            j.last_run_at = datetime.now(UTC)
    summary = f"{len(ok) + len(warned)} of {len(wanted)} namespaces backed up"
    if failed:
        summary += f"; failed: {', '.join(failed)}"
    if failed and not (ok or warned):
        return TaskState.failed, summary
    if failed or warned:
        return TaskState.warning, summary
    return TaskState.success, summary


def _run_etcd_collection(job, repo_row, ctx) -> tuple[TaskState, str]:
    from ..config import get_settings
    from ..kube import etcd
    from ..repo import nfs

    if repo_row is None:
        return TaskState.failed, "Repository no longer exists"
    src = job.selection["source"]
    label = src.get("label") or ""
    if not label and job.cluster_id:
        with session_scope() as db:
            c = db.get(KubeCluster, job.cluster_id)
            label = c.name if c else ""
    label = label or job.name
    mp = get_settings().mount_root / f"etcd-job-{job.id}"
    nfs.ensure_mounted(src["server"], src["export"], mp, src.get("options") or "nfsvers=3,hard",
                       read_only=True)
    root = mp / src["path"].strip("/") if src.get("path", "").strip("/") else mp
    sets = etcd.find_sets(root)
    if not sets:
        return TaskState.failed, f"No complete etcd backup sets in {src['server']}:" \
                                 f"{src['export']}/{src.get('path', '')}"
    with services.open_repository(repo_row) as repo:
        have = etcd.collected_sets(repo, label)
        new = [s for s in sets if s.name not in have]
        ctx.log(f"{len(sets)} etcd backup set(s) on the share, {len(new)} not yet collected")
        ctx.progress(0.0, total=sum(f.stat().st_size for s in new for f in s.files) or 1)
        for s in new:
            if ctx.cancelled():
                raise Cancelled()
            ctx.item(s.name, kind="etcd", state="running")
            manifest = etcd.ingest(repo, s, label, job.id, job.name, ctx)
            record_point(repo_row.id, manifest)
            ctx.item(s.name, state="success", point_id=manifest["id"],
                     new=manifest["new_bytes"])
            ctx.log(f"Collected {s.name} ({len(s.files)} files)")
        _apply_retention(ctx, repo, repo_row.id, job, etcd.subject_id(label))
        _gc_if_needed(ctx, repo)
    with session_scope() as db:
        j = db.get(Job, job.id)
        if j:
            j.last_run_at = datetime.now(UTC)
    newest = max(s.taken_at for s in sets)
    age_h = (datetime.now(UTC) - newest).total_seconds() / 3600
    summary = f"{len(new)} new etcd backup set(s) collected; newest is {sets[-1].name}"
    if age_h > 26:
        ctx.log(f"The newest etcd backup on the share is {age_h:.0f} hours old: is the "
                "cluster's backup schedule still running?", "warning")
        return TaskState.warning, summary
    return TaskState.success, summary


# --------------------------------------------------------------------- restore


def run_restore(task_id: int, ctx: DbTaskContext) -> tuple[TaskState, str]:
    task, _job, repo_row, vc = _load(task_id)
    p = task.params
    if repo_row is None:
        return TaskState.failed, "Repository no longer exists"
    with services.open_repository(repo_row) as repo:
        if p["mode"] == "namespace":
            return _restore_namespace(p, repo, ctx)
        if p["mode"] == "files":
            return _restore_files(task_id, p, repo_row, repo, vc, ctx)
        if p["mode"] == "export":
            dest = Path(p["path"])
            files = export_disks(repo, p["point_id"], dest, p.get("format", "raw"), ctx)
            return TaskState.success, f"Exported {len(files)} disk(s) to {dest}"
        if vc is None:
            return TaskState.failed, "vCenter no longer exists"
        source = source_factory(vc)
        source.connect()
        try:
            if p["mode"] == "new_vm":
                t = p["target"]
                moref = restore_new_vm(source, repo, p["point_id"], NewVmTarget(
                    name=t["name"], folder=t["folder"], resource_pool=t["resource_pool"],
                    datastore=t["datastore"], host=t.get("host"),
                    network_map=t.get("network_map", {}), power_on=t.get("power_on", False)),
                    ctx)
                return TaskState.success, f"Restored as new VM {t['name']} ({moref})"
            if p["mode"] == "in_place":
                restore_in_place(source, repo, p["point_id"], ctx,
                                 power_on=p.get("power_on", False))
                return TaskState.success, "Restored over the original VM"
        finally:
            source.close()
    return TaskState.failed, f"Unknown restore mode {p['mode']}"


class VolumeRestore:
    """Fills each restored volume claim with its backed-up data, through a
    mover pod in the target namespace (see kube/mover.py)."""

    def __init__(self, kube, repo, point_id: str, namespace: str, ctx):
        self.kube, self.repo, self.point_id, self.ns, self.ctx = (kube, repo, point_id,
                                                                  namespace, ctx)
        self.manifest = repo.load_manifest(point_id)
        self.session = None

    def _image(self) -> str:
        from ..config import get_settings
        from ..kube.mover import resolve_image

        return resolve_image(self.kube, get_settings().mover_image)

    def _producer(self, pvc: str, block: bool, key: str):
        from ..kube.mover import write_raw

        if block:
            m = self.repo.load_map(self.point_id, key)
            chunks = (self.repo.read_chunk(c, m.block_length(i)) for i, c in enumerate(m.ids))
            return lambda fifo: write_raw(fifo, chunks)
        if self.session is None:
            self.ctx.log("Opening the backed-up volumes (starts a small helper VM)")
            self.session = flr_session_factory(None, self.point_id, self.repo)
        vol = next((v for v in self.session.volumes
                    if v.get("disk") == pvc and v["mounted"]), None)
        if vol is None:
            raise RuntimeError(f"no readable filesystem found on the backup of {pvc}")
        return lambda fifo: self.session.tar_out(f"/{vol['id']}", fifo)

    def __call__(self, pvcs: list[dict]) -> list[str]:
        from ..config import get_settings
        from ..kube.mover import MoverError, run_mover

        settings = get_settings()
        disks = {d["pvc"]: d for d in self.manifest.get("disks", []) if d.get("pvc")}
        wanted = [o for o in pvcs if o["metadata"]["name"] in disks]
        if not wanted:
            return []
        try:
            image = self._image()
            ca_pem = settings.tls_cert_file.read_text()
        except (MoverError, OSError) as e:
            return [f"volume data not restored: {e}"]
        errors = []
        for o in wanted:
            pvc = o["metadata"]["name"]
            block = (o.get("spec") or {}).get("volumeMode") == "Block"
            self.ctx.log(f"Restoring data of volume {pvc} ({'raw block' if block else 'files'})")
            self.ctx.item(pvc, kind="volume", state="running")
            try:
                produce = self._producer(pvc, block, disks[pvc]["key"])
                base_url, address = settings.mover_endpoint()
                if pvc == wanted[0]["metadata"]["name"]:
                    self.ctx.log(f"Mover pods fetch data from {base_url}"
                                 + (f" at {address}" if address else ""))
                run_mover(self.kube, self.ns, pvc, block, image, base_url, ca_pem,
                          settings.data_dir, produce, self.ctx, address=address)
                self.ctx.item(pvc, state="success")
                self.ctx.log(f"Volume {pvc} restored")
            except Exception as e:
                errors.append(f"volume {pvc}: {e}")
                self.ctx.item(pvc, state="failed", error=str(e))
                self.ctx.log(f"Volume {pvc} could not be restored: {e}", "error")
        return errors

    def close(self) -> None:
        if self.session is not None:
            self.session.close()


def _restore_namespace(p, repo, ctx) -> tuple[TaskState, str]:
    from ..auth.secrets import decrypt
    from ..kube.engine import NamespaceRestoreOptions, restore_namespace

    with session_scope() as db:
        cluster = db.get(KubeCluster, p["target_cluster_id"])
        if cluster is not None:
            db.expunge(cluster)
    if cluster is None:
        return TaskState.failed, "Target cluster no longer exists"
    token_enc = p.get("restore_token_enc") or cluster.restore_token_enc
    if not token_enc:
        return TaskState.failed, "No restore credential; start the restore again with a token"
    o = p["options"]
    opts = NamespaceRestoreOptions(
        target_namespace=o["target_namespace"], storage_class_map=o.get("storage_class_map", {}),
        route_host_map=o.get("route_host_map", {}), keep_uid_range=o.get("keep_uid_range", True),
        merge=o.get("merge", False), include_data=o.get("include_data", True))
    kube = kube_client_factory(cluster.api_url, decrypt(token_enc), cluster.ca_pem)
    vr = VolumeRestore(kube, repo, p["point_id"], opts.target_namespace, ctx)
    try:
        ctx.log(f"Restoring into {cluster.name}/{opts.target_namespace}")
        res = restore_namespace(kube, repo, p["point_id"], opts, ctx, volume_restore=vr)
    finally:
        vr.close()
        kube.close()
    summary = f"{res.applied} object(s) restored into {cluster.name}/{opts.target_namespace}"
    if res.skipped:
        summary += f", {len(res.skipped)} skipped (not available on the target cluster)"
    if res.errors:
        summary += f", {len(res.errors)} failed"
        return (TaskState.failed if not res.applied else TaskState.warning), summary
    return (TaskState.warning if res.skipped else TaskState.success), summary


def _restore_files(task_id, p, repo_row, repo, vc, ctx) -> tuple[TaskState, str]:
    from ..auth.secrets import decrypt
    from ..config import get_settings
    from ..engine.filerestore import restore_files

    if vc is None:
        return TaskState.failed, "vCenter no longer exists"
    if not p.get("guest_password_enc"):
        return TaskState.failed, "Guest credentials are no longer available; start again"
    manifest = repo.load_manifest(p["point_id"])
    stamp = datetime.fromisoformat(manifest["created_at"]).strftime("%Y%m%d-%H%M")
    source = source_factory(vc)
    source.connect()
    try:
        guest = guest_factory(source, p["vm_moref"], p["guest_user"],
                              decrypt(p["guest_password_enc"]))
        info = guest.check()
        ctx.log(f"Guest {info.hostname or p['vm_moref']} ({info.os_name}): VMware Tools ready, "
                "credentials accepted")
        ctx.log("Opening the restore point (this starts a small helper VM and can take a "
                "minute)")
        session = flr_session_factory(repo_row, p["point_id"], repo)
        try:
            res = restore_files(session, guest, p["items"], p["conflict"], p.get("target_dir"),
                                stamp, get_settings().data_dir / "flr-staging" / str(task_id),
                                ctx)
        finally:
            session.close()
    finally:
        source.close()
    summary = f"{res.files} file(s) restored ({res.bytes / 2**20:.1f} MiB)"
    if res.renamed:
        summary += f", {len(res.renamed)} restored under a new name"
    if res.skipped:
        summary += f", {res.skipped} skipped"
    if res.errors:
        summary += f", {len(res.errors)} failed"
        return (TaskState.failed if not res.files else TaskState.warning), summary
    return (TaskState.warning if res.skipped else TaskState.success), summary


# --------------------------------------------------------------- maintenance


def run_verify(task_id: int, ctx: DbTaskContext) -> tuple[TaskState, str]:
    task, _job, repo_row, _vc = _load(task_id)
    if repo_row is None:
        return TaskState.failed, "Repository no longer exists"
    with services.open_repository(repo_row) as repo:
        ids = [task.params["point_id"]] if task.params.get("point_id") else repo.point_ids()
        bad = 0
        checked = 0
        for i, pid in enumerate(ids):
            if ctx.cancelled():
                raise Cancelled()
            ctx.log(f"Verifying {pid}")
            res = verify_point(repo, pid, log=ctx.log, cancelled=ctx.cancelled,
                               progress=lambda f, i=i: ctx.progress((i + f) / len(ids)))
            checked += res.bytes_checked
            ctx.progress(read=res.bytes_checked)
            ctx.item(pid, state="success" if res.ok else "failed",
                     errors=res.errors[:20], chunks=res.chunks_checked)
            if not res.ok:
                bad += 1
                ctx.log(f"{pid}: {len(res.errors)} problem(s)", "error")
    if bad:
        return TaskState.failed, f"{bad} of {len(ids)} restore point(s) failed verification"
    return TaskState.success, f"{len(ids)} restore point(s) verified, " \
                              f"{checked / 2**30:.2f} GiB read"


def run_gc(task_id: int, ctx: DbTaskContext) -> tuple[TaskState, str]:
    task, _job, repo_row, _vc = _load(task_id)
    if repo_row is None:
        return TaskState.failed, "Repository no longer exists"
    with services.open_repository(repo_row) as repo:
        for pid in task.params.get("delete_points", []):
            repo.delete_point(pid)
            ctx.log(f"Deleted restore point {pid}")
        n = sync_points(repo_row.id, repo)
        ctx.log(f"Repository holds {n} restore point(s)")
        if task.params.get("rescan_only"):
            return TaskState.success, f"Found {n} restore point(s)"
        res = collect_garbage(repo, log=ctx.log)
    return TaskState.success, (f"{res.packs_deleted} packs deleted, {res.packs_repacked} "
                               f"repacked, {res.bytes_freed / 2**30:.2f} GiB freed")


EXECUTORS = {
    TaskKind.backup: run_backup,
    TaskKind.restore: run_restore,
    TaskKind.verify: run_verify,
    TaskKind.gc: run_gc,
}
