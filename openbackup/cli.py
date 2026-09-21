"""Command line interface."""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    BarColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn,
)
from rich.table import Table

from .config import EXAMPLE_CONFIG, Config, ConfigError
from .jobs.backup import BackupJob
from .jobs.restore import RestoreToFile, verify_point
from .repo.gc import collect_garbage
from .repo.index import rebuild
from .repo.retention import RetentionPolicy, apply as apply_retention
from .repo.repository import open_repository
from .repo.restorepoint import PointStore
from .transport.directnfs import DirectNfsTransport
from .vsphere.connection import VSphereConnection

def _console() -> Console:
    """Rich falls back to 80 columns when stdout is not a terminal, which
    squeezes wide tables into unreadable ellipses when output is piped or
    captured. Use the real width when we have one, a sane default otherwise."""
    import os
    import shutil
    if sys.stdout.isatty():
        return Console()
    width = int(os.environ.get("COLUMNS") or 0) or shutil.get_terminal_size(
        (140, 24)).columns
    return Console(width=max(width, 140))


console = _console()


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def _short_guest(name: str) -> str:
    """Trim VMware's verbose guest names to something a table can show."""
    return (name.replace("Red Hat Enterprise Linux", "RHEL")
                .replace("Microsoft Windows Server", "Windows Server")
                .replace(" (64-bit)", "").replace(" versions", "")[:22])


def fail(message: str) -> None:
    console.print(f"[bold red]error:[/] {message}")
    sys.exit(1)


class Context:
    def __init__(self, config_path: str | None, verbose: bool):
        self.config_path = config_path
        self.verbose = verbose
        self._config: Config | None = None

    @property
    def config(self) -> Config:
        if self._config is None:
            try:
                self._config = Config.load(self.config_path)
            except ConfigError as exc:
                fail(str(exc))
        return self._config

    def connect(self) -> VSphereConnection:
        return VSphereConnection(self.config.vcenter).connect()

    def open_repo(self, create: bool = False):
        return open_repository(self.config.repository, create=create,
                               index_dir=self.config.index_dir)

    def direct_nfs(self) -> DirectNfsTransport | None:
        mounts = self.config.direct_nfs
        return DirectNfsTransport(mounts) if mounts else None


@click.group()
@click.option("-c", "--config", "config_path", type=click.Path(),
              help="Path to config.json (default: /etc/openbackup/config.json).")
