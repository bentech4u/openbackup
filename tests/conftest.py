from __future__ import annotations

import os
import time

import pytest

os.environ["OPENBACKUP_ENV_FILE"] = "/nonexistent"

PASSWORD = "correct-horse-battery"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBACKUP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OPENBACKUP_SECRET_KEY_FILE", str(tmp_path / "secret.key"))
    monkeypatch.setenv("OPENBACKUP_MOUNT_ROOT", str(tmp_path / "mnt"))
    monkeypatch.setenv("OPENBACKUP_COOKIE_SECURE", "false")

    from openbackup import config, db
    from openbackup.auth import secrets

    for fn in (config.get_settings, db.get_engine, db.get_sessionmaker, secrets._fernet):
        fn.cache_clear()
    s = config.get_settings()
    secrets.ensure_key_file(s.secret_key_file)
    db.init_db()
    yield s
    db.get_engine().dispose()
    for fn in (config.get_settings, db.get_engine, db.get_sessionmaker, secrets._fernet):
        fn.cache_clear()


@pytest.fixture
def client(settings):
    from fastapi.testclient import TestClient

    from openbackup.api.app import create_app

    with TestClient(create_app(), base_url="http://testserver") as c:
        yield c


@pytest.fixture
def make_user(settings):
    from openbackup.auth.passwords import hash_password
    from openbackup.db import session_scope
    from openbackup.db.models import Role, User

    def make(username: str, role: str = "admin", must_change: bool = False,
             password: str = PASSWORD) -> int:
        with session_scope() as db:
            u = User(username=username, password_hash=hash_password(password),
                     role=Role(role), must_change_password=must_change)
            db.add(u)
            db.flush()
            return u.id

    return make


class Api:
    """A logged-in test client that sends the CSRF header automatically."""

    def __init__(self, client, csrf: str):
        self.c = client
        self.csrf = csrf

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def _send(self, method, url, **kw):
        headers = {"X-CSRF-Token": self.csrf, **kw.pop("headers", {})}
        return self.c.request(method, url, headers=headers, **kw)

    def post(self, url, **kw):
        return self._send("POST", url, **kw)

    def patch(self, url, **kw):
        return self._send("PATCH", url, **kw)

    def put(self, url, **kw):
        return self._send("PUT", url, **kw)

    def delete(self, url, **kw):
        return self._send("DELETE", url, **kw)


@pytest.fixture
def login(client):
    def do(username: str, password: str = PASSWORD) -> Api:
        r = client.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        return Api(client, r.json()["csrf_token"])

    return do


@pytest.fixture
def admin_api(make_user, login) -> Api:
    make_user("admin", "admin")
    return login("admin")


# ---------------------------------------------- shared API + worker fixtures

MiB = 1 << 20
THUMB = ":".join(["AB"] * 20)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    from fake_vsphere import FakeVSphere

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


