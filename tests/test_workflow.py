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


def test_file_restore_into_vm(admin_api, fake, setup, make_user, login, monkeypatch, tmp_path):
    from test_filerestore import FakeGuest, FakeSession

    from openbackup.db import session_scope
    from openbackup.db.models import Task
    from openbackup.vsphere.guestops import GuestInfo
    from openbackup.worker import tasks

    admin_api.post(f"/api/jobs/{setup['job_id']}/run")
    run_worker_until_idle()
    pid = admin_api.get("/api/points").json()[0]["id"]

    backup = tmp_path / "backup"
    (backup / "etc").mkdir(parents=True)
    (backup / "etc/app.conf").write_text("good config")
    guest_root = tmp_path / "guest"
    (guest_root / "etc").mkdir(parents=True)
    (guest_root / "etc/app.conf").write_text("broken config")
    seen = {}

    class Guest(FakeGuest):
        def check(self):
            return GuestInfo("linuxGuest", "Test Linux", "web01")

    def guest_factory(source, moref, user, password):
        seen.update(moref=moref, user=user, password=password)
        return Guest(guest_root)

    class Session(FakeSession):
        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(tasks, "guest_factory", guest_factory)
    monkeypatch.setattr(tasks, "flr_session_factory", lambda row, point, repo: Session(backup))

    body = {"mode": "files", "vcenter_id": setup["vc_id"],
            "files": {"items": ["/v0/etc/app.conf"], "vm_moref": setup["vm"].moref,
                      "guest_user": "root", "guest_password": "guest-secret-pw",
                      "conflict": "rename"}}

    # Operators may restore beside the original, but not overwrite.
    make_user("op", "operator")
    op = login("op")
    over = {**body, "files": {**body["files"], "conflict": "overwrite"}}
    assert op.post(f"/api/points/{pid}/restore", json=over).status_code == 403
    bad = {**body, "files": {**body["files"], "items": ["/v0/../../etc/shadow"]}}
    assert op.post(f"/api/points/{pid}/restore", json=bad).status_code == 400

    r = op.post(f"/api/points/{pid}/restore", json=body)
    assert r.status_code == 202, r.text
    assert "guest-secret-pw" not in r.text and "guest_password" not in r.text
    tid = r.json()["id"]
    run_worker_until_idle()

    t = admin_api.get(f"/api/tasks/{tid}").json()
    assert t["state"] == "success", t
    assert seen == {"moref": setup["vm"].moref, "user": "root", "password": "guest-secret-pw",
                    "closed": True}
    assert (guest_root / "etc/app.conf").read_text() == "broken config"
    restored = list((guest_root / "etc").glob("app_restored_*.conf"))
    assert len(restored) == 1 and restored[0].read_text() == "good config"

    # The credentials are gone from the database once the task is over, and
    # never reached the audit log.
    from openbackup.db.models import AuditEntry

    with session_scope() as db:
        assert "guest_password_enc" not in db.get(Task, tid).params
        entry = db.query(AuditEntry).filter_by(action="point.restore.files").one()
        assert "guest-secret-pw" not in str(entry.detail)
        assert "guest_password_enc" not in entry.detail


def test_openshift_namespace_backup_and_restore(admin_api, setup, make_user, login, monkeypatch,
                                                tmp_path):
    import subprocess

    from fake_kube import FakeKube
    from test_kube_namespace import seed

    from openbackup.api.routers import clusters
    from openbackup.kube.client import KubeClient
    from openbackup.worker import tasks

    src, dr = FakeKube(), FakeKube()
    seed(src)
    by_url = {"https://api.homelab.example:6443": src, "https://api.dr.example:6443": dr}

    def factory(url, token, ca, **kw):
        assert token in ("backup-token", "restore-token", "pasted-token")
        return KubeClient(url, token, ca, transport=by_url[url].transport())

    monkeypatch.setattr(clusters, "client_factory", factory)
    monkeypatch.setattr(tasks, "kube_client_factory", factory)
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-keyout", str(tmp_path / "k"), "-out", str(tmp_path / "c"),
                    "-subj", "/CN=ca", "-addext", "basicConstraints=critical,CA:TRUE"],
                   check=True, capture_output=True)
    ca = (tmp_path / "c").read_text()
    home = admin_api.post("/api/clusters", json={
        "name": "homelab", "api_url": "https://api.homelab.example:6443", "ca_pem": ca,
        "backup_token": "backup-token"}).json()["cluster"]["id"]
    drc = admin_api.post("/api/clusters", json={
        "name": "dr", "api_url": "https://api.dr.example:6443", "ca_pem": ca,
        "backup_token": "backup-token", "restore_token": "restore-token"}).json()["cluster"]["id"]

    bad = {"name": "ocp", "kind": "openshift", "cluster_id": home,
           "repository_id": setup["repo_id"], "selection": {"namespaces": ["Bad_Name"]}}
    assert admin_api.post("/api/jobs", json=bad).status_code == 422
    r = admin_api.post("/api/jobs", json={**bad, "selection": {"namespaces": ["shop"]},
                                          "retention_points": 3})
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["kind"] == "openshift" and job["selection"]["namespaces"] == ["shop"]

    tid = admin_api.post(f"/api/jobs/{job['id']}/run").json()["id"]
    run_worker_until_idle()
    t = admin_api.get(f"/api/tasks/{tid}").json()
    assert t["state"] == "success", t
    point = next(p for p in admin_api.get("/api/points").json()
                 if p["subject_kind"] == "namespace")
    assert point["vm_name"] == "homelab/shop"
    detail = admin_api.get(f"/api/points/{point['id']}").json()
    assert detail["namespace"] == "shop" and detail["resources"]["Deployment"] == 1

    # Operators restore into a new namespace on the DR cluster, using its stored token.
    make_user("op", "operator")
    op = login("op")
    body = {"mode": "namespace", "namespace": {
        "cluster_id": drc, "target_namespace": "shop-restored",
        "storage_class_map": {"thin-csi": "thin-dr"}}}
    r = op.post(f"/api/points/{point['id']}/restore", json=body)
    assert r.status_code == 202, r.text
    run_worker_until_idle()
    assert op.get(f"/api/tasks/{r.json()['id']}").json()["state"] in ("success", "warning")
    assert dr.get_obj("apps", "deployments", "shop-restored", "web")
    # Merging into an existing namespace is for admins only.
    merge = {"mode": "namespace", "namespace": {**body["namespace"], "merge": True}}
    assert op.post(f"/api/points/{point['id']}/restore", json=merge).status_code == 403
    # The home cluster has no stored restore token: one must be pasted, and is never echoed.
    home_body = {"mode": "namespace", "namespace": {"cluster_id": home,
                                                    "target_namespace": "shop2"}}
    assert op.post(f"/api/points/{point['id']}/restore", json=home_body).status_code == 400
    home_body["namespace"]["restore_token"] = "pasted-token"
    r = op.post(f"/api/points/{point['id']}/restore", json=home_body)
    assert r.status_code == 202 and "pasted-token" not in r.text
    run_worker_until_idle()
    assert src.get_obj("apps", "deployments", "shop2", "web")