@click.option("-v", "--verbose", is_flag=True, help="Show debug logging.")
@click.version_option(package_name="openbackup", prog_name="openbackup")
@click.pass_context
def main(ctx, config_path, verbose):
    """Image-level backup and restore for VMware vSphere."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s", datefmt="%H:%M:%S",
        handlers=[RichHandler(console=console, show_path=verbose,
                              rich_tracebacks=True)],
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    ctx.obj = Context(config_path, verbose)


# -- configuration ----------------------------------------------------------

@main.command("init-config")
@click.option("-o", "--output", default="/etc/openbackup/config.json",
              type=click.Path(), show_default=True)
def init_config(output):
    """Write a starter configuration file."""
    path = Path(output)
    if path.exists():
        fail(f"{path} already exists; refusing to overwrite it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(EXAMPLE_CONFIG, indent=2) + "\n")
    path.chmod(0o640)
    console.print(f"wrote [bold]{path}[/]; edit it, then run "
                  f"[bold]openbackup inventory[/] to check the connection")


# -- inventory --------------------------------------------------------------

@main.command()
@click.option("--all", "show_all", is_flag=True,
              help="Include VMs that cannot currently be backed up.")
@click.pass_obj
def inventory(ctx: Context, show_all):
    """List virtual machines and whether they can be backed up."""
    from pyVmomi import vim
    from .vsphere.inventory import describe_vm

    with ctx.connect() as conn:
        table = Table(title=f"{conn.about.fullName}", header_style="bold")
        table.add_column("VM", max_width=30, no_wrap=True)
        table.add_column("Power", no_wrap=True)
        table.add_column("Guest", max_width=22, no_wrap=True)
        table.add_column("Disks", justify="right")
        table.add_column("Provisioned", justify="right", no_wrap=True)
        table.add_column("CBT", no_wrap=True)
        table.add_column("Snaps", no_wrap=True)
        table.add_column("Ready", no_wrap=True)
        for vm in sorted(conn.find_all(vim.VirtualMachine), key=lambda v: v.name):
            try:
                info = describe_vm(conn, vm)
            except Exception:
                continue
            blockers = []
            if info.has_snapshots:
                blockers.append("has snapshots")
            if not info.disks:
                blockers.append("no disks")
            if any(d.independent for d in info.disks):
                blockers.append("independent disk")
            ready = "[green]yes[/]" if not blockers else f"[yellow]{blockers[0]}[/]"
            if blockers and not show_all:
                continue
            table.add_row(
                info.name, info.power_state.replace("powered", ""),
                _short_guest(info.guest_full_name), str(len(info.disks)),
                human(sum(d.capacity for d in info.disks)),
                "[green]on[/]" if info.cbt_enabled else "[yellow]off[/]",
                "yes" if info.has_snapshots else "-", ready,
            )
        console.print(table)


# -- backup -----------------------------------------------------------------

@main.command()
@click.argument("vm")
@click.option("--full", "force_full", is_flag=True,
              help="Read every allocated block even if CBT offers an incremental.")
@click.option("--no-quiesce", is_flag=True,
              help="Skip the guest filesystem freeze (crash-consistent).")
@click.option("--no-cbt", is_flag=True,
              help="Do not enable changed block tracking if it is off.")
@click.option("--workers", default=8, show_default=True,
              help="Concurrent reads against the datastore endpoint.")
@click.pass_obj
def backup(ctx: Context, vm, force_full, no_quiesce, no_cbt, workers):
    """Back up a VM to the configured repository."""
    direct = ctx.direct_nfs()
    started = time.monotonic()
    with ctx.connect() as conn:
        repo = ctx.open_repo(create=True)
        try:
            with Progress(
                TextColumn("[bold blue]{task.fields[label]}"), BarColumn(),
                TaskProgressColumn(), TimeRemainingColumn(), console=console,
            ) as bar:
                tasks: dict[str, int] = {}

                def progress(label, done, total):
                    if label not in tasks:
                        tasks[label] = bar.add_task("", total=total, label=label)
                    bar.update(tasks[label], completed=done, total=total)

                job = BackupJob(
                    conn, repo, vm, quiesce=not no_quiesce,
                    enable_cbt=not no_cbt, force_full=force_full,
                    read_workers=workers, direct_nfs=direct, progress=progress,
                )
                result = job.run()
            repo.close()
        except BaseException:
            repo.abort()
            raise
        finally:
            if direct:
                direct.close()

    console.print()
    table = Table(show_header=False, box=None)
    table.add_row("restore point", f"[bold]{result.point_id}[/]")
    table.add_row("type", f"{result.kind} (quiesced: {result.quiesced})")
    table.add_row("duration", f"{result.seconds / 60:.1f} min")
    table.add_row("provisioned", human(result.capacity))
    table.add_row("read from VM", f"{human(result.bytes_read)} "
                                  f"({human(result.bytes_read / max(result.seconds, 1))}/s)")
    table.add_row("stored", human(result.bytes_stored))
    table.add_row("chunks", f"{result.chunks_written} new, "
                            f"{result.chunks_deduped} deduplicated")
    console.print(table)


# -- restore points ---------------------------------------------------------

@main.command()
@click.argument("vm", required=False)
@click.pass_obj
def points(ctx: Context, vm):
    """List restore points in the repository."""
    repo = ctx.open_repo()
    try:
        store = PointStore(repo.backend)
        vm_ids = store.list_vms()
        if not vm_ids:
            console.print("[yellow]no restore points in this repository[/]")
            return
        table = Table(header_style="bold")
        for col in ("VM", "Restore point", "Created", "Type", "Disks",
                    "Read", "Parent"):
            table.add_column(col)
        for vm_uuid in vm_ids:
            for point_id in store.list_points(vm_uuid):
                point = store.load(vm_uuid, point_id)
                if vm and vm.lower() not in point.vm_name.lower():
                    continue
                table.add_row(
                    point.vm_name, point.id, point.created_at, point.kind,
                    str(len(point.disks)), human(point.total_bytes_read),
                    (point.parent_id or "-")[:24],
                )
        console.print(table)
    finally:
        repo.close()


def _find_point(store: PointStore, point_id: str):
    for vm_uuid in store.list_vms():
        if point_id in store.list_points(vm_uuid):
            return store.load(vm_uuid, point_id)
    return None


@main.command()
@click.argument("point_id")
@click.pass_obj
def show(ctx: Context, point_id):
    """Show the details of one restore point."""
    repo = ctx.open_repo()
    try:
        point = _find_point(PointStore(repo.backend), point_id)
        if point is None:
            fail(f"no restore point {point_id!r} in this repository")
        console.print(f"[bold]{point.vm_name}[/]  {point.id}")
        for field in ("created_at", "kind", "parent_id", "datacenter",
                      "guest_full_name", "hardware_version", "power_state",
                      "quiesced"):
            console.print(f"  {field:18s} {getattr(point, field)}")
        table = Table(header_style="bold")
        for col in ("Disk", "Capacity", "Read", "Blocks changed", "changeId"):
            table.add_column(col)
        for disk in point.disks:
            table.add_row(disk.label, human(disk.capacity),
                          human(disk.bytes_read), str(disk.blocks_changed),
                          (disk.change_id or "-")[:40])
        console.print(table)
    finally:
        repo.close()


@main.command()
@click.argument("point_id")
@click.pass_obj
def verify(ctx: Context, point_id):
    """Check every chunk a restore point needs is present and intact."""
    repo = ctx.open_repo()
    try:
        point = _find_point(PointStore(repo.backend), point_id)
        if point is None:
            fail(f"no restore point {point_id!r} in this repository")
        with Progress(TextColumn("[bold blue]{task.fields[label]}"),
                      BarColumn(), TaskProgressColumn(),
                      console=console) as bar:
            tasks: dict[str, int] = {}

            def progress(label, done, total):
                if label not in tasks:
                    tasks[label] = bar.add_task("", total=total, label=label)
                bar.update(tasks[label], completed=done, total=total)

            result = verify_point(repo, point, progress=progress)

        console.print(f"\nchecked {result.chunks_checked} distinct chunks "
                      f"({human(result.bytes_checked)}) across {result.blocks} "
                      f"blocks in {result.seconds:.1f}s")
        if result.ok:
            console.print("[bold green]OK[/] every chunk present and verified")
        else:
            for digest in result.missing[:10]:
                console.print(f"  [red]missing[/] {digest}")
            for detail in result.corrupt[:10]:
                console.print(f"  [red]corrupt[/] {detail}")
            fail(f"{len(result.missing)} missing, {len(result.corrupt)} corrupt")
    finally:
        repo.close()


@main.command()
@click.argument("point_id")
@click.option("-d", "--to-dir", required=True, type=click.Path(),
              help="Directory to write the restored VMDK files into.")
@click.pass_obj
def restore(ctx: Context, point_id, to_dir):
    """Restore a point's disks to flat VMDK files."""
    repo = ctx.open_repo()
    try:
        point = _find_point(PointStore(repo.backend), point_id)
        if point is None:
            fail(f"no restore point {point_id!r} in this repository")
        with Progress(TextColumn("[bold blue]{task.fields[label]}"),
                      BarColumn(), TaskProgressColumn(),
                      TimeRemainingColumn(), console=console) as bar:
            tasks: dict[str, int] = {}

            def progress(label, done, total):
                if label not in tasks:
                    tasks[label] = bar.add_task("", total=total, label=label)
                bar.update(tasks[label], completed=done, total=total)

            result = RestoreToFile(repo, point, to_dir, progress=progress).run()

        console.print()
        for disk in result.disks:
            console.print(f"  {disk.label}: {disk.path}")
            console.print(f"    {human(disk.size)} logical, "
                          f"{human(disk.bytes_written)} written, "
                          f"{disk.blocks_skipped_zero} empty blocks skipped "
                          f"({disk.seconds:.1f}s)")
    finally:
        repo.close()


