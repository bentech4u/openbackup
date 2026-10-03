from __future__ import annotations

from datetime import timedelta

from conftest import PASSWORD


def test_unauthenticated_is_rejected(client):
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/users").status_code == 401


def test_login_sets_httponly_strict_cookie(client, make_user):
    make_user("alice")
    r = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert r.json()["user"]["username"] == "alice"
    assert "password_hash" not in r.text


def test_wrong_password_and_unknown_user_look_the_same(client, make_user):
    make_user("alice")
    a = client.post("/api/auth/login", json={"username": "alice", "password": "nope"})
    b = client.post("/api/auth/login", json={"username": "bob", "password": "nope"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_username_is_case_insensitive(client, make_user):
    make_user("Alice")
    r = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    assert r.status_code == 200


def test_lockout_after_repeated_failures(client, make_user, settings):
    make_user("alice")
    for _ in range(settings.login_max_failures):
        client.post("/api/auth/login", json={"username": "alice", "password": "wrong"})
    r = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    assert r.status_code == 401
    assert "locked" in r.json()["detail"]


def test_admin_can_unlock(client, make_user, login, settings):
    make_user("admin")
    uid = make_user("alice")
    api = login("admin")
    for _ in range(settings.login_max_failures):
        client.post("/api/auth/login", json={"username": "alice", "password": "wrong"})
    assert api.patch(f"/api/users/{uid}", json={"unlock": True}).status_code == 200
    login("alice")


def test_per_ip_throttle(client, make_user):
    from openbackup.auth.service import IP_MAX_FAILURES

    make_user("alice")
    for i in range(IP_MAX_FAILURES):
        client.post("/api/auth/login", json={"username": f"user{i}", "password": "x"})
    r = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    assert r.status_code == 401
    assert "address" in r.json()["detail"]


def test_csrf_required_for_mutations(client, make_user, login):
    make_user("admin")
    api = login("admin")
    body = {"username": "bob", "password": "a-long-enough-password", "role": "viewer"}
    r = client.post("/api/users", json=body)
    assert r.status_code == 403 and "CSRF" in r.json()["detail"]
    r = client.post("/api/users", json=body, headers={"X-CSRF-Token": "forged"})
    assert r.status_code == 403
    assert api.post("/api/users", json=body).status_code == 201


def test_role_enforcement(client, make_user, login):
    make_user("viewer1", "viewer")
    v = login("viewer1")
    assert v.get("/api/users").status_code == 403
    assert v.get("/api/audit").status_code == 403
    assert v.get("/api/auth/me").status_code == 200


def test_forced_password_change(client, make_user, login):
    make_user("admin", must_change=True)
    api = login("admin")
    assert api.get("/api/users").status_code == 403
    r = api.post("/api/auth/password",
                 json={"current_password": PASSWORD, "new_password": "short"})
    assert r.status_code == 400
    r = api.post("/api/auth/password",
                 json={"current_password": PASSWORD, "new_password": "a-brand-new-passphrase"})
    assert r.status_code == 204
    assert api.get("/api/users").status_code == 200


def test_password_change_revokes_other_sessions(client, make_user, login):
    from fastapi.testclient import TestClient

    make_user("alice", "viewer")
    other = TestClient(client.app, base_url="http://testserver")
    r = other.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    assert r.status_code == 200
    api = login("alice")
    r = api.post("/api/auth/password",
                 json={"current_password": PASSWORD, "new_password": "another-good-passphrase"})
    assert r.status_code == 204
    assert other.get("/api/auth/me").status_code == 401
    assert api.get("/api/auth/me").status_code == 200


def test_logout_invalidates_session(client, make_user, login):
    make_user("alice")
    api = login("alice")
    assert api.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_idle_session_expires(client, make_user, login, settings):
    from openbackup.db import session_scope
    from openbackup.db.models import AuthSession, utcnow

    make_user("alice")
    login("alice")
    with session_scope() as db:
        for s in db.query(AuthSession):
            s.last_seen_at = utcnow() - timedelta(minutes=settings.session_idle_minutes + 1)
    assert client.get("/api/auth/me").status_code == 401


def test_disabled_user_cannot_log_in_and_is_signed_out(client, make_user, login):
    from fastapi.testclient import TestClient

    make_user("admin")
    uid = make_user("alice", "viewer")
    other = TestClient(client.app, base_url="http://testserver")
    other.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    api = login("admin")
    assert api.patch(f"/api/users/{uid}", json={"is_active": False}).status_code == 200
    assert other.get("/api/auth/me").status_code == 401
    r = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    assert r.status_code == 401


def test_last_admin_is_protected(client, make_user, login):
    uid = make_user("admin")
    api = login("admin")
    assert api.patch(f"/api/users/{uid}", json={"role": "viewer"}).status_code == 400
    assert api.patch(f"/api/users/{uid}", json={"is_active": False}).status_code == 400
    assert api.delete(f"/api/users/{uid}").status_code == 400


def test_password_policy_on_create(admin_api):
    r = admin_api.post("/api/users", json={"username": "bob", "password": "short"})
    assert r.status_code == 400
    r = admin_api.post("/api/users",
                       json={"username": "bob", "password": "bob-is-my-password"})
    assert r.status_code == 400


def test_audit_log_records_logins_and_changes(client, admin_api):
    client.post("/api/auth/login", json={"username": "admin", "password": "bad"})
    admin_api.post("/api/users", json={"username": "bob", "password": "a-long-enough-password"})
    entries = admin_api.get("/api/audit").json()
    actions = [(e["action"], e["success"]) for e in entries]
    assert ("auth.login", True) in actions
    assert ("auth.login", False) in actions
    assert ("user.create", True) in actions


def test_security_headers(client):
    r = client.get("/api/health")
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_secrets_round_trip(settings):
    from openbackup.auth import secrets

    token = secrets.encrypt("vcenter-password")
    assert "vcenter-password" not in token
    assert secrets.decrypt(token) == "vcenter-password"
