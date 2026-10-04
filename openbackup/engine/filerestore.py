"""Restore files and folders from a restore point into a running VM.

``session`` reads the backup (an FlrSession), ``guest`` writes into the VM
(a GuestFiles). Both are duck-typed so tests can substitute fakes.

Conflicts, when the target already exists:

* overwrite: replace it (files inside a restored folder are replaced too);
* rename:    keep it, and restore beside it as ``name_restored_<stamp>``;
* skip:      keep it, and do not restore that item.
"""

from __future__ import annotations

import os
import posixpath
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .context import TaskContext, check_cancel

CONFLICT_MODES = ("overwrite", "rename", "skip")


class FileRestoreError(Exception):
    pass


@dataclass
class FileRestoreResult:
    files: int = 0
    folders: int = 0
    bytes: int = 0
    skipped: int = 0
    renamed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def guest_destination(session: Any, guest: Any, path: str, target_dir: str | None) -> str:
    """Where ``path`` (a browse path like /v0/home/a.txt) goes in the guest."""
    vol = session.volume_of(path)
    rel = path[len("/" + vol["id"]):]
    if target_dir:
        return guest.join(target_dir, posixpath.basename(rel))
    if not vol.get("guest_path"):
        raise FileRestoreError(
            f"The original location of {rel or '/'} is unknown (the volume is not mapped to a "
            "drive or mount point in the guest). Choose a target folder.")
    if not rel.strip("/"):
        raise FileRestoreError("Restoring a whole volume over itself is not supported; "
                               "choose a target folder")
    return guest.join(vol["guest_path"], rel)


def restore_files(session: Any, guest: Any, items: list[str], conflict: str,
                  target_dir: str | None, stamp: str, staging: Path,
                  ctx: TaskContext) -> FileRestoreResult:
    if conflict not in CONFLICT_MODES:
        raise FileRestoreError(f"Unknown conflict mode {conflict}")
    res = FileRestoreResult()
    staging.mkdir(parents=True, exist_ok=True)
    os.chmod(staging, 0o700)
    if target_dir:
        guest.mkdirs(target_dir)

    plans = []
    total = 0
    for path in items:
        st = session.stat(path)
        dest = guest_destination(session, guest, path, target_dir)
        tree = session.walk(path) if st["type"] == "dir" else []
        size = st["size"] if st["type"] == "file" else sum(
            e["size"] for e in tree if e["type"] == "file")
        plans.append((path, st, dest, tree))
        total += size
    ctx.progress(0.0, total=total)

    for path, st, dest, tree in plans:
        check_cancel(ctx)
        name = posixpath.basename(path)
        existing = guest.exists(dest)
        if existing and conflict == "skip":
            ctx.log(f"{dest} exists; skipped")
            ctx.item(name, state="warning", target=dest, note="exists, skipped")
            res.skipped += 1
            continue
        if existing and conflict == "rename":
            n = 1
            new = guest.renamed(dest, stamp)
            while guest.exists(new):
                n += 1
                new = guest.renamed(dest, stamp, n)
            ctx.log(f"{dest} exists; restoring as {new}")
            res.renamed.append(new)
            dest = new
        if st["type"] == "file":
            ctx.item(name, state="running", target=dest)
            _copy_file(session, guest, path, dest, st, conflict == "overwrite", staging, ctx,
                       res)
            ctx.item(name, state="success", target=dest)
        elif st["type"] == "dir":
            ctx.item(name, state="running", target=dest, files=len(tree))
            _copy_tree(session, guest, path, dest, tree, conflict, staging, ctx, res)
            ctx.item(name, state="success" if not res.errors else "warning", target=dest)
        else:
            ctx.log(f"{path} is a {st['type']}; only files and folders can be restored",
                    "warning")
            res.skipped += 1
    return res


def _copy_tree(session, guest, path, dest, tree, conflict, staging, ctx, res) -> None:
    guest.mkdirs(dest)
    res.folders += 1
    for e in sorted(tree, key=lambda e: e["path"]):
        check_cancel(ctx)
        target = guest.join(dest, e["path"])
        if e["type"] == "dir":
            guest.mkdirs(target)
            res.folders += 1
        elif e["type"] == "file":
            if conflict != "overwrite" and guest.exists(target):
                # A renamed folder is new, so this only happens for skip.
                ctx.log(f"{target} exists; skipped")
                res.skipped += 1
                continue
            try:
                _copy_file(session, guest, path + e["path"], target, e,
                           conflict == "overwrite", staging, ctx, res)
            except Exception as err:  # keep going; report every failure
                res.errors.append(f"{target}: {err}")
                ctx.log(f"{target}: {err}", "error")
        else:
            ctx.log(f"{path + e['path']}: {e['type']} skipped", "warning")
            res.skipped += 1


def _copy_file(session, guest, path, dest, meta, overwrite, staging, ctx, res) -> None:
    tmp = staging / secrets.token_hex(8)
    t0 = time.monotonic()
    try:
        session.download(path, tmp)
        guest.upload(tmp, dest, overwrite, meta)
    finally:
        tmp.unlink(missing_ok=True)
    res.files += 1
    res.bytes += meta["size"]
    ctx.progress(written=meta["size"])
    ctx.log(f"Restored {dest} ({meta['size']:,} bytes, {time.monotonic() - t0:.1f}s)")