@main.command()
@click.argument("vm", required=False)
@click.option("--keep-last", type=int, default=None, help="Keep the N newest points.")
@click.option("--keep-daily", type=int, default=None)
@click.option("--keep-weekly", type=int, default=None)
@click.option("--keep-monthly", type=int, default=None)
@click.option("--keep-yearly", type=int, default=None)
@click.option("--dry-run", is_flag=True, help="Show what would be removed.")
@click.pass_obj
def forget(ctx: Context, vm, keep_last, keep_daily, keep_weekly, keep_monthly,
           keep_yearly, dry_run):
    """Expire restore points under a retention policy.

    This removes metadata only. Run `openbackup repo gc` afterwards to
    reclaim the space, once you are satisfied with what was expired.
    """
    policy = RetentionPolicy(keep_last=keep_last, keep_daily=keep_daily,
                             keep_weekly=keep_weekly, keep_monthly=keep_monthly,
                             keep_yearly=keep_yearly)
    if not policy.keeps_anything:
        fail("specify at least one --keep-* option; a policy that keeps "
             "nothing would delete every backup")

    repository = ctx.open_repo()
    try:
        store = PointStore(repository.backend)
        for vm_uuid in store.list_vms():
            points_here = store.list_points(vm_uuid)
            if not points_here:
                continue
            name = store.load(vm_uuid, points_here[0]).vm_name
            if vm and vm.lower() not in name.lower():
                continue
            kept, expired = apply_retention(store, vm_uuid, policy,
                                            dry_run=dry_run)
            verb = "would expire" if dry_run else "expired"
            console.print(f"[bold]{name}[/]: keeping {len(kept)}, "
                          f"{verb} {len(expired)}")
            for point in expired:
                console.print(f"    {point.id}  {point.created_at}")
    finally:
        repository.close()


