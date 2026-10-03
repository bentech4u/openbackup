"""End to end through the HTTP API and the worker, against a fake vSphere."""

from __future__ import annotations

import os
import shutil
import time

import pytest
from fake_vsphere import FakeVSphere

pytestmark = pytest.mark.skipif(shutil.which("nbdkit") is None, reason="nbdkit not installed")

MiB = 1 << 20
THUMB = ":".join(["AB"] * 20)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    from openbackup.api.routers import vcenters
    from openbackup.worker import tasks

    vs = FakeVSphere(tmp_path / "vsphere")
    monkeypatch.setattr(vcenters, "connector", lambda *a: vs)
    monkeypatch.setattr(tasks, "source_factory", lambda vc: vs)
    return vs


def run_worker_until_idle(timeout: float = 60) -> None:
    from openbackup.db import session_scope
    from openbackup.db.models import Task, TaskState
    from openbackup.worker.main import Worker

    w = Worker()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        w.run_once()
        with session_scope() as db:
            busy = db.query(Task).filter(Task.state.in_([TaskState.queued,
                                                         TaskState.running])).count()
        if not busy and not w.threads:
            return
        time.sleep(0.1)
    raise AssertionError("worker did not finish")


@pytest.fixture
def setup(admin_api, fake, tmp_path):
    vm = fake.add_vm("web01", [8 * MiB, 2 * MiB])
    fake.write(vm, 2000, 0, os.urandom(3 * MiB))
    fake.write(vm, 2001, MiB, os.urandom(1000))
    r = admin_api.post("/api/vcenters", json={"name": "vc1", "host": "vc.example",
                                              "username": "backup@vsphere.local",
                                              "password": "vc-pass", "thumbprint": THUMB})
    assert r.status_code == 201, r.text
    vc_id = r.json()["id"]
    assert "vc-pass" not in r.text
    r = admin_api.post("/api/repositories", json={
        "name": "local", "kind": "local", "path": str(tmp_path / "backups"),
        "passphrase": "repository passphrase"})
    assert r.status_code == 201, r.text
    repo_id = r.json()["id"]
    r = admin_api.post("/api/jobs", json={
        "name": "Daily", "vcenter_id": vc_id, "repository_id": repo_id,
        "vms": [{"moref": vm.moref, "name": vm.name}], "schedule_cron": "0 22 * * *",
        "retention_points": 2})
    assert r.status_code == 201, r.text
    return {"vm": vm, "vc_id": vc_id, "repo_id": repo_id, "job_id": r.json()["id"]}


def test_backup_restore_cycle(admin_api, fake, setup):
    vm, job_id = setup["vm"], setup["job_id"]
    vms = admin_api.get(f"/api/vcenters/{setup['vc_id']}/vms").json()
    assert vms[0]["name"] == "web01"

    r = admin_api.post(f"/api/jobs/{job_id}/run")
    assert r.status_code == 202
    assert admin_api.post(f"/api/jobs/{job_id}/run").status_code == 409  # already queued
    run_worker_until_idle()

    task = admin_api.get(f"/api/tasks/{r.json()['id']}").json()
    assert task["state"] == "success", task
    assert task["progress"] == 1.0
    logs = admin_api.get(f"/api/tasks/{task['id']}/logs").json()
    assert any("restore point" in entry["message"] for entry in logs)

    # Two more runs; retention keeps 2.
    for _ in range(2):
        fake.write(vm, 2000, 5 * MiB, os.urandom(100))
        admin_api.post(f"/api/jobs/{job_id}/run")
        run_worker_until_idle()
    points = admin_api.get("/api/points").json()
    assert len(points) == 2
    assert {p["kind"] for p in points} == {"incremental"}
    latest = points[0]

    detail = admin_api.get(f"/api/points/{latest['id']}").json()
    assert detail["config"]["name"] == "web01"

    r = admin_api.post(f"/api/points/{latest['id']}/restore", json={
        "mode": "new_vm", "vcenter_id": setup["vc_id"],
        "target": {"name": "web01-restored", "folder": "group-v1", "resource_pool": "rp-1",
                   "datastore": "ds1"}})
    assert r.status_code == 202, r.text
    run_worker_until_idle()
    assert admin_api.get(f"/api/tasks/{r.json()['id']}").json()["state"] == "success"
    new = next(v for v in fake.vms.values() if v.name == "web01-restored")
    assert fake.read(new, 2000) == fake.read(vm, 2000)
    assert fake.read(new, 2001) == fake.read(vm, 2001)

    r = admin_api.post(f"/api/points/{latest['id']}/verify")
    run_worker_until_idle()
    assert admin_api.get(f"/api/tasks/{r.json()['id']}").json()["state"] == "success"

    r = admin_api.post(f"/api/points/{latest['id']}/restore", json={"mode": "export",
                                                                    "format": "raw"})
    run_worker_until_idle()
    assert admin_api.get(f"/api/tasks/{r.json()['id']}").json()["state"] == "success"

    r = admin_api.delete(f"/api/points/{points[1]['id']}")
    run_worker_until_idle()
    assert len(admin_api.get("/api/points").json()) == 1

    dash = admin_api.get("/api/dashboard").json()
    assert dash["protected_vms"] == 1
    assert dash["success_rate_24h"] == 1.0


