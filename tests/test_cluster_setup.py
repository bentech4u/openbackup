"""Setting a cluster up with admin credentials used once."""

from __future__ import annotations

import httpx
import pytest
from fake_kube import FakeKube
from test_clusters_api import ca_pem  # noqa: F401  (fixture)

from openbackup.kube.bootstrap import revoke_session_token
from openbackup.kube.client import KubeClient

ADMIN_TOKEN = "sha256~admin-session-secret"


@pytest.fixture
def cluster(monkeypatch):
    from openbackup.api.routers import clusters

    fk = FakeKube()
    fk.tokens = {"pasted-admin-token": ("kube:admin", set()),
                 "": ("system:anonymous", {("create", "clusterrolebindings")})}
    fk.add({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "shop"}})
    logins = []

    def oauth(req: httpx.Request) -> httpx.Response:
        auth = req.headers.get("authorization", "")
        import base64

        user, _, pw = base64.b64decode(auth.removeprefix("Basic ")).decode().partition(":")
        logins.append(user)
        if (user, pw) != ("kubeadmin", "the-kubeadmin-password"):
            return httpx.Response(401)
        fk.tokens[ADMIN_TOKEN] = ("kube:admin", set())
        fk.add({"apiVersion": "oauth.openshift.io/v1", "kind": "OAuthAccessToken",
                "metadata": {"name": "sha256~" + _digest("admin-session-secret")}})
        return httpx.Response(302, headers={
            "location": f"https://oauth/oauth/token/implicit#access_token={ADMIN_TOKEN}"
                        "&expires_in=86400&token_type=Bearer"})

    monkeypatch.setattr(clusters, "client_factory",
                        lambda url, token, ca, **kw: KubeClient(url, token, ca,
                                                                transport=fk.transport()))
    monkeypatch.setattr(clusters, "oauth_transport", httpx.MockTransport(oauth))
    monkeypatch.setattr("openbackup.kube.bootstrap.time.sleep", lambda s: None)
    fk.logins = logins
    return fk


def _digest(secret: str) -> str:
    import base64
    import hashlib

    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()).rstrip(
        b"=").decode()


def _body(ca, **admin):
    return {"name": "homelab", "api_url": "https://api.homelab.example:6443", "ca_pem": ca,
            "admin": admin}


def test_setup_with_kubeadmin_password(admin_api, cluster, ca_pem):  # noqa: F811
    r = admin_api.post("/api/clusters/setup", json=_body(
        ca_pem, kind="password", username="kubeadmin", password="the-kubeadmin-password"))
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["admin_user"] == "kube:admin"
    assert "ClusterRoleBinding/openbackup-backup-cluster-reader" in out["created"]
    assert out["backup"]["user"] == "system:serviceaccount:openbackup:openbackup-backup"
    assert out["backup"]["permissions"]["read_secrets"] is False
    assert out["restore"]["user"].endswith("openbackup-restore")
    assert out["cluster"]["has_restore_token"]
    # The objects exist, the session we logged in with is revoked, and no
    # credential appears anywhere in the response or the audit log.
    assert cluster.get_obj("", "serviceaccounts", "openbackup", "openbackup-backup")
    assert cluster.get_obj("oauth.openshift.io", "oauthaccesstokens", "",
                           "sha256~" + _digest("admin-session-secret")) is None
    audit = admin_api.get("/api/audit?action=cluster.setup").json()
    for secret in ("the-kubeadmin-password", ADMIN_TOKEN, "sa-openbackup"):
        assert secret not in r.text and secret not in str(audit)
    # Namespaces are now read with the backup account, not the admin.
    ns = admin_api.get(f"/api/clusters/{out['cluster']['id']}/namespaces").json()
    assert [n["name"] for n in ns] == ["shop"]


def test_setup_with_pasted_admin_token_leaves_it_alone(admin_api, cluster, ca_pem):  # noqa: F811
    r = admin_api.post("/api/clusters/setup",
                       json={**_body(ca_pem, kind="token", token="pasted-admin-token"),
                             "restore_account": False})
    assert r.status_code == 201, r.text
    assert r.json()["restore"] is None and not r.json()["cluster"]["has_restore_token"]
    assert "pasted-admin-token" in cluster.tokens  # still valid: it belongs to the user


def test_wrong_password_and_non_admins_are_refused(admin_api, cluster, ca_pem):  # noqa: F811
    r = admin_api.post("/api/clusters/setup", json=_body(
        ca_pem, kind="password", username="kubeadmin", password="wrong"))
    assert r.status_code == 400 and "wrong username or password" in r.json()["detail"]
    cluster.tokens["developer-token"] = ("developer", {("create", "clusterrolebindings")})
    r = admin_api.post("/api/clusters/setup",
                       json=_body(ca_pem, kind="token", token="developer-token"))
    assert r.status_code == 400 and "not a cluster administrator" in r.json()["detail"]
    assert admin_api.get("/api/clusters").json() == []


def test_revoke_names_the_token_object(cluster):
    cluster.tokens["x"] = ("kube:admin", set())
    cluster.add({"apiVersion": "oauth.openshift.io/v1", "kind": "OAuthAccessToken",
                 "metadata": {"name": "sha256~" + _digest("abc")}})
    c = KubeClient("https://api:6443", "x", "", transport=cluster.transport())
    revoke_session_token(c, "sha256~abc")
    assert cluster.get_obj("oauth.openshift.io", "oauthaccesstokens", "",
                           "sha256~" + _digest("abc")) is None