# -- repository maintenance -------------------------------------------------

@main.group()
def repo():
    """Repository maintenance."""


@repo.command("info")
@click.pass_obj
def repo_info(ctx: Context):
    """Show repository settings and contents."""
    repository = ctx.open_repo()
    try:
        cfg = repository.config
        console.print(f"[bold]{repository.destination.describe()}[/]")
        console.print(f"  block size   {human(cfg.chunk_size)}")
        console.print(f"  pack size    {human(cfg.pack_size)}")
        console.print(f"  encrypted    {cfg.encrypted}")
        console.print(f"  packs        {repository.index.pack_count()}")
        console.print(f"  chunks       {repository.index.chunk_count()}")
        store = PointStore(repository.backend)
        console.print(f"  VMs          {len(store.list_vms())}")
        console.print(f"  free space   {human(repository.backend.free_space())}")
    finally:
        repository.close()


@repo.command("reindex")
@click.pass_obj
def repo_reindex(ctx: Context):
    """Rebuild the local chunk index from the repository."""
    repository = ctx.open_repo()
    try:
        started = time.monotonic()
        packs = rebuild(repository.index, repository.backend)
        console.print(f"indexed {packs} pack(s), "
                      f"{repository.index.chunk_count()} chunks in "
                      f"{time.monotonic() - started:.1f}s")
    finally:
        repository.close()


@repo.command("gc")
@click.option("--dry-run", is_flag=True, help="Report what would be freed.")
@click.option("--repack-below", default=0.5, show_default=True,
              help="Rewrite a pack when this fraction or less is still live.")
@click.pass_obj
def repo_gc(ctx: Context, dry_run, repack_below):
    """Reclaim space no restore point references any more."""
    repository = ctx.open_repo()
    try:
        result = collect_garbage(repository, repack_below=repack_below,
                                 dry_run=dry_run)
        verb = "would free" if dry_run else "freed"
        console.print(f"  live chunks     {result.live_chunks}")
        console.print(f"  packs examined  {result.packs_examined}")
        console.print(f"  packs deleted   {result.packs_deleted}")
        console.print(f"  packs rewritten {result.packs_repacked} "
                      f"(-> {result.packs_written} new)")
        if result.orphans_removed:
            console.print(f"  stale index entries removed "
                          f"{result.orphans_removed}")
        console.print(f"  [bold]{verb} {human(result.bytes_reclaimed)}[/]")
    finally:
        repository.close()


if __name__ == "__main__":
    main()
