"""Set a cluster up for OpenBackup with admin credentials used once.

With a cluster-admin (kubeadmin, or a token from `oc whoami -t`), OpenBackup
creates its own namespace, a read-only backup ServiceAccount and optionally
a restore ServiceAccount, from the same manifests an administrator could
apply by hand (kube/manifests/). It then works only with those accounts'
tokens. The admin credentials are never stored; a session token obtained by
logging in with a password is revoked as soon as setup is done.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import yaml

from .client import KubeClient, KubeError

MANIFESTS = Path(__file__).with_name("manifests")
NAMESPACE = "openbackup"
BACKUP_SECRET = "openbackup-backup-token"
RESTORE_SECRET = "openbackup-restore-token"


def manifests(name: str) -> list[dict]:
    return [d for d in yaml.safe_load_all((MANIFESTS / name).read_text()) if d]


def oauth_endpoint(api: KubeClient) -> str:
    """The OpenShift OAuth server's authorize URL (on the ingress, not the API)."""
    try:
        meta = api.get("/.well-known/oauth-authorization-server")
    except KubeError as e:
        raise KubeError("This cluster does not offer password login (no OpenShift OAuth "
                        f"server found: {e}). Use an admin token instead.") from None
    return meta["authorization_endpoint"]


def password_login(authorize_url: str, username: str, password: str, oauth_ca_pem: str,
                   transport: httpx.BaseTransport | None = None) -> str:
    """Log in the way `oc login -u -p` does and return the session token."""
    params = urlencode({"response_type": "token", "client_id": "openshift-challenging-client"})
    kw = {"transport": transport} if transport else {
        "verify": ssl.create_default_context(cadata=oauth_ca_pem)}
    try:
        with httpx.Client(timeout=30, follow_redirects=False, **kw) as http:
            r = http.get(f"{authorize_url}?{params}", auth=(username, password),
                         headers={"X-CSRF-Token": secrets.token_hex(8)})
    except httpx.ConnectError as e:
        if "CERTIFICATE_VERIFY_FAILED" in str(e) or "certificate verify failed" in str(e):
            raise KubeError("The OAuth server certificate is not signed by the confirmed CA") \
                from None
        raise KubeError(f"Cannot reach the OAuth server: {e}") from None
    if r.status_code == 401:
        raise KubeError("Login failed: wrong username or password")
    if r.status_code != 302 or "access_token=" not in r.headers.get("location", ""):
        raise KubeError(f"Unexpected answer from the OAuth server (HTTP {r.status_code})")
    fragment = urlsplit(r.headers["location"]).fragment
    return parse_qs(fragment)["access_token"][0]


def revoke_session_token(api: KubeClient, token: str) -> None:
    """Delete an OAuth access token. Objects are named sha256~<base64url of
    the SHA-256 of the token's secret part>."""
    secret = token.split("~", 1)[1] if token.startswith("sha256~") else token
    digest = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()).rstrip(b"=")
    api.delete(f"/apis/oauth.openshift.io/v1/oauthaccesstokens/sha256~{digest.decode()}")


def _object_path(api: KubeClient, obj: dict) -> str:
    r = api.resource(obj["apiVersion"], obj["kind"])
    if r is None:
        raise KubeError(f"{obj['kind']} ({obj['apiVersion']}) is not served by this cluster")
    return r.path(obj["metadata"].get("namespace"), obj["metadata"]["name"])


@dataclass
class SetupResult:
    admin_user: str
    backup_token: str
    restore_token: str | None
    created: list[str]
    skipped: list[str]


def setup_accounts(api: KubeClient, with_restore: bool, *,
                   wait: Callable[[float], None] = time.sleep) -> SetupResult:
    """Create OpenBackup's namespace and ServiceAccounts with ``api`` (an
    admin client) and return their tokens."""
    user = api.whoami()
    if not api.can("create", "clusterrolebindings", "rbac.authorization.k8s.io"):
        raise KubeError(f"{user or 'This account'} is not a cluster administrator (it cannot "
                        "create cluster role bindings)")
    created, skipped = [], []
    docs = manifests("backup-serviceaccount.yaml")
    if with_restore:
        docs += manifests("restore-serviceaccount.yaml")
    kubevirt = api.has_group("kubevirt.io")
    for obj in docs:
        label = f"{obj['kind']}/{obj['metadata']['name']}"
        if not kubevirt and "vm-freeze" in obj["metadata"]["name"]:
            skipped.append(f"{label} (OpenShift Virtualization is not installed)")
            continue
        api.apply(_object_path(api, obj), obj)
        if label not in created:
            created.append(label)

    def token(secret: str) -> str:
        for _ in range(30):  # the token controller fills the Secret asynchronously
            s = api.get(f"/api/v1/namespaces/{NAMESPACE}/secrets/{secret}")
            t = (s.get("data") or {}).get("token")
            if t:
                return base64.b64decode(t).decode()
            wait(1)
        raise KubeError(f"Secret {secret} did not receive a token")

    return SetupResult(user, token(BACKUP_SECRET),
                       token(RESTORE_SECRET) if with_restore else None, created, skipped)
