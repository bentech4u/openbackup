"""Read-only status interface.

Deliberately read-only for now. A dashboard over an engine this young would
mostly hide problems: the bugs found so far -- silently truncated reads, a
restore that would hang forever, a collection that deleted live data -- would
all have shown as a green tick. Showing state honestly is useful; offering
buttons that imply the state is trustworthy is not, yet.

The JSON API underneath is the same one job control will use when it arrives.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..config import Config
from ..repo.repository import open_repository
from ..repo.restorepoint import PointStore
from ..vsphere.connection import VSphereConnection, VSphereError

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"

#: vCenter inventory is slow enough that re-fetching per request makes the
#: page feel broken, and it changes slowly. Serve it from a short cache.
INVENTORY_TTL = 60.0


class State:
    """Process-wide handles, opened once rather than per request."""

    def __init__(self, config: Config):
        self.config = config
        self.repo = None
        self.repo_error: str | None = None
        self._inventory: list[dict] | None = None
        self._inventory_at = 0.0
        self.inventory_error: str | None = None

    def open_repo(self) -> None:
        try:
            self.repo = open_repository(
                self.config.repository, index_dir=self.config.index_dir)
            self.repo_error = None
        except Exception as exc:
            # The UI is still worth serving without a repository: telling
            # someone their repository is unreachable is exactly the moment
            # they need a status page.
            self.repo = None
            self.repo_error = str(exc)
            log.warning("could not open repository: %s", exc)

    def close(self) -> None:
        if self.repo is not None:
            self.repo.close()
            self.repo = None

    def inventory(self, force: bool = False) -> list[dict]:
        fresh = time.monotonic() - self._inventory_at < INVENTORY_TTL
        if self._inventory is not None and fresh and not force:
            return self._inventory
        try:
            self._inventory = self._fetch_inventory()
            self.inventory_error = None
            self._inventory_at = time.monotonic()
        except Exception as exc:
            self.inventory_error = str(exc)
            log.warning("could not read vCenter inventory: %s", exc)
            if self._inventory is None:
                self._inventory = []
        return self._inventory

    def _fetch_inventory(self) -> list[dict]:
        from pyVmomi import vim

        from ..vsphere.inventory import describe_vm

        out = []
        with VSphereConnection(self.config.vcenter) as conn:
            for vm in conn.find_all(vim.VirtualMachine):
                try:
                    info = describe_vm(conn, vm)
                except Exception:
                    continue
                blockers = []
                if info.has_snapshots:
                    blockers.append("has snapshots")
                if not info.disks:
                    blockers.append("no virtual disks")
                if any(d.independent for d in info.disks):
                    blockers.append("independent disk")
                out.append({
                    "name": info.name,
                    "uuid": info.uuid,
                    "instance_uuid": info.instance_uuid,
                    "power_state": info.power_state,
                    "guest": info.guest_full_name,
                    "hardware_version": info.hardware_version,
                    "tools": info.tools_status,
                    "cbt_enabled": info.cbt_enabled,
                    "has_snapshots": info.has_snapshots,
                    "datacenter": info.datacenter,
                    "disk_count": len(info.disks),
                    "provisioned": sum(d.capacity for d in info.disks),
                    "datastores": sorted({d.datastore for d in info.disks}),
                    "blockers": blockers,
                })
        return sorted(out, key=lambda v: v["name"].lower())


state: State | None = None


def require_repo():
    if state is None or state.repo is None:
        raise HTTPException(
            status_code=503,
            detail=state.repo_error if state else "not initialised")
    return state.repo


def _points() -> list[dict]:
    repo = require_repo()
    store = PointStore(repo.backend)
    out = []
    for vm_uuid in store.list_vms():
        for point_id in store.list_points(vm_uuid):
            try:
                point = store.load(vm_uuid, point_id)
            except Exception as exc:
                log.warning("skipping unreadable point %s: %s", point_id, exc)
                continue
            out.append({
                "id": point.id,
                "vm_name": point.vm_name,
                "vm_uuid": vm_uuid,
                "instance_uuid": point.vm_instance_uuid,
                "created_at": point.created_at,
                "kind": point.kind,
                "parent_id": point.parent_id,
                "quiesced": point.quiesced,
                "guest": point.guest_full_name,
                "datacenter": point.datacenter,
                "capacity": point.total_capacity,
                "bytes_read": point.total_bytes_read,
                "disks": [
                    {
                        "key": d.key, "label": d.label, "capacity": d.capacity,
                        "datastore": d.datastore, "flat_path": d.flat_path,
                        "bytes_read": d.bytes_read,
                        "blocks_changed": d.blocks_changed,
                        "change_id": d.change_id, "thin": d.thin,
                    }
                    for d in point.disks
                ],
            })
    return sorted(out, key=lambda p: p["created_at"], reverse=True)


def create_app(config: Config) -> FastAPI:
    global state
    state = State(config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        state.open_repo()
        yield
        state.close()

    app = FastAPI(title="openbackup", lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": state.repo is not None,
            "repository": state.config.repository.describe(),
            "repository_error": state.repo_error,
            "vcenter": state.config.vcenter.host,
            "vcenter_error": state.inventory_error,
        }

    @app.get("/api/repository")
    def repository() -> dict[str, Any]:
        repo = require_repo()
        store = PointStore(repo.backend)
        packs = 0
        pack_bytes = 0
        for relpath in repo.backend.list("packs"):
            if relpath.endswith(".pack"):
                packs += 1
                pack_bytes += repo.backend.size(relpath)
        return {
            "destination": repo.destination.describe(),
            "kind": repo.destination.kind,
            "chunk_size": repo.config.chunk_size,
            "pack_size": repo.config.pack_size,
            "encrypted": repo.config.encrypted,
            "packs": packs,
            "pack_bytes": pack_bytes,
            "chunks": repo.index.chunk_count(),
            "vms": len(store.list_vms()),
            "free_space": repo.backend.free_space(),
        }

    @app.get("/api/vms")
    def vms(refresh: bool = False) -> dict[str, Any]:
        inventory = state.inventory(force=refresh)
        by_instance: dict[str, dict] = {}
        if state.repo is not None:
            for point in _points():
                key = point["instance_uuid"]
                current = by_instance.get(key)
                if current is None or point["created_at"] > current["created_at"]:
                    by_instance[key] = point
        for vm in inventory:
            last = by_instance.get(vm["instance_uuid"])
            vm["last_backup"] = last["created_at"] if last else None
            vm["last_point_id"] = last["id"] if last else None
            vm["protected"] = last is not None
        return {"vms": inventory, "error": state.inventory_error}

    @app.get("/api/points")
    def points(vm: str | None = None) -> dict[str, Any]:
        found = _points()
        if vm:
            needle = vm.lower()
            found = [p for p in found if needle in p["vm_name"].lower()]
        return {"points": found}

    @app.get("/api/points/{point_id}")
    def point(point_id: str) -> dict[str, Any]:
        for candidate in _points():
            if candidate["id"] == point_id:
                return candidate
        raise HTTPException(status_code=404, detail=f"no point {point_id}")

    @app.get("/api/summary")
    def summary() -> dict[str, Any]:
        result: dict[str, Any] = {
            "repository_error": state.repo_error,
            "vcenter_error": state.inventory_error,
        }
        inventory = state.inventory()
        found = _points() if state.repo is not None else []
        protected = {p["instance_uuid"] for p in found}
        result["vm_count"] = len(inventory)
        result["protected_count"] = sum(
            1 for v in inventory if v["instance_uuid"] in protected)
        result["unprotected"] = [
            v["name"] for v in inventory
            if v["instance_uuid"] not in protected
        ][:50]
        result["point_count"] = len(found)
        result["latest"] = found[0] if found else None
        result["protected_bytes"] = sum(
            v["provisioned"] for v in inventory
            if v["instance_uuid"] in protected)
        if state.repo is not None:
            result["repository"] = repository()
        return result

    if STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC), name="static")

        @app.get("/")
        def index():
            return FileResponse(STATIC / "index.html")

    return app