def test_one_failing_vm_gives_warning_not_failure(admin_api, fake, setup):
    other = fake.add_vm("gone01", [MiB])
    r = admin_api.get(f"/api/jobs/{setup['job_id']}").json()
    body = {k: r[k] for k in ("name", "vcenter_id", "repository_id", "schedule_cron",
                              "retention_points")}
    body["vms"] = r["vms"] + [{"moref": other.moref, "name": other.name}]
    assert admin_api.put(f"/api/jobs/{setup['job_id']}", json=body).status_code == 200
    del fake.vms[other.moref]
    r = admin_api.post(f"/api/jobs/{setup['job_id']}/run")
    run_worker_until_idle()
    task = admin_api.get(f"/api/tasks/{r.json()['id']}").json()
    assert task["state"] == "warning"
    assert "gone01" in task["summary"]


def test_cancel_queued_task(admin_api, setup):
    r = admin_api.post(f"/api/jobs/{setup['job_id']}/run")
    tid = r.json()["id"]
    assert admin_api.post(f"/api/tasks/{tid}/cancel").status_code == 200
    run_worker_until_idle()
    assert admin_api.get(f"/api/tasks/{tid}").json()["state"] == "cancelled"


def test_roles_on_operations(client, admin_api, make_user, login, setup):
    make_user("op", "operator")
    make_user("view", "viewer")
    v = login("view")
    assert v.get("/api/jobs").status_code == 200
    assert v.post(f"/api/jobs/{setup['job_id']}/run").status_code == 403
    op = login("op")
    assert op.post("/api/jobs", json={}).status_code in (403, 422)
    assert op.delete(f"/api/jobs/{setup['job_id']}").status_code == 403
    assert op.post(f"/api/jobs/{setup['job_id']}/run").status_code == 202
    run_worker_until_idle()
    pid = op.get("/api/points").json()[0]["id"]
    r = op.post(f"/api/points/{pid}/restore", json={"mode": "in_place",
                                                    "vcenter_id": setup["vc_id"]})
    assert r.status_code == 403
    assert op.delete(f"/api/points/{pid}").status_code == 403


def test_scheduler_queues_due_jobs_once(admin_api, setup):
    from datetime import UTC, datetime, timedelta

    from openbackup.db import session_scope
    from openbackup.db.models import Job
    from openbackup.worker.main import schedule_tick

    with session_scope() as db:
        db.get(Job, setup["job_id"]).next_run_at = datetime.now(UTC) - timedelta(days=3)
    assert schedule_tick() == 1
    assert schedule_tick() == 0  # next_run_at moved forward; no catch-up storm
    with session_scope() as db:
        assert db.get(Job, setup["job_id"]).next_run_at > datetime.now(UTC)


def test_interrupted_tasks_are_marked_failed(admin_api, setup):
    from openbackup.db import session_scope
    from openbackup.db.models import Task, TaskState
    from openbackup.worker.main import recover_interrupted

    tid = admin_api.post(f"/api/jobs/{setup['job_id']}/run").json()["id"]
    with session_scope() as db:
        db.get(Task, tid).state = TaskState.running
    recover_interrupted()
    t = admin_api.get(f"/api/tasks/{tid}").json()
    assert t["state"] == "failed" and "Interrupted" in t["summary"]


def test_import_existing_repository(admin_api, setup, tmp_path):
    admin_api.post(f"/api/jobs/{setup['job_id']}/run")
    run_worker_until_idle()
    # Simulate a rebuilt server: detach (after deleting the job), then import.
    assert admin_api.delete(f"/api/jobs/{setup['job_id']}").status_code == 204
    assert admin_api.delete(f"/api/repositories/{setup['repo_id']}").status_code == 204
    from openbackup.db import session_scope
    from openbackup.db.models import RestorePoint

    with session_scope() as db:
        assert db.query(RestorePoint).count() == 0
    body = {"name": "again", "kind": "local", "path": str(tmp_path / "backups"),
            "mode": "import", "passphrase": "wrong passphrase!!"}
    assert admin_api.post("/api/repositories", json=body).status_code == 400
    body["passphrase"] = "repository passphrase"
    assert admin_api.post("/api/repositories", json=body).status_code == 201
    run_worker_until_idle()
    assert len(admin_api.get("/api/points").json()) == 1


def test_validation(admin_api, setup):
    r = admin_api.post("/api/repositories", json={"name": "x", "kind": "local", "path": "/etc"})
    assert r.status_code == 400
    r = admin_api.post("/api/repositories", json={
        "name": "x", "kind": "nfs", "nfs_server": "nas", "nfs_export": "/v",
        "nfs_options": "soft"})
    assert r.status_code == 400
    job = admin_api.get(f"/api/jobs/{setup['job_id']}").json()
    body = {k: job[k] for k in ("name", "vcenter_id", "repository_id", "vms")}
    body["schedule_cron"] = "not a cron"
    assert admin_api.put(f"/api/jobs/{setup['job_id']}", json=body).status_code == 422
    r = admin_api.post("/api/vcenters", json={"name": "vc2", "host": "h", "username": "u",
                                              "password": "p", "thumbprint": "nope"})
    assert r.status_code == 400
