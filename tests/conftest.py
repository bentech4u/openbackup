from __future__ import annotations

import os

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
